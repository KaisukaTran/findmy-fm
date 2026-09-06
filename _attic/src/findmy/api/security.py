"""
Security Middleware and Dependencies (v0.7.0)

Provides rate limiting, CORS, and authentication dependencies.
"""

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from datetime import datetime
from services.auth.service import verify_token
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# =========================================================================
# Rate Limiting (v0.7.0)
# =========================================================================

limiter = Limiter(key_func=get_remote_address, default_limits=["1000/day", "100/minute"])


class RateLimitConfig:
    """Rate limiting configuration."""
    
    # Global limits (per IP)
    GLOBAL_REQUESTS_PER_MINUTE = 100
    GLOBAL_REQUESTS_PER_DAY = 1000
    
    # Endpoint-specific limits
    ENDPOINTS = {
        "login": "5/minute",  # Prevent brute force
        "trading": "30/minute",  # Trading endpoints
        "data": "60/minute",  # Data retrieval
        "default": "100/minute",  # Default for other endpoints
    }


def rate_limit_error_handler(request: Request, exc: RateLimitExceeded):
    """Handle rate limit exceeded errors."""
    logger.warning(f"Rate limit exceeded: {request.client.host} for {request.url.path}")
    return {
        "error": "Rate limit exceeded",
        "detail": str(exc.detail),
        "retry_after": 60,
    }


# =========================================================================
# Authentication (v0.7.0)
# =========================================================================

security = HTTPBearer()


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Dependency to validate JWT token from Authorization header.
    
    Usage:
        @app.get("/api/protected")
        async def protected_route(user = Depends(get_current_user)):
            return {"user": user}
    """
    token = credentials.credentials
    
    token_data = verify_token(token)
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "bearer"},
        )
    
    # Check token expiration
    if datetime.utcnow() > token_data.exp:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "bearer"},
        )
    
    return token_data


async def get_optional_user(request: Request) -> Optional[str]:
    """
    Optional authentication - returns username if token provided, None otherwise.
    
    Usage for non-protected endpoints that still want user context:
        @app.get("/api/public")
        async def public_route(user = Depends(get_optional_user)):
            if user:
                # User is authenticated
                pass
            else:
                # User is anonymous
                pass
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("bearer "):
        return None
    
    token = auth_header.replace("bearer ", "")
    token_data = verify_token(token)
    
    if token_data and datetime.utcnow() <= token_data.exp:
        return token_data.sub
    
    return None


# =========================================================================
# CORS Configuration (v0.7.0)
# =========================================================================

CORS_CONFIG = {
    "allow_origins": ["http://localhost:3000", "http://localhost:8080", "https://yourdomain.com"],
    "allow_credentials": True,
    "allow_methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    "allow_headers": ["*"],
    "expose_headers": ["X-Total-Count", "X-Page", "X-Page-Size"],
    "max_age": 600,
}


# =========================================================================
# Security Headers (v0.7.0)
# =========================================================================

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",  # Prevent MIME type sniffing
    "X-Frame-Options": "DENY",  # Prevent clickjacking
    "X-XSS-Protection": "1; mode=block",  # Enable XSS protection
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",  # HSTS
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'",
}


# =========================================================================
# Request Logging (v0.7.0)
# =========================================================================

class RequestLogger:
    """Log API requests for security auditing."""
    
    @staticmethod
    async def log_request(request: Request, user: Optional[str] = None):
        """Log request details."""
        logger.info(
            f"API Request: {request.method} {request.url.path} "
            f"from {request.client.host} "
            f"by {user or 'anonymous'}"
        )


# =========================================================================
# Input Validation Utilities (v0.7.0)
# =========================================================================

class InputValidator:
    """Validate and sanitize user input."""
    
    @staticmethod
    def validate_symbol(symbol: str) -> str:
        """Validate trading symbol format."""
        VALID_SYMBOLS = {"BTC/USD", "ETH/USD", "BNB/USD", "XRP/USD", "ADA/USD"}
        symbol = symbol.upper().strip()
        
        if symbol not in VALID_SYMBOLS:
            raise ValueError(f"Invalid symbol: {symbol}")
        
        return symbol
    
    @staticmethod
    def validate_quantity(quantity: float) -> float:
        """Validate order quantity."""
        if quantity <= 0 or quantity > 1000000:
            raise ValueError(f"Invalid quantity: {quantity}")
        return quantity
    
    @staticmethod
    def validate_price(price: float) -> float:
        """Validate order price."""
        if price <= 0 or price > 1000000:
            raise ValueError(f"Invalid price: {price}")
        return price
