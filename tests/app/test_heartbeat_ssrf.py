"""heartbeat_url is runtime-editable (POST /api/kss-settings, auth off on paper) and the scheduler
GETs it every cycle — without a guard that is a blind SSRF into the LAN / cloud metadata. The
guard: https only, public addresses only, checked at save time (syntax) AND at ping time (DNS),
and the ping never follows a redirect (a 302 → 127.0.0.1 would bypass both checks)."""

from __future__ import annotations

import http.server
import threading
import urllib.error

import pytest

from app import runtime, scheduler, security
from app.config import settings

# --- save-time check (no DNS) ---------------------------------------------------------------

_REFUSED = [
    "http://hc-ping.com/abc",               # plain http
    "ftp://hc-ping.com/abc",
    "file:///etc/passwd",
    "https:///no-host",
    "https://127.0.0.1/x",
    "https://10.0.0.5/x",
    "https://172.16.3.4/x",
    "https://192.168.1.1/x",
    "https://169.254.169.254/latest/meta-data",  # cloud metadata
    "https://100.64.0.1/x",                 # CGNAT
    "https://0.0.0.0/x",
    "https://[::1]/x",
    "https://[::ffff:127.0.0.1]/x",         # IPv4-mapped loopback
    "https://[fe80::1]/x",
    "https://[fd00::1]/x",
    "https://localhost/x",
    "https://LOCALHOST:8000/x",
    "https://printer.local/x",
    "https://app.localhost/x",
    "https://db.internal/x",
    "https://2130706433/x",                 # decimal 127.0.0.1 — getaddrinfo accepts it
    "https://0x7f.1/x",                     # hex/short form
]


@pytest.mark.parametrize("url", _REFUSED)
def test_save_time_check_refuses(url):
    assert security.outbound_url_problem(url, resolve=False) is not None


@pytest.mark.parametrize("url", [
    "https://hc-ping.com/abc123",
    "https://hc-ping.com:443/abc123",
    "https://8.8.8.8/ping",
    "https://monitor.example.org/ping/xyz?status=ok",
])
def test_save_time_check_accepts_public_https(url):
    assert security.outbound_url_problem(url, resolve=False) is None


# --- ping-time check (DNS) ------------------------------------------------------------------

@pytest.mark.parametrize("addrs", [["10.0.0.1"], ["93.184.216.34", "127.0.0.1"], ["::1"], []])
def test_ping_time_check_refuses_a_name_resolving_to_a_private_address(monkeypatch, addrs):
    monkeypatch.setattr(security, "_resolve", lambda host, port: addrs)
    assert security.outbound_url_problem("https://evil.example.com/x", resolve=True) is not None


def test_ping_time_check_refuses_when_resolution_fails(monkeypatch):
    def _fail(host, port):
        raise OSError("NXDOMAIN")

    monkeypatch.setattr(security, "_resolve", _fail)
    assert security.outbound_url_problem("https://nope.example.com/x", resolve=True) is not None


def test_ping_time_check_accepts_a_public_resolution(monkeypatch):
    monkeypatch.setattr(security, "_resolve", lambda host, port: ["93.184.216.34"])
    assert security.outbound_url_problem("https://hc-ping.com/x", resolve=True) is None


# --- the ping itself ------------------------------------------------------------------------

class _SpyResponse:
    def __init__(self):
        self.read_calls = 0
        self.closed = False

    def read(self, n=-1):
        self.read_calls += 1
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


@pytest.mark.parametrize("url,addrs", [
    ("http://hc-ping.com/x", ["93.184.216.34"]),
    ("https://hc-ping.com/x", ["192.168.0.10"]),
    ("https://127.0.0.1/x", ["127.0.0.1"]),
])
def test_ping_never_opens_a_refused_url(monkeypatch, url, addrs):
    monkeypatch.setattr(security, "_resolve", lambda host, port: addrs)
    calls = []
    monkeypatch.setattr(scheduler, "_open_heartbeat", lambda u, timeout=5: calls.append(u))
    scheduler._fire_heartbeat_ping(url)
    assert calls == []


def test_ping_opens_and_closes_a_public_https_url(monkeypatch):
    monkeypatch.setattr(security, "_resolve", lambda host, port: ["93.184.216.34"])
    resp = _SpyResponse()
    monkeypatch.setattr(scheduler, "_open_heartbeat", lambda u, timeout=5: resp)
    scheduler._fire_heartbeat_ping("https://hc-ping.com/test-id")
    assert resp.read_calls == 1
    assert resp.closed is True


def test_heartbeat_opener_does_not_follow_redirects():
    """A public monitor that answers 302 → http://127.0.0.1/... must not drag the ping inside."""
    hits: list[str] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib hook name
            hits.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/internal")
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            scheduler._open_heartbeat(f"http://127.0.0.1:{srv.server_port}/start", timeout=5)
        assert ei.value.code == 302
        assert hits == ["/start"], "the redirect target must never be requested"
    finally:
        srv.shutdown()
        srv.server_close()


# --- persistence paths ----------------------------------------------------------------------

def test_runtime_does_not_persist_a_refused_url(db, monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_url", "https://hc-ping.com/ok")
    runtime.set_kss_settings(db, {"heartbeat_url": "http://192.168.1.10/ping"})
    assert settings.heartbeat_url == "https://hc-ping.com/ok"


def test_runtime_still_clears_the_url(db, monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_url", "https://hc-ping.com/ok")
    runtime.set_kss_settings(db, {"heartbeat_url": ""})
    assert settings.heartbeat_url == ""


@pytest.mark.parametrize("url", ["http://hc-ping.com/abc", "https://169.254.169.254/latest",
                                 "https://localhost:8000/api/kill-switch"])
def test_endpoint_rejects_a_refused_url_loudly(url):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        before = c.get("/api/kss-settings").json()["heartbeat_url"]
        r = c.post("/api/kss-settings", json={"heartbeat_url": url})
        assert r.status_code == 400
        assert "heartbeat_url" in r.json()["detail"]
        assert c.get("/api/kss-settings").json()["heartbeat_url"] == before


def test_endpoint_accepts_public_https_and_empty():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        assert c.post("/api/kss-settings",
                      json={"heartbeat_url": "https://hc-ping.com/abc"}).status_code == 200
        assert c.post("/api/kss-settings", json={"heartbeat_url": ""}).status_code == 200
        assert c.get("/api/kss-settings").json()["heartbeat_url"] == ""
