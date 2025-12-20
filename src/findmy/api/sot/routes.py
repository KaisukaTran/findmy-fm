from fastapi import APIRouter, HTTPException, Query

from services.sot.db import SessionLocal
from services.sot import repository as sot_repo
from services.executor.service import ExecutorService

from findmy.api.sot.schemas import (
    OrderRequestCreate,
    OrderRequestResponse,
    OrderExecuteResponse,
    OrderStatusResponse,
    OrderPnlResponse,
)

router = APIRouter(
    prefix="/sot",
    tags=["SOT"],
)


@router.post("/order-requests", response_model=OrderRequestResponse)
def create_order_request(payload: OrderRequestCreate):
    db = SessionLocal()
    try:
        req = sot_repo.create_order_request(
            db,
            source=payload.source,
            symbol=payload.symbol,
            side=payload.side,
            order_type=payload.order_type,
            quantity=payload.quantity,
            price=payload.price,
            strategy_code=payload.strategy_code,
            requested_by=payload.requested_by,
        )
        db.commit()
        return OrderRequestResponse(order_request_id=req.id)
    finally:
        db.close()


@router.post("/execute/{order_request_id}", response_model=OrderExecuteResponse)
def execute_order(
    order_request_id: int,
    market_price: float = Query(..., description="Reference market price for PnL snapshot"),
):
    executor = ExecutorService()
    try:
        order_id = executor.execute_order_request(
            order_request_id=order_request_id,
            market_price=market_price,
        )
        return OrderExecuteResponse(order_id=order_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/orders/{order_id}", response_model=OrderStatusResponse)
def get_order_status(order_id: int):
    db = SessionLocal()
    try:
        order = db.get(sot_repo.Order, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        return OrderStatusResponse(
            order_id=order.id,
            status=order.status,
            exchange=order.exchange,
            created_at=order.created_at,
        )
    finally:
        db.close()


@router.get("/orders/{order_id}/pnl", response_model=OrderPnlResponse)
def get_order_pnl(order_id: int):
    db = SessionLocal()
    try:
        pnl = db.get(sot_repo.OrderPnl, order_id)
        if not pnl:
            raise HTTPException(status_code=404, detail="PnL not found")

        return OrderPnlResponse(
            order_id=pnl.order_id,
            realized_pnl=pnl.realized_pnl,
            cost_basis=pnl.cost_basis,
            calculated_at=pnl.calculated_at,
        )
    finally:
        db.close()
