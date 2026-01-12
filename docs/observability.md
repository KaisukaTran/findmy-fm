# Observability Guide

> v1.0.1 – Centralized error handling, structured logging, and request tracing.

## Overview

FINDMY FM includes a comprehensive observability stack for:

- **Centralized Exception Handling** – All errors return standardized JSON responses
- **Structured Logging** – JSON or console format with trace correlation
- **Request Tracing** – Unique trace_id for every request
- **Health Monitoring** – Enhanced health endpoint with component checks

---

## Quick Start

### Environment Variables

```bash
# Logging configuration
LOG_LEVEL=INFO          # DEBUG, INFO, WARNING, ERROR
LOG_FORMAT=console      # console (colored) or json (structured)
DEBUG=false             # Set true to expose error details
```

### View Logs

```bash
# Console format (development)
LOG_FORMAT=console uvicorn src.findmy.api.main:app

# JSON format (production, for log aggregators)
LOG_FORMAT=json LOG_LEVEL=INFO uvicorn src.findmy.api.main:app
```

---

## Error Response Format

All errors return a standardized JSON format:

```json
{
    "error": "not_found",
    "detail": "Resource not found",
    "trace_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

### Error Types

| HTTP Code | Error Type | Description |
|-----------|------------|-------------|
| 400 | `bad_request` | Invalid request parameters |
| 401 | `unauthorized` | Authentication required |
| 403 | `forbidden` | Insufficient permissions |
| 404 | `not_found` | Resource not found |
| 422 | `validation_error` | Request validation failed |
| 429 | `rate_limited` | Too many requests |
| 500 | `internal_error` | Server error (safe message) |
| 503 | `service_unavailable` | Service temporarily unavailable |

### Using trace_id

Every request generates a unique `trace_id`:

1. **In Response Headers**: `X-Trace-ID: <uuid>`
2. **In Error Response**: `{"trace_id": "<uuid>"}`
3. **In Log Entries**: `{"trace_id": "<uuid>"}`

Use this to correlate user-reported errors with server logs:

```bash
# Find all logs for a specific request
grep "550e8400-e29b-41d4" application.log
```

---

## Structured Logging

### Log Entry Format (JSON)

```json
{
    "timestamp": "2026-01-12T10:30:45.123456Z",
    "level": "ERROR",
    "logger": "findmy.api.main",
    "message": "Unhandled exception: ValueError: Invalid price",
    "module": "main",
    "function": "process_order",
    "line": 245,
    "trace_id": "550e8400-e29b-41d4-a716-446655440000",
    "extra": {
        "event": "unhandled_exception",
        "exception_type": "ValueError",
        "path": "/api/orders"
    },
    "exception": {
        "type": "ValueError",
        "message": "Invalid price",
        "traceback": "Traceback (most recent call last):\n..."
    }
}
```

### Log Levels

| Level | Usage |
|-------|-------|
| DEBUG | Detailed debugging information |
| INFO | Request/response logging, startup events |
| WARNING | 4xx errors, deprecated usage |
| ERROR | 5xx errors, unhandled exceptions |
| CRITICAL | System failures |

### Using the Logger

```python
from findmy.api.logging_config import get_logger

logger = get_logger(__name__)

# Basic logging
logger.info("Order processed", extra={"order_id": 123})

# With exception
try:
    process_order()
except Exception as e:
    logger.error("Order failed", exc_info=True, extra={"order_id": 123})
```

---

## Request/Response Logging

Every HTTP request is automatically logged:

### Request Started

```
→ POST /api/orders
```

### Request Completed

```
← POST /api/orders 201 (45.23ms)
```

### Skipped Paths

These paths are not logged to reduce noise:
- `/health`
- `/metrics`
- `/static/*`
- `/favicon.ico`

---

## Health Endpoint

### Enhanced Health Check

```bash
curl http://localhost:8000/health
```

Response:

```json
{
    "status": "ok",
    "service": "FINDMY FM API",
    "version": "1.0.1",
    "timestamp": "2026-01-12T10:30:45.123456Z",
    "components": {
        "database": {
            "status": "ok",
            "latency_ms": 1.23
        },
        "cache": {
            "status": "ok"
        },
        "binance": {
            "status": "ok",
            "latency_ms": 45.67
        }
    }
}
```

### Status Values

| Status | Meaning |
|--------|---------|
| `ok` | All systems operational |
| `degraded` | Some non-critical components unavailable |
| `unhealthy` | Critical components failed |

---

## Integration with Log Aggregators

### ELK Stack (Elasticsearch, Logstash, Kibana)

```yaml
# docker-compose.yml
services:
  app:
    environment:
      - LOG_FORMAT=json
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

### CloudWatch

```python
# Enable JSON logging for CloudWatch
LOG_FORMAT=json
```

### Datadog

```yaml
# datadog.yaml
logs:
  - type: file
    path: /app/logs/*.json
    service: findmy-fm
    source: python
```

---

## Debugging Production Errors

### 1. User Reports Error

User sees:
```json
{
    "error": "internal_error",
    "detail": "An internal error occurred. Please try again later.",
    "trace_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

### 2. Find Related Logs

```bash
# Search by trace_id
grep "550e8400-e29b-41d4" /var/log/app.log

# With jq for JSON logs
cat /var/log/app.log | jq 'select(.trace_id == "550e8400-e29b-41d4")'
```

### 3. Full Error Context

```json
{
    "timestamp": "2026-01-12T10:30:45.123456Z",
    "level": "ERROR",
    "trace_id": "550e8400-e29b-41d4-a716-446655440000",
    "message": "Unhandled exception: NameError: name 'undefined' is not defined",
    "exception": {
        "type": "NameError",
        "message": "name 'undefined' is not defined",
        "traceback": "Traceback (most recent call last):\n  File \"/app/main.py\", line 123, in endpoint\n    return undefined\nNameError: name 'undefined' is not defined"
    },
    "extra": {
        "path": "/api/orders",
        "method": "POST"
    }
}
```

---

## Security Considerations

### Production Mode (DEBUG=false)

- Stack traces are **never** exposed to users
- Internal error messages are replaced with generic text
- Sensitive headers (Authorization, Cookie) are redacted in logs

### Debug Mode (DEBUG=true)

- Stack traces included in error responses
- Full exception details exposed
- **Only use in development!**

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        FastAPI App                           │
├──────────────────────────────────────────────────────────────┤
│  RequestLoggingMiddleware                                    │
│  ├─ Generate trace_id                                        │
│  ├─ Log request start                                        │
│  ├─ Add X-Trace-ID header                                    │
│  └─ Log request completion + duration                        │
├──────────────────────────────────────────────────────────────┤
│  Exception Handlers                                          │
│  ├─ HTTPException → Standardized 4xx/5xx response           │
│  ├─ RequestValidationError → Detailed 422 response          │
│  └─ Exception → Safe 500 response + full log                │
├──────────────────────────────────────────────────────────────┤
│  Structured Logger (JSON/Console)                            │
│  ├─ Automatic trace_id injection                             │
│  ├─ Exception traceback capture                              │
│  └─ Extra context fields                                     │
└──────────────────────────────────────────────────────────────┘
```

---

## Module Reference

### logging_config.py

```python
# Configure logging at startup
configure_logging(level="INFO", json_output=True)

# Get a logger
logger = get_logger(__name__)

# Trace ID context
set_trace_id("custom-id")
trace_id = get_trace_id()
clear_trace_id()
```

### middleware.py

```python
# Add to FastAPI app
from findmy.api.middleware import RequestLoggingMiddleware
app.add_middleware(RequestLoggingMiddleware)
```

### exception_handlers.py

```python
# Register handlers
from findmy.api.exception_handlers import register_exception_handlers
register_exception_handlers(app)

# Create custom error response
from findmy.api.exception_handlers import ErrorResponse
return ErrorResponse.create(
    error_type="custom_error",
    detail="Something went wrong",
    status_code=400
)
```

---

## Testing

Run observability tests:

```bash
pytest tests/test_observability.py -v
```

Test coverage includes:
- Exception handler responses
- Logging format validation
- Trace ID generation
- Middleware functionality
- Health endpoint checks

---

## Changelog

### v1.0.1

- Added centralized exception handlers
- Added structured JSON logging
- Added request/response logging middleware
- Added trace_id correlation
- Enhanced health endpoint with component checks
- Added comprehensive test suite
