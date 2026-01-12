"""
Centralized exception handlers for FastAPI application.

v1.0.1: Observability - Standardized error responses with trace_id correlation.

All errors return a consistent JSON format:
{
    "error": "error_type",
    "detail": "Human-readable message",
    "trace_id": "uuid for correlation"
}
"""
import os
import traceback
from typing import Any, Dict, Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import ValidationError

from findmy.api.logging_config import get_logger, get_trace_id

logger = get_logger(__name__)

# Environment check for debug mode
IS_DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")


class ErrorResponse:
    """Standard error response builder."""
    
    @staticmethod
    def create(
        error_type: str,
        detail: str,
        status_code: int,
        trace_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> JSONResponse:
        """
        Create a standardized error response.
        
        Args:
            error_type: Type/category of error (e.g., "validation_error", "not_found")
            detail: Human-readable error message
            status_code: HTTP status code
            trace_id: Request trace ID for correlation
            extra: Additional fields to include in response (debug mode only)
        
        Returns:
            JSONResponse with standardized error format
        """
        content = {
            "error": error_type,
            "detail": detail,
            "trace_id": trace_id or get_trace_id() or "unknown",
        }
        
        # Add extra fields in debug mode
        if IS_DEBUG and extra:
            content["debug"] = extra
        
        return JSONResponse(
            status_code=status_code,
            content=content,
        )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """
    Handler for HTTPException (4xx/5xx errors raised intentionally).
    
    These are expected errors that the application raises deliberately
    (e.g., 404 Not Found, 403 Forbidden, 400 Bad Request).
    """
    trace_id = get_trace_id()
    
    # Map status codes to error types
    error_type_map = {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        422: "validation_error",
        429: "rate_limited",
        500: "internal_error",
        502: "bad_gateway",
        503: "service_unavailable",
        504: "gateway_timeout",
    }
    error_type = error_type_map.get(exc.status_code, "http_error")
    
    # Log based on severity
    if exc.status_code >= 500:
        logger.error(
            f"HTTP {exc.status_code}: {exc.detail}",
            extra={
                "event": "http_exception",
                "status_code": exc.status_code,
                "error_type": error_type,
                "path": request.url.path,
            }
        )
    elif exc.status_code >= 400:
        logger.warning(
            f"HTTP {exc.status_code}: {exc.detail}",
            extra={
                "event": "http_exception",
                "status_code": exc.status_code,
                "error_type": error_type,
                "path": request.url.path,
            }
        )
    
    return ErrorResponse.create(
        error_type=error_type,
        detail=str(exc.detail),
        status_code=exc.status_code,
        trace_id=trace_id,
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """
    Handler for request validation errors (422 Unprocessable Entity).
    
    Provides detailed information about which fields failed validation.
    """
    trace_id = get_trace_id()
    
    # Extract validation errors
    errors = []
    for error in exc.errors():
        field_path = " → ".join(str(loc) for loc in error.get("loc", []))
        errors.append({
            "field": field_path,
            "message": error.get("msg", "Invalid value"),
            "type": error.get("type", "unknown"),
        })
    
    # Create human-readable summary
    if len(errors) == 1:
        detail = f"Validation error: {errors[0]['field']} - {errors[0]['message']}"
    else:
        detail = f"Validation errors in {len(errors)} fields"
    
    logger.warning(
        f"Validation error: {detail}",
        extra={
            "event": "validation_error",
            "path": request.url.path,
            "errors": errors,
        }
    )
    
    return ErrorResponse.create(
        error_type="validation_error",
        detail=detail,
        status_code=422,
        trace_id=trace_id,
        extra={"validation_errors": errors} if IS_DEBUG else None,
    )


async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Handler for unhandled exceptions (500 Internal Server Error).
    
    CRITICAL: Never expose stack traces or internal details to users in production.
    Always log the full traceback for debugging.
    """
    trace_id = get_trace_id()
    
    # Log full exception with traceback
    logger.error(
        f"Unhandled exception: {type(exc).__name__}: {str(exc)}",
        exc_info=True,  # Includes full traceback
        extra={
            "event": "unhandled_exception",
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "path": request.url.path,
            "method": request.method,
        }
    )
    
    # Safe message for users (never expose internals)
    user_message = "An internal error occurred. Please try again later."
    
    # In debug mode, provide more details
    extra = None
    if IS_DEBUG:
        extra = {
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "traceback": traceback.format_exc(),
        }
        user_message = f"{type(exc).__name__}: {str(exc)}"
    
    return ErrorResponse.create(
        error_type="internal_error",
        detail=user_message,
        status_code=500,
        trace_id=trace_id,
        extra=extra,
    )


async def starlette_http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """
    Handler for Starlette HTTPExceptions (from middleware, etc.).
    
    Converts Starlette exceptions to our standard format.
    """
    # Delegate to our HTTP exception handler
    fastapi_exc = HTTPException(status_code=exc.status_code, detail=exc.detail)
    return await http_exception_handler(request, fastapi_exc)


def register_exception_handlers(app: FastAPI) -> None:
    """
    Register all exception handlers with the FastAPI application.
    
    Args:
        app: FastAPI application instance
    
    Usage:
        from findmy.api.exception_handlers import register_exception_handlers
        register_exception_handlers(app)
    """
    # HTTPException (intentional errors)
    app.add_exception_handler(HTTPException, http_exception_handler)
    
    # Starlette HTTPException (from middleware)
    app.add_exception_handler(StarletteHTTPException, starlette_http_exception_handler)
    
    # Request validation errors
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    
    # Pydantic validation errors
    app.add_exception_handler(ValidationError, validation_exception_handler)
    
    # General exception handler (catch-all for unhandled errors)
    app.add_exception_handler(Exception, general_exception_handler)
    
    logger.info("Exception handlers registered")


# Prometheus metrics for errors (optional integration)
def track_error_metric(error_type: str, status_code: int, path: str) -> None:
    """
    Track error in Prometheus metrics.
    
    Args:
        error_type: Type of error
        status_code: HTTP status code
        path: Request path
    
    This is a placeholder - integrate with your metrics module.
    """
    try:
        from findmy.api.metrics import trades_total  # Import your error counter
        # errors_total.labels(error_type=error_type, status_code=status_code).inc()
        pass
    except ImportError:
        pass
