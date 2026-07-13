"""
Security helpers for FINDMY-FM (lean rebuild).

- require_api_key : FastAPI dependency guarding write/mutation endpoints.
- SecurityHeadersMiddleware : adds standard hardening headers + a tight CSP.
- install_security : wire CORS, security headers, and rate limiting onto the app.

Auth is intentionally simple for a local paper-trading demo: a single shared API
key in the `X-API-Key` header, enforced only when settings.require_auth is true.
"""

from __future__ import annotations

import hmac
import logging
import threading
import time

from fastapi import FastAPI, Header, HTTPException, status
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import settings

_log = logging.getLogger("app.security")

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'"
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


# --- Brute-force lockout on repeated failed X-API-Key attempts ---------------
#
# In-process, per-IP tracker: a list of failure timestamps (monotonic clock) plus
# an optional lockout_until. Single uvicorn process + sync dependency, so a plain
# dict guarded by one lock is enough — no need for a shared cache/store.
_auth_failures: dict[str, list[float]] = {}
_auth_lockout_until: dict[str, float] = {}
_auth_throttle_lock = threading.Lock()


def _reset_auth_throttle() -> None:
    """Clear the brute-force tracker. Test-only helper for deterministic runs."""
    with _auth_throttle_lock:
        _auth_failures.clear()
        _auth_lockout_until.clear()


def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    """Reject the request unless a valid API key is presented (when auth is enabled).

    Also throttles brute-force guessing: an IP that racks up
    `settings.auth_max_failures` wrong/missing-key attempts within
    `settings.auth_lockout_window_sec` seconds is locked out (429 + Retry-After)
    for `settings.auth_lockout_sec` seconds. A correct key clears the IP's record.
    """
    if not settings.require_auth:
        return
    ip = get_remote_address(request)
    now = time.monotonic()

    with _auth_throttle_lock:
        lockout_until = _auth_lockout_until.get(ip, 0.0)
        if now < lockout_until:
            retry_after = int(lockout_until - now) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed auth attempts",
                headers={"Retry-After": str(retry_after)},
            )

    expected = settings.api_key.get_secret_value()
    if x_api_key and hmac.compare_digest(x_api_key, expected):
        with _auth_throttle_lock:
            _auth_failures.pop(ip, None)
            _auth_lockout_until.pop(ip, None)
        return

    window = settings.auth_lockout_window_sec
    with _auth_throttle_lock:
        attempts = [t for t in _auth_failures.get(ip, []) if now - t < window]
        attempts.append(now)
        _auth_failures[ip] = attempts
        just_tripped = len(attempts) > settings.auth_max_failures
        if just_tripped:
            _auth_lockout_until[ip] = now + settings.auth_lockout_sec

    if just_tripped:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed auth attempts",
            headers={"Retry-After": str(settings.auth_lockout_sec)},
        )
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
