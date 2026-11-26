# Web UI checks: sign-in, redirects, damaged data files, form input.

import os
import json
import time
import sqlite3

import pytest

import prss_core as core


@pytest.fixture
def web(data_dir, monkeypatch):
    import main_web
    main_web.app.config.update(TESTING=True)
    client = main_web.app.test_client()
    yield main_web, client


def csrf(client):
    """A form token, from whichever page this client can open."""
    for page in ("/login", "/settings"):
        client.get(page)
        with client.session_transaction() as s:
            if "csrf" in s:
                return s["csrf"]
    raise AssertionError("no CSRF token issued")


def make_owner(password="correct horse"):
    with core.edit_users() as users:
        users.append({"id": "u1", "username": "owner", "role": "owner", "session_epoch": 0,
                      "password": __import__("werkzeug.security").security.generate_password_hash(password)})


def login(client, next_url=None, password="correct horse"):
    token = csrf(client)
    url = "/login" + (f"?next={next_url}" if next_url else "")
    return client.post(url, data={"username": "owner", "password": password, "csrf_token": token})


@pytest.mark.parametrize("target,ok", [
    ("/settings", True), ("/edit/x?y=1", True),
    ("//evil.test/", False), ("/\\evil.test", False), ("https://evil.test/", False),
    ("/%0d%0aSet-Cookie:x", True), ("javascript:alert(1)", False), ("", False),
])
def test_safe_next(web, target, ok):
    main_web, _ = web
    with main_web.app.test_request_context("/"):
        assert (main_web.safe_next(target) == target) is ok


def test_login_does_not_redirect_off_site(web):
    _, client = web
    make_owner()
    r = login(client, next_url="/%5Cevil.test")
    assert r.status_code == 302
    assert r.headers["Location"] in ("/", "http://localhost/")


def test_corrupt_user_file_blocks_setup(web):
    _, client = web
    with open(core.USER_FILE, "w") as f:
        f.write("{broken")
    r = client.get("/setup")
    assert r.status_code == 503
    r = client.post("/setup", data={"username": "attacker", "password": "12345678", "confirm": "12345678"})
    assert r.status_code == 503
    with open(core.USER_FILE) as f:
        assert f.read() == "{broken"


def test_corrupt_config_is_never_overwritten(web):
    _, client = web
    make_owner()
    login(client)
    with open(core.CONFIG_FILE, "w") as f:
        f.write("{broken")
    token = csrf(client)
    r = client.post("/add", data={"csrf_token": token, "url": "https://ex.test/feed", "name": "x",
                                  "interval_value": "5", "interval_unit": "minutes",
                                  "dest_type": "webhook", "dest_url": "https://ex.test/hook"})
    assert r.status_code == 302
    with open(core.CONFIG_FILE) as f:
        assert f.read() == "{broken"


def test_infinite_interval_is_a_form_error(web):
    _, client = web
    make_owner()
    login(client)
    token = csrf(client)
    r = client.post("/add", data={"csrf_token": token, "url": "https://ex.test/feed",
                                  "interval_value": "inf", "interval_unit": "hours",
                                  "dest_type": "webhook", "dest_url": "https://ex.test/hook"})
    assert r.status_code == 400


def test_password_spraying_is_throttled_per_address(web):
    main_web, client = web
    make_owner()
    for i in range(main_web.LOGIN_FREE_ATTEMPTS_IP):
        token = csrf(client)
        client.post("/login", data={"username": f"user{i}", "password": "nope", "csrf_token": token})
    token = csrf(client)
    r = client.post("/login", data={"username": "owner", "password": "correct horse", "csrf_token": token})
    assert r.status_code == 429


def test_csrf_failure_ignores_foreign_referrer(web):
    _, client = web
    make_owner()
    login(client)
    r = client.post("/logout", headers={"Referer": "https://evil.test/x"})
    assert r.status_code == 302
    assert "evil.test" not in r.headers["Location"]


def test_healthz_reports_scheduler(web):
    _, client = web
    assert client.get("/healthz").status_code == 503
    with core.db() as conn:
        core.heartbeat(conn)
    assert client.get("/healthz").status_code == 200


def test_old_password_hash_is_upgraded(web):
    from werkzeug.security import generate_password_hash
    _, client = web
    with core.edit_users() as users:
        users.append({"id": "u1", "username": "owner", "role": "owner", "session_epoch": 0,
                      "password": generate_password_hash("correct horse", method="pbkdf2:sha256")})
    assert login(client).status_code == 302
    assert core.load_users()[0]["password"].startswith("scrypt:")


def test_schema_2_database_is_upgraded(tmp_path):
    core.set_data_dir(str(tmp_path))
    core.save_config({"FEEDS": [{"id": "f1", "url": "https://ex.test/f", "webhooks": []}]})
    conn = sqlite3.connect(core.DB_FILE)
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '2');
        CREATE TABLE source_state (feed_id TEXT NOT NULL, url TEXT NOT NULL, status_code INTEGER,
            error TEXT, redirected_to TEXT, redirect_code INTEGER, last_checked REAL, last_success REAL,
            failures INTEGER NOT NULL DEFAULT 0, etag TEXT, modified TEXT,
            PRIMARY KEY (feed_id, url)) WITHOUT ROWID;
        INSERT INTO source_state(feed_id, url, last_success) VALUES ('f1', 'https://ex.test/f', 1700000000);
    """)
    conn.commit()
    conn.close()
    report = core.initialize()
    assert any("schema 3" in line for line in report)
    with core.db() as conn:
        row = conn.execute("SELECT last_parsed FROM source_state").fetchone()
        assert row[0] == 1700000000
        assert core.get_meta(conn, "schema_version") == "3"
    assert core.initialize() == []


def test_every_page_renders(web):
    _, client = web
    make_owner()
    login(client)
    core.save_config({"FEEDS": [{"id": "f1", "name": "F", "url": "https://ex.test/f",
                                 "webhooks": [{"type": "discord", "url": "https://discord.com/api/webhooks/1/abc"}]}]})
    for page in ("/", "/add", "/edit/f1", "/feeds/bulk", "/settings", "/settings/users/add",
                 "/backup-restore", "/backup/download", "/backup/users/download", "/api/status"):
        r = client.get(page)
        assert r.status_code == 200, page
    assert json.loads(client.get("/api/status").data)["feeds"][0]["active"] is True
