# scheduler.py
# Background process: checks feeds on their interval and posts new articles
# to their destinations (Discord, Slack, Matrix, ...). Run it next to the web
# UI (Docker does this for you). Only one scheduler runs per data directory;
# a second one waits until the first exits.

import os
import re
import sys
import html
import time
import signal
import hashlib
import calendar
import threading
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urlsplit, urljoin, parse_qsl, urlencode

import requests
import feedparser

import prss_core as core
import destinations as dest

TICK_SECONDS = 3                  # how often the loop looks for due feeds
WORKERS = max(1, int(os.environ.get("PRSS_WORKERS", "4")))
FEED_TIMEOUT = 20                 # wall-clock seconds for one feed download
FEED_MAX_BYTES = 15 * 1024 * 1024
RECENT_WINDOW = 24 * 3600         # dated articles older than this are never posted
MAX_POSTS_PER_CHECK = max(1, int(os.environ.get("PRSS_MAX_POSTS_PER_CHECK", "20")))
PRUNE_EVERY = 600
POST_DELAY = 0.4
# A source is downloaded in full (no If-None-Match / If-Modified-Since) at
# least this often. A source that only ever answers 304 would otherwise never
# refresh its articles' "last seen" time, and its memory would be pruned.
FULL_FETCH_EVERY = 6 * 3600
# A source that has not been read for this long (paused, or failing) may have
# lost its sent-article memory to pruning, so its articles are marked as seen
# again instead of being treated as new. Kept below the retention period.
STALE_SOURCE_AFTER = core.SENT_RETENTION_SECONDS - 2 * 86400
# Minimum gap between two checks of one feed, even when forced or when the
# result could not be written to the database. Stops hot retry loops.
MIN_RECHECK = 5

TITLE_MAX = 256
FEED_NAME_MAX = 200
SUMMARY_LEN = 350

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")

STOP = threading.Event()

_hook_locks = {}
_hook_locks_guard = threading.Lock()


def _hook_lock(key):
    """Serialize work per destination. Two feeds that share a destination (and
    possibly articles) must not both decide an article is new and post it."""
    with _hook_locks_guard:
        lock = _hook_locks.get(key)
        if lock is None:
            lock = _hook_locks[key] = threading.Lock()
        return lock


def log(msg):
    print(msg, flush=True)


# --- Fetching -------------------------------------------------------------------

# Chrome stable ships every four weeks; M132 shipped on 2025-01-14. Deriving the
# version from the date keeps the default User-Agent current without edits.
_CHROME_BASE = calendar.timegm((2025, 1, 14, 0, 0, 0))


def default_user_agent(now=None):
    custom = os.environ.get("PRSS_USER_AGENT", "").strip()
    if custom:
        return custom
    weeks4 = max(0, int(((now or time.time()) - _CHROME_BASE) // (28 * 86400)))
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{132 + weeks4}.0.0.0 Safari/537.36")


def cookie_jar(url, cookie_text):
    """Cookies scoped to the feed's own host. requests only sends jar cookies
    to a matching domain, so a redirect to another site never receives them."""
    pairs = core.parse_cookies(cookie_text)
    if not pairs:
        return None
    host = urlparse(url).hostname or ""
    jar = requests.cookies.RequestsCookieJar()
    for name, value in pairs:
        jar.set(name, value, domain=host, path="/")
    return jar


def _looks_like_login(body, final_url):
    head = bytes(body[:200000]).decode("utf-8", "ignore").lower()
    return ('type="password"' in head or "type='password'" in head
            or "/login" in (final_url or "").lower())


def _permanent_redirect(resp):
    """Where the feed has permanently moved, or None.

    Only an unbroken run of 301/308 hops from the requested address counts.
    A 302 or 307 is temporary by definition; adopting its target would point
    the feed at an address the site may stop using."""
    target, code = None, None
    for hop in resp.history:
        loc = hop.headers.get("Location")
        if hop.status_code not in (301, 308) or not loc:
            break
        target = urljoin(hop.url, loc)
        code = code or hop.status_code
    return target, code


def _read_body(resp, deadline, timeout):
    """Read the body with a wall-clock limit.

    read1() returns after each network read, so the deadline is checked at
    least once per socket timeout. Plain iter_content() waits for a full
    chunk, which lets a server that drips one byte at a time hold a worker
    for hours."""
    raw = resp.raw
    reader = getattr(raw, "read1", None)
    body = bytearray()
    while True:
        if time.monotonic() > deadline:
            return None, f"Timed out after {timeout}s"
        chunk = reader(65536, decode_content=True) if reader else raw.read(65536, decode_content=True)
        if not chunk:
            return body, None
        body += chunk
        if len(body) > FEED_MAX_BYTES:
            return None, "Feed is larger than 15 MB"


def fetch_feed(url, etag=None, modified=None, timeout=FEED_TIMEOUT, cookies=None, user_agent=None):
    """Download and parse a feed. Returns a dict describing the result."""
    headers = {
        "User-Agent": user_agent or default_user_agent(),
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5",
    }
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    out = {"status": None, "error": None, "redirected_to": None, "redirect_code": None,
           "not_modified": False, "parsed": None, "etag": None, "modified": None}
    deadline = time.monotonic() + timeout
    try:
        with requests.get(url, headers=headers, timeout=(10, timeout), stream=True,
                          cookies=cookie_jar(url, cookies)) as resp:
            out["status"] = resp.status_code
            moved, code = _permanent_redirect(resp)
            if moved and moved.rstrip("/") != url.rstrip("/"):
                out["redirected_to"], out["redirect_code"] = moved, code
            if resp.status_code == 304:
                out["not_modified"] = True
                return out
            if not (200 <= resp.status_code < 300):
                out["error"] = f"HTTP {resp.status_code}"
                if resp.status_code in (401, 403):
                    out["error"] += ("; the site refused access, so the cookies may have expired" if cookies
                                     else "; if this feed needs a login, add cookies under Access")
                return out
            body, err = _read_body(resp, deadline, timeout)
            if err:
                out["error"] = err
                return out
            out["etag"] = resp.headers.get("ETag")
            out["modified"] = resp.headers.get("Last-Modified")
            ctype = resp.headers.get("Content-Type", "")
            final_url = resp.url
    except requests.Timeout:
        out["error"] = f"Timed out after {timeout}s"
        return out
    except requests.exceptions.SSLError:
        out["error"] = "TLS/SSL error"
        return out
    except requests.ConnectionError:
        out["error"] = "Could not connect"
        return out
    except requests.RequestException as e:
        out["error"] = f"Request failed ({type(e).__name__})"
        return out
    except Exception as e:                     # decoding errors from a broken server
        out["error"] = f"Download failed ({type(e).__name__})"
        return out

    parsed = feedparser.parse(bytes(body), response_headers={"content-type": ctype})
    # feedparser happily "parses" an HTML page into an empty feed. An empty
    # version string means it did not recognize RSS, Atom or RDF at all.
    if not parsed.entries and not parsed.get("version"):
        if _looks_like_login(body, final_url):
            out["error"] = ("The site returned a login page instead of the feed. "
                            + ("The cookies may have expired or been signed out." if cookies
                               else "It probably needs cookies from a signed-in browser (see the feed's Access settings)."))
        else:
            out["error"] = "Not an RSS or Atom feed (the address returned a web page or other content)"
        return out
    out["parsed"] = parsed
    return out


# --- Articles -------------------------------------------------------------------

def entry_time(entry):
    """Published time as UTC epoch seconds, or None.

    feedparser normalizes *_parsed to UTC, so calendar.timegm is the correct
    inverse; time.mktime would apply the server's local offset."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(key)
        if t:
            try:
                return float(calendar.timegm(t))
            except (OverflowError, ValueError, TypeError):
                continue
    return None


def entry_id(entry):
    """Primary memory key. Unchanged from earlier versions so existing memory
    keeps matching."""
    for key in ("id", "guid", "link"):
        v = entry.get(key)
        if v:
            return str(v).strip()
    title = entry.get("title")
    return f"title:{title.strip()}" if title and title.strip() else None


def clean_text(raw, limit):
    if not raw:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", str(raw)))
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _valid_http(url):
    return isinstance(url, str) and url.startswith(("http://", "https://"))


# Query parameters that only track the click. Feeds without a stable guid
# often stamp a fresh value into every link, which used to make the same
# article look new on every check.
_TRACKING_PARAM = re.compile(
    r"^(utm_\w+|fbclid|gclid|gbraid|wbraid|dclid|msclkid|yclid|mc_cid|mc_eid|igshid|"
    r"_hsenc|_hsmi|mkt_tok|ref_src|cmpid|ocid|soc_src|soc_trk|__twitter_impression)$", re.I)


def _norm_link(link):
    try:
        p = urlsplit(link.strip())
        port = p.port
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    query = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                   if not _TRACKING_PARAM.match(k))
    port = f":{port}" if port and port not in (80, 443) else ""
    frag = f"#{p.fragment}" if p.fragment else ""
    return f"{host}{port}{p.path.rstrip('/') or '/'}?{urlencode(query)}{frag}"


def _short_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def entry_aliases(entry, published):
    """Secondary memory keys that survive a feed changing an article's id.

    Each one pairs the title with something else, so two different articles
    only share a key when both the title and the date (or the link) match.
    The link key is only used for entries without a guid of their own: a feed
    with guids may reuse one title and link for different items ("Deal of the
    day" pointing at the home page), and those must not collapse into one.
    Scheme, www., trailing slashes and tracking parameters are ignored."""
    title = clean_text(entry.get("title"), 1000).casefold()
    if not title:
        return []
    out = []
    if published:
        out.append("~tp:" + _short_hash(f"{title}|{int(published)}"))
    link = entry.get("link")
    if not (entry.get("id") or entry.get("guid")) and _valid_http(link):
        norm = _norm_link(link)
        if norm:
            out.append("~lt:" + _short_hash(f"{norm}|{title}"))
    return out


# aid: primary key; keys: aid plus aliases; published: epoch or None
Article = namedtuple("Article", "aid keys entry published")


def collect_articles(parsed_results):
    """Merge entries from every parsed source, one Article per primary id.

    Returns (articles, ids_by_source) where ids_by_source[url] is the set of
    primary ids that source carried."""
    articles, by_id, ids_by_source = [], {}, {}
    for url, _sst, r in parsed_results:
        ids = ids_by_source.setdefault(url, set())
        for e in r["parsed"].entries:
            aid = entry_id(e)
            if not aid:
                continue
            ids.add(aid)
            if aid in by_id:
                continue
            published = entry_time(e)
            by_id[aid] = len(articles)
            articles.append((aid, e, published, entry_aliases(e, published)))
    # An alias that two different articles share in this very download
    # cannot tell them apart, so it is not used for either.
    owners = {}
    for aid, _e, _p, aliases in articles:
        for a in aliases:
            owners.setdefault(a, set()).add(aid)
    out = [Article(aid, (aid, *[a for a in aliases if len(owners[a]) == 1]), e, p)
           for aid, e, p, aliases in articles]
    return out, ids_by_source


def _entry_image(entry):
    for m in entry.get("media_thumbnail") or []:
        if isinstance(m, dict) and _valid_http(m.get("url")):
            return m["url"]
    for m in entry.get("media_content") or []:
        if isinstance(m, dict) and _valid_http(m.get("url")):
            kind = str(m.get("medium") or m.get("type") or "")
            if kind.startswith("image"):
                return m["url"]
    for link in entry.get("links") or []:
        if (link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image")
                and _valid_http(link.get("href"))):
            return link["href"]
    return None


def build_article(feed, entry, published, uid=None):
    """Platform-neutral article; destinations.py formats it per service.
    `uid` is stable per (destination, article) so services that support
    idempotent sends (Matrix) can drop a repeat of the same post."""
    link = entry.get("link")
    return {
        "title": clean_text(entry.get("title"), TITLE_MAX) or "Untitled article",
        "link": link if _valid_http(link) else None,
        "summary": clean_text(entry.get("summary") or entry.get("description"), SUMMARY_LEN),
        "published": published,
        "image": _entry_image(entry),
        "feed_name": clean_text(feed["name"], FEED_NAME_MAX) or "RSS",
        "uid": uid,
    }


# --- Posting --------------------------------------------------------------------

def _mark_sent(conn, key, ids, now):
    conn.executemany(
        "INSERT INTO sent(webhook, article_id, first_sent, last_seen) VALUES (?,?,?,?) "
        "ON CONFLICT(webhook, article_id) DO UPDATE SET last_seen = excluded.last_seen",
        [(key, i, now, now) for i in dict.fromkeys(ids)],
    )


def _unmark_sent(conn, key, ids):
    conn.executemany("DELETE FROM sent WHERE webhook = ? AND article_id = ?", [(key, i) for i in ids])


def _known_ids(conn, key, ids):
    known = set()
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" for _ in chunk)
        known.update(r[0] for r in conn.execute(
            f"SELECT article_id FROM sent WHERE webhook = ? AND article_id IN ({marks})",
            (key, *chunk)))
    return known


def _deliver_to_hook(feed, hook, articles, seeded, seed_only=frozenset(), can_seed=True):
    """Handle one destination. Returns (posted, failure_message, outcome)
    where outcome is "seeded", "waiting" or None.

    `seed_only` holds primary ids from sources that loaded for the first time
    (or the first time in a long while) in this check: they are remembered as
    seen and never posted, so adding a source does not post its backlog.

    Every article is recorded as sent *before* it is posted. A clean failure
    removes the record again so the next check retries it. Anything that goes
    wrong after the post (a crash, a full disk, a locked database) can then
    only lose a post, never repeat it on every check."""
    key = core.dest_key(hook)
    label = hook["label"] or dest.TYPES[hook["type"]]["name"]
    now = time.time()
    all_keys = [k for a in articles for k in a.keys]
    with _hook_lock(key), core.db() as conn:
        if key not in seeded:
            if not can_seed:
                # A source failed this round. Seeding now would leave its
                # articles out of the memory and post them once it recovers.
                return 0, None, "waiting"
            # First time this feed goes to this destination: remember every
            # article currently in the feed without posting any of them.
            with core.transaction(conn):
                _mark_sent(conn, key, all_keys, now)
                conn.execute("INSERT OR REPLACE INTO seeded(feed_id, webhook, seeded_at) VALUES (?,?,?)",
                             (feed["id"], key, now))
            log(f"[{feed['name']}] seeded {len(articles)} article(s) for {label}")
            return 0, None, "seeded"

        known = _known_ids(conn, key, all_keys)
        old = [a for a in articles if not known.isdisjoint(a.keys)]
        new = [a for a in articles if known.isdisjoint(a.keys)]
        with core.transaction(conn):
            # Refresh "last seen" and record every key of a known article, so
            # it is still recognized if the feed later changes one of them.
            _mark_sent(conn, key, [k for a in old for k in a.keys], now)
            _mark_sent(conn, key, [k for a in new if a.aid in seed_only for k in a.keys], now)
        cutoff = now - RECENT_WINDOW
        # Undated entries are eligible; the seeded memory keeps them from
        # repeating.
        fresh = sorted((a for a in new if a.aid not in seed_only
                        and (a.published is None or a.published >= cutoff)),
                       key=lambda a: a.published or 0)
        if not fresh:
            return 0, None, None
        if len(fresh) > MAX_POSTS_PER_CHECK:
            skipped, fresh = fresh[:-MAX_POSTS_PER_CHECK], fresh[-MAX_POSTS_PER_CHECK:]
            with core.transaction(conn):
                _mark_sent(conn, key, [k for a in skipped for k in a.keys], now)
                core.log_delivery(conn, feed, label, f"{len(skipped)} older article(s) not posted", None,
                                  f"Skipped: more than {MAX_POSTS_PER_CHECK} new articles in one check", False)

        posted = 0
        for i, a in enumerate(fresh):
            if STOP.is_set():
                break                              # the rest go out on the next check
            if i:
                time.sleep(POST_DELAY)
            article = build_article(feed, a.entry, a.published, uid=_short_hash(f"{key}|{a.aid}"))
            with core.transaction(conn):
                _mark_sent(conn, key, a.keys, time.time())
            res = dest.send(hook, article)
            if res.ok or res.permanent:
                core.log_delivery(conn, feed, label, article["title"], article["link"], res.message, res.ok)
                posted += res.ok
                continue
            if res.uncertain and hook["type"] not in dest.IDEMPOTENT:
                # The request went out but no answer came back, so the post
                # may well be up. Retrying risks a duplicate; keep it recorded.
                msg = f"{res.message}; not retried, so it can not be posted twice"
                core.log_delivery(conn, feed, label, article["title"], article["link"], msg, False)
                return posted, msg, None
            with core.transaction(conn):
                _unmark_sent(conn, key, a.keys)
            core.log_delivery(conn, feed, label, article["title"], article["link"], res.message, False)
            return posted, res.message, None
        return posted, None, None


def _source_name(url):
    p = urlparse(url)
    path = p.path if len(p.path) <= 40 else p.path[:39] + "…"
    return f"{p.hostname or url}{path}"


def check_feed(feed):
    """Check one feed end to end (every source) and record the result."""
    fid = feed["id"]
    urls = core.feed_urls(feed)
    with core.db() as conn:
        core.update_feed_state(conn, fid, checking_since=time.time(), force_requested=None)
        seeded = {r[0] for r in conn.execute("SELECT webhook FROM seeded WHERE feed_id = ?", (fid,))}
        src_rows = core.source_states(conn, fid)

    hook_keys = [core.dest_key(h) for h in feed["webhooks"]]
    # No conditional GET while a destination still needs seeding; a 304 would
    # leave that source's articles out of the seed and post them later.
    all_seeded = bool(hook_keys) and all(k in seeded for k in hook_keys)

    started = time.time()
    results = []
    for url in urls:
        if STOP.is_set():
            return
        sst = src_rows.get(url, {})
        last_parsed = sst.get("last_parsed") or 0
        conditional = all_seeded and started - last_parsed < FULL_FETCH_EVERY
        res = fetch_feed(url,
                         sst.get("etag") if conditional else None,
                         sst.get("modified") if conditional else None,
                         cookies=feed.get("cookies"), user_agent=feed.get("user_agent"))
        results.append((url, sst, res))

    now = time.time()
    failed, redirected = [], None
    with core.db() as conn:
        for url, sst, res in results:
            upd = {"last_checked": now, "status_code": res["status"],
                   "redirected_to": res["redirected_to"], "redirect_code": res["redirect_code"]}
            if res["error"]:
                upd.update(error=res["error"], failures=(sst.get("failures") or 0) + 1)
                failed.append((url, res["error"], upd["failures"]))
            else:
                upd.update(error=None, failures=0, last_success=now)
                if not res["not_modified"]:
                    upd.update(etag=res["etag"], modified=res["modified"])
            if res["redirected_to"] and not redirected:
                redirected = (res["redirected_to"], res["redirect_code"])
            core.update_source_state(conn, fid, url, **upd)

    state = {"last_checked": now, "checking_since": None,
             "status_code": results[0][2]["status"] if len(results) == 1 else None,
             "redirected_to": redirected[0] if redirected else None,
             "redirect_code": redirected[1] if redirected else None}
    if failed:
        url, err, n = failed[0]
        if len(urls) == 1:
            summary = err
        else:
            summary = f"{_source_name(url)}: {err}"
            if len(failed) > 1:
                summary += f" (+{len(failed) - 1} more source{'s' if len(failed) > 2 else ''} failing)"
        state.update(error=summary, failures=max(f[2] for f in failed))
        log(f"[{feed['name']}] {summary}")
    else:
        state.update(error=None, failures=0)

    ok_results = [(u, sst, r) for u, sst, r in results if not r["error"]]
    if not ok_results:
        with core.db() as conn:
            core.update_feed_state(conn, fid, **state)
        return
    state["last_success"] = now
    parsed_results = [(u, sst, r) for u, sst, r in ok_results if r["parsed"] is not None]
    if not parsed_results:
        with core.db() as conn:        # every working source answered 304
            core.update_feed_state(conn, fid, **state)
        return

    articles, ids_by_source = collect_articles(parsed_results)
    # Sources read for the first time, or for the first time in so long that
    # their memory may have been pruned, contribute only to the memory. An
    # article that an established source also carries is treated normally.
    established, first_load = set(), set()
    for url, sst, r in parsed_results:
        last = sst.get("last_parsed")
        is_new = not last or now - last > STALE_SOURCE_AFTER
        (first_load if is_new else established).update(ids_by_source.get(url, ()))
        if is_new and all_seeded:
            why = "new source" if not last else f"not read for {int((now - last) // 86400)} days"
            log(f"[{feed['name']}] {_source_name(url)}: {why}; existing articles marked as seen")
    seed_only = frozenset(first_load - established)
    complete = len(parsed_results) == len(results)

    if not feed["webhooks"]:
        state.update(last_post_status="No destinations configured", last_post_ok=0,
                     last_post_time=time.time())
    else:
        total, failures, seeded_any, waiting = 0, [], False, False
        for hook in feed["webhooks"]:
            if STOP.is_set():
                break
            posted, fail, outcome = _deliver_to_hook(feed, hook, articles, seeded, seed_only, complete)
            total += posted
            seeded_any = seeded_any or outcome == "seeded"
            waiting = waiting or outcome == "waiting"
            if fail:
                failures.append(f"{hook['label'] or dest.TYPES[hook['type']]['name']}: {fail}")

        if failures:
            extra = f" (+{len(failures) - 1} more)" if len(failures) > 1 else ""
            state.update(last_post_status=failures[0] + extra, last_post_ok=0, last_post_time=time.time())
        elif total:
            state.update(last_post_status=f"Posted {total} article{'s' if total != 1 else ''}",
                         last_post_ok=1, last_post_time=time.time())
        elif seeded_any:
            state.update(last_post_status="New destination ready; existing articles marked as seen",
                         last_post_ok=1, last_post_time=time.time())
        elif waiting:
            state.update(last_post_status="New destination waits until every source loads",
                         last_post_ok=None, last_post_time=time.time())
        if total:
            log(f"[{feed['name']}] posted {total}")

    # Recorded last: a source only counts as read once its articles are in
    # the memory of every destination that was ready for them.
    with core.db() as conn:
        for url, _sst, _r in parsed_results:
            core.update_source_state(conn, fid, url, last_parsed=now)
        core.update_feed_state(conn, fid, **state)


def preview_feed(url, cookies=None, user_agent=None):
    """Used by the web UI to validate a URL before it is saved."""
    res = fetch_feed(url, timeout=15, cookies=cookies, user_agent=user_agent)
    out = {"ok": not res["error"], "error": res["error"], "status": res["status"],
           "redirected_to": res["redirected_to"], "title": None, "items": [], "count": 0}
    if res["parsed"] is not None:
        p = res["parsed"]
        out["title"] = clean_text(p.feed.get("title"), 120) or None
        out["count"] = len(p.entries)
        for e in p.entries[:5]:
            out["items"].append({"title": clean_text(e.get("title"), 160) or "Untitled",
                                 "published": entry_time(e)})
        if not p.entries:
            out["ok"] = False
            out["error"] = "The feed loaded but contains no articles."
    return out


# --- Main loop ------------------------------------------------------------------

class FeedScheduler:
    def __init__(self):
        self.stop = STOP
        self.inflight = set()
        self.started = {}                  # feed id -> wall-clock start of its last check
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="feed")
        self.last_prune = 0.0

    def _run_one(self, feed):
        try:
            check_feed(feed)
        except Exception as e:
            log(f"[{feed.get('name')}] unexpected error: {e!r}")
            try:
                with core.db() as conn:
                    core.update_feed_state(conn, feed["id"], checking_since=None,
                                           last_checked=time.time(),
                                           error=f"Internal error ({type(e).__name__})")
            except Exception:
                pass
        finally:
            with self.lock:
                self.inflight.discard(feed["id"])

    def run(self):
        core.initialize()
        lock = core.scheduler_lock()
        if lock is None:
            log("Another scheduler is already using this data directory; waiting for it to stop.")
            while lock is None and not self.stop.wait(5):
                lock = core.scheduler_lock()
            if lock is None:
                return
        log(f"Scheduler started. Data dir: {core.DATA_DIR}, workers: {WORKERS}")
        try:
            with core.db() as conn:
                conn.execute("UPDATE feed_state SET checking_since = NULL")
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception as e:
                    log(f"Scheduler tick failed: {e!r}")
                self.stop.wait(TICK_SECONDS)
            self.pool.shutdown(wait=True, cancel_futures=True)
            with core.db() as conn:
                conn.execute("UPDATE feed_state SET checking_since = NULL")
        finally:
            lock.close()
        log("Scheduler stopped.")

    def tick(self):
        try:
            cfg = core.load_config(strict=True)
        except core.ConfigError as e:
            # Never prune state from a config we could not read.
            log(f"config.json unreadable ({e}); skipping this cycle")
            with core.db() as conn:
                core.heartbeat(conn)
            return
        with core.db() as conn:
            core.heartbeat(conn)
            if time.time() - self.last_prune > PRUNE_EVERY:
                core.prune_state(conn, cfg)
                self.last_prune = time.time()
            states = core.feed_states(conn)
        now = time.time()
        for feed in cfg["FEEDS"]:
            st = states.get(feed["id"], {})
            started = self.started.get(feed["id"], 0)
            if now - started < MIN_RECHECK:
                continue
            forced = bool(st.get("force_requested"))
            if not forced and not feed["active"]:
                continue
            # The in-memory start time also counts, so a check whose result
            # could not be saved is not retried every few seconds.
            last = max(st.get("last_checked") or 0, started)
            if not forced and now - last < feed["update_interval"]:
                continue
            with self.lock:
                if feed["id"] in self.inflight:
                    continue
                self.inflight.add(feed["id"])
            self.started[feed["id"]] = now
            self.pool.submit(self._run_one, feed)
        if len(self.started) > 2 * len(cfg["FEEDS"]) + 50:
            live = {f["id"] for f in cfg["FEEDS"]}
            self.started = {k: v for k, v in self.started.items() if k in live}


def main():
    sys.stdout.reconfigure(line_buffering=True)
    sched = FeedScheduler()

    def _stop(signum, frame):
        log(f"Received signal {signum}; finishing current checks.")
        sched.stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    sched.run()


if __name__ == "__main__":
    main()
