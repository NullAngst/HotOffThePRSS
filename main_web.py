# main_web.py
# Web dashboard. Serve with: gunicorn --bind 0.0.0.0:5000 main_web:app

import os
import sys
import logging
import time
import uuid
import hmac
import json
import secrets
from datetime import timedelta
from urllib.parse import urlparse

from flask import (Flask, render_template, request, redirect, url_for, flash,
                   send_file, session, g, jsonify, abort)
from werkzeug.security import generate_password_hash, check_password_hash

import prss_core as core
import scheduler as sched
import destinations as dest

log = logging.getLogger("prss.web")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("[%(asctime)s] [web] %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 256
MAX_USERNAME_LEN = 64
MAX_NAME_LEN = 120
LOGIN_FREE_ATTEMPTS = 5
SCHEDULER_STALE_AFTER = 45

core.initialize()

app = Flask(__name__,
            template_folder=os.path.join(core.SCRIPT_DIR, "templates"),
            static_folder=os.path.join(core.SCRIPT_DIR, "static"))
app.secret_key = core.get_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PRSS_SECURE_COOKIES") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    SEND_FILE_MAX_AGE_DEFAULT=3600,
)

if os.environ.get("PRSS_TRUST_PROXY") == "1":
    # Only enable behind a reverse proxy you control; otherwise clients can
    # spoof X-Forwarded-For and dodge login throttling.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


# --- Helpers ----------------------------------------------------------------------

def wants_json():
    return request.headers.get("X-Requested-With") == "fetch"


def rank(role):
    return {"owner": 3, "super_admin": 2, "admin": 1}.get(role, 0)


def can_manage(actor, target):
    """Owner manages everyone but themselves as owner; super admins manage admins."""
    if not actor or not target or target["id"] == actor["id"] or target["role"] == "owner":
        return False
    if actor["role"] == "owner":
        return True
    return actor["role"] == "super_admin" and target["role"] == "admin"


def require_role(minimum):
    if rank(g.user["role"]) < rank(minimum):
        if wants_json():
            abort(403)
        flash("Your role does not allow that action.", "error")
        return redirect(url_for("view_feeds"))
    return None


def validate_password(pw):
    if not pw or len(pw) < MIN_PASSWORD_LEN:
        return f"Passwords need at least {MIN_PASSWORD_LEN} characters."
    if len(pw) > MAX_PASSWORD_LEN:
        return f"Passwords can be at most {MAX_PASSWORD_LEN} characters."
    return None


def validate_username(name, users, exclude_id=None):
    if not name:
        return "Enter a username."
    if len(name) > MAX_USERNAME_LEN:
        return f"Usernames can be at most {MAX_USERNAME_LEN} characters."
    if any(u["username"].casefold() == name.casefold() and u["id"] != exclude_id for u in users):
        return "That username is taken."
    return None


def host_of(url):
    try:
        return urlparse(url).hostname or url
    except Exception:
        return url


def human_interval(seconds):
    seconds = int(seconds)
    if seconds % 86400 == 0:
        n, unit = seconds // 86400, "day"
    elif seconds % 3600 == 0:
        n, unit = seconds // 3600, "hour"
    elif seconds % 60 == 0:
        n, unit = seconds // 60, "minute"
    else:
        n, unit = seconds, "second"
    return f"every {unit}" if n == 1 else f"every {n} {unit}s"


def feed_view(feed, st, now):
    """Everything the dashboard shows about one feed, computed in one place so
    the HTML render and the live JSON updates can never disagree."""
    st = st or {}
    if not feed["active"]:
        status, label = "paused", "Paused"
    elif st.get("checking_since"):
        status, label = "checking", "Checking"
    elif st.get("force_requested"):
        status, label = "queued", "Queued"
    elif not st.get("last_checked"):
        status, label = "pending", "Waiting for first check"
    elif st.get("error"):
        status, label = "error", st["error"]
    elif st.get("redirected_to"):
        status, label = "redirected", f"Moved ({st.get('redirect_code') or 'redirect'})"
    else:
        status, label = "ok", "Healthy"
    delivery_failing = st.get("last_post_ok") == 0 and bool(st.get("last_post_status"))
    attention = status in ("error", "redirected") or (feed["active"] and delivery_failing)
    last_checked = st.get("last_checked")
    return {
        "id": feed["id"],
        "status": status,
        "label": label,
        "attention": attention,
        "failures": st.get("failures") or 0,
        "error": st.get("error"),
        "redirected_to": st.get("redirected_to"),
        "last_checked": last_checked,
        "next_check": (last_checked + feed["update_interval"]) if (feed["active"] and last_checked) else None,
        "last_success": st.get("last_success"),
        "last_post_status": st.get("last_post_status"),
        "last_post_time": st.get("last_post_time"),
        "last_post_ok": st.get("last_post_ok"),
        "delivery_failing": delivery_failing,
    }


def scheduler_view(beat, now):
    if not beat:
        return {"state": "down", "age": None, "label": "Scheduler has not started"}
    age = now - beat
    if age > SCHEDULER_STALE_AFTER:
        return {"state": "down", "age": age, "label": "Scheduler is not running"}
    return {"state": "up", "age": age, "label": "Scheduler running"}


def _ago(ts, future=False):
    if not ts:
        return "never"
    d = (ts - time.time()) if future else (time.time() - ts)
    if d < 0:
        return "due now" if future else "just now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if d >= size:
            n = f"{int(d // size)}{unit}"
            return f"in {n}" if future else f"{n} ago"
    return "in under a minute" if future else "just now"


def _iso(ts):
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


app.jinja_env.filters["ago"] = _ago
app.jinja_env.filters["iso"] = _iso


# --- Request lifecycle ------------------------------------------------------------

PUBLIC_ENDPOINTS = {"login", "setup", "static", "healthz"}


@app.before_request
def load_user():
    g.users = core.load_users()
    g.user = None
    uid = session.get("user_id")
    if uid:
        user = next((u for u in g.users if u["id"] == uid), None)
        # Sessions carry the user's epoch; changing a password bumps it and
        # signs out every other session. Old sessions without one match 0.
        if user and int(session.get("epoch", 0)) == user["session_epoch"]:
            g.user = user
        else:
            session.clear()


@app.before_request
def csrf_protect():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    expected = session.get("csrf")
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    if not expected or not hmac.compare_digest(str(expected), str(supplied)):
        if wants_json():
            log.warning("Rejected %s %s: missing or stale CSRF token", request.method, request.path)
            return jsonify(ok=False, error="Your session expired. Reload the page."), 400
        flash("That form expired. Try again.", "error")
        return redirect(request.referrer or url_for("view_feeds"))
    return None


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if not g.users:
        return redirect(url_for("setup"))
    if g.user is None:
        if wants_json():
            return jsonify(ok=False, error="Signed out"), 401
        return redirect(url_for("login", next=request.path))
    return None


@app.context_processor
def inject_globals():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return {"csrf_token": session["csrf"], "rank": rank, "can_manage": can_manage}


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
    return resp


# --- Auth ------------------------------------------------------------------------

def _throttle_key(username):
    return f"{request.remote_addr}|{username.casefold()}"


def _login_wait(conn, key):
    row = conn.execute("SELECT count, last FROM login_failures WHERE key = ?", (key,)).fetchone()
    if not row or row["count"] < LOGIN_FREE_ATTEMPTS:
        return 0
    delay = min(15 * 2 ** (row["count"] - LOGIN_FREE_ATTEMPTS), 900)
    return max(0, int(row["last"] + delay - time.time()))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if g.users:
        return redirect(url_for("login"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        err = validate_username(username, []) or validate_password(password)
        if not err and password != (request.form.get("confirm") or ""):
            err = "The passwords do not match."
        if err:
            flash(err, "error")
            return render_template("setup.html", username=username)
        with core.edit_users() as users:
            if users:  # somebody else finished setup first
                return redirect(url_for("login"))
            users.append({"id": str(uuid.uuid4()), "username": username,
                          "password": generate_password_hash(password),
                          "role": "owner", "session_epoch": 0})
        flash("Owner account created. Sign in to continue.", "success")
        return redirect(url_for("login"))
    return render_template("setup.html", username="")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not g.users:
        return redirect(url_for("setup"))
    if g.user:
        return redirect(url_for("view_feeds"))
    username = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        key = _throttle_key(username)
        with core.db() as conn:
            wait = _login_wait(conn, key)
            if wait:
                flash(f"Too many failed attempts. Try again in {wait} seconds.", "error")
                return render_template("login.html", username=username), 429
            user = next((u for u in g.users if u["username"] == username), None)
            if user is None:
                matches = [u for u in g.users if u["username"].casefold() == username.casefold()]
                user = matches[0] if len(matches) == 1 else None
            # Always run a hash check so response time does not reveal
            # whether the username exists.
            ok = check_password_hash(user["password"] if user else _DUMMY_HASH, password) and user
            if ok:
                conn.execute("DELETE FROM login_failures WHERE key = ?", (key,))
                session.clear()
                session.permanent = True
                session["user_id"] = user["id"]
                session["epoch"] = user["session_epoch"]
                nxt = request.args.get("next") or ""
                if not nxt.startswith("/") or nxt.startswith("//"):
                    nxt = url_for("view_feeds")
                return redirect(nxt)
            conn.execute(
                "INSERT INTO login_failures(key, count, last) VALUES (?, 1, ?) "
                "ON CONFLICT(key) DO UPDATE SET count = count + 1, last = excluded.last",
                (key, time.time()))
        flash("Wrong username or password.", "error")
    return render_template("login.html", username=username)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    with core.db() as conn:
        s = scheduler_view(core.last_heartbeat(conn), time.time())
    return jsonify(ok=True, scheduler=s["state"]), 200


# --- Dashboard ----------------------------------------------------------------------

@app.route("/")
def view_feeds():
    cfg = core.load_config()
    now = time.time()
    with core.db() as conn:
        states = core.feed_states(conn)
        deliveries = core.recent_deliveries(conn, 40)
        sched_state = scheduler_view(core.last_heartbeat(conn), now)
    feeds = []
    for f in cfg["FEEDS"]:
        v = feed_view(f, states.get(f["id"]), now)
        v.update(name=f["name"], url=f["url"], host=host_of(f["url"]), active=f["active"],
                 interval=f["update_interval"], interval_text=human_interval(f["update_interval"]),
                 cookies=bool(f.get("cookies")),
                 hooks=[{"label": h["label"], "type": dest.TYPES[h["type"]]["name"], "masked": dest.describe(h)}
                        for h in f["webhooks"]])
        feeds.append(v)
    counts = {
        "all": len(feeds),
        "ok": sum(1 for f in feeds if f["active"] and not f["attention"]),
        "attention": sum(1 for f in feeds if f["attention"]),
        "paused": sum(1 for f in feeds if not f["active"]),
    }
    return render_template("dashboard.html", feeds=feeds, counts=counts,
                           deliveries=deliveries, sched=sched_state)


@app.route("/api/status")
def api_status():
    cfg = core.load_config()
    now = time.time()
    with core.db() as conn:
        states = core.feed_states(conn)
        deliveries = core.recent_deliveries(conn, 40)
        sched_state = scheduler_view(core.last_heartbeat(conn), now)
    return jsonify(
        now=now,
        scheduler=sched_state,
        feeds=[feed_view(f, states.get(f["id"]), now) for f in cfg["FEEDS"]],
        deliveries=[{k: d[k] for k in ("id", "ts", "feed_name", "webhook_label", "title", "link", "status", "ok")}
                    for d in deliveries],
    )


@app.route("/api/reorder", methods=["POST"])
def api_reorder():
    order = (request.get_json(silent=True) or {}).get("order")
    if not isinstance(order, list):
        return jsonify(ok=False, error="Expected a list of feed ids."), 400
    pos = {fid: i for i, fid in enumerate(order) if isinstance(fid, str)}
    with core.edit_config() as cfg:
        # Feeds missing from the request keep their relative order at the end.
        cfg["FEEDS"].sort(key=lambda f: pos.get(f["id"], len(pos)))
    return jsonify(ok=True)


# --- Feed forms ----------------------------------------------------------------------

def parse_feed_form(form):
    """Returns (fields, errors, warnings)."""
    errors, warnings = [], []
    url = (form.get("url") or "").strip()
    name = (form.get("name") or "").strip()[:MAX_NAME_LEN]
    if not url:
        errors.append("Enter the feed address.")
    elif not url.startswith(("http://", "https://")):
        errors.append("The feed address must start with http:// or https://.")

    try:
        value = float(form.get("interval_value") or 0)
        unit = {"seconds": 1, "minutes": 60, "hours": 3600}.get(form.get("interval_unit"), 60)
        interval = int(value * unit)
    except (TypeError, ValueError):
        interval = 0
    if interval < core.MIN_INTERVAL:
        errors.append(f"Check at most every {core.MIN_INTERVAL} seconds.")
    elif interval > core.MAX_INTERVAL:
        errors.append("Check at least once every 30 days.")

    hooks, seen = [], set()
    cols = {k: form.getlist(f"dest_{k}") for k in ("type", "label", "url", "target", "token")}
    for i in range(len(cols["type"])):
        raw = {k: (v[i] if i < len(v) else "") for k, v in cols.items()}
        raw["label"] = (raw["label"] or "").strip()[:MAX_NAME_LEN]
        d = dest.normalize(raw)
        if d is None:
            continue                     # an empty row
        errs, warns = dest.validate(d)
        errors.extend(errs)
        warnings.extend(warns)
        k = dest.identity(d)
        if errs or k in seen:
            continue
        seen.add(k)
        hooks.append(d)
    if not hooks and not errors:
        errors.append("Add at least one destination.")

    cookies = (form.get("cookies") or "").strip()
    if cookies and not core.parse_cookies(cookies):
        errors.append("The cookies could not be read. Paste them as name=value pairs, for example xf_user=...; xf_session=...")
    user_agent = (form.get("user_agent") or "").strip()[:400]

    fields = {"name": name or host_of(url), "url": url, "webhooks": hooks,
              "update_interval": max(interval, core.MIN_INTERVAL), "active": form.get("active") == "true",
              "cookies": cookies, "user_agent": user_agent}
    return fields, errors, warnings


def interval_parts(seconds):
    if seconds % 3600 == 0:
        return seconds // 3600, "hours"
    if seconds % 60 == 0:
        return seconds // 60, "minutes"
    return seconds, "seconds"


def render_feed_form(feed, mode):
    value, unit = interval_parts(int(feed.get("update_interval") or core.DEFAULT_INTERVAL))
    return render_template("feed_form.html", feed=feed, mode=mode, dest_types=dest.TYPES,
                           interval_value=value, interval_unit=unit)


@app.route("/add", methods=["GET", "POST"])
def add_feed():
    if request.method == "POST":
        fields, errors, warnings = parse_feed_form(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_feed_form(fields, "add"), 400
        with core.edit_config() as cfg:
            cfg["FEEDS"].append({"id": str(uuid.uuid4()), **fields})  # empty options dropped on save
        for w in warnings:
            flash(w, "warning")
        flash(f"Added {fields['name']}. The first check marks existing articles as seen; "
              "only articles published after that are posted.", "success")
        return redirect(url_for("view_feeds"))
    blank = {"name": "", "url": "", "webhooks": [], "update_interval": core.DEFAULT_INTERVAL, "active": True}
    return render_feed_form(blank, "add")


@app.route("/edit/<feed_id>", methods=["GET", "POST"])
def edit_feed(feed_id):
    feed = core.find_feed(core.load_config(), feed_id)
    if feed is None:
        flash("That feed no longer exists.", "error")
        return redirect(url_for("view_feeds"))
    if request.method == "POST":
        fields, errors, warnings = parse_feed_form(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_feed_form({**feed, **fields}, "edit"), 400
        url_changed = False
        with core.edit_config() as cfg:
            target = core.find_feed(cfg, feed_id)
            if target is None:
                flash("That feed was deleted while you were editing it.", "error")
                return redirect(url_for("view_feeds"))
            # A different address, or different cookies (a signed-in view can
            # show threads a guest never saw), is a different set of articles.
            url_changed = (target["url"] != fields["url"]
                           or (target.get("cookies") or "") != fields["cookies"])
            target.update(fields)
        if url_changed:
            # Re-seed every destination so the new article set's backlog is
            # not posted.
            with core.db() as conn, core.transaction(conn):
                conn.execute("DELETE FROM seeded WHERE feed_id = ?", (feed_id,))
                core.update_feed_state(conn, feed_id, etag=None, modified=None, redirected_to=None,
                                       redirect_code=None)
        for w in warnings:
            flash(w, "warning")
        flash(f"Saved {fields['name']}.", "success")
        return redirect(url_for("view_feeds"))
    return render_feed_form(feed, "edit")


@app.route("/feeds/bulk", methods=["GET", "POST"])
def bulk_edit():
    """Change the check interval and/or active state of many feeds at once."""
    cfg = core.load_config()
    if not cfg["FEEDS"]:
        flash("There are no feeds to edit yet.", "warning")
        return redirect(url_for("view_feeds"))
    if request.method == "POST":
        form = request.form
        ids = set(form.getlist("feed_id"))
        change_interval = form.get("change_interval") == "true"
        status = form.get("status") or "keep"
        errors = []
        if not ids:
            errors.append("Select at least one feed.")
        interval = None
        if change_interval:
            try:
                value = float(form.get("interval_value") or 0)
                unit = {"seconds": 1, "minutes": 60, "hours": 3600}.get(form.get("interval_unit"), 60)
                interval = int(value * unit)
            except (TypeError, ValueError):
                interval = 0
            if interval < core.MIN_INTERVAL:
                errors.append(f"Check at most every {core.MIN_INTERVAL} seconds.")
            elif interval > core.MAX_INTERVAL:
                errors.append("Check at least once every 30 days.")
        if status not in ("keep", "active", "paused"):
            status = "keep"
        if not change_interval and status == "keep":
            errors.append("Choose something to change.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_bulk_form(cfg, form), 400
        changed = 0
        with core.edit_config() as cfg:
            for f in cfg["FEEDS"]:
                if f["id"] not in ids:
                    continue
                before = (f["update_interval"], f["active"])
                if interval is not None:
                    f["update_interval"] = interval
                if status != "keep":
                    f["active"] = status == "active"
                if (f["update_interval"], f["active"]) != before:
                    changed += 1
        parts = []
        if interval is not None:
            parts.append(f"checked {human_interval(interval)}")
        if status != "keep":
            parts.append("active" if status == "active" else "paused")
        flash(f"Updated {changed} of {len(ids)} selected feed{'s' if len(ids) != 1 else ''}: now "
              + " and ".join(parts) + ".", "success")
        return redirect(url_for("view_feeds"))
    return render_bulk_form(cfg, None)


def render_bulk_form(cfg, form):
    feeds = [{"id": f["id"], "name": f["name"], "host": host_of(f["url"]), "active": f["active"],
              "interval": f["update_interval"], "interval_text": human_interval(f["update_interval"])}
             for f in cfg["FEEDS"]]
    groups = {}
    for f in feeds:
        groups.setdefault(f["interval"], []).append(f)
    interval_groups = [{"seconds": k, "text": human_interval(k), "count": len(v)}
                       for k, v in sorted(groups.items())]
    selected = set(form.getlist("feed_id")) if form is not None else {f["id"] for f in feeds}
    value, unit = interval_parts(core.DEFAULT_INTERVAL)
    if form is not None:
        value = form.get("interval_value") or value
        unit = form.get("interval_unit") if form.get("interval_unit") in ("seconds", "minutes", "hours") else unit
    return render_template("bulk_edit.html", feeds=feeds, groups=interval_groups, selected=selected,
                           interval_value=value, interval_unit=unit,
                           change_interval=(form is None or form.get("change_interval") == "true"),
                           status=(form.get("status") if form is not None else "keep"))


@app.route("/delete/<feed_id>", methods=["POST"])
def delete_feed(feed_id):
    removed = None
    with core.edit_config() as cfg:
        removed = core.find_feed(cfg, feed_id)
        cfg["FEEDS"] = [f for f in cfg["FEEDS"] if f["id"] != feed_id]
    if removed:
        with core.db() as conn, core.transaction(conn):
            conn.execute("DELETE FROM feed_state WHERE feed_id = ?", (feed_id,))
            conn.execute("DELETE FROM seeded WHERE feed_id = ?", (feed_id,))
        flash(f"Deleted {removed['name']}.", "success")
    else:
        flash("That feed no longer exists.", "error")
    return redirect(url_for("view_feeds"))


@app.route("/toggle_pause/<feed_id>", methods=["POST"])
def toggle_pause_feed(feed_id):
    with core.edit_config() as cfg:
        feed = core.find_feed(cfg, feed_id)
        if feed:
            feed["active"] = not feed["active"]
    if not feed:
        if wants_json():
            return jsonify(ok=False, error="Feed not found"), 404
        flash("That feed no longer exists.", "error")
    elif wants_json():
        return jsonify(ok=True, active=feed["active"])
    else:
        flash(f"{'Resumed' if feed['active'] else 'Paused'} {feed['name']}.", "success")
    return redirect(url_for("view_feeds"))


@app.route("/force_check/<feed_id>", methods=["POST"])
def force_check_feed(feed_id):
    """Queues a check for the scheduler instead of running it inside the web
    request. Running it here (as before) could time out the gunicorn worker
    and raced with the scheduler checking the same feed."""
    feed = core.find_feed(core.load_config(), feed_id)
    if not feed:
        if wants_json():
            return jsonify(ok=False, error="Feed not found"), 404
        flash("That feed no longer exists.", "error")
        return redirect(url_for("view_feeds"))
    core.request_force_check(feed_id)
    with core.db() as conn:
        s = scheduler_view(core.last_heartbeat(conn), time.time())
    msg = None if s["state"] == "up" else "Queued, but the scheduler is not running, so nothing will happen until it starts."
    if wants_json():
        return jsonify(ok=True, warning=msg)
    flash(msg or f"Checking {feed['name']} now.", "warning" if msg else "success")
    return redirect(url_for("view_feeds"))


@app.route("/feeds/<feed_id>/adopt-redirect", methods=["POST"])
def adopt_redirect(feed_id):
    with core.db() as conn:
        row = conn.execute("SELECT redirected_to FROM feed_state WHERE feed_id = ?", (feed_id,)).fetchone()
    new_url = row["redirected_to"] if row else None
    if not new_url:
        flash("There is no new address to switch to.", "error")
        return redirect(url_for("view_feeds"))
    with core.edit_config() as cfg:
        feed = core.find_feed(cfg, feed_id)
        if feed:
            feed["url"] = new_url
    with core.db() as conn:
        # Same feed at a new address: keep the seen-article memory.
        core.update_feed_state(conn, feed_id, redirected_to=None, redirect_code=None, etag=None, modified=None)
    flash(f"Now using {new_url}.", "success")
    return redirect(url_for("view_feeds"))


@app.route("/api/preview", methods=["POST"])
def api_preview():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify(ok=False, error="Enter an address starting with http:// or https://.")
    cookies = (data.get("cookies") or "").strip()
    if cookies and not core.parse_cookies(cookies):
        return jsonify(ok=False, error="The cookies could not be read. Paste them as name=value pairs.")
    return jsonify(sched.preview_feed(url, cookies=cookies or None,
                                      user_agent=(data.get("user_agent") or "").strip()[:400] or None))


@app.route("/api/test-webhook", methods=["POST"])
def api_test_webhook():
    data = request.get_json(silent=True) or {}
    d = dest.normalize({k: data.get(k) for k in ("type", "label", "url", "target", "token")})
    if d is None:
        log.info("Test destination from %s: nothing entered", g.user["username"])
        return jsonify(ok=False, error="Fill in the destination first.")
    errors, _ = dest.validate(d)
    if errors:
        return jsonify(ok=False, error=" ".join(errors))
    label = d["label"] or "this channel"
    ok, msg, _ = dest.send(d, dest.test_article(label, g.user["username"]))
    name = dest.TYPES[d["type"]]["name"]
    if ok:
        log.info("Test %s to %s sent by %s", name, dest.describe(d), g.user["username"])
    else:
        log.warning("Test %s to %s failed: %s", name, dest.describe(d), msg)
    return jsonify(ok=ok, error=None if ok else msg)


# --- Settings and users --------------------------------------------------------------

@app.route("/settings")
def settings():
    order = {"owner": 0, "super_admin": 1, "admin": 2}
    users = sorted(g.users, key=lambda u: (order.get(u["role"], 3), u["username"].casefold()))
    return render_template("settings.html", users=users)


@app.route("/settings/change-password", methods=["POST"])
def change_password():
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    err = validate_password(new)
    if not err and new != (request.form.get("confirm") or ""):
        err = "The new passwords do not match."
    if not err and new == current:
        err = "Choose a password different from the current one."
    if err:
        flash(err, "error")
        return redirect(url_for("settings"))
    with core.edit_users() as users:
        me = next((u for u in users if u["id"] == g.user["id"]), None)
        if not me or not check_password_hash(me["password"], current):
            flash("Your current password is wrong.", "error")
            return redirect(url_for("settings"))
        me["password"] = generate_password_hash(new)
        me["session_epoch"] = me["session_epoch"] + 1
        epoch = me["session_epoch"]
    session["epoch"] = epoch
    flash("Password changed. Other devices have been signed out.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/users/add", methods=["GET", "POST"])
def add_user():
    denied = require_role("super_admin")
    if denied:
        return denied
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        role = request.form.get("role") if g.user["role"] == "owner" else "admin"
        role = role if role in ("admin", "super_admin") else "admin"
        err = validate_username(username, g.users) or validate_password(password)
        if err:
            flash(err, "error")
            return render_template("user_form.html", username=username, role=role), 400
        with core.edit_users() as users:
            if validate_username(username, users):
                flash("That username is taken.", "error")
                return redirect(url_for("add_user"))
            users.append({"id": str(uuid.uuid4()), "username": username,
                          "password": generate_password_hash(password), "role": role,
                          "session_epoch": 0})
        flash(f"Created {username}.", "success")
        return redirect(url_for("settings"))
    return render_template("user_form.html", username="", role="admin")


def _edit_target(user_id, owner_only=False):
    """Context-free permission check. Returns (target, error_message)."""
    target = next((u for u in g.users if u["id"] == user_id), None)
    if not target:
        return None, "That user no longer exists."
    if owner_only and g.user["role"] != "owner":
        return None, "Only the owner can change roles."
    if not can_manage(g.user, target):
        return None, "Your role does not allow changes to that account."
    return target, None


@app.route("/settings/users/role/<user_id>", methods=["POST"])
def set_role(user_id):
    target, err = _edit_target(user_id, owner_only=True)
    role = request.form.get("role")
    if not err and role not in ("admin", "super_admin"):
        err = "Unknown role."
    if err:
        flash(err, "error")
        return redirect(url_for("settings"))
    with core.edit_users() as users:
        for u in users:
            if u["id"] == user_id:
                u["role"] = role
    flash(f"{target['username']} is now {'a Super Admin' if role == 'super_admin' else 'an Admin'}.", "success")
    return redirect(url_for("settings"))


# Kept for anyone scripting against the old endpoints.
@app.route("/settings/users/promote/<user_id>", methods=["POST"])
def promote_user(user_id):
    return set_role_compat(user_id, "super_admin")


@app.route("/settings/users/demote/<user_id>", methods=["POST"])
def demote_user(user_id):
    return set_role_compat(user_id, "admin")


def set_role_compat(user_id, role):
    target, err = _edit_target(user_id, owner_only=True)
    if err:
        flash(err, "error")
        return redirect(url_for("settings"))
    with core.edit_users() as users:
        for u in users:
            if u["id"] == user_id:
                u["role"] = role
    flash(f"Updated {target['username']}.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/users/reset-password/<user_id>", methods=["GET", "POST"])
def reset_password_page(user_id):
    target, err = _edit_target(user_id)
    if err:
        flash(err, "error")
        return redirect(url_for("settings"))
    if request.method == "POST":
        pw = request.form.get("new_password") or ""
        e = validate_password(pw)
        if not e and pw != (request.form.get("confirm") or ""):
            e = "The passwords do not match."
        if e:
            flash(e, "error")
            return render_template("reset_password.html", target=target), 400
        with core.edit_users() as users:
            for u in users:
                if u["id"] == user_id:
                    u["password"] = generate_password_hash(pw)
                    u["session_epoch"] = u["session_epoch"] + 1
        flash(f"Set a new password for {target['username']} and signed them out.", "success")
        return redirect(url_for("settings"))
    return render_template("reset_password.html", target=target)


@app.route("/settings/users/force-reset-password/<user_id>", methods=["POST"])
def force_reset_password(user_id):
    return reset_password_page(user_id)


@app.route("/settings/users/delete/<user_id>", methods=["POST"])
def delete_user(user_id):
    target, err = _edit_target(user_id)
    if err:
        flash(err, "error")
        return redirect(url_for("settings"))
    with core.edit_users() as users:
        users[:] = [u for u in users if u["id"] != user_id]
    flash(f"Deleted {target['username']}.", "success")
    return redirect(url_for("settings"))


# --- Backup and restore ----------------------------------------------------------------

@app.route("/backup-restore")
def backup_restore():
    denied = require_role("super_admin")
    if denied:
        return denied
    cfg = core.load_config()
    return render_template("backup.html", feed_count=len(cfg["FEEDS"]),
                           hook_count=sum(len(f["webhooks"]) for f in cfg["FEEDS"]),
                           user_count=len(g.users))


@app.route("/backup/download")
def download_backup():
    denied = require_role("super_admin")
    if denied:
        return denied
    with core.file_lock("config"):
        data = json.dumps(core.load_config(), indent=4).encode("utf-8")
    from io import BytesIO
    return send_file(BytesIO(data), as_attachment=True, download_name="config.json",
                     mimetype="application/json")


@app.route("/backup/users/download")
def download_users_backup():
    denied = require_role("owner")
    if denied:
        return denied
    from io import BytesIO
    data = json.dumps(core.load_users(), indent=2).encode("utf-8")
    return send_file(BytesIO(data), as_attachment=True, download_name="user.json",
                     mimetype="application/json")


def _read_upload():
    f = request.files.get("backup_file")
    if not f or not f.filename:
        raise ValueError("Choose a file first.")
    if not f.filename.lower().endswith(".json"):
        raise ValueError("Upload a .json file.")
    try:
        return json.loads(f.read().decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"That file is not valid JSON ({e}).")


@app.route("/backup/upload", methods=["POST"])
def upload_backup():
    denied = require_role("super_admin")
    if denied:
        return denied
    try:
        data = _read_upload()
        if not isinstance(data, dict) or not isinstance(data.get("FEEDS"), list):
            raise ValueError("That file has no FEEDS list, so it is not a Hot Off The PRSS config.")
        cfg, _ = core.normalize_config(data)
        if data["FEEDS"] and not cfg["FEEDS"]:
            raise ValueError("None of the feeds in that file have an address.")
        with core.file_lock("config"):
            core.backup_copy(core.CONFIG_FILE, time.strftime("pre-restore-%Y%m%d-%H%M%S"))
            core.save_config(cfg)
        flash(f"Restored {len(cfg['FEEDS'])} feed(s). The scheduler picks them up within a few seconds.", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("backup_restore"))


@app.route("/backup/users/upload", methods=["POST"])
def upload_users_backup():
    denied = require_role("owner")
    if denied:
        return denied
    try:
        data = _read_upload()
        raw = [data] if isinstance(data, dict) else data
        if not isinstance(raw, list) or not raw:
            raise ValueError("That file does not contain any users.")
        if not any(isinstance(u, dict) and u.get("role") == "owner" for u in raw) and len(raw) > 1:
            raise ValueError("That file has no owner account; restoring it would lock everyone out.")
        users, _ = core.normalize_users(raw)
        if not users:
            raise ValueError("None of the users in that file have a username and password.")
        with core.file_lock("users"):
            core.backup_copy(core.USER_FILE, time.strftime("pre-restore-%Y%m%d-%H%M%S"))
            core.save_users(users)
        session.clear()
        flash(f"Restored {len(users)} user(s). Sign in with an account from the backup.", "success")
        return redirect(url_for("login"))
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("backup_restore"))


@app.errorhandler(403)
def forbidden(_):
    if wants_json():
        return jsonify(ok=False, error="Your role does not allow that action."), 403
    return redirect(url_for("view_feeds"))


@app.errorhandler(413)
def too_large(_):
    flash("That file is larger than 5 MB.", "error")
    return redirect(url_for("backup_restore"))


if __name__ == "__main__":
    # Development only. Use gunicorn in production.
    debug = os.environ.get("PRSS_DEV_DEBUG") == "1"
    host = "0.0.0.0" if os.environ.get("PRSS_DEV_UNSAFE") == "1" else "127.0.0.1"
    app.run(host=host, port=5000, debug=debug)
