# End-to-end checks of the scheduler against a local feed and destination.
# Most of these are the ways an article could be posted twice, or never.

import time
import random
import sqlite3
import threading

import pytest

import prss_core as core
import scheduler as sched
import destinations as dest
from conftest import make_feed

NOW = time.time()


def check(feed, n=1):
    for _ in range(n):
        sched.check_feed(feed)


def test_new_feed_posts_only_new_articles(data_dir, site):
    items = [{"title": "Old", "guid": "1", "date": NOW - 3600}]
    site.feed("/f", lambda: items)
    site.receiver()
    feed = make_feed(site, ["/f"])
    check(feed)
    assert site.titles() == []
    items.append({"title": "New", "guid": "2", "date": NOW - 60})
    check(feed, 3)
    assert site.titles() == ["New"]


def test_changing_tracking_parameter_does_not_repost(data_dir, site):
    items = []
    site.feed("/f", lambda: [dict(i, link=f"{i['link']}?utm_source={random.random()}") for i in items])
    site.receiver()
    feed = make_feed(site, ["/f"])
    check(feed)
    items.append({"title": "Story", "link": "https://www.ex.test/story/", "date": NOW - 60})
    check(feed, 4)
    assert site.titles() == ["Story"]


def test_changing_guid_does_not_repost(data_dir, site):
    items = []
    site.feed("/f", lambda: [dict(i, guid=str(random.random())) for i in items])
    site.receiver()
    feed = make_feed(site, ["/f"])
    check(feed)
    items.append({"title": "Story", "link": "https://ex.test/s", "date": NOW - 60})
    check(feed, 4)
    assert site.titles() == ["Story"]


def test_reused_title_and_link_still_post(data_dir, site):
    items = [{"title": "Deal of the day", "link": "https://ex.test/", "guid": "d1", "date": NOW - 7200}]
    site.feed("/f", lambda: items)
    site.receiver()
    feed = make_feed(site, ["/f"])
    check(feed)
    items[:] = [{"title": "Deal of the day", "link": "https://ex.test/", "guid": "d2", "date": NOW - 60}]
    check(feed, 2)
    assert site.titles() == ["Deal of the day"]


def test_slow_destination_is_not_posted_twice(data_dir, site, monkeypatch):
    monkeypatch.setattr(dest, "HTTP_TIMEOUT", 0.5)
    items = [{"title": "Old", "guid": "1"}]
    site.feed("/f", lambda: items)
    site.receiver(delay=1.0)
    feed = make_feed(site, ["/f"])
    check(feed)
    items.append({"title": "New", "guid": "2"})
    check(feed, 3)
    time.sleep(1.2)
    assert site.titles() == ["New"]
    with core.db() as conn:
        status = conn.execute("SELECT status FROM deliveries ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "not retried" in status


def test_server_error_is_retried_next_check(data_dir, site):
    items = [{"title": "Old", "guid": "1"}]
    site.feed("/f", lambda: items)
    answers = [500, 204]
    accepted = []

    def hook(h):
        code = answers.pop(0) if answers else 204
        if code == 204:
            accepted.append(1)
        return code, {}, b""
    site.routes["/hook"] = hook
    feed = make_feed(site, ["/f"])
    check(feed)
    items.append({"title": "New", "guid": "2"})
    check(feed, 3)
    assert len(accepted) == 1


def test_failed_database_write_after_post_does_not_repeat(data_dir, site, monkeypatch):
    items = [{"title": "Old", "guid": "1"}]
    site.feed("/f", lambda: items)
    site.receiver()
    feed = make_feed(site, ["/f"])
    check(feed)
    items.append({"title": "New", "guid": "2"})
    real = core.log_delivery
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)
    monkeypatch.setattr(core, "log_delivery", flaky)
    with pytest.raises(sqlite3.OperationalError):
        check(feed)
    check(feed, 2)
    assert site.titles() == ["New"]


def test_shared_destination_gets_article_once(data_dir, site):
    items = [{"title": "Old", "guid": "1"}]
    site.feed("/a", lambda: items)
    site.feed("/b", lambda: items)
    site.receiver()
    hook = {"type": "webhook", "label": "hook", "url": site.url("/hook")}
    feeds = [{"id": f"f{i}", "name": f"F{i}", "url": site.url(p), "update_interval": 300,
              "active": True, "webhooks": [hook]} for i, p in enumerate(("/a", "/b"))]
    core.save_config({"FEEDS": feeds})
    feeds = core.load_config()["FEEDS"]
    for f in feeds:
        check(f)
    items.append({"title": "New", "guid": "2"})
    threads = [threading.Thread(target=check, args=(f,)) for f in feeds]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert site.titles() == ["New"]


def test_new_source_backlog_is_not_posted(data_dir, site):
    a = [{"title": "A1", "guid": "a1"}]
    b = [{"title": "B1", "guid": "b1"}, {"title": "B2", "guid": "b2"}]
    site.feed("/a", lambda: a)
    site.feed("/b", lambda: b)
    site.receiver()
    feed = make_feed(site, ["/a"])
    check(feed)
    feed = make_feed(site, ["/a", "/b"])
    check(feed)
    assert site.titles() == []
    b.append({"title": "B3", "guid": "b3"})
    check(feed)
    assert site.titles() == ["B3"]


def test_new_destination_waits_for_every_source(data_dir, site):
    a = [{"title": "A1", "guid": "a1"}]
    b = [{"title": "B1", "guid": "b1"}]
    up = {"b": False}
    site.feed("/a", lambda: a)
    from conftest import rss
    site.routes["/b"] = lambda h: (200, {}, rss(b)) if up["b"] else (500, {}, b"down")
    site.receiver()
    feed = make_feed(site, ["/a", "/b"])
    check(feed)
    with core.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM seeded").fetchone()[0] == 0
    up["b"] = True
    check(feed, 2)
    assert site.titles() == []


def test_stale_source_is_reseeded_not_reposted(data_dir, site):
    items = [{"title": "Undated", "guid": "u1"}]
    site.feed("/f", lambda: items)
    site.receiver()
    feed = make_feed(site, ["/f"])
    check(feed)
    # Paused for 40 days: memory pruned, source not read since.
    with core.db() as conn:
        conn.execute("UPDATE source_state SET last_parsed = ?", (NOW - 40 * 86400,))
        conn.execute("UPDATE sent SET last_seen = ?", (NOW - 40 * 86400,))
        core.prune_state(conn, core.load_config())
        assert conn.execute("SELECT COUNT(*) FROM sent").fetchone()[0] == 0
    check(feed, 2)
    assert site.titles() == []


def test_full_download_replaces_conditional_get_regularly(data_dir, site):
    seen = []
    from conftest import rss

    def handler(h):
        seen.append(h.headers.get("If-None-Match"))
        if h.headers.get("If-None-Match") == '"v1"':
            return 304, {"ETag": '"v1"'}, b""
        return 200, {"ETag": '"v1"'}, rss([{"title": "A", "guid": "a"}])
    site.routes["/f"] = handler
    site.receiver()
    feed = make_feed(site, ["/f"])
    check(feed, 2)
    assert seen == [None, '"v1"']
    with core.db() as conn:
        conn.execute("UPDATE source_state SET last_parsed = ?", (NOW - sched.FULL_FETCH_EVERY - 60,))
    check(feed)
    assert seen[-1] is None


def test_too_many_new_articles_are_capped(data_dir, site, monkeypatch):
    monkeypatch.setattr(sched, "MAX_POSTS_PER_CHECK", 3)
    monkeypatch.setattr(sched, "POST_DELAY", 0)
    items = []
    site.feed("/f", lambda: items)
    site.receiver()
    feed = make_feed(site, ["/f"])
    check(feed)
    items.extend({"title": f"N{i}", "guid": f"n{i}", "date": NOW - 600 + i} for i in range(6))
    check(feed, 2)
    assert site.titles() == ["N3", "N4", "N5"]


def test_drip_feed_is_cut_off(data_dir, site):
    def drip(h):
        h.send_response(200)
        h.send_header("Content-Type", "application/rss+xml")
        h.send_header("Content-Length", "100000")
        h.end_headers()
        try:
            for _ in range(40):
                h.wfile.write(b" ")
                h.wfile.flush()
                time.sleep(0.25)
        except OSError:
            pass
        return 200, {}, None
    site.routes["/drip"] = drip
    start = time.monotonic()
    res = sched.fetch_feed(site.url("/drip"), timeout=2)
    assert time.monotonic() - start < 5
    assert "Timed out" in res["error"]


@pytest.mark.parametrize("code,flagged", [(301, True), (308, True), (302, False), (307, False)])
def test_only_permanent_redirects_are_flagged(data_dir, site, code, flagged):
    from conftest import rss
    site.routes["/old"] = lambda h: (code, {"Location": "/new"}, b"")
    site.routes["/new"] = lambda h: (200, {}, rss([{"title": "A", "guid": "a"}]))
    res = sched.fetch_feed(site.url("/old"))
    assert res["error"] is None
    assert (res["redirected_to"] == site.url("/new")) is flagged


def test_default_user_agent_tracks_the_date():
    old = sched.default_user_agent(sched._CHROME_BASE)
    later = sched.default_user_agent(sched._CHROME_BASE + 365 * 86400)
    assert "Chrome/132." in old and "Chrome/145." in later


def test_only_one_scheduler_per_data_dir(data_dir):
    first = core.scheduler_lock()
    assert first is not None
    assert core.scheduler_lock() is None
    first.close()
    again = core.scheduler_lock()
    assert again is not None
    again.close()
