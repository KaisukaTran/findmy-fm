from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from findmy.api.common.enums import OrderSide, OrderType


# =========================
# Request Schemas
# =========================

class OrderRequestCreate(BaseModel):
    source: str = Field(..., max_length=32)
    symbol: str = Field(..., max_length=20)
    side: OrderSide
    order_type: OrderType
    quantity: float = Field(..., gt=0)
    price: Optional[float] = Field(None, gt=0)
    strategy_code: Optional[str] = Field(None, max_length=64)
    requested_by: Optional[str] = Field(None, max_length=64)


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