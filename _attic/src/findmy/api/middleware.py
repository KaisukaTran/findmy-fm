"""
Request/Response logging middleware with trace_id correlation.

v1.0.1: Observability - Comprehensive request tracking and timing.
"""
import time
import uuid
from typing import Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from findmy.api.logging_config import get_logger, set_trace_id, get_trace_id, clear_trace_id

logger = get_logger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware for logging all HTTP requests and responses.
    
    Features:
    - Generates unique trace_id for each request
    - Logs request details (method, URL, headers)
    - Logs response status and duration
    - Adds trace_id to response headers
    """
    
    # Paths to skip logging (health checks, metrics, static files)
    SKIP_PATHS = {"/health", "/metrics", "/static", "/favicon.ico"}
    
    # Headers to redact in logs
    REDACT_HEADERS = {"authorization", "cookie", "x-api-key", "x-auth-token"}
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and log details."""
        # Skip logging for certain paths
        if any(request.url.path.startswith(skip) for skip in self.SKIP_PATHS):
            return await call_next(request)
        
        # Generate unique trace_id
        trace_id = str(uuid.uuid4())
        set_trace_id(trace_id)
        
        # Record start time
        start_time = time.perf_counter()
        
        # Extract request info
        request_info = self._extract_request_info(request)
        
        # Log incoming request
        logger.info(
            f"→ {request.method} {request.url.path}",
            extra={
                "event": "request_started",
                "request": request_info,
            }
        )
        
        # Process request
        response = None
        error = None
        try:
            response = await call_next(request)
        except Exception as e:
            error = e
            raise
        finally:
            # Calculate duration
            duration_ms = (time.perf_counter() - start_time) * 1000
            
            # Determine status code
            status_code = response.status_code if response else 500
            
            # Log response
            log_level = "info" if status_code < 400 else "warning" if status_code < 500 else "error"
            log_func = getattr(logger, log_level)
            
            log_func(
                f"← {request.method} {request.url.path} {status_code} ({duration_ms:.2f}ms)",
                extra={
                    "event": "request_completed",
                    "status_code": status_code,
                    "duration_ms": round(duration_ms, 2),
                    "path": request.url.path,
                    "method": request.method,
                }
            )
            
            # Clear trace_id
            clear_trace_id()
        
        # Add trace_id to response headers
        if response:
            response.headers["X-Trace-ID"] = trace_id
        
        return response
    
    def _extract_request_info(self, request: Request) -> dict:
        """Extract relevant request information for logging."""
        # Redact sensitive headers
        headers = {}
        for key, value in request.headers.items():
            if key.lower() in self.REDACT_HEADERS:
                headers[key] = "[REDACTED]"
            else:
                headers[key] = value
        
        return {
            "method": request.method,
            "path": request.url.path,
            "query": str(request.query_params) if request.query_params else None,
            "client_ip": self._get_client_ip(request),
            "user_agent": request.headers.get("user-agent"),
            "content_type": request.headers.get("content-type"),
            "content_length": request.headers.get("content-length"),
        }
    
    def _get_client_ip(self, request: Request) -> str:
        """Get client IP, considering proxies."""
        # Check X-Forwarded-For header (from proxy/load balancer)
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            # Take first IP (original client)
            return forwarded_for.split(",")[0].strip()
        
        # Check X-Real-IP header
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip
        
        # Fall back to direct client
        if request.client:
            return request.client.host
        
        return "unknown"


class TraceIDMiddleware:
    """
    Minimal middleware that only adds trace_id without full logging.
    
    Use this when you want trace_id correlation but not request logging
    (e.g., in combination with another logging solution).
    """
    
    def __init__(self, app):
        self.app = app
    
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        # Generate and set trace_id
        trace_id = str(uuid.uuid4())
        set_trace_id(trace_id)
        
        # Custom send wrapper to add trace_id header
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-trace-id", trace_id.encode()))
                message["headers"] = headers
            await send(message)
        
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            clear_trace_id()
