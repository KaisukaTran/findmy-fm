"""
Prometheus Observability Module (v0.7.0)

Provides Prometheus metrics for monitoring API performance, database queries, caching, and system health.
"""

from prometheus_client import Counter, Histogram, Gauge, Info
import time
from functools import wraps
from typing import Callable
import logging

logger = logging.getLogger(__name__)


# =========================================================================
# API Performance Metrics
# =========================================================================

api_requests_total = Counter(
    "api_requests_total",
    "Total API requests",
    ["method", "endpoint", "status"]
)

api_request_duration_seconds = Histogram(
    "api_request_duration_seconds",
    "API request duration in seconds",
    ["method", "endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
)


# =========================================================================
# Database Metrics
# =========================================================================

db_queries_total = Counter(
    "db_queries_total",
    "Total database queries",
    ["table", "operation"]  # e.g., trades, SELECT
)

db_query_duration_seconds = Histogram(
    "db_query_duration_seconds",
    "Database query duration in seconds",
    ["table"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5)
)

db_connections_active = Gauge(
    "db_connections_active",
    "Number of active database connections"
)


# =========================================================================
# Cache Metrics
# =========================================================================

cache_hits_total = Counter(
    "cache_hits_total",
    "Total cache hits",
    ["cache_level", "key_pattern"]  # L1, L2; positions, trades, etc
)

cache_misses_total = Counter(
    "cache_misses_total",
    "Total cache misses",
    ["cache_level", "key_pattern"]
)

cache_size_bytes = Gauge(
    "cache_size_bytes",
    "Cache size in bytes",
    ["cache_level"]
)

cache_entries = Gauge(
    "cache_entries",
    "Number of cache entries",
    ["cache_level"]
)


# =========================================================================
# Trading Metrics
# =========================================================================

trades_total = Counter(
    "trades_total",
    "Total number of trades executed",
    ["symbol", "side"]  # BTC/USD, BUY
)

trades_pnl_total = Histogram(
    "trades_pnl_total",
    "P&L distribution of trades",
    ["symbol"],
    buckets=(-1000, -100, -10, 0, 10, 100, 1000, 10000)
)

positions_active = Gauge(
    "positions_active",
    "Number of active trading positions",
    ["symbol"]
)

positions_total_value = Gauge(
    "positions_total_value",
    "Total value of all positions",
    ["currency"]
)

daily_loss_total = Gauge(
    "daily_loss_total",
    "Total daily loss (negative value)"
)


# =========================================================================
# Order Metrics
# =========================================================================

orders_pending_total = Gauge(
    "orders_pending_total",
    "Number of pending orders awaiting approval"
)

orders_approved_total = Counter(
    "orders_approved_total",
    "Total orders approved",
    ["symbol"]
)

orders_rejected_total = Counter(
    "orders_rejected_total",
    "Total orders rejected",
    ["symbol"]
)

order_processing_time_seconds = Histogram(
    "order_processing_time_seconds",
    "Time from creation to approval/rejection",
    ["status"],
    buckets=(1, 5, 10, 30, 60, 300, 600)
)


# =========================================================================
# System Health Metrics
# =========================================================================

app_info = Info(
    "findmy_app",
    "FINDMY FM Application Information",
    labelnames=["version"]
)

health_check_failures_total = Counter(
    "health_check_failures_total",
    "Total health check failures"
)

uptime_seconds = Gauge(
    "app_uptime_seconds",
    "Application uptime in seconds"
)


# =========================================================================
# Utility Functions
# =========================================================================

def track_db_query(table: str, operation: str = "SELECT"):
    """Decorator to track database query metrics."""
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.time()
            try:
                result = func(*args, **kwargs)
                return result
            finally:
                duration = time.time() - start
                db_queries_total.labels(table=table, operation=operation).inc()
                db_query_duration_seconds.labels(table=table).observe(duration)
                
                if duration > 0.1:  # Log slow queries
                    logger.warning(f"Slow query: {table}.{operation} took {duration:.3f}s")
        
        return wrapper
    return decorator


def track_api_request(endpoint: str, method: str = "GET"):
    """Decorator to track API request metrics."""
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.time()
            status = "200"
            try:
                result = func(*args, **kwargs)
                return result
            except Exception as e:
                status = str(getattr(e, "status_code", "500"))
                raise
            finally:
                duration = time.time() - start
                api_requests_total.labels(method=method, endpoint=endpoint, status=status).inc()
                api_request_duration_seconds.labels(method=method, endpoint=endpoint).observe(duration)
        
        return wrapper
    return decorator


# =========================================================================
# Metrics Snapshots (for logs/reports)
# =========================================================================

class MetricsSnapshot:
    """Capture a snapshot of key metrics."""
    
    @staticmethod
    def get_cache_stats(cache_manager) -> dict:
        """Get cache statistics."""
        l1_stats = cache_manager.l1.get_stats()
        return {
            "l1_hits": l1_stats["hits"],
            "l1_misses": l1_stats["misses"],
            "l1_hit_rate": l1_stats["hit_rate"],
            "l1_entries": l1_stats["entries"],
        }
    
    @staticmethod
    def get_db_stats() -> dict:
        """Get database statistics."""
        # In a real implementation, this would get actual pool stats
        return {
            "active_connections": db_connections_active._value.get(),
            "total_queries": sum([
                m.labels(table=t, operation=op)._value.get()
                for t in ["trades", "positions", "orders"]
                for op in ["SELECT", "INSERT", "UPDATE"]
            ]) if db_queries_total._metrics else 0,
        }
    
    @staticmethod
    def log_metrics(cache_manager):
        """Log current metrics snapshot."""
        cache_stats = MetricsSnapshot.get_cache_stats(cache_manager)
        logger.info(f"Cache Stats: hits={cache_stats['l1_hits']}, "
                   f"misses={cache_stats['l1_misses']}, "
                   f"hit_rate={cache_stats['l1_hit_rate']}")
