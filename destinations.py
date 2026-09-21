# destinations.py
# Everything platform-specific about where articles are sent: which fields a
# destination needs, how it is identified, how a message is formatted for it,
# and how it is delivered.
#
# A destination is a dict stored in config.json under a feed's "webhooks" list:
#
#   {"type": "discord", "label": "...", "url": "...", "target": "...", "token": "..."}
#
# Only the fields a type uses are meaningful. "target" and "token" are omitted
# from config.json when empty, so a Discord destination looks exactly like it
# did in earlier versions apart from the added "type".

import html
import time
import uuid
import hashlib
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse, quote

import os

import requests

USER_AGENT = "HotOffThePRSS (self-hosted RSS relay)"
HTTP_TIMEOUT = 15
MAX_RATE_LIMIT_RETRIES = 4
EMBED_COLOR = 0x58B9FF

# Override only for a self-hosted Telegram Bot API server.
TELEGRAM_API = os.environ.get("PRSS_TELEGRAM_API", "https://api.telegram.org").rstrip("/")

DISCORD_HOSTS = {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}

# UI metadata. "fields" lists which of url/target/token the type uses, with the
# label, placeholder and whether it is required. The form and the validator
# both read this, so adding a type means adding one entry here plus a sender.
TYPES = {
    "discord": {
        "name": "Discord",
        "help": "Channel settings, Integrations, Webhooks, New Webhook, Copy Webhook URL.",
        "fields": {"url": ("Webhook URL", "https://discord.com/api/webhooks/…", True)},
    },
    "slack": {
        "name": "Slack",
        "help": "Create a Slack app with Incoming Webhooks turned on, add a webhook to a channel, and copy its URL.",
        "fields": {"url": ("Webhook URL", "https://hooks.slack.com/services/…", True)},
    },
    "mattermost": {
        "name": "Mattermost",
        "help": "Integrations, Incoming Webhooks, Add Incoming Webhook. Rocket.Chat incoming webhooks also work with this type.",
        "fields": {"url": ("Webhook URL", "https://chat.example.com/hooks/…", True)},
    },
    "matrix": {
        "name": "Matrix",
        "help": "Use a separate bot account that has joined the room. Its access token is under Settings, Help & About, Advanced in Element. Room aliases (#room:server) and room IDs (!id:server) both work.",
        "fields": {
            "url": ("Homeserver", "https://matrix.org", True),
            "target": ("Room", "#news:matrix.org or !abc123:matrix.org", True),
            "token": ("Access token", "syt_…", True),
        },
    },
    "telegram": {
        "name": "Telegram",
        "help": "Create a bot with @BotFather and add it to the group or channel (as an admin for channels). Chat IDs look like -1001234567890; public channels can use @channelname.",
        "fields": {
            "token": ("Bot token", "123456789:AA…", True),
            "target": ("Chat ID", "-1001234567890 or @channelname", True),
        },
    },
    "ntfy": {
        "name": "ntfy",
        "help": "The full topic address on ntfy.sh or your own server. An access token is only needed for protected topics.",
        "fields": {
            "url": ("Topic URL", "https://ntfy.sh/your-topic", True),
            "token": ("Access token (optional)", "tk_…", False),
        },
    },
    "gotify": {
        "name": "Gotify",
        "help": "Create an application in Gotify and use its token.",
        "fields": {
            "url": ("Server", "https://gotify.example.com", True),
            "token": ("Application token", "A1b2C3…", True),
        },
    },
    "webhook": {
        "name": "Generic webhook",
        "help": "Posts a JSON object (feed, title, link, summary, published, image) to any URL. Useful for n8n, Node-RED or Home Assistant.",
        "fields": {"url": ("URL", "https://example.com/hook", True)},
    },
}

# Types whose identity is the URL alone. Their memory key is the same hash
# earlier versions used for Discord, so existing sent-article memory carries over.
_URL_KEYED = {"discord", "slack", "mattermost", "ntfy", "webhook"}


def _hash(text):
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:32]


def guess_type(url):
    """Type for a destination saved before types existed."""
    host = (urlparse(url or "").hostname or "").lower()
    if host == "hooks.slack.com":
        return "slack"
    return "discord"


def normalize(raw, fallback_label=""):
    """Clean one destination dict. Returns None if it is unusable."""
    if isinstance(raw, str):
        raw = {"url": raw}
    if not isinstance(raw, dict):
        return None
    url = str(raw.get("url") or "").strip()
    dtype = str(raw.get("type") or "").strip().lower()
    if dtype not in TYPES:
        dtype = guess_type(url)
    d = {"type": dtype, "label": str(raw.get("label") or fallback_label or "").strip(), "url": url}
    for k in ("target", "token"):
        v = str(raw.get(k) or "").strip()
        if v and k in TYPES[dtype]["fields"]:
            d[k] = v
    if "url" not in TYPES[dtype]["fields"]:
        d["url"] = ""
    if not any(d.get(k) for k in TYPES[dtype]["fields"]):
        return None
    return d


def key(d):
    """Stable identity used for sent-article memory. Never contains secrets."""
    t = d["type"]
    if t in _URL_KEYED:
        return _hash(d["url"])
    if t == "matrix":
        return _hash(f"matrix|{d['url'].rstrip('/')}|{d.get('target', '')}")
    if t == "telegram":
        bot = (d.get("token") or "").split(":", 1)[0]
        return _hash(f"telegram|{bot}|{d.get('target', '')}")
    if t == "gotify":
        return _hash(f"gotify|{d['url'].rstrip('/')}|{_hash(d.get('token', ''))}")
    return _hash(f"{t}|{d.get('url', '')}|{d.get('target', '')}")


def identity(d):
    """Used to drop duplicate destinations within one feed."""
    return key(d)


def _is_http(url):
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def validate(d):
    """Returns (errors, warnings) for one destination."""
    errors, warnings = [], []
    name = d.get("label") or TYPES[d["type"]]["name"]
    for field, (flabel, _ph, required) in TYPES[d["type"]]["fields"].items():
        if required and not d.get(field):
            errors.append(f"{name}: {flabel} is required.")
    if "url" in TYPES[d["type"]]["fields"] and d.get("url") and not _is_http(d["url"]):
        errors.append(f"{name}: the address must start with https:// or http://.")
    if errors:
        return errors, warnings
    host = (urlparse(d.get("url") or "").hostname or "").lower()
    t = d["type"]
    if t == "discord" and not (host in DISCORD_HOSTS and "/api/webhooks/" in d["url"]):
        warnings.append(f"{name} does not look like a Discord webhook address.")
    if t == "slack" and host != "hooks.slack.com":
        warnings.append(f"{name} does not look like a Slack webhook address.")
    if t == "mattermost" and "/hooks/" not in d["url"]:
        warnings.append(f"{name} does not look like a Mattermost webhook address.")
    if t == "matrix" and not d.get("target", "").startswith(("!", "#")):
        errors.append(f"{name}: the room must start with ! (room ID) or # (alias).")
    if t == "telegram" and ":" not in d.get("token", ""):
        errors.append(f"{name}: the bot token should look like 123456789:AA... from @BotFather.")
    if t == "ntfy" and len([p for p in urlparse(d["url"]).path.split("/") if p]) != 1:
        errors.append(f"{name}: use the full topic address, like https://ntfy.sh/your-topic.")
    return errors, warnings


def describe(d):
    """Short display string with secrets removed."""
    t, url = d["type"], d.get("url") or ""
    p = urlparse(url)
    host = p.netloc or url
    if t == "discord":
        parts = [x for x in p.path.split("/") if x]
        if len(parts) >= 4 and parts[0] == "api" and parts[1] == "webhooks":
            return f"{host}/api/webhooks/{parts[2]}/{parts[3][:4]}\u2026"
        return f"{host}/\u2026"
    if t in ("slack", "mattermost", "webhook"):
        return f"{host}/\u2026"
    if t == "matrix":
        return f"{d.get('target', '')} on {host}"
    if t == "telegram":
        return f"chat {d.get('target', '')}"
    if t == "ntfy":
        return f"{host}{p.path}"
    if t == "gotify":
        return host
    return host


# --- Message formatting -----------------------------------------------------------
#
# Every sender receives the same article dict, prepared by the scheduler:
#   title, link (or None), summary, published (epoch or None), image (or None),
#   feed_name

def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


def _md_escape_link_text(text):
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _slack_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _markdown(a):
    title = _md_escape_link_text(a["title"])
    head = f"**[{title}]({a['link']})**" if a.get("link") else f"**{title}**"
    parts = [head]
    if a.get("summary"):
        parts.append(a["summary"])
    parts.append(f"_{a['feed_name']}_")
    return "\n".join(parts)


def _html_message(a):
    title = html.escape(a["title"])
    head = (f'<b><a href="{html.escape(a["link"], quote=True)}">{title}</a></b>'
            if a.get("link") else f"<b>{title}</b>")
    parts = [head]
    if a.get("summary"):
        parts.append(html.escape(a["summary"]))
    parts.append(f"<i>{html.escape(a['feed_name'])}</i>")
    return parts


def _plain(a):
    lines = [a["title"]]
    if a.get("summary"):
        lines.append(a["summary"])
    if a.get("link"):
        lines.append(a["link"])
    lines.append(a["feed_name"])
    return "\n".join(lines)


def discord_embed(a):
    embed = {"title": a["title"][:256] or "Untitled article", "color": EMBED_COLOR,
             "footer": {"text": (a["feed_name"] or "RSS")[:2048]}}
    if _is_http(a.get("link")):
        embed["url"] = a["link"]  # Discord rejects the whole embed on an invalid url
    if a.get("summary"):
        embed["description"] = a["summary"]
    if a.get("published"):
        embed["timestamp"] = _iso(a["published"])
    if a.get("image"):
        embed["thumbnail"] = {"url": a["image"]}
    return embed


# --- HTTP ---------------------------------------------------------------------------

def _error_detail(r):
    try:
        j = r.json()
    except Exception:
        return (r.text or "").strip()[:120]
    if isinstance(j, dict):
        for k in ("message", "error", "description", "errorDescription"):
            if j.get(k):
                return str(j[k])[:120]
    return ""


def _retry_after(r):
    try:
        j = r.json()
        if isinstance(j, dict):
            if "retry_after" in j:
                return float(j["retry_after"])                        # Discord, seconds
            if "retry_after_ms" in j:
                return float(j["retry_after_ms"]) / 1000.0            # Matrix
            if isinstance(j.get("parameters"), dict) and "retry_after" in j["parameters"]:
                return float(j["parameters"]["retry_after"])          # Telegram
    except Exception:
        pass
    try:
        return float(r.headers.get("Retry-After", "2"))
    except (TypeError, ValueError):
        return 2.0


def _request(method, url, config_errors=(), **kw):
    """Send with rate-limit handling. Returns (ok, message, permanent).

    `permanent` means retrying this exact message will never succeed because
    the service rejected its content, so the article is marked as handled.
    Address, permission and token problems are not permanent: the article
    stays queued and is retried once the destination is fixed.
    `config_errors` lists phrases in an error reply that mean a setup problem
    even when the status code would otherwise count as a content rejection.
    """
    headers = {"User-Agent": USER_AGENT, **kw.pop("headers", {})}
    for _ in range(MAX_RATE_LIMIT_RETRIES):
        try:
            r = requests.request(method, url, headers=headers, timeout=HTTP_TIMEOUT, **kw)
        except requests.Timeout:
            return False, "The service did not respond in time", False
        except requests.RequestException:
            return False, "Could not reach the service", False
        if 200 <= r.status_code < 300:
            if r.headers.get("X-RateLimit-Remaining") == "0":
                try:
                    time.sleep(min(float(r.headers.get("X-RateLimit-Reset-After", "1")), 10))
                except ValueError:
                    time.sleep(1)
            return True, "Sent", False
        if r.status_code == 429:
            time.sleep(min(max(_retry_after(r), 0.5), 30))
            continue
        detail = _error_detail(r)
        if r.status_code in (401, 403, 404, 410):
            return False, f"Rejected ({r.status_code}) {detail}".strip() + "; check the address and token", False
        if 400 <= r.status_code < 500:
            if any(p in detail.lower() for p in config_errors):
                return False, f"Rejected ({r.status_code}) {detail}".strip(), False
            return False, f"Refused the post ({r.status_code}) {detail}".strip(), True
        return False, f"Server error ({r.status_code})", False
    return False, "Still rate limited after retries", False


# --- Senders ------------------------------------------------------------------------

def _send_discord(d, a):
    payload = {"allowed_mentions": {"parse": []}, "embeds": [discord_embed(a)]}
    return _request("POST", d["url"], json=payload)


def _send_slack(d, a):
    title = _slack_escape(a["title"]).replace("|", "\u2223")
    head = f"*<{a['link']}|{title}>*" if a.get("link") else f"*{title}*"
    body = head + (f"\n{_slack_escape(a['summary'])}" if a.get("summary") else "")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": body[:3000]}}]
    if a.get("image"):
        blocks[0]["accessory"] = {"type": "image", "image_url": a["image"], "alt_text": "thumbnail"}
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _slack_escape(a["feed_name"])[:300]}]})
    fallback = a["title"] + (f" {a['link']}" if a.get("link") else "")
    return _request("POST", d["url"], json={"text": fallback, "blocks": blocks},
                    config_errors=("no_service", "channel_not_found", "invalid_token", "no_team"))


def _send_mattermost(d, a):
    return _request("POST", d["url"], json={"text": _markdown(a)})


_matrix_rooms = {}
_matrix_lock = threading.Lock()


def _matrix_room_id(d):
    room = d["target"]
    if room.startswith("!"):
        return room, None
    base = d["url"].rstrip("/")
    cache_key = (base, room)
    with _matrix_lock:
        if cache_key in _matrix_rooms:
            return _matrix_rooms[cache_key], None
    try:
        r = requests.get(f"{base}/_matrix/client/v3/directory/room/{quote(room, safe='')}",
                         headers={"User-Agent": USER_AGENT, "Authorization": f"Bearer {d['token']}"},
                         timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        return None, "Could not reach the homeserver"
    if r.status_code != 200:
        return None, f"Could not resolve room alias {room} ({r.status_code}) {_error_detail(r)}".strip()
    rid = (r.json() or {}).get("room_id")
    if not rid:
        return None, f"Could not resolve room alias {room}"
    with _matrix_lock:
        _matrix_rooms[cache_key] = rid
    return rid, None


def _send_matrix(d, a):
    rid, err = _matrix_room_id(d)
    if err:
        return False, err, False
    url = (f"{d['url'].rstrip('/')}/_matrix/client/v3/rooms/{quote(rid, safe='')}"
           f"/send/m.room.message/prss{uuid.uuid4().hex}")
    content = {"msgtype": "m.notice", "body": _plain(a),
               "format": "org.matrix.custom.html", "formatted_body": "<br>".join(_html_message(a))}
    return _request("PUT", url, json=content, headers={"Authorization": f"Bearer {d['token']}"})


def _send_telegram(d, a):
    url = f"{TELEGRAM_API}/bot{d['token']}/sendMessage"
    text = "\n".join(_html_message(a))[:4096]
    payload = {"chat_id": d["target"], "text": text, "parse_mode": "HTML"}
    return _request("POST", url, json=payload,
                    config_errors=("chat not found", "not enough rights", "bot was kicked",
                                   "not a member", "have no rights", "group chat was upgraded"))


def _send_ntfy(d, a):
    p = urlparse(d["url"])
    topic = [x for x in p.path.split("/") if x][0]
    payload = {"topic": topic, "title": a["title"][:250], "message": a.get("summary") or a["feed_name"],
               "tags": ["newspaper"]}
    if a.get("link"):
        payload["click"] = a["link"]
        payload["actions"] = [{"action": "view", "label": "Open", "url": a["link"]}]
    headers = {"Authorization": f"Bearer {d['token']}"} if d.get("token") else {}
    return _request("POST", f"{p.scheme}://{p.netloc}", json=payload, headers=headers)


def _send_gotify(d, a):
    msg = a.get("summary") or ""
    if a.get("link"):
        msg = (msg + "\n\n" if msg else "") + a["link"]
    payload = {"title": a["title"][:250], "message": msg or a["feed_name"], "priority": 5,
               "extras": {"client::display": {"contentType": "text/plain"}}}
    if a.get("link"):
        payload["extras"]["client::notification"] = {"click": {"url": a["link"]}}
    return _request("POST", f"{d['url'].rstrip('/')}/message", json=payload,
                    headers={"X-Gotify-Key": d["token"]})


def _send_webhook(d, a):
    payload = {"feed": a["feed_name"], "title": a["title"], "link": a.get("link"),
               "summary": a.get("summary"), "published": _iso(a.get("published")),
               "image": a.get("image")}
    return _request("POST", d["url"], json=payload)


_SENDERS = {
    "discord": _send_discord, "slack": _send_slack, "mattermost": _send_mattermost,
    "matrix": _send_matrix, "telegram": _send_telegram, "ntfy": _send_ntfy,
    "gotify": _send_gotify, "webhook": _send_webhook,
}


def send(d, article):
    """Deliver one article. Returns (ok, message, permanent)."""
    sender = _SENDERS.get(d.get("type"))
    if sender is None:
        return False, f"Unknown destination type {d.get('type')!r}", False
    return sender(d, article)


def test_article(label, username):
    return {"title": "Hot Off The PRSS is connected",
            "link": None,
            "summary": f"New articles for {label[:80]} will appear here.",
            "published": time.time(), "image": None,
            "feed_name": f"Test sent by {username}"}
