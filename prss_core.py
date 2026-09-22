# prss_core.py
# Shared storage layer for the web UI and the scheduler.
#
# Layout of the data directory (PRSS_DATA_DIR, defaults to the script dir so
# existing installs keep working without any changes):
#
#   config.json      feeds and their destinations (human readable, backed up)
#   user.json        user accounts (hashed passwords)
#   secret.key       Flask session signing key
#   prss_state.db    SQLite: sent-article memory, feed health, delivery log,
#                    scheduler heartbeat, login throttling
#
# Legacy files from older versions (sent_articles.yaml, feed_state.json) are
# imported into prss_state.db on first start and renamed to *.migrated so a
# rollback is possible.

import os
import json
import time
import uuid
import fcntl
import shutil
import sqlite3
import hashlib
import tempfile
import contextlib

import destinations as dest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.environ.get("PRSS_DATA_DIR") or SCRIPT_DIR)

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
USER_FILE = os.path.join(DATA_DIR, "user.json")
SECRET_KEY_FILE = os.path.join(DATA_DIR, "secret.key")
DB_FILE = os.path.join(DATA_DIR, "prss_state.db")
LEGACY_SENT_FILE = os.path.join(DATA_DIR, "sent_articles.yaml")
LEGACY_STATE_FILE = os.path.join(DATA_DIR, "feed_state.json")

# 2: normalized ids and webhooks.  3: destination types (Discord, Slack,
# Matrix, ...), per-feed cookies and User-Agent.  4: extra source addresses
# per feed ("extra_urls").
CONFIG_VERSION = 4
# 1: initial SQLite layout.  2: per-source state (source_state).
DB_SCHEMA_VERSION = 2
MAX_SOURCES = 20

MIN_INTERVAL = 30
MAX_INTERVAL = 86400 * 30
DEFAULT_INTERVAL = 300

# Sent-article memory is pruned by "last time we saw this article in the feed",
# not by when it was sent. Anything still present in a feed is never pruned,
# which is what prevents undated or long-lived items from being reposted.
SENT_RETENTION_SECONDS = 30 * 86400
# Grace period before memory for a webhook that was removed from every feed is
# dropped. Protects against an accidental delete followed by a re-add/restore.
ORPHAN_GRACE_SECONDS = 86400
DELIVERY_LOG_KEEP = 500

ROLES = ("owner", "super_admin", "admin")


# --- Filesystem helpers -------------------------------------------------------

def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def atomic_write(path, data, mode=0o644):
    """Atomically replace `path` with `data` (str or bytes)."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=directory)
    try:
        with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def file_lock(name):
    """Advisory lock on a dedicated lock file.

    The lock file is never replaced, unlike the data files themselves, which
    are swapped in with os.replace(). Locking the data file directly (as the
    previous version did) does not work with atomic replacement because the
    lock stays attached to the old inode.
    """
    ensure_data_dir()
    path = os.path.join(DATA_DIR, f".{name}.lock")
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def backup_copy(path, tag):
    """Keep a one-off copy of a file before a migration rewrites it."""
    if not os.path.exists(path):
        return None
    dest = f"{path}.{tag}.bak"
    if not os.path.exists(dest):
        shutil.copy2(path, dest)
    return dest


def webhook_key(url):
    """Stable key for a webhook URL. The DB stores this instead of the URL so
    webhook tokens are not duplicated into a second file."""
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:32]


def dest_key(d):
    """Memory key for any destination type. For Discord (and the other
    URL-only types) this equals webhook_key(url), so memory from earlier
    versions stays attached to the same destination."""
    return dest.key(d)


# --- Config -------------------------------------------------------------------

def _as_bool(value, default=True):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in ("false", "0", "no", "off", "")


def _as_interval(value):
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(MAX_INTERVAL, v))


def _legacy_webhooks(feed):
    """Collect webhooks from every historical shape a feed may carry."""
    out = []
    hooks = feed.get("webhooks")
    if isinstance(hooks, list):
        for h in hooks:
            if isinstance(h, dict):
                out.append(dict(h))
            elif isinstance(h, str):
                out.append({"url": h, "label": ""})
    urls = feed.get("webhook_urls")
    if isinstance(urls, list):
        out.extend({"url": u, "label": ""} for u in urls if isinstance(u, str))
    single = feed.get("webhook_url")
    if isinstance(single, str):
        out.append({"url": single, "label": feed.get("name") or ""})
    return out


def normalize_config(data):
    """Return (config, changed). Idempotent and safe to run on every load.

    Handles: missing or duplicate feed ids, legacy webhook fields, string
    booleans/intervals, duplicate webhooks inside one feed. Missing ids are
    derived deterministically so an unpersisted normalization still yields the
    same id on every load.
    """
    original = json.dumps(data, sort_keys=True, default=str)
    if not isinstance(data, dict):
        data = {}
    feeds_in = data.get("FEEDS")
    if not isinstance(feeds_in, list):
        feeds_in = []

    seen_ids = set()
    feeds = []
    for idx, feed in enumerate(feeds_in):
        if not isinstance(feed, dict):
            continue
        url = str(feed.get("url") or "").strip()
        if not url:
            continue
        f = dict(feed)
        fid = str(f.get("id") or "").strip()
        if not fid or fid in seen_ids:
            fid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{url}#{idx}"))
            n = 0
            while fid in seen_ids:
                n += 1
                fid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{url}#{idx}#{n}"))
        seen_ids.add(fid)
        f["id"] = fid
        f["url"] = url
        f["name"] = str(f.get("name") or "").strip() or url
        f["update_interval"] = _as_interval(f.get("update_interval"))
        f["active"] = _as_bool(f.get("active"), True)

        hooks, seen_keys = [], set()
        for h in _legacy_webhooks(feed):
            d = dest.normalize(h)
            if d is None:
                continue
            k = dest.identity(d)
            if k in seen_keys:
                continue
            seen_keys.add(k)
            hooks.append(d)
        f["webhooks"] = hooks
        f.pop("webhook_urls", None)
        f.pop("webhook_url", None)

        # Additional source addresses. "url" stays the first source, so a feed
        # with one source looks exactly like it did before, and an older
        # version reading this file still fetches the first source.
        extra, seen_src = [], {url}
        raw_extra = f.get("extra_urls")
        if isinstance(raw_extra, str):
            raw_extra = [raw_extra]
        for u in raw_extra if isinstance(raw_extra, list) else []:
            u = str(u or "").strip()
            if u and u not in seen_src and u.startswith(("http://", "https://")):
                seen_src.add(u)
                extra.append(u)
        extra = extra[:MAX_SOURCES - 1]
        if extra:
            f["extra_urls"] = extra
        else:
            f.pop("extra_urls", None)

        # Optional fetch settings. Stored only when set.
        for opt in ("cookies", "user_agent"):
            v = f.get(opt)
            v = str(v).strip() if isinstance(v, (str, int, float)) else ""
            if v:
                f[opt] = v
            else:
                f.pop(opt, None)
        feeds.append(f)

    out = dict(data)
    out["FEEDS"] = feeds
    out["version"] = CONFIG_VERSION
    changed = json.dumps(out, sort_keys=True, default=str) != original
    return out, changed


class ConfigError(Exception):
    pass


def load_config(strict=False):
    """Load and normalize config.json.

    With strict=True a missing file is fine (empty config) but a corrupt file
    raises ConfigError, so callers that prune state can tell "no feeds" apart
    from "could not read feeds".
    """
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = {"FEEDS": []}
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        if strict:
            raise ConfigError(str(e))
        print(f"Warning: config.json unreadable ({e}); using empty config.")
        raw = {"FEEDS": []}
    cfg, _ = normalize_config(raw)
    return cfg


def save_config(cfg):
    cfg, _ = normalize_config(cfg)
    atomic_write(CONFIG_FILE, json.dumps(cfg, indent=4), mode=0o600)
    return cfg


@contextlib.contextmanager
def edit_config():
    """Read-modify-write the config under a lock. Yields the config dict;
    it is saved when the block exits without an exception."""
    with file_lock("config"):
        cfg = load_config()
        yield cfg
        save_config(cfg)


def feed_urls(feed):
    """Every source address of a feed, primary first."""
    return [feed["url"], *feed.get("extra_urls", [])]


def find_feed(cfg, feed_id):
    return next((f for f in cfg["FEEDS"] if f["id"] == feed_id), None)


# --- Fetch cookies --------------------------------------------------------------

def parse_cookies(text):
    """Parse cookies pasted by a user. Returns a list of (name, value).

    Accepts a Cookie header ("a=1; b=2", optionally prefixed "Cookie:"), one
    name=value per line, a Netscape cookies.txt export, or the JSON array that
    browser cookie-editor extensions export. Values are kept exactly as pasted
    (no URL-decoding), since that is how the browser sends them.
    """
    text = (text or "").strip()
    if not text:
        return []
    out = []
    if text.startswith("["):
        try:
            for c in json.loads(text):
                if isinstance(c, dict) and c.get("name"):
                    out.append((str(c["name"]).strip(), str(c.get("value", ""))))
            return _dedupe_cookies(out)
        except (ValueError, TypeError):
            pass
    for line in text.splitlines():
        line = line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        cols = line.split("\t")
        if len(cols) == 7:                      # Netscape cookies.txt
            out.append((cols[5].strip(), cols[6].strip()))
            continue
        if line.lower().startswith("cookie:"):
            line = line[7:]
        for part in line.split(";"):
            name, sep, value = part.partition("=")
            name = name.strip()
            if sep and name and name.lower() not in ("path", "domain", "expires", "max-age",
                                                     "secure", "httponly", "samesite"):
                out.append((name, value.strip()))
    return _dedupe_cookies(out)


def _dedupe_cookies(pairs):
    seen, out = {}, []
    for name, value in pairs:
        if name in seen:
            out[seen[name]] = (name, value)
        else:
            seen[name] = len(out)
            out.append((name, value))
    return out


# --- Users --------------------------------------------------------------------

def normalize_users(data):
    """Return (users, changed). Accepts the legacy single-user object.

    The previous migration wrapped a legacy object in a list but left it
    without an id or role, which made login crash with a KeyError.
    """
    original = json.dumps(data, sort_keys=True, default=str)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        data = []
    users, seen_ids, seen_names = [], set(), set()
    for u in data:
        if not isinstance(u, dict):
            continue
        name = str(u.get("username") or "").strip()
        pw = u.get("password")
        # Exact-duplicate usernames are dropped. Case variants ("Bob"/"bob")
        # are kept so a migration never deletes an account; the UI blocks
        # creating new case-variant duplicates.
        if not name or not isinstance(pw, str) or not pw or name in seen_names:
            continue
        nu = dict(u)
        nu["username"] = name
        uid = str(nu.get("id") or "").strip()
        if not uid or uid in seen_ids:
            uid = str(uuid.uuid5(uuid.NAMESPACE_OID, f"user:{name}"))
        nu["id"] = uid
        if nu.get("role") not in ROLES:
            nu["role"] = "admin"
        try:
            nu["session_epoch"] = int(nu.get("session_epoch") or 0)
        except (TypeError, ValueError):
            nu["session_epoch"] = 0
        seen_ids.add(uid)
        seen_names.add(name)
        users.append(nu)
    if users and not any(u["role"] == "owner" for u in users):
        users[0]["role"] = "owner"
    changed = json.dumps(users, sort_keys=True, default=str) != original
    return users, changed


def load_users():
    try:
        with open(USER_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        raw = json.loads(content) if content.strip() else []
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        print(f"Warning: user.json unreadable ({e}).")
        return []
    users, _ = normalize_users(raw)
    return users


def save_users(users):
    users, _ = normalize_users(users)
    atomic_write(USER_FILE, json.dumps(users, indent=2), mode=0o600)


@contextlib.contextmanager
def edit_users():
    with file_lock("users"):
        users = load_users()
        yield users
        save_users(users)


# --- Secret key ---------------------------------------------------------------

def get_secret_key():
    """Load the session key, creating it exactly once.

    Several gunicorn workers import the app at the same moment on first boot.
    The old check-then-write let each worker generate its own key, so sessions
    were randomly rejected depending on which worker served a request.
    os.link() fails if the target exists, which makes creation atomic.
    """
    ensure_data_dir()
    for _ in range(50):
        try:
            with open(SECRET_KEY_FILE, "rb") as f:
                key = f.read()
            if len(key) >= 16:
                return key
        except FileNotFoundError:
            pass
        fd, tmp = tempfile.mkstemp(prefix=".tmp_key_", dir=DATA_DIR)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(os.urandom(32))
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            try:
                os.link(tmp, SECRET_KEY_FILE)
            except FileExistsError:
                pass
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        time.sleep(0.05)
    raise RuntimeError("Could not create or read secret.key")


# --- SQLite state ---------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS sent (
    webhook    TEXT NOT NULL,
    article_id TEXT NOT NULL,
    first_sent REAL NOT NULL,
    last_seen  REAL NOT NULL,
    PRIMARY KEY (webhook, article_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS sent_last_seen ON sent(last_seen);
CREATE TABLE IF NOT EXISTS seeded (
    feed_id   TEXT NOT NULL,
    webhook   TEXT NOT NULL,
    seeded_at REAL NOT NULL,
    PRIMARY KEY (feed_id, webhook)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS feed_state (
    feed_id          TEXT PRIMARY KEY,
    status_code      INTEGER,
    error            TEXT,
    redirected_to    TEXT,
    redirect_code    INTEGER,
    last_checked     REAL,
    last_success     REAL,
    failures         INTEGER NOT NULL DEFAULT 0,
    last_post_status TEXT,
    last_post_time   REAL,
    last_post_ok     INTEGER,
    etag             TEXT,
    modified         TEXT,
    force_requested  REAL,
    checking_since   REAL
);
CREATE TABLE IF NOT EXISTS source_state (
    feed_id       TEXT NOT NULL,
    url           TEXT NOT NULL,
    status_code   INTEGER,
    error         TEXT,
    redirected_to TEXT,
    redirect_code INTEGER,
    last_checked  REAL,
    last_success  REAL,
    failures      INTEGER NOT NULL DEFAULT 0,
    etag          TEXT,
    modified      TEXT,
    PRIMARY KEY (feed_id, url)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS deliveries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    feed_id       TEXT,
    feed_name     TEXT,
    webhook_label TEXT,
    title         TEXT,
    link          TEXT,
    status        TEXT,
    ok            INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS login_failures (
    key   TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    last  REAL NOT NULL
);
"""


def connect():
    ensure_data_dir()
    conn = sqlite3.connect(DB_FILE, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextlib.contextmanager
def db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def _iso_to_epoch(value):
    if not value:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _load_legacy_yaml(path):
    import yaml
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.load(f, Loader=loader)


def _migrate_legacy_state(conn, cfg):
    """Import sent_articles.yaml and feed_state.json (pre-SQLite versions)."""
    now = time.time()
    all_hook_urls = sorted({h["url"] for f in cfg["FEEDS"] for h in f["webhooks"] if h["type"] == "discord"})
    known_keys = set()
    report = []

    if os.path.exists(LEGACY_SENT_FILE):
        try:
            memory = _load_legacy_yaml(LEGACY_SENT_FILE) or {}
        except Exception as e:
            memory = None
            report.append(f"sent_articles.yaml unreadable ({e}); webhooks will be re-seeded")
        rows = []
        if isinstance(memory, dict):
            for url, ids in memory.items():
                if not isinstance(url, str) or not isinstance(ids, (list, set, tuple)):
                    continue
                k = webhook_key(url)
                known_keys.add(k)
                rows.extend((k, str(i), now, now) for i in ids if i is not None)
        elif isinstance(memory, list):
            # Oldest format: one global list. Apply it to every current
            # webhook so nothing already posted gets posted again.
            for url in all_hook_urls:
                k = webhook_key(url)
                known_keys.add(k)
                rows.extend((k, str(i), now, now) for i in memory if i is not None)
        conn.executemany(
            "INSERT OR IGNORE INTO sent(webhook, article_id, first_sent, last_seen) VALUES (?,?,?,?)",
            rows,
        )
        report.append(f"imported {len(rows)} sent-article records")

    if os.path.exists(LEGACY_STATE_FILE):
        try:
            with open(LEGACY_STATE_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            state = json.loads(content) if content.strip() else {}
        except Exception as e:
            state = {}
            report.append(f"feed_state.json unreadable ({e})")
        if not isinstance(state, dict):
            state = {}
        valid_ids = {f["id"] for f in cfg["FEEDS"]}
        imported = 0
        for fid, entry in state.items():
            if fid not in valid_ids or not isinstance(entry, dict):
                continue
            lp = entry.get("last_post") if isinstance(entry.get("last_post"), dict) else {}
            code = entry.get("status_code")
            code = code if isinstance(code, int) else None
            checked = _iso_to_epoch(entry.get("last_checked"))
            ok = code is not None and 200 <= code < 300
            conn.execute(
                "INSERT OR REPLACE INTO feed_state(feed_id, status_code, error, last_checked, "
                "last_success, failures, last_post_status, last_post_time, last_post_ok) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (fid, code, None if ok or code is None else f"HTTP {code}", checked,
                 checked if ok else None, 0 if ok or code is None else 1,
                 lp.get("status"), _iso_to_epoch(lp.get("timestamp")),
                 1 if lp.get("status") in ("Success", "Initial check (seeded)") else 0),
            )
            imported += 1
        # Reproduce the old "already handled" rule exactly: a (feed, webhook)
        # pair counts as seeded if the feed had been checked and the webhook
        # had memory. Anything else gets seeded on its next check, which is
        # what the old version would have done too.
        pairs = 0
        for f in cfg["FEEDS"]:
            if f["id"] not in state:
                continue
            for h in f["webhooks"]:
                k = dest_key(h)
                if k in known_keys:
                    conn.execute(
                        "INSERT OR IGNORE INTO seeded(feed_id, webhook, seeded_at) VALUES (?,?,?)",
                        (f["id"], k, now),
                    )
                    pairs += 1
        report.append(f"imported state for {imported} feed(s), {pairs} seeded destination(s)")
    return report


def initialize():
    """Create/upgrade everything. Safe to call from both processes at once."""
    ensure_data_dir()
    with file_lock("migrate"):
        report = []

        # config.json: normalize and persist once so ids are stable on disk.
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                cfg, changed = normalize_config(raw)
                if changed:
                    tag = f"pre-v{CONFIG_VERSION}"
                    backup_copy(CONFIG_FILE, tag)
                    with file_lock("config"):
                        save_config(cfg)
                    report.append(f"config.json upgraded to version {CONFIG_VERSION} (backup: config.json.{tag}.bak)")
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                report.append(f"config.json is corrupt ({e}); left untouched")
        else:
            save_config({"FEEDS": []})

        # user.json: fix legacy single-user files and missing ids/roles.
        if os.path.exists(USER_FILE):
            try:
                with open(USER_FILE, "r", encoding="utf-8") as f:
                    content = f.read()
                raw = json.loads(content) if content.strip() else []
                users, changed = normalize_users(raw)
                if changed and users:
                    backup_copy(USER_FILE, "pre-v2")
                    with file_lock("users"):
                        save_users(users)
                    report.append("user.json normalized (backup: user.json.pre-v2.bak)")
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                report.append(f"user.json is corrupt ({e}); left untouched")

        with db() as conn:
            conn.executescript(SCHEMA)
            version = int(get_meta(conn, "schema_version", "0") or 0)
            if version < 1:
                cfg = load_config()
                with transaction(conn):
                    report += _migrate_legacy_state(conn, cfg)
                    set_meta(conn, "schema_version", 1)
                for path in (LEGACY_SENT_FILE, LEGACY_STATE_FILE):
                    if os.path.exists(path):
                        os.replace(path, path + ".migrated")
            if version < 2:
                # Per-source state. Each feed's existing health and cache
                # headers move to its (only) source, so a feed that has
                # already been running is not treated as a new source.
                cfg = load_config()
                primary = {f["id"]: f["url"] for f in cfg["FEEDS"]}
                with transaction(conn):
                    moved = 0
                    for r in conn.execute("SELECT * FROM feed_state").fetchall():
                        url = primary.get(r["feed_id"])
                        if not url:
                            continue
                        last_success = r["last_success"]
                        if last_success is None and conn.execute(
                                "SELECT 1 FROM seeded WHERE feed_id = ? LIMIT 1", (r["feed_id"],)).fetchone():
                            last_success = r["last_checked"] or time.time()
                        conn.execute(
                            "INSERT OR IGNORE INTO source_state(feed_id, url, status_code, error, redirected_to, "
                            "redirect_code, last_checked, last_success, failures, etag, modified) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (r["feed_id"], url, r["status_code"], r["error"], r["redirected_to"], r["redirect_code"],
                             r["last_checked"], last_success, r["failures"] or 0, r["etag"], r["modified"]))
                        moved += 1
                    set_meta(conn, "schema_version", DB_SCHEMA_VERSION)
                if moved:
                    report.append(f"database upgraded to schema 2 ({moved} feed(s) moved to per-source state)")
            # Future schema upgrades go here: `if version < 3: ...`

        for line in report:
            print(f"[migrate] {line}")
        return report


# --- State accessors ------------------------------------------------------------

def feed_states(conn):
    return {r["feed_id"]: dict(r) for r in conn.execute("SELECT * FROM feed_state")}


def update_source_state(conn, feed_id, url, **fields):
    if not fields:
        return
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    updates = ", ".join(f"{c} = excluded.{c}" for c in fields)
    conn.execute(
        f"INSERT INTO source_state(feed_id, url, {cols}) VALUES (?, ?, {marks}) "
        f"ON CONFLICT(feed_id, url) DO UPDATE SET {updates}",
        (feed_id, url, *fields.values()),
    )


def source_states(conn, feed_id=None):
    """{feed_id: {url: row}} for every feed, or {url: row} for one."""
    if feed_id is not None:
        return {r["url"]: dict(r) for r in conn.execute("SELECT * FROM source_state WHERE feed_id = ?", (feed_id,))}
    out = {}
    for r in conn.execute("SELECT * FROM source_state"):
        out.setdefault(r["feed_id"], {})[r["url"]] = dict(r)
    return out


def update_feed_state(conn, feed_id, **fields):
    if not fields:
        return
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    updates = ", ".join(f"{c} = excluded.{c}" for c in fields)
    conn.execute(
        f"INSERT INTO feed_state(feed_id, {cols}) VALUES (?, {marks}) "
        f"ON CONFLICT(feed_id) DO UPDATE SET {updates}",
        (feed_id, *fields.values()),
    )


def request_force_check(feed_id):
    with db() as conn:
        update_feed_state(conn, feed_id, force_requested=time.time())


def log_delivery(conn, feed, label, title, link, status, ok):
    conn.execute(
        "INSERT INTO deliveries(ts, feed_id, feed_name, webhook_label, title, link, status, ok) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (time.time(), feed["id"], feed["name"], label, title, link, status, 1 if ok else 0),
    )


def recent_deliveries(conn, limit=40):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM deliveries ORDER BY id DESC LIMIT ?", (limit,))]


def heartbeat(conn):
    set_meta(conn, "scheduler_heartbeat", time.time())


def last_heartbeat(conn):
    try:
        return float(get_meta(conn, "scheduler_heartbeat", "0") or 0) or None
    except ValueError:
        return None


def prune_state(conn, cfg):
    """Drop state for feeds and destinations that no longer exist."""
    now = time.time()
    feed_ids = {f["id"] for f in cfg["FEEDS"]}
    pairs = {(f["id"], dest_key(h)) for f in cfg["FEEDS"] for h in f["webhooks"]}
    sources = {(f["id"], u) for f in cfg["FEEDS"] for u in feed_urls(f)}
    live_hooks = {k for _, k in pairs}
    with transaction(conn):
        for r in conn.execute("SELECT feed_id FROM feed_state").fetchall():
            if r["feed_id"] not in feed_ids:
                conn.execute("DELETE FROM feed_state WHERE feed_id = ?", (r["feed_id"],))
        for r in conn.execute("SELECT feed_id, url FROM source_state").fetchall():
            if (r["feed_id"], r["url"]) not in sources:
                conn.execute("DELETE FROM source_state WHERE feed_id = ? AND url = ?", (r["feed_id"], r["url"]))
        for r in conn.execute("SELECT feed_id, webhook FROM seeded").fetchall():
            if (r["feed_id"], r["webhook"]) not in pairs:
                conn.execute("DELETE FROM seeded WHERE feed_id = ? AND webhook = ?",
                             (r["feed_id"], r["webhook"]))
        conn.execute("DELETE FROM sent WHERE last_seen < ?", (now - SENT_RETENTION_SECONDS,))
        for r in conn.execute("SELECT DISTINCT webhook FROM sent").fetchall():
            if r["webhook"] not in live_hooks:
                conn.execute("DELETE FROM sent WHERE webhook = ? AND last_seen < ?",
                             (r["webhook"], now - ORPHAN_GRACE_SECONDS))
        conn.execute(
            "DELETE FROM deliveries WHERE id <= (SELECT MAX(id) FROM deliveries) - ?",
            (DELIVERY_LOG_KEEP,),
        )
        conn.execute("DELETE FROM login_failures WHERE last < ?", (now - 86400,))
