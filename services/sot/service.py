from typing import Optional

from services.sot.db import SessionLocal
from services.sot import repository as sot_repo


class SOTService:
    """
    SOT domain service.

    Responsibilities:
    - Own DB session for SOT
    - Expose domain-level operations
    - Hide DAL / ORM from API layer

    API layer MUST NOT touch DB or repository directly.
    """

    def __init__(self):
        self.db = SessionLocal()

    # =========================
    # Order Request (Intent)
    # =========================

    def create_order_request(
        self,
        *,
        source: str,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        strategy_code: Optional[str] = None,
        requested_by: Optional[str] = None,
    ) -> int:
        """
        Create a new order request (intent).
        """
        req = sot_repo.create_order_request(
            self.db,
            source=source,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            strategy_code=strategy_code,
            requested_by=requested_by,
        )
        self.db.commit()
        return req.id

    # =========================
    # Read Models
    # =========================

    def get_order_status(self, order_id: int):
        order = self.db.get(sot_repo.Order, order_id)
        if not order:
            raise ValueError(f"Order {order_id} not found")
        return order

    def get_order_pnl(self, order_id: int):
        pnl = self.db.get(sot_repo.OrderPnl, order_id)
        if not pnl:
            raise ValueError(f"PnL for order {order_id} not found")
        return pnl

    # =========================
    # Housekeeping
    # =========================

    def close(self):
        self.db.close()
