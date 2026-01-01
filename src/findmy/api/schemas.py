from pydantic import BaseModel, Field, validator, ValidationError
from typing import Optional, List
from datetime import datetime
from findmy.api.common.enums import OrderSide, OrderType
import re

# v0.7.0: Enhanced validation
VALID_SYMBOLS = {"BTC/USD", "ETH/USD", "BNB/USD", "XRP/USD", "ADA/USD"}  # Whitelist
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{1,10}/[A-Z]{3}$")
MAX_ORDER_QUANTITY = 1000000
MAX_ORDER_PRICE = 1000000


# =========================
# Request Schemas (v0.7.0)
# =========================

class OrderRequestCreate(BaseModel):
    source: str = Field(..., max_length=32, min_length=1)
    symbol: str = Field(..., max_length=20)
    side: OrderSide
    order_type: OrderType
    quantity: float = Field(..., gt=0, le=MAX_ORDER_QUANTITY)
    price: Optional[float] = Field(None, gt=0, le=MAX_ORDER_PRICE)
    strategy_code: Optional[str] = Field(None, max_length=64)
    requested_by: Optional[str] = Field(None, max_length=64)
    
    @validator('symbol')
    def validate_symbol(cls, v):
        """Validate symbol format and whitelisting."""
        v = v.upper().strip()
        
        # Check format
        if not SYMBOL_PATTERN.match(v):
            raise ValueError(f"Invalid symbol format: {v}. Expected: XXXX/XXX")
        
        # Check whitelist
        if v not in VALID_SYMBOLS:
            raise ValueError(f"Unsupported symbol: {v}. Allowed: {VALID_SYMBOLS}")
        
        return v
    
    @validator('quantity')
    def validate_quantity(cls, v):
        """Validate quantity is reasonable."""
        if v <= 0:
            raise ValueError("Quantity must be positive")
        if v > MAX_ORDER_QUANTITY:
            raise ValueError(f"Quantity exceeds maximum: {MAX_ORDER_QUANTITY}")
        return v
    
    @validator('price')
    def validate_price(cls, v):
        """Validate price if provided."""
        if v is not None and (v <= 0 or v > MAX_ORDER_PRICE):
            raise ValueError(f"Price must be > 0 and <= {MAX_ORDER_PRICE}")
        return v
    
    class Config:
        from_attributes = True


# =========================
# Response Schemas
# =========================

class OrderRequestResponse(BaseModel):
    order_request_id: int


class OrderExecuteResponse(BaseModel):
    order_id: int


class OrderStatusResponse(BaseModel):
    order_id: int
    status: str
    exchange: str
    created_at: datetime


class OrderPnlResponse(BaseModel):
    order_id: int
    realized_pnl: float
    cost_basis: Optional[float] = None
    calculated_at: Optional[datetime] = None


# =========================
# Authentication Schemas
# =========================

class LoginRequest(BaseModel):
    """User login request."""
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8, max_length=256)


class TokenResponse(BaseModel):
    """Authentication token response."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    """User information response."""
    username: str
    email: Optional[str] = None
    full_name: Optional[str] = None
    disabled: bool = False
    
    class Config:
        from_attributes = True
