from services.sot.db import SessionLocal
from services.sot.repository import create_order_request
from services.executor.service import ExecutorService

db = SessionLocal()

req = create_order_request(
    db,
    source="manual",
    symbol="BTCUSDT",
    side="BUY",
    order_type="MARKET",
    quantity=0.01,
    requested_by="kai",
)

db.commit()   # 👈 commit intent so other sessions can see it

executor = ExecutorService()
order_id = executor.execute_order_request(
    order_request_id=req.id,
    market_price=43500,
)

print("Executed order:", order_id)
