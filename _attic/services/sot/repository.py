from sqlalchemy.orm import Session
from datetime import datetime
from sqlalchemy import func
from .models import (
    OrderRequest,
    Order,
    OrderEvent,
    OrderFill,
    OrderCost,
    OrderPnl,
)


from .models import (
    OrderRequest,
    Order,
    OrderEvent,
    OrderFill
)
def create_order_request(
    db: Session,
    *,
    source: str,
    symbol: str,
    side: str,
    order_type: str,
    quantity: float,
    price: float | None = None,
    strategy_code: str | None = None,
    requested_by: str | None = None,
    raw_payload: str | None = None,
):
    req = OrderRequest(
        source=source,
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
        strategy_code=strategy_code,
        requested_by=requested_by,
        raw_payload=raw_payload,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req
def create_order(
    db: Session,
    *,
    order_request_id: int,
    exchange: str,
    status: str,
    time_in_force: str | None = None,
):
    order = Order(
        order_request_id=order_request_id,
        exchange=exchange,
        status=status,
        time_in_force=time_in_force,
        sent_at=datetime.utcnow(),
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order
def append_order_event(
    db: Session,
    *,
    order_id: int,
    event_type: str,
    payload: str | None = None,
):
    event = OrderEvent(
        order_id=order_id,
        event_type=event_type,
        payload=payload,
    )
    db.add(event)
    db.commit()
    return event
def insert_order_fill(
    db: Session,
    *,
    order_id: int,
    fill_price: float,
    fill_qty: float,
    fee_amount: float | None = None,
    fee_asset: str | None = None,
    liquidity: str | None = None,
):
    fill = OrderFill(
        order_id=order_id,
        fill_price=fill_price,
        fill_qty=fill_qty,
        fee_amount=fee_amount,
        fee_asset=fee_asset,
        liquidity=liquidity,
        filled_at=datetime.utcnow(),
    )
    db.add(fill)
    db.commit()
    return fill
def calculate_and_save_order_cost(db: Session, *, order_id: int):
    total_fee = (
        db.query(func.sum(OrderFill.fee_amount))
        .filter(OrderFill.order_id == order_id)
        .scalar()
    ) or 0.0

    cost = OrderCost(
        order_id=order_id,
        total_fee=total_fee,
    )

    db.merge(cost)
    db.commit()
    return cost
def calculate_and_save_order_pnl(
    db: Session,
    *,
    order_id: int,
    market_price: float,
):
    order = db.query(Order).filter(Order.id == order_id).one()

    fills = (
        db.query(OrderFill)
        .filter(OrderFill.order_id == order_id)
        .all()
    )

    if not fills:
        raise ValueError("Cannot calculate PnL without fills")

    total_qty = sum(f.fill_qty for f in fills)
    avg_price = sum(f.fill_price * f.fill_qty for f in fills) / total_qty
    total_fee = sum(f.fee_amount or 0 for f in fills)

    side = order.order_request.side.upper()

    if side == "BUY":
        pnl = (market_price - avg_price) * total_qty - total_fee
    else:
        pnl = (avg_price - market_price) * total_qty - total_fee

    pnl_snapshot = OrderPnl(
        order_id=order_id,
        realized_pnl=pnl,
        cost_basis=avg_price * total_qty,
    )

    db.merge(pnl_snapshot)
    db.commit()
    return pnl_snapshot
