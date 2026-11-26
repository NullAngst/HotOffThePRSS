# Shared fixtures: a throwaway data directory and a local HTTP server that
# plays both the feed site and the destination receiving posts.

import os
import sys
import json
import time
import threading
from email.utils import formatdate
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from xml.sax.saxutils import escape

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import prss_core as core  # noqa: E402


@pytest.fixture
def data_dir(tmp_path):
    core.set_data_dir(str(tmp_path))
    core.initialize()
    yield tmp_path


def rss(items, title="Test feed"):
    """items: dicts with title, link, and optional guid and date (epoch)."""
    out = ['<?xml version="1.0"?><rss version="2.0"><channel>',
           f"<title>{escape(title)}</title><link>http://example.test/</link><description>t</description>"]
    for it in items:
        out.append("<item>")
        out.append(f"<title>{escape(it['title'])}</title>")
        if it.get("link"):
            out.append(f"<link>{escape(it['link'])}</link>")
        if it.get("guid"):
            out.append(f'<guid isPermaLink="false">{escape(it["guid"])}</guid>')
        if it.get("date"):
            out.append(f"<pubDate>{formatdate(it['date'], usegmt=True)}</pubDate>")
        out.append("</item>")
    out.append("</channel></rss>")
    return "".join(out).encode("utf-8")


class Site:
    """Routes map a path to a function(handler) -> (status, headers, body)."""

    def __init__(self):
        self.routes = {}
        self.posts = []
        self.lock = threading.Lock()
        site = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _serve(self):
                path = self.path.split("?", 1)[0]
                fn = site.routes.get(path)
                if fn is None:
                    status, headers, body = 404, {}, b"not found"
                else:
                    status, headers, body = fn(self)
                if body is None:          # handler wrote the response itself
                    return
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._serve()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.body = self.rfile.read(n)
                self._serve()

            do_PUT = do_POST

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass                      # clients that time out on purpose

        self.server = Server(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def feed(self, path, items_fn):
        """Serve an RSS feed whose items come from items_fn() on each request."""
        def handler(_h):
            return 200, {"Content-Type": "application/rss+xml"}, rss(items_fn())
        self.routes[path] = handler

    def receiver(self, path="/hook", status=204, delay=0.0):
        """A destination that records each post."""
        def handler(h):
            if delay:
                time.sleep(delay)
            with self.lock:
                self.posts.append(json.loads(h.body or b"{}"))
            return status, {}, b""
        self.routes[path] = handler

    def titles(self):
        with self.lock:
            return [p.get("title") for p in self.posts]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def site():
    s = Site()
    yield s
    s.close()


def make_feed(site, urls, hook_path="/hook", **extra):
    feed = {"id": "feed-1", "name": "Test", "url": site.url(urls[0]),
            "extra_urls": [site.url(u) for u in urls[1:]],
            "update_interval": 300, "active": True,
            "webhooks": [{"type": "webhook", "label": "hook", "url": site.url(hook_path)}]}
    feed.update(extra)
    core.save_config({"FEEDS": [feed]})
    return core.find_feed(core.load_config(), "feed-1")
