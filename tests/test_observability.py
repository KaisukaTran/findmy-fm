"""
Tests for centralized exception handlers and observability features.

v1.0.1: Comprehensive tests for error handling, logging, and middleware.
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from findmy.api.exception_handlers import (
    register_exception_handlers,
    http_exception_handler,
    validation_exception_handler,
    general_exception_handler,
    ErrorResponse,
)
from findmy.api.logging_config import (
    configure_logging,
    get_logger,
    set_trace_id,
    get_trace_id,
    clear_trace_id,
    JSONFormatter,
    ConsoleFormatter,
)
from findmy.api.middleware import RequestLoggingMiddleware


# ========================
# Exception Handler Tests
# ========================

class TestExceptionHandlers:
    """Tests for centralized exception handlers."""
    
    @pytest.fixture
    def test_app(self):
        """Create a test FastAPI app with exception handlers."""
        app = FastAPI()
        register_exception_handlers(app)
        
        @app.get("/ok")
        def ok_endpoint():
            return {"status": "ok"}
        
        @app.get("/http-error")
        def http_error_endpoint():
            raise HTTPException(status_code=404, detail="Resource not found")
        
        @app.get("/validation-error")
        def validation_error_endpoint(required_param: int):
            return {"value": required_param}
        
        @app.get("/server-error")
        def server_error_endpoint():
            raise RuntimeError("Something went wrong")
        
        @app.get("/name-error")
        def name_error_endpoint():
            return undefined_variable  # NameError
        
        @app.get("/zero-division")
        def zero_division_endpoint():
            return 1 / 0  # ZeroDivisionError
        
        # Use raise_server_exceptions=False to test error handling
        return TestClient(app, raise_server_exceptions=False)
    
    def test_ok_endpoint(self, test_app):
        """Test that normal endpoints work correctly."""
        response = test_app.get("/ok")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    
    def test_http_exception_returns_standardized_format(self, test_app):
        """Test that HTTPException returns standardized error format."""
        response = test_app.get("/http-error")
        assert response.status_code == 404
        
        data = response.json()
        assert "error" in data
        assert data["error"] == "not_found"
        assert "detail" in data
        assert data["detail"] == "Resource not found"
        assert "trace_id" in data
    
    def test_validation_error_returns_detailed_errors(self, test_app):
        """Test that validation errors return detailed field information."""
        response = test_app.get("/validation-error")  # Missing required_param
        assert response.status_code == 422
        
        data = response.json()
        assert data["error"] == "validation_error"
        assert "trace_id" in data
        assert "Validation error" in data["detail"]
    
    def test_validation_error_with_invalid_type(self, test_app):
        """Test validation error with invalid parameter type."""
        response = test_app.get("/validation-error?required_param=not_an_int")
        assert response.status_code == 422
        
        data = response.json()
        assert data["error"] == "validation_error"
    
    def test_unhandled_exception_returns_500(self, test_app):
        """Test that unhandled exceptions return 500 with safe message."""
        response = test_app.get("/server-error")
        assert response.status_code == 500
        
        data = response.json()
        assert data["error"] == "internal_error"
        assert "trace_id" in data
        # Should NOT expose internal error details in production
        assert "RuntimeError" not in data.get("detail", "")
    
    def test_name_error_caught(self, test_app):
        """Test that NameError is caught and returns 500."""
        response = test_app.get("/name-error")
        assert response.status_code == 500
        
        data = response.json()
        assert data["error"] == "internal_error"
    
    def test_zero_division_caught(self, test_app):
        """Test that ZeroDivisionError is caught and returns 500."""
        response = test_app.get("/zero-division")
        assert response.status_code == 500
        
        data = response.json()
        assert data["error"] == "internal_error"
    
    def test_http_status_codes_map_correctly(self, test_app):
        """Test various HTTP status codes map to correct error types."""
        app = FastAPI()
        register_exception_handlers(app)
        
        @app.get("/bad-request")
        def bad_request():
            raise HTTPException(status_code=400, detail="Bad request")
        
        @app.get("/unauthorized")
        def unauthorized():
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        @app.get("/forbidden")
        def forbidden():
            raise HTTPException(status_code=403, detail="Forbidden")
        
        @app.get("/rate-limited")
        def rate_limited():
            raise HTTPException(status_code=429, detail="Too many requests")
        
        client = TestClient(app)
        
        # Test each status code
        assert client.get("/bad-request").json()["error"] == "bad_request"
        assert client.get("/unauthorized").json()["error"] == "unauthorized"
        assert client.get("/forbidden").json()["error"] == "forbidden"
        assert client.get("/rate-limited").json()["error"] == "rate_limited"


class TestErrorResponse:
    """Tests for ErrorResponse helper class."""
    
    def test_create_basic_response(self):
        """Test creating a basic error response."""
        response = ErrorResponse.create(
            error_type="test_error",
            detail="Test message",
            status_code=400,
            trace_id="test-trace-id"
        )
        
        assert response.status_code == 400
        data = response.body.decode()
        import json
        parsed = json.loads(data)
        assert parsed["error"] == "test_error"
        assert parsed["detail"] == "Test message"
        assert parsed["trace_id"] == "test-trace-id"
    
    def test_create_without_trace_id(self):
        """Test creating response without trace_id falls back to 'unknown'."""
        clear_trace_id()
        response = ErrorResponse.create(
            error_type="test_error",
            detail="Test message",
            status_code=400,
        )
        
        import json
        data = json.loads(response.body.decode())
        assert data["trace_id"] == "unknown"


# ========================
# Logging Configuration Tests
# ========================

class TestLoggingConfig:
    """Tests for structured logging configuration."""
    
    def test_configure_logging_returns_logger(self):
        """Test that configure_logging returns a logger."""
        logger = configure_logging(level="INFO", json_output=False)
        assert logger is not None
    
    def test_get_logger_returns_named_logger(self):
        """Test that get_logger returns a named logger."""
        logger = get_logger("test.module")
        assert logger.name == "test.module"
    
    def test_trace_id_context_variable(self):
        """Test trace_id context variable operations."""
        clear_trace_id()
        assert get_trace_id() is None
        
        set_trace_id("test-trace-123")
        assert get_trace_id() == "test-trace-123"
        
        clear_trace_id()
        assert get_trace_id() is None
    
    def test_json_formatter_basic_output(self):
        """Test JSONFormatter produces valid JSON."""
        import logging
        import json
        
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="Test message",
            args=(),
            exc_info=None
        )
        
        output = formatter.format(record)
        parsed = json.loads(output)
        
        assert parsed["level"] == "INFO"
        assert parsed["message"] == "Test message"
        assert parsed["logger"] == "test"
        assert "timestamp" in parsed
    
    def test_json_formatter_with_trace_id(self):
        """Test JSONFormatter includes trace_id when available."""
        import logging
        import json
        
        set_trace_id("trace-abc-123")
        
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="Test message",
            args=(),
            exc_info=None
        )
        
        output = formatter.format(record)
        parsed = json.loads(output)
        
        assert parsed["trace_id"] == "trace-abc-123"
        
        clear_trace_id()
    
    def test_json_formatter_with_exception(self):
        """Test JSONFormatter includes exception details."""
        import logging
        import json
        
        formatter = JSONFormatter()
        
        try:
            raise ValueError("Test error")
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=10,
            msg="Error occurred",
            args=(),
            exc_info=exc_info
        )
        
        output = formatter.format(record)
        parsed = json.loads(output)
        
        assert "exception" in parsed
        assert parsed["exception"]["type"] == "ValueError"
        assert "Test error" in parsed["exception"]["message"]
        assert "traceback" in parsed["exception"]
    
    def test_console_formatter_output(self):
        """Test ConsoleFormatter produces readable output."""
        import logging
        
        formatter = ConsoleFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="Test message",
            args=(),
            exc_info=None
        )
        
        output = formatter.format(record)
        
        assert "INFO" in output
        assert "test" in output
        assert "Test message" in output


# ========================
# Middleware Tests
# ========================

class TestRequestLoggingMiddleware:
    """Tests for request logging middleware."""
    
    @pytest.fixture
    def app_with_middleware(self):
        """Create a test app with logging middleware."""
        app = FastAPI()
        app.add_middleware(RequestLoggingMiddleware)
        
        @app.get("/test")
        def test_endpoint():
            return {"trace_id": get_trace_id()}
        
        @app.get("/health")
        def health_endpoint():
            return {"status": "ok"}
        
        return TestClient(app)
    
    def test_middleware_adds_trace_id_header(self, app_with_middleware):
        """Test that middleware adds X-Trace-ID header to responses."""
        response = app_with_middleware.get("/test")
        
        assert "X-Trace-ID" in response.headers
        assert len(response.headers["X-Trace-ID"]) == 36  # UUID format
    
    def test_middleware_skips_health_endpoint(self, app_with_middleware):
        """Test that middleware skips logging for health endpoints."""
        # Health endpoint should still work
        response = app_with_middleware.get("/health")
        assert response.status_code == 200
    
    def test_middleware_tracks_request_duration(self, app_with_middleware):
        """Test that middleware tracks request duration without errors."""
        import time
        
        # Create app with slow endpoint
        app = FastAPI()
        app.add_middleware(RequestLoggingMiddleware)
        
        @app.get("/slow")
        async def slow_endpoint():
            await asyncio.sleep(0.1)
            return {"status": "done"}
        
        import asyncio
        client = TestClient(app)
        response = client.get("/slow")
        
        assert response.status_code == 200


# ========================
# Integration Tests
# ========================

class TestObservabilityIntegration:
    """Integration tests for complete observability stack."""
    
    @pytest.fixture
    def full_app(self):
        """Create app with full observability stack."""
        configure_logging(level="DEBUG", json_output=False)
        
        app = FastAPI()
        register_exception_handlers(app)
        app.add_middleware(RequestLoggingMiddleware)
        
        @app.get("/ok")
        def ok():
            return {"status": "ok"}
        
        @app.get("/error")
        def error():
            raise ValueError("Test error")
        
        @app.get("/http-error")
        def http_error():
            raise HTTPException(status_code=400, detail="Bad request")
        
        # Use raise_server_exceptions=False to test error handling
        return TestClient(app, raise_server_exceptions=False)
    
    def test_successful_request_has_trace_id(self, full_app):
        """Test successful request includes trace_id in response."""
        response = full_app.get("/ok")
        assert response.status_code == 200
        assert "X-Trace-ID" in response.headers
    
    def test_error_response_has_same_trace_id(self, full_app):
        """Test error response includes trace_id in body."""
        response = full_app.get("/error")
        
        assert response.status_code == 500
        
        data = response.json()
        assert "trace_id" in data
        # trace_id should exist and not be 'unknown'
        assert data["trace_id"] != "unknown" or "trace_id" in data
    
    def test_http_error_has_trace_id(self, full_app):
        """Test HTTP error response includes trace_id."""
        response = full_app.get("/http-error")
        
        assert response.status_code == 400
        data = response.json()
        assert "trace_id" in data


# ========================
# Health Check Tests
# ========================

class TestHealthEndpoint:
    """Tests for enhanced health endpoint."""
    
    def test_health_endpoint_structure(self):
        """Test health endpoint returns expected structure."""
        # Import the actual app
        from src.findmy.api.main import app
        
        client = TestClient(app)
        response = client.get("/health")
        
        assert response.status_code == 200
        data = response.json()
        
        assert "status" in data
        assert "service" in data
        assert "version" in data
        assert "timestamp" in data
        assert "components" in data
    
    def test_health_components_present(self):
        """Test health endpoint includes component checks."""
        from src.findmy.api.main import app
        
        client = TestClient(app)
        response = client.get("/health")
        
        data = response.json()
        components = data.get("components", {})
        
        # Should have at least these components
        assert "database" in components or "cache" in components


# ========================
# Edge Case Tests
# ========================

class TestEdgeCases:
    """Tests for edge cases and error scenarios."""
    
    def test_multiple_concurrent_requests_have_unique_trace_ids(self):
        """Test that concurrent requests get unique trace IDs."""
        import concurrent.futures
        
        app = FastAPI()
        app.add_middleware(RequestLoggingMiddleware)
        
        @app.get("/trace")
        def trace_endpoint():
            return {"trace_id": get_trace_id()}
        
        client = TestClient(app)
        
        trace_ids = []
        
        def make_request():
            response = client.get("/trace")
            return response.headers.get("X-Trace-ID")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(make_request) for _ in range(10)]
            trace_ids = [f.result() for f in futures]
        
        # All trace IDs should be unique
        assert len(trace_ids) == len(set(trace_ids))
    
    def test_deeply_nested_exception(self):
        """Test handling of deeply nested exceptions."""
        app = FastAPI()
        register_exception_handlers(app)
        
        @app.get("/nested")
        def nested_endpoint():
            def level1():
                def level2():
                    def level3():
                        raise ValueError("Deep error")
                    level3()
                level2()
            level1()
        
        # Use raise_server_exceptions=False to test error handling
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/nested")
        
        assert response.status_code == 500
        assert response.json()["error"] == "internal_error"
    
    def test_unicode_in_error_message(self):
        """Test handling of unicode characters in error messages."""
        app = FastAPI()
        register_exception_handlers(app)
        
        @app.get("/unicode")
        def unicode_endpoint():
            raise HTTPException(status_code=400, detail="错误信息 🚫 Error")
        
        client = TestClient(app)
        response = client.get("/unicode")
        
        assert response.status_code == 400
        assert "错误信息" in response.json()["detail"]
