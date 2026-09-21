# scheduler.py
# Background process: checks feeds on their interval and posts new articles
# to Discord webhooks. Run it next to the web UI (Docker does this for you).

import os
import re
import html
import time
import signal
import calendar
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
import feedparser

import prss_core as core

USER_AGENT = os.environ.get(
    "PRSS_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
)
feedparser.USER_AGENT = USER_AGENT

TICK_SECONDS = 3                  # how often the loop looks for due feeds
WORKERS = max(1, int(os.environ.get("PRSS_WORKERS", "4")))
FEED_TIMEOUT = 20                 # wall-clock seconds for one feed download
FEED_MAX_BYTES = 15 * 1024 * 1024
RECENT_WINDOW = 24 * 3600         # dated articles older than this are never posted
MAX_POSTS_PER_CHECK = max(1, int(os.environ.get("PRSS_MAX_POSTS_PER_CHECK", "20")))
PRUNE_EVERY = 600
POST_DELAY = 0.4

DISCORD_TITLE_MAX = 256
DISCORD_FOOTER_MAX = 2048
SUMMARY_LEN = 350
EMBED_COLOR = 0x58B9FF

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")

_hook_locks = {}
_hook_locks_guard = threading.Lock()


def _hook_lock(key):
    """Serialize work per webhook. Two feeds that share a destination (and
    possibly articles) must not both decide an article is new and post it."""
    with _hook_locks_guard:
        lock = _hook_locks.get(key)
        if lock is None:
            lock = _hook_locks[key] = threading.Lock()
        return lock


# --- Fetching -------------------------------------------------------------------

def fetch_feed(url, etag=None, modified=None, timeout=FEED_TIMEOUT):
    """Download and parse a feed. Returns a dict describing the result.

    requests follows redirects itself, so the final status is almost never
    3xx. The redirect is read from the response history instead, which is what
    the "redirected" badge was always meant to show.
    """
    headers = {
        "User-Agent": USER_AGENT,
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
        with requests.get(url, headers=headers, timeout=(10, timeout), stream=True) as resp:
            out["status"] = resp.status_code
            if resp.history and resp.url.rstrip("/") != url.rstrip("/"):
                out["redirected_to"] = resp.url
                out["redirect_code"] = resp.history[0].status_code
            if resp.status_code == 304:
                out["not_modified"] = True
                return out
            if not (200 <= resp.status_code < 300):
                out["error"] = f"HTTP {resp.status_code}"
                return out
            chunks, size = [], 0
            for chunk in resp.iter_content(65536):
                size += len(chunk)
                if size > FEED_MAX_BYTES:
                    out["error"] = "Feed is larger than 15 MB"
                    return out
                if time.monotonic() > deadline:
                    out["error"] = f"Timed out after {timeout}s"
                    return out
                chunks.append(chunk)
            body = b"".join(chunks)
            out["etag"] = resp.headers.get("ETag")
            out["modified"] = resp.headers.get("Last-Modified")
            ctype = resp.headers.get("Content-Type", "")
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

    parsed = feedparser.parse(body, response_headers={"content-type": ctype})
    # feedparser happily "parses" an HTML page into an empty feed. An empty
    # version string means it did not recognize RSS, Atom or RDF at all.
    if not parsed.entries and not parsed.get("version"):
        out["error"] = "Not an RSS or Atom feed (the address returned a web page or other content)"
        return out
    out["parsed"] = parsed
    return out


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
        text = text[: limit - 1].rstrip() + "\u2026"
    return text


def _valid_http(url):
    return isinstance(url, str) and url.startswith(("http://", "https://"))


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


def build_embed(feed, entry, published):
    embed = {
        "title": clean_text(entry.get("title"), DISCORD_TITLE_MAX) or "Untitled article",
        "color": EMBED_COLOR,
        "footer": {"text": clean_text(feed["name"], DISCORD_FOOTER_MAX) or "RSS"},
    }
    link = entry.get("link")
    if _valid_http(link):
        embed["url"] = link  # Discord rejects the whole embed on an invalid url
    summary = clean_text(entry.get("summary") or entry.get("description"), SUMMARY_LEN)
    if summary:
        embed["description"] = summary
    if published:
        embed["timestamp"] = datetime.fromtimestamp(published, tz=timezone.utc).isoformat()
    image = _entry_image(entry)
    if image:
        embed["thumbnail"] = {"url": image}
    return embed


# --- Posting --------------------------------------------------------------------

def send_webhook(url, payload):
    """POST to a Discord webhook. Returns (ok, message, permanent).

    `permanent` means retrying this exact payload will never succeed (Discord
    rejected its content), so the article is marked as handled. Everything
    else stays unmarked and is retried on the next check.
    """
    payload = {"allowed_mentions": {"parse": []}, **payload}
    for _ in range(4):
        try:
            r = requests.post(url, json=payload, timeout=15,
                              headers={"User-Agent": "HotOffThePRSS (self-hosted RSS relay)"})
        except requests.Timeout:
            return False, "Discord did not respond in time", False
        except requests.RequestException:
            return False, "Could not reach Discord", False
        if r.status_code in (200, 204):
            if r.headers.get("X-RateLimit-Remaining") == "0":
                try:
                    time.sleep(min(float(r.headers.get("X-RateLimit-Reset-After", "1")), 10))
                except ValueError:
                    time.sleep(1)
            return True, "Sent", False
        if r.status_code == 429:
            wait = 2.0
            try:
                wait = float(r.json().get("retry_after", wait))
            except Exception:
                try:
                    wait = float(r.headers.get("Retry-After", wait))
                except (TypeError, ValueError):
                    pass
            time.sleep(min(max(wait, 0.5), 30))
            continue
        if r.status_code in (401, 403, 404):
            return False, f"Webhook rejected ({r.status_code}); it may have been deleted in Discord", False
        if 400 <= r.status_code < 500:
            detail = ""
            try:
                detail = str(r.json().get("message", ""))[:120]
            except Exception:
                pass
            return False, f"Discord refused the post ({r.status_code}) {detail}".strip(), True
        return False, f"Discord server error ({r.status_code})", False
    return False, "Still rate limited by Discord after retries", False


def _mark_sent(conn, key, ids, now):
    conn.executemany(
        "INSERT INTO sent(webhook, article_id, first_sent, last_seen) VALUES (?,?,?,?) "
        "ON CONFLICT(webhook, article_id) DO UPDATE SET last_seen = excluded.last_seen",
        [(key, i, now, now) for i in ids],
    )


def _known_ids(conn, key, ids):
    known = set()
    ids = list(ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" for _ in chunk)
        known.update(r[0] for r in conn.execute(
            f"SELECT article_id FROM sent WHERE webhook = ? AND article_id IN ({marks})",
            (key, *chunk)))
    return known


def _deliver_to_hook(feed, hook, recent, all_ids, seeded):
    """Handle one destination. Returns (posted, failure_message, seeded_now)."""
    key = core.webhook_key(hook["url"])
    label = hook["label"] or "Unlabeled destination"
    now = time.time()
    with _hook_lock(key), core.db() as conn:
        if key not in seeded:
            # First time this feed goes to this destination: remember every
            # article currently in the feed without posting any of them.
            # Tracked per (feed, destination); the old per-webhook check let a
            # known webhook added to an existing feed receive the whole backlog.
            with core.transaction(conn):
                _mark_sent(conn, key, all_ids, now)
                conn.execute("INSERT OR REPLACE INTO seeded(feed_id, webhook, seeded_at) VALUES (?,?,?)",
                             (feed["id"], key, now))
            print(f"[{feed['name']}] seeded {len(all_ids)} article(s) for {label}")
            return 0, None, True

        known = _known_ids(conn, key, all_ids)
        if known:
            conn.executemany("UPDATE sent SET last_seen = ? WHERE webhook = ? AND article_id = ?",
                             [(now, key, i) for i in known])
        fresh = sorted((a for a in recent if a[0] not in known), key=lambda a: a[2] or 0)
        if not fresh:
            return 0, None, False
        if len(fresh) > MAX_POSTS_PER_CHECK:
            skipped, fresh = fresh[:-MAX_POSTS_PER_CHECK], fresh[-MAX_POSTS_PER_CHECK:]
            _mark_sent(conn, key, [a[0] for a in skipped], now)
            core.log_delivery(conn, feed, label, f"{len(skipped)} older article(s) not posted", None,
                              f"Skipped: more than {MAX_POSTS_PER_CHECK} new articles in one check", False)

        posted = 0
        for aid, entry, published in fresh:
            embed = build_embed(feed, entry, published)
            ok, msg, permanent = send_webhook(hook["url"], {"embeds": [embed]})
            # Record only after Discord accepted it (or can never accept it).
            # The old code recorded first, so any failed post was lost for good.
            if ok or permanent:
                _mark_sent(conn, key, [aid], time.time())
            core.log_delivery(conn, feed, label, embed["title"], embed.get("url"), msg, ok)
            if ok:
                posted += 1
            elif not permanent:
                return posted, msg, False
            time.sleep(POST_DELAY)
        return posted, None, False


def check_feed(feed):
    """Check one feed end to end and record the result."""
    fid = feed["id"]
    with core.db() as conn:
        core.update_feed_state(conn, fid, checking_since=time.time(), force_requested=None)
        row = conn.execute("SELECT * FROM feed_state WHERE feed_id = ?", (fid,)).fetchone()
        seeded = {r[0] for r in conn.execute("SELECT webhook FROM seeded WHERE feed_id = ?", (fid,))}
    st = dict(row) if row else {}

    hook_keys = [core.webhook_key(h["url"]) for h in feed["webhooks"]]
    # No conditional GET while a destination still needs seeding; a 304 would
    # postpone seeding until the next real change and then swallow it.
    conditional = bool(hook_keys) and all(k in seeded for k in hook_keys)
    res = fetch_feed(feed["url"],
                     st.get("etag") if conditional else None,
                     st.get("modified") if conditional else None)

    state = {"last_checked": time.time(), "checking_since": None, "status_code": res["status"],
             "redirected_to": res["redirected_to"], "redirect_code": res["redirect_code"]}

    if res["error"]:
        state.update(error=res["error"], failures=(st.get("failures") or 0) + 1)
        with core.db() as conn:
            core.update_feed_state(conn, fid, **state)
        print(f"[{feed['name']}] {res['error']}")
        return

    state.update(error=None, failures=0, last_success=time.time())
    if res["not_modified"]:
        with core.db() as conn:
            core.update_feed_state(conn, fid, **state)
        return
    state.update(etag=res["etag"], modified=res["modified"])

    entries, seen = [], set()
    for e in res["parsed"].entries:
        aid = entry_id(e)
        if aid and aid not in seen:
            seen.add(aid)
            entries.append((aid, e, entry_time(e)))
    all_ids = [a[0] for a in entries]
    cutoff = time.time() - RECENT_WINDOW
    # Undated entries are eligible; the seeded memory keeps them from
    # repeating. The old version dropped them, so date-less feeds never posted.
    recent = [a for a in entries if a[2] is None or a[2] >= cutoff]

    if not feed["webhooks"]:
        state.update(last_post_status="No destinations configured", last_post_ok=0,
                     last_post_time=time.time())
        with core.db() as conn:
            core.update_feed_state(conn, fid, **state)
        return

    total, failures, seeded_any = 0, [], False
    for hook in feed["webhooks"]:
        posted, fail, seeded_now = _deliver_to_hook(feed, hook, recent, all_ids, seeded)
        total += posted
        seeded_any = seeded_any or seeded_now
        if fail:
            failures.append(f"{hook['label'] or 'Destination'}: {fail}")

    if failures:
        extra = f" (+{len(failures) - 1} more)" if len(failures) > 1 else ""
        state.update(last_post_status=failures[0] + extra, last_post_ok=0, last_post_time=time.time())
    elif total:
        state.update(last_post_status=f"Posted {total} article{'s' if total != 1 else ''}",
                     last_post_ok=1, last_post_time=time.time())
    elif seeded_any:
        state.update(last_post_status="New destination ready; existing articles marked as seen",
                     last_post_ok=1, last_post_time=time.time())
    if total:
        print(f"[{feed['name']}] posted {total}")
    with core.db() as conn:
        core.update_feed_state(conn, fid, **state)


def preview_feed(url):
    """Used by the web UI to validate a URL before it is saved."""
    res = fetch_feed(url, timeout=15)
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
        self.stop = threading.Event()
        self.inflight = set()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="feed")
        self.last_prune = 0.0

    def _run_one(self, feed):
        try:
            check_feed(feed)
        except Exception as e:
            print(f"[{feed.get('name')}] unexpected error: {e!r}")
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
        print(f"Scheduler started. Data dir: {core.DATA_DIR}, workers: {WORKERS}")
        with core.db() as conn:
            conn.execute("UPDATE feed_state SET checking_since = NULL")
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:
                print(f"Scheduler tick failed: {e!r}")
            self.stop.wait(TICK_SECONDS)
        self.pool.shutdown(wait=True, cancel_futures=True)
        print("Scheduler stopped.")

    def tick(self):
        try:
            cfg = core.load_config(strict=True)
        except core.ConfigError as e:
            # Never prune state from a config we could not read: the old code
            # treated a corrupt file as "no feeds" and wiped all memory.
            print(f"config.json unreadable ({e}); skipping this cycle")
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
            forced = bool(st.get("force_requested"))
            if not forced and not feed["active"]:
                continue
            if not forced and now - (st.get("last_checked") or 0) < feed["update_interval"]:
                continue
            with self.lock:
                if feed["id"] in self.inflight:
                    continue
                self.inflight.add(feed["id"])
            self.pool.submit(self._run_one, feed)


def main():
    sched = FeedScheduler()

    def _stop(signum, frame):
        print(f"Received signal {signum}; finishing current checks.")
        sched.stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    sched.run()


if __name__ == "__main__":
    main()
