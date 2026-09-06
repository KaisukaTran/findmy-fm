from fastapi import APIRouter, HTTPException, Query

from services.executor.service import ExecutorService
from services.sot.service import SOTService

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
    service = SOTService()
    try:
        order_request_id = service.create_order_request(
            source=payload.source,
            symbol=payload.symbol,
            side=payload.side,
            order_type=payload.order_type,
            quantity=payload.quantity,
            price=payload.price,
            strategy_code=payload.strategy_code,
            requested_by=payload.requested_by,
        )
        return OrderRequestResponse(order_request_id=order_request_id)
    finally:
        service.close()


@router.post("/execute/{order_request_id}", response_model=OrderExecuteResponse)
def execute_order(order_request_id: int, market_price: float = Query(...)):
    executor = ExecutorService()
    order_id = executor.execute_order_request(
        order_request_id=order_request_id,
        market_price=market_price,
    )
    return OrderExecuteResponse(order_id=order_id)


@router.get("/orders/{order_id}", response_model=OrderStatusResponse)
def get_order_status(order_id: int):
    service = SOTService()
    try:
        order = service.get_order_status(order_id)
        return OrderStatusResponse(
            order_id=order.id,
            status=order.status,
            exchange=order.exchange,
            created_at=order.created_at,
        )
    finally:
        service.close()


@router.get("/orders/{order_id}/pnl", response_model=OrderPnlResponse)
def get_order_pnl(order_id: int):
    service = SOTService()
    try:
        pnl = service.get_order_pnl(order_id)
        return OrderPnlResponse(
            order_id=pnl.order_id,
            realized_pnl=pnl.realized_pnl,
            cost_basis=pnl.cost_basis,
            calculated_at=pnl.calculated_at,
        )
    finally:
        service.close()
