# scheduler.py
# This script is a dedicated background process for fetching and posting RSS feeds.
# It's designed to be run as a standalone service.

import os
import re
import json
import yaml
import time
import uuid
import fcntl
import calendar
import tempfile
import feedparser
import requests
from datetime import datetime, timedelta, timezone

# --- Get the absolute path of the script's directory ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Configuration & State Files (now with absolute paths) ---
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
SENT_ARTICLES_FILE = os.path.join(SCRIPT_DIR, "sent_articles.yaml")
FEED_STATE_FILE = os.path.join(SCRIPT_DIR, "feed_state.json")

# --- Set a common User-Agent for all requests ---
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0"
feedparser.USER_AGENT = USER_AGENT

# --- Limits ---
FEED_FETCH_TIMEOUT = 20  # seconds, to avoid a hung host stalling the whole loop
DISCORD_TITLE_MAX = 256
DISCORD_DESC_MAX = 4096
DISCORD_FOOTER_MAX = 2048
SUMMARY_TARGET_LEN = 250  # our own soft cap to keep embeds tidy
SENT_ARTICLES_PER_WEBHOOK_CAP = 10000

_HTML_TAG_RE = re.compile(r'<[^<]+?>')


# --- Atomic file write helper -------------------------------------------------

def _atomic_write(path, writer):
    """Write to `path` atomically. `writer(fileobj)` does the actual writing.

    Writes to a temp file in the same directory, then os.replace() swaps it in.
    This prevents a partially-written file if the process is killed mid-write.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=directory)
    try:
        with os.fdopen(fd, 'w') as f:
            writer(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --- Data Loading and Saving Functions ---------------------------------------

def load_config():
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"FEEDS": []}


def load_feed_state():
    """Load feed state with a shared lock so we don't read a half-written file."""
    try:
        with open(FEED_STATE_FILE, 'r') as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                content = f.read()
                if not content:
                    return {}
                return json.loads(content)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_feed_state(state_data):
    """Write feed state atomically. Callers that mutate existing state should
    prefer update_feed_state() to avoid clobbering concurrent writers."""
    _atomic_write(
        FEED_STATE_FILE,
        lambda f: json.dump(state_data, f, indent=4),
    )


def update_feed_state(feed_id, updates):
    """Atomically read-modify-write a single feed's entry in feed_state.json.

    Holds an exclusive file lock across the read and write so the web UI's
    force-check and the background scheduler cannot clobber each other.
    """
    # Make sure the file exists so we can open r+ with a lock.
    if not os.path.exists(FEED_STATE_FILE):
        try:
            _atomic_write(FEED_STATE_FILE, lambda f: json.dump({}, f))
        except FileExistsError:
            pass

    with open(FEED_STATE_FILE, 'r+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            content = f.read()
            try:
                state = json.loads(content) if content else {}
            except json.JSONDecodeError:
                state = {}

            entry = state.get(feed_id, {})
            entry.update(updates)
            state[feed_id] = entry

            f.seek(0)
            f.truncate()
            json.dump(state, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

    return state


def prune_feed_state(valid_feed_ids):
    """Remove feed_state entries for feeds that no longer exist in config."""
    if not os.path.exists(FEED_STATE_FILE):
        return
    valid = set(valid_feed_ids)
    with open(FEED_STATE_FILE, 'r+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            content = f.read()
            try:
                state = json.loads(content) if content else {}
            except json.JSONDecodeError:
                return
            stale = [k for k in state if k not in valid]
            if not stale:
                return
            for k in stale:
                del state[k]
            f.seek(0)
            f.truncate()
            json.dump(state, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
            print(f"Pruned {len(stale)} stale feed_state entr{'y' if len(stale) == 1 else 'ies'}.")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def prune_sent_articles(active_webhook_urls):
    """Drop sent-article memory for webhooks no longer referenced by any feed."""
    if not os.path.exists(SENT_ARTICLES_FILE):
        return
    active = set(active_webhook_urls)
    with open(SENT_ARTICLES_FILE, 'r+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            try:
                memory = yaml.safe_load(f) or {}
            except yaml.YAMLError:
                return
            if not isinstance(memory, dict):
                # Legacy list format from pre-per-webhook era; nothing useful to keep.
                memory = {}
            stale = [url for url in memory if url not in active]
            if not stale:
                return
            for url in stale:
                del memory[url]
            f.seek(0)
            f.truncate()
            yaml.dump(memory, f)
            f.flush()
            os.fsync(f.fileno())
            print(f"Pruned sent-article memory for {len(stale)} unused webhook(s).")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def filter_and_update_sent_articles_for_webhook(webhook_url, article_ids_to_check):
    """
    Atomically checks which articles are new *for a specific webhook*
    and updates the sent articles file.
    Returns a set of article IDs that are genuinely new for this webhook.
    """
    if not article_ids_to_check:
        return set()

    # Ensure file exists so we can always open r+ with a lock.
    if not os.path.exists(SENT_ARTICLES_FILE):
        try:
            with open(SENT_ARTICLES_FILE, 'x') as f:
                yaml.dump({}, f)
        except FileExistsError:
            pass

    new_article_ids = set()
    try:
        with open(SENT_ARTICLES_FILE, 'r+') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                all_webhooks_memory = yaml.safe_load(f) or {}
                if not isinstance(all_webhooks_memory, dict):
                    all_webhooks_memory = {}

                sent_articles_set = set(all_webhooks_memory.get(webhook_url, []))
                genuinely_new_ids = set(article_ids_to_check) - sent_articles_set

                if genuinely_new_ids:
                    updated = sent_articles_set | genuinely_new_ids
                    updated_list = sorted(updated)[-SENT_ARTICLES_PER_WEBHOOK_CAP:]
                    all_webhooks_memory[webhook_url] = updated_list

                    f.seek(0)
                    f.truncate()
                    yaml.dump(all_webhooks_memory, f)
                    f.flush()
                    os.fsync(f.fileno())

                    new_article_ids = genuinely_new_ids
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        print(f"Error in filter_and_update_sent_articles_for_webhook: {e}")

    return new_article_ids


def seed_sent_articles_for_webhook(webhook_url, article_ids):
    """Silently mark a set of articles as already-sent for `webhook_url`,
    without triggering the 'new' return path. Used when a webhook is seen for
    the first time so we don't blast the channel with every article from the
    last 24 hours.
    """
    if not article_ids:
        return

    if not os.path.exists(SENT_ARTICLES_FILE):
        try:
            with open(SENT_ARTICLES_FILE, 'x') as f:
                yaml.dump({}, f)
        except FileExistsError:
            pass

    try:
        with open(SENT_ARTICLES_FILE, 'r+') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                memory = yaml.safe_load(f) or {}
                if not isinstance(memory, dict):
                    memory = {}

                existing = set(memory.get(webhook_url, []))
                combined = existing | set(article_ids)
                memory[webhook_url] = sorted(combined)[-SENT_ARTICLES_PER_WEBHOOK_CAP:]

                f.seek(0)
                f.truncate()
                yaml.dump(memory, f)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        print(f"Error seeding sent articles for {webhook_url}: {e}")


def webhook_is_known(webhook_url):
    """Return True if we've ever recorded sent articles for this webhook."""
    if not os.path.exists(SENT_ARTICLES_FILE):
        return False
    try:
        with open(SENT_ARTICLES_FILE, 'r') as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                memory = yaml.safe_load(f) or {}
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return isinstance(memory, dict) and webhook_url in memory
    except Exception:
        return False


# --- Parsing helpers ----------------------------------------------------------

def _entry_published_utc(entry):
    """Convert a feedparser time tuple to a timezone-aware UTC datetime.

    feedparser returns a time.struct_time in UTC (it normalizes on parse).
    The previous implementation used time.mktime() which interprets the tuple
    as *local* time and would silently shift the 24h window by the server's
    UTC offset. calendar.timegm() is the correct inverse for UTC tuples.
    """
    t = entry.get('published_parsed') or entry.get('updated_parsed')
    if not t:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    except (OverflowError, ValueError, TypeError):
        return None


def _clean_summary(raw):
    if not raw:
        return "No summary available."
    text = _HTML_TAG_RE.sub('', raw).strip()
    if len(text) > SUMMARY_TARGET_LEN:
        text = text[:SUMMARY_TARGET_LEN - 3].rstrip() + "..."
    return text


def _truncate(text, limit):
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "\u2026"  # single ellipsis char


# --- Core Logic --------------------------------------------------------------

def send_to_webhook(webhook_url, embed):
    """Sends a rich embed to a Discord webhook."""
    headers = {"Content-Type": "application/json"}
    payload = {"embeds": [embed]}
    try:
        response = requests.post(webhook_url, headers=headers, json=payload, timeout=10)
        if response.status_code in (200, 204):
            return "Success"
        if response.status_code == 429:
            print(f"Rate limited by Discord for webhook {webhook_url}.")
            return "Rate Limited"
        print(f"Error sending to webhook {webhook_url}: {response.status_code} - {response.text[:200]}")
        return f"Error: {response.status_code}"
    except requests.RequestException as e:
        print(f"Failed to connect to webhook {webhook_url}: {e}")
        return "Failed to Connect"


def _fetch_feed(feed_url):
    """Fetch an RSS/Atom feed with a wall-clock timeout.

    feedparser.parse() accepts a URL but doesn't expose a timeout, so we do
    the HTTP fetch ourselves and hand feedparser the bytes. This prevents a
    slow host from stalling the entire scheduler loop.
    """
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(feed_url, headers=headers, timeout=FEED_FETCH_TIMEOUT)
    except requests.Timeout:
        print(f"Timeout fetching feed: {feed_url}")
        return None, 408
    except requests.RequestException as e:
        print(f"Network error fetching feed {feed_url}: {e}")
        return None, 503

    parsed = feedparser.parse(resp.content)
    # Prefer the real HTTP status from requests over feedparser's best-effort.
    return parsed, resp.status_code


def check_single_feed(feed_config, feed_state):
    """Checks a single feed for new articles and posts them.

    Note: `feed_state` is passed in for *read-only* reference (to determine
    whether this is a feed's first check). State writes are done by the caller
    via update_feed_state() under a file lock.
    """
    feed_url = feed_config.get("url")
    feed_id = feed_config.get("id")

    feed_data, status_code = _fetch_feed(feed_url)
    last_post_status = None

    if feed_data is None or not feed_data.entries:
        if status_code and not (200 <= status_code < 300):
            print(f"Could not fetch feed: {feed_url} (Status: {status_code})")
        return status_code or 500, last_post_status

    now = datetime.now(timezone.utc)
    twenty_four_hours_ago = now - timedelta(hours=24)

    recent_articles = []
    for entry in feed_data.entries:
        published_dt = _entry_published_utc(entry)
        if published_dt is None:
            continue
        if published_dt >= twenty_four_hours_ago:
            recent_articles.append(entry)

    if not recent_articles:
        return status_code, last_post_status

    # Sort newest-first for the id-map build; we'll re-sort post candidates
    # oldest-first at send time so users see chronological order in Discord.
    recent_articles.sort(
        key=lambda x: _entry_published_utc(x) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    article_id_map = {}
    for entry in recent_articles:
        aid = entry.get('id') or entry.get('link')
        if aid and aid not in article_id_map:
            article_id_map[aid] = entry
    all_recent_ids = list(article_id_map.keys())

    webhooks = feed_config.get('webhooks', [])
    if not webhooks:
        return status_code, "No webhooks configured"

    feed_is_new = feed_id not in feed_state

    for webhook in webhooks:
        webhook_url = webhook.get("url")
        if not webhook_url:
            continue

        # A webhook is "new" if the feed has never been checked OR this
        # specific webhook URL has never been seen before (e.g. just added
        # to an existing feed). In either case, silently seed its memory
        # so we don't flood the channel with backlog.
        if feed_is_new or not webhook_is_known(webhook_url):
            print(
                f"Seeding memory for webhook '{webhook.get('label') or webhook_url}' "
                f"on feed '{feed_config.get('name', feed_url)}' "
                f"({len(all_recent_ids)} article(s) marked as already-sent)."
            )
            seed_sent_articles_for_webhook(webhook_url, all_recent_ids)
            if last_post_status is None:
                last_post_status = "Initial check (seeded)"
            continue

        newly_found_ids = filter_and_update_sent_articles_for_webhook(webhook_url, all_recent_ids)
        if not newly_found_ids:
            continue

        articles_to_post = [article_id_map[i] for i in newly_found_ids if i in article_id_map]
        articles_to_post.sort(
            key=lambda x: _entry_published_utc(x) or datetime.min.replace(tzinfo=timezone.utc)
        )

        print(
            f"Found {len(articles_to_post)} new article(s) for "
            f"{feed_url} -> {webhook.get('label') or webhook_url}"
        )

        for entry in articles_to_post:
            title = _truncate(entry.get('title', 'No Title'), DISCORD_TITLE_MAX)
            link = entry.get('link', '')
            summary = _clean_summary(entry.get('summary'))
            footer_text = _truncate(f"From: {feed_config.get('name', feed_url)}", DISCORD_FOOTER_MAX)

            embed = {
                "title": title,
                "url": link,
                "description": _truncate(summary, DISCORD_DESC_MAX),
                "color": 5814783,
                "footer": {"text": footer_text},
            }
            last_post_status = send_to_webhook(webhook_url, embed)

    return status_code, last_post_status


# --- Main Scheduler Class ----------------------------------------------------

class FeedScheduler:
    def __init__(self, interval=60):
        self.interval = interval

    def run(self):
        print(f"Scheduler started. Using config file: {CONFIG_FILE}")
        while True:
            cycle_start = time.monotonic()
            print("Scheduler running check...")

            try:
                config = load_config()
                feed_state = load_feed_state()
            except Exception as e:
                print(f"Error loading config/state: {e}")
                time.sleep(self.interval)
                continue

            feeds = config.get("FEEDS", []) or []

            # Opportunistic GC: drop state for deleted feeds and webhooks.
            try:
                prune_feed_state([f.get("id") for f in feeds if f.get("id")])
                active_webhooks = [
                    wh.get("url")
                    for f in feeds
                    for wh in (f.get("webhooks") or [])
                    if wh.get("url")
                ]
                prune_sent_articles(active_webhooks)
            except Exception as e:
                print(f"Warning: state pruning failed: {e}")

            now = datetime.now(timezone.utc)

            for feed_config in feeds:
                feed_id = feed_config.get("id")
                if not feed_id:
                    continue

                if not feed_config.get('active', True):
                    continue

                last_checked_str = feed_state.get(feed_id, {}).get('last_checked')
                update_interval = feed_config.get("update_interval", 300)

                should_check = True
                if last_checked_str:
                    try:
                        last_checked = datetime.fromisoformat(last_checked_str)
                        if now - last_checked < timedelta(seconds=update_interval):
                            should_check = False
                    except ValueError:
                        print(f"Warning: invalid last_checked for feed {feed_id}; will check.")

                if not should_check:
                    continue

                print(f"Checking feed: {feed_config.get('url')}")
                try:
                    status_code, last_post_status = check_single_feed(feed_config, feed_state)

                    updates = {
                        'status_code': status_code,
                        'last_checked': now.isoformat(),
                    }
                    if last_post_status:
                        updates['last_post'] = {
                            "status": last_post_status,
                            "timestamp": now.isoformat(),
                        }

                    feed_state = update_feed_state(feed_id, updates)
                except Exception as e:
                    print(f"An unexpected error occurred while checking feed {feed_config.get('url')}: {e}")

                # Small courtesy delay between feeds to avoid hammering upstream hosts.
                time.sleep(1)

            # Sleep for the remainder of the cycle, not a flat `interval` on
            # top of however long the checks took.
            elapsed = time.monotonic() - cycle_start
            remaining = max(1.0, self.interval - elapsed)
            time.sleep(remaining)


if __name__ == "__main__":
    scheduler = FeedScheduler()
    scheduler.run()
