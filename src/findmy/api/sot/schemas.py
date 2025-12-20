from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from findmy.api.common.enums import OrderSide, OrderType


class OrderRequestCreate(BaseModel):
    source: str = Field(..., max_length=32)
    symbol: str = Field(..., max_length=20)
    side: OrderSide
    order_type: OrderType
    quantity: float = Field(..., gt=0)
    price: Optional[float] = Field(None, gt=0)
    strategy_code: Optional[str] = Field(None, max_length=64)
    requested_by: Optional[str] = Field(None, max_length=64)
