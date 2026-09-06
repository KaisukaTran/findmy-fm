from services.sot.db import SessionLocal
from services.sot.repository import (
    create_order_request,
    create_order,
    append_order_event,
    insert_order_fill
)

db = SessionLocal()

req = create_order_request(
    db,
    source="manual",
    symbol="BTCUSDT",
    side="BUY",
    order_type="MARKET",
    quantity=0.01,
    requested_by="kai"
)

order = create_order(
    db,
    order_request_id=req.id,
    exchange="binance",
    status="SENT",
)

append_order_event(
    db,
    order_id=order.id,
    event_type="SENT"
)

insert_order_fill(
    db,
    order_id=order.id,
    fill_price=43000,
    fill_qty=0.01,
    fee_amount=0.43,
    fee_asset="USDT",
    liquidity="TAKER"
)

print("OK")
from services.sot.repository import (
    calculate_and_save_order_cost,
    calculate_and_save_order_pnl
)

calculate_and_save_order_cost(db, order_id=order.id)

pnl = calculate_and_save_order_pnl(
    db,
    order_id=order.id,
    market_price=43500
)

print("PnL:", pnl.realized_pnl)
