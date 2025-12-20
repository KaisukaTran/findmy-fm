from dataclasses import dataclass
from datetime import datetime


@dataclass
class MockFill:
    price: float
    qty: float
    fee: float
    fee_asset: str
    liquidity: str
    filled_at: datetime


class MockExchangeClient:
    """
    Mock exchange client.
    Does NOT connect to real exchange.
    """

    def execute_order(self, *, symbol: str, side: str, quantity: float):
        # simple deterministic mock
        price = 43000.0
        fee = price * quantity * 0.001  # 0.1% fee

        return [
            MockFill(
                price=price,
                qty=quantity,
                fee=fee,
                fee_asset="USDT",
                liquidity="TAKER",
                filled_at=datetime.utcnow(),
            )
        ]
