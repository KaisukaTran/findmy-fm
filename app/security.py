"""
Security helpers for FINDMY-FM (lean rebuild).

- require_api_key : FastAPI dependency guarding write/mutation endpoints.
- SecurityHeadersMiddleware : adds standard hardening headers + a tight CSP.
- install_security : wire CORS, security headers, and rate limiting onto the app.
- outbound_url_problem : SSRF guard for operator-editable URLs the server itself requests.

Auth is intentionally simple for a local paper-trading demo: a single shared API
key in the `X-API-Key` header, enforced only when settings.require_auth is true.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import re
import socket
import urllib.parse

from fastapi import FastAPI, Header, HTTPException, status
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import settings

_log = logging.getLogger("app.security")

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

# Every directive the dashboard depends on is explicit rather than inherited from default-src, so
# a later edit to default-src can never silently widen scripts/connections/framing. No 'unsafe-*'
# ever (tests/app/test_api.py) — Alpine is the CSP build, all JS is same-origin. connect-src
# 'self' also covers the same-origin /ws socket (CSP3). frame-ancestors duplicates
# X-Frame-Options: DENY for browsers that honour only one of them.
_CSP = (
    "default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "style-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)
_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": _CSP,
}

# Shipped placeholder keys that must never guard a real deployment.
_DEFAULT_KEYS = {"", "dev-key", "change_this_api_key"}

# Methods that change state — the CSRF Origin check applies to these only.
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Reject the request unless a valid API key is presented (when auth is enabled)."""
    if not settings.require_auth:
        return
    expected = settings.api_key.get_secret_value()
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


def validate_auth_config() -> None:
    """Fail closed at boot on an insecure auth posture.

    Raises RuntimeError (which aborts uvicorn startup) when:
      - auth is enabled but api_key is still a shipped placeholder, or
      - live trading is armed without auth enforcement.
    A safe paper posture (auth on + a real key) passes silently.
    """
    key = settings.api_key.get_secret_value()
    weak_key = key in _DEFAULT_KEYS
    if settings.live_trading and not settings.require_auth:
        raise RuntimeError(
            "Refusing to boot: live_trading is ON but require_auth is OFF — real-money "
            "endpoints would be unauthenticated. Set REQUIRE_AUTH=true and a strong API_KEY."
        )
    if settings.require_auth and weak_key:
        raise RuntimeError(
            "Refusing to boot: require_auth is ON but API_KEY is still a shipped default "
            f"({key!r}). Set a strong, unique API_KEY in the environment."
        )
    if settings.live_trading and weak_key:
        raise RuntimeError(
            "Refusing to boot: live_trading is ON with a default API_KEY. Set a strong API_KEY."
        )


def enforce_auth_posture(db: Session) -> str | None:
    """Force a persisted live_trading flag OFF when it no longer fits the auth posture.

    `validate_auth_config` only sees env values at import time; the persisted flag is restored
    later by `runtime.sync_from_db`, so a switch flipped on while auth was off survives restarts
    unchallenged. Called right after that restore. Fails CLOSED without raising (a raise inside
    lifespan makes uvicorn exit and the watchdog restart-loop): returns the reason when it
    forced the flag off, None when the posture is fine. Never includes the key in the reason.
    """
    if not settings.live_trading:
        return None
    weak_key = settings.api_key.get_secret_value() in _DEFAULT_KEYS
    if settings.require_auth and not weak_key:
        return None
    reason = (
        "live_trading was ON with require_auth OFF - real-money endpoints would be "
        "unauthenticated; forced OFF at boot"
        if not settings.require_auth
        else "live_trading was ON with a default API_KEY; forced OFF at boot"
    )
    # Memory first: execution.live_enabled() reads this flag, so it must be off even if the DB
    # writes below fail.
    settings.live_trading = False
    try:
        from app import audit, runtime

        runtime.set_live_trading(db, False)
        audit.log(db, "security", "live_forced_off", reason=reason)
        db.commit()
    except Exception:
        _log.exception("could not persist the live_trading force-off (the in-memory flag is off)")
    _log.warning(reason)
    return reason


def _allowed_origins() -> set[str]:
    """Origins permitted to make cross-origin mutating requests (same-origin dashboard)."""
    return set(settings.cors_origins)


class CSRFOriginMiddleware(BaseHTTPMiddleware):
    """Block cross-site state-changing requests by checking the browser-set Origin header.

    Browsers attach `Origin` to every mutating fetch — including a malicious page's
    `fetch(..., {mode:'no-cors'})` CSRF attempt, which carries the ATTACKER's origin. A
    same-origin dashboard request carries the app's own origin (allowed). Non-browser
    clients (curl, server-to-server) send no Origin and are not a CSRF vector, so they pass.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method in _MUTATING_METHODS:
            origin = request.headers.get("origin")
            if origin is not None:
                allowed = _allowed_origins()
                host = request.headers.get("host")
                if host:
                    allowed.add(f"http://{host}")
                    allowed.add(f"https://{host}")
                if origin not in allowed:
                    _log.warning("CSRF: blocked %s %s from origin %s",
                                 request.method, request.url.path, origin)
                    return JSONResponse(status_code=403,
                                        content={"detail": "Cross-origin request blocked"})
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        for key, value in _HEADERS.items():
            response.headers.setdefault(key, value)
        return response


def install_security(app: FastAPI) -> None:
    """Attach CORS, CSRF, security headers, and the rate limiter to the app."""
    validate_auth_config()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    app.add_middleware(CSRFOriginMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)


def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
    from starlette.responses import JSONResponse

    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})


# --- outbound URL guard (SSRF) ----------------------------------------------------------------
# A URL the server fetches on its own (heartbeat_url) but that is editable at runtime — with
# auth off on paper, by anyone who can reach the dashboard. Only public https endpoints pass.

_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
# A public DNS name ends in an alphabetic TLD (or punycode xn--). This also refuses the numeric
# host forms that getaddrinfo quietly turns into an address ("2130706433", "0x7f.1" = 127.0.0.1).
_TLD_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")


def _resolve(host: str, port: int) -> list[str]:
    """Every address *host* resolves to (a seam so tests never touch real DNS)."""
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _ip_is_public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])  # drop an IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address):
        # An IPv6 form that tunnels an IPv4 address is judged by the IPv4 inside it.
        inner = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if inner is not None:
            ip = inner
    return ip.is_global and not ip.is_multicast


def outbound_url_problem(url: str, *, resolve: bool) -> str | None:
    """Why *url* must not be requested by the server, or None when it may be.

    Always: https only, a host is required, and an IP-literal / local-looking host must be
    public. With ``resolve=True`` (at request time) the name is also resolved and EVERY address
    must be public — a name that points into the LAN is refused. Residual risk, accepted: DNS
    can change between this check and the connect (rebinding); the caller makes one blind GET
    with a short timeout and never follows a redirect, which bounds what that buys an attacker.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port or 443
    except ValueError:
        return "URL không hợp lệ"
    if parts.scheme.lower() != "https":
        return "chỉ nhận https://"
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return "thiếu tên máy chủ"
    try:
        ipaddress.ip_address(host)
        is_literal = True
    except ValueError:
        is_literal = False
    if is_literal:
        if not _ip_is_public(host):
            return f"{host} là địa chỉ nội bộ/không công khai"
    elif host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return f"{host} là máy nội bộ"
    elif not _TLD_RE.match(host.rsplit(".", 1)[-1]):
        return f"{host} không phải tên miền công khai"
    if resolve and not is_literal:
        try:
            addrs = _resolve(host, port)
        except OSError:
            return f"không phân giải được {host}"
        if not addrs:
            return f"không phân giải được {host}"
        bad = [a for a in addrs if not _ip_is_public(a)]
        if bad:
            return f"{host} trỏ tới địa chỉ nội bộ {bad[0]}"
    return None
