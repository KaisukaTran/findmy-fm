"""
Caching Service (v0.7.0)

L1/L2 cache hierarchy for hot data:
- L1: In-memory cache (5-60s TTL)
- L2: Redis cache (optional, falls back to database)

Targets:
- 70-80% cache hit rate for common queries
- 90% faster reads on cached data
"""

from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable
import json
import hashlib
import asyncio
import logging
from functools import wraps
import os

logger = logging.getLogger(__name__)


class CacheConfig:
    """Cache configuration."""
    
    # L1 Cache (in-memory) TTLs
    TTL_POSITIONS = 30  # seconds
    TTL_TRADES = 60
    TTL_SUMMARY = 10
    TTL_MARKET_DATA = 30
    
    # L2 Cache (Redis) - longer TTL
    REDIS_ENABLED = os.getenv("REDIS_ENABLED", "false").lower() == "true"
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    
    # Cache keys
    CACHE_KEYS = {
        "positions": "findmy:positions:{symbol}",
        "trades": "findmy:trades:{symbol}",
        "summary": "findmy:summary",
        "market_data": "findmy:market:{symbol}",
        "daily_loss": "findmy:daily_loss:{date}",
        "equity": "findmy:equity",
    }


class CacheEntry:
    """Represents a cached value with expiration."""
    
    def __init__(self, value: Any, ttl: int):
        self.value = value
        self.ttl = ttl
        self.created_at = datetime.utcnow()
        self.expires_at = self.created_at + timedelta(seconds=ttl)
    
    def is_expired(self) -> bool:
        """Check if entry has expired."""
        return datetime.utcnow() > self.expires_at
    
    def __repr__(self):
        remaining = (self.expires_at - datetime.utcnow()).total_seconds()
        return f"<CacheEntry(expires_in={remaining:.1f}s)>"


class L1Cache:
    """In-memory L1 cache with TTL support."""
    
    def __init__(self):
        self.cache: Dict[str, CacheEntry] = {}
        self.hits = 0
        self.misses = 0
    
    def get(self, key: str) -> Optional[Any]:
        """Get value from cache."""
        if key not in self.cache:
            self.misses += 1
            return None
        
        entry = self.cache[key]
        if entry.is_expired():
            del self.cache[key]
            self.misses += 1
            return None
        
        self.hits += 1
        logger.debug(f"L1 cache hit: {key}")
        return entry.value
    
    def set(self, key: str, value: Any, ttl: int):
        """Set value in cache."""
        self.cache[key] = CacheEntry(value, ttl)
        logger.debug(f"L1 cache set: {key} (TTL: {ttl}s)")
    
    def delete(self, key: str):
        """Delete from cache."""
        if key in self.cache:
            del self.cache[key]
            logger.debug(f"L1 cache delete: {key}")
    
    def clear(self):
        """Clear all cache entries."""
        self.cache.clear()
        logger.info("L1 cache cleared")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total = self.hits + self.misses
        hit_rate = (self.hits / total * 100) if total > 0 else 0
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": f"{hit_rate:.1f}%",
            "entries": len(self.cache),
        }


class RedisCache:
    """L2 Redis cache (optional, async)."""
    
    def __init__(self):
        self.redis = None
        self.enabled = CacheConfig.REDIS_ENABLED
    
    async def init(self):
        """Initialize Redis connection."""
        if not self.enabled:
            return
        
        try:
            import aioredis
            self.redis = await aioredis.create_redis_pool(CacheConfig.REDIS_URL)
            logger.info("Redis cache initialized")
        except Exception as e:
            logger.warning(f"Redis cache failed to initialize: {e}")
            self.enabled = False
    
    async def get(self, key: str) -> Optional[Any]:
        """Get from Redis."""
        if not self.enabled or not self.redis:
            return None
        
        try:
            value = await self.redis.get(key)
            if value:
                logger.debug(f"L2 cache hit: {key}")
                return json.loads(value)
        except Exception as e:
            logger.warning(f"Redis get failed: {e}")
        
        return None
    
    async def set(self, key: str, value: Any, ttl: int):
        """Set in Redis."""
        if not self.enabled or not self.redis:
            return
        
        try:
            await self.redis.setex(key, ttl, json.dumps(value))
            logger.debug(f"L2 cache set: {key} (TTL: {ttl}s)")
        except Exception as e:
            logger.warning(f"Redis set failed: {e}")
    
    async def delete(self, key: str):
        """Delete from Redis."""
        if not self.enabled or not self.redis:
            return
        
        try:
            await self.redis.delete(key)
            logger.debug(f"L2 cache delete: {key}")
        except Exception as e:
            logger.warning(f"Redis delete failed: {e}")


class CacheManager:
    """
    Two-level cache manager: L1 (in-memory) + L2 (Redis).
    
    Usage:
        cache = CacheManager()
        
        # Simple get/set
        value = cache.get("positions:BTC/USD")
        cache.set("positions:BTC/USD", data, ttl=30)
        
        # Decorator for functions
        @cache.cached(ttl=30)
        def get_positions(symbol):
            return db.query(...).all()
    """
    
    def __init__(self):
        self.l1 = L1Cache()
        self.l2 = RedisCache()
    
    async def init(self):
        """Initialize all caches."""
        await self.l2.init()
    
    async def get(self, key: str) -> Optional[Any]:
        """Get from L1 then L2."""
        # Try L1 first (fast)
        value = self.l1.get(key)
        if value is not None:
            return value
        
        # Try L2 (slower)
        value = await self.l2.get(key)
        if value is not None:
            # Populate L1 from L2
            self.l1.set(key, value, CacheConfig.TTL_POSITIONS)
            return value
        
        return None
    
    async def set(self, key: str, value: Any, ttl: int = 30):
        """Set in both L1 and L2."""
        self.l1.set(key, value, ttl)
        await self.l2.set(key, value, ttl)
    
    async def delete(self, key: str):
        """Delete from both L1 and L2."""
        self.l1.delete(key)
        await self.l2.delete(key)
    
    async def clear(self):
        """Clear all caches."""
        self.l1.clear()
        # TODO: Add Redis FLUSHDB
    
    def cached(self, ttl: int = 30):
        """Decorator to cache function results."""
        def decorator(func: Callable):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                # Generate cache key from function name and args
                cache_key = f"{func.__name__}:{hash(str(args) + str(kwargs))}"
                
                # Try cache first
                value = self.l1.get(cache_key)
                if value is not None:
                    return value
                
                # Call function if not cached
                result = await func(*args, **kwargs) if asyncio.iscoroutinefunction(func) else func(*args, **kwargs)
                
                # Cache result
                await self.set(cache_key, result, ttl)
                return result
            
            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                # Generate cache key
                cache_key = f"{func.__name__}:{hash(str(args) + str(kwargs))}"
                
                # Try cache first
                value = self.l1.get(cache_key)
                if value is not None:
                    return value
                
                # Call function
                result = func(*args, **kwargs)
                
                # Cache result (sync set)
                self.l1.set(cache_key, result, ttl)
                return result
            
            return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper
        
        return decorator
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        return {
            "l1": self.l1.get_stats(),
            "l2_enabled": self.l2.enabled,
        }


# Global cache instance
cache_manager = CacheManager()
