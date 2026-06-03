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

from fastapi import FastAPI, Header, HTTPException, status
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import settings

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

_CSP = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'"
_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": _CSP,
}


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


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        for key, value in _HEADERS.items():
            response.headers.setdefault(key, value)
        return response


def install_security(app: FastAPI) -> None:
    """Attach CORS, security headers, and the rate limiter to the app."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)


def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
    from starlette.responses import JSONResponse

    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})
