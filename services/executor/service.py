from services.sot.db import SessionLocal
from services.sot import repository as sot_repo
from services.executor.mock_exchange import MockExchangeClient


class ExecutorService:
    """
    SOT-centric executor.
    Responsible ONLY for order lifecycle orchestration.
    """

    def __init__(self):
        self.db = SessionLocal()
        self.exchange = MockExchangeClient()

    def execute_order_request(self, *, order_request_id: int, market_price: float):

        try:
            with self.db.begin():

                # 1. Load order request INSIDE transaction
                req = self.db.get(
                    sot_repo.OrderRequest,
                    order_request_id
                )

                if not req:
                    raise ValueError(f"OrderRequest {order_request_id} not found")

                # 2. Idempotency check
                existing_order = (
                    self.db.query(sot_repo.Order)
                    .filter(sot_repo.Order.order_request_id == req.id)
                    .first()
                )

                if existing_order:
                    return existing_order.id

                # 3. Create order
                order = sot_repo.create_order(
                    self.db,
                    order_request_id=req.id,
                    exchange="mock",
                    status="SENT",
                    time_in_force="GTC",
                )

                sot_repo.append_order_event(
                    self.db,
                    order_id=order.id,
                    event_type="SENT",
                )

                # 4. Mock execution
                fills = self.exchange.execute_order(
                    symbol=req.symbol,
                    side=req.side,
                    quantity=req.quantity,
                )

                for f in fills:
                    sot_repo.insert_order_fill(
                        self.db,
                        order_id=order.id,
                        fill_price=f.price,
                        fill_qty=f.qty,
                        fee_amount=f.fee,
                        fee_asset=f.fee_asset,
                        liquidity=f.liquidity,
                    )

                # 5. Filled lifecycle
                sot_repo.append_order_event(
                    self.db,
                    order_id=order.id,
                    event_type="FILLED",
                )

                sot_repo.update_order_status(
                    self.db,
                    order_id=order.id,
                    status="FILLED",
                )

                # 6. Derived data
                sot_repo.calculate_and_save_order_cost(
                    self.db,
                    order_id=order.id,
                )

                sot_repo.calculate_and_save_order_pnl(
                    self.db,
                    order_id=order.id,
                    market_price=market_price,
                )

                return order.id

        except Exception as e:
            # FAILED path (best effort)
            try:
                sot_repo.append_order_event(
                    self.db,
                    order_id=order.id,
                    event_type="FAILED",
                    payload=str(e),
                )
                sot_repo.update_order_status(
                    self.db,
                    order_id=order.id,
                    status="FAILED",
                )
            except Exception:
                pass
            raise
