"""
Centralized logging configuration with structured JSON output.

v1.0.1: Observability - Structured logging with trace_id correlation.
"""
import logging
import json
import sys
import os
from datetime import datetime
from typing import Any, Dict, Optional
from contextvars import ContextVar

# Context variable for request trace_id (thread-safe)
trace_id_var: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)


class JSONFormatter(logging.Formatter):
    """
    Custom JSON formatter for structured logging.
    
    Outputs logs in JSON format for easy parsing by log aggregators
    (ELK, Datadog, CloudWatch, etc.).
    """
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Add trace_id if available
        trace_id = trace_id_var.get()
        if trace_id:
            log_entry["trace_id"] = trace_id
        
        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "traceback": self.formatException(record.exc_info),
            }
        
        # Add extra fields from record
        extra_fields = {
            k: v for k, v in record.__dict__.items()
            if k not in {
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs",
                "pathname", "process", "processName", "relativeCreated",
                "stack_info", "exc_info", "exc_text", "thread", "threadName",
                "message", "taskName"
            }
        }
        if extra_fields:
            log_entry["extra"] = extra_fields
        
        return json.dumps(log_entry, default=str)


class ConsoleFormatter(logging.Formatter):
    """
    Human-readable console formatter with colors for development.
    """
    
    COLORS = {
        "DEBUG": "\033[36m",     # Cyan
        "INFO": "\033[32m",      # Green
        "WARNING": "\033[33m",   # Yellow
        "ERROR": "\033[31m",     # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record with colors."""
        color = self.COLORS.get(record.levelname, "")
        trace_id = trace_id_var.get()
        trace_str = f"[{trace_id[:8]}]" if trace_id else ""
        
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        
        base = f"{color}{timestamp} {record.levelname:8}{self.RESET} {trace_str} {record.name}: {record.getMessage()}"
        
        if record.exc_info:
            base += f"\n{self.formatException(record.exc_info)}"
        
        return base


def configure_logging(
    level: Optional[str] = None,
    json_output: Optional[bool] = None,
) -> logging.Logger:
    """
    Configure application logging with structured output.
    
    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR). 
               Defaults to LOG_LEVEL env var or INFO.
        json_output: If True, output JSON. If False, output colored console.
                     Defaults to LOG_FORMAT env var == "json" or False.
    
    Returns:
        Root logger configured for the application.
    """
    # Get configuration from environment
    if level is None:
        level = os.getenv("LOG_LEVEL", "INFO").upper()
    
    if json_output is None:
        json_output = os.getenv("LOG_FORMAT", "console").lower() == "json"
    
    # Get numeric level
    numeric_level = getattr(logging, level, logging.INFO)
    
    # Create root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    
    # Remove existing handlers
    root_logger.handlers.clear()
    
    # Create handler with appropriate formatter
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(numeric_level)
    
    if json_output:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(ConsoleFormatter())
    
    root_logger.addHandler(handler)
    
    # Configure specific loggers
    # Reduce noise from uvicorn access logs (we have our own middleware)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    
    # Keep uvicorn error logs
    logging.getLogger("uvicorn.error").setLevel(numeric_level)
    
    # Reduce SQLAlchemy noise unless debugging
    if level != "DEBUG":
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    
    return root_logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with the given name.
    
    Args:
        name: Logger name (typically __name__ of the module).
    
    Returns:
        Configured logger instance.
    """
    return logging.getLogger(name)


# Convenience function to set trace_id
def set_trace_id(trace_id: str) -> None:
    """Set the current request's trace_id."""
    trace_id_var.set(trace_id)


def get_trace_id() -> Optional[str]:
    """Get the current request's trace_id."""
    return trace_id_var.get()


def clear_trace_id() -> None:
    """Clear the current trace_id."""
    trace_id_var.set(None)
