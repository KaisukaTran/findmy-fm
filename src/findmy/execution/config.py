"""
Execution configuration for paper trading simulator.

Controls:
- Partial fill behavior
- Fees and slippage models
- Execution delays
- Stop-loss triggers
"""

from dataclasses import dataclass
from typing import Literal
import random


@dataclass
class PartialFillConfig:
    """Configuration for partial order fills."""
    enabled: bool = True
    fill_type: Literal["random", "fixed", "progressive"] = "random"
    fill_percentage: float = 0.75  # If fixed, fill this % of order
    min_fill_pct: float = 0.25  # Min % to fill (for random)
    max_fill_pct: float = 0.95  # Max % to fill (for random)
    
    def get_fill_qty(self, total_qty: float) -> float:
        """Calculate fill quantity based on configuration."""
        if not self.enabled:
            return total_qty
        
        if self.fill_type == "fixed":
            return total_qty * self.fill_percentage
        elif self.fill_type == "progressive":
            # Each fill reduces remaining (simulates market absorption)
            return total_qty * random.uniform(self.min_fill_pct, self.max_fill_pct)
        else:  # random
            return total_qty * random.uniform(self.min_fill_pct, self.max_fill_pct)


@dataclass
class FeeConfig:
    """Configuration for trading fees."""
    maker_fee_pct: float = 0.001  # 0.1% maker fee
    taker_fee_pct: float = 0.001  # 0.1% taker fee
    enabled: bool = True
    
    def calculate_fee(self, notional_value: float, is_maker: bool = False) -> float:
        """Calculate fee for a trade."""
        if not self.enabled:
            return 0.0
        
        fee_pct = self.maker_fee_pct if is_maker else self.taker_fee_pct
        return notional_value * fee_pct


@dataclass
class SlippageConfig:
    """Configuration for market slippage."""
    enabled: bool = True
    slippage_type: Literal["fixed", "random", "percentage"] = "random"
    slippage_bps: float = 5.0  # Basis points (5 bps = 0.05%)
    min_slippage_bps: float = 1.0  # Min slippage in bps
    max_slippage_bps: float = 20.0  # Max slippage in bps
    
    def apply_slippage(self, price: float, side: str = "BUY") -> tuple[float, float]:
        """
        Apply slippage to price.
        
        Args:
            price: Original price
            side: BUY or SELL (determines direction of slippage)
        
        Returns:
            Tuple of (slipped_price, slippage_amount)
        """
        if not self.enabled:
            return price, 0.0
        
        if self.slippage_type == "fixed":
            slippage_bps = self.slippage_bps
        elif self.slippage_type == "percentage":
            # Slippage as % of price
            slippage_bps = (price * 0.0001) * 10000  # Convert to bps
        else:  # random
            slippage_bps = random.uniform(self.min_slippage_bps, self.max_slippage_bps)
        
        slippage_amount = price * (slippage_bps / 10000)
        
        # BUY orders get worse prices (higher), SELL orders get worse prices (lower)
        if side.upper() == "BUY":
            slipped_price = price + slippage_amount
        else:
            slipped_price = price - slippage_amount
        
        return slipped_price, slippage_amount


@dataclass
class LatencyConfig:
    """Configuration for execution latency simulation."""
    enabled: bool = False
    latency_ms: float = 0.0  # Milliseconds delay
    async_processing: bool = False  # Process in background
    
    def get_delay_seconds(self) -> float:
        """Get delay in seconds."""
        return self.latency_ms / 1000.0


@dataclass
class StopLossConfig:
    """Configuration for stop-loss orders."""
    enabled: bool = True
    market_fill: bool = True  # Execute as market order when triggered
    max_slippage_on_stop: float = 50.0  # Max acceptable slippage in bps


@dataclass
class ExecutionConfig:
    """Main execution configuration."""
    partial_fill: PartialFillConfig = None
    fees: FeeConfig = None
    slippage: SlippageConfig = None
    latency: LatencyConfig = None
    stop_loss: StopLossConfig = None
    
    def __post_init__(self):
        """Initialize with defaults if not provided."""
        if self.partial_fill is None:
            self.partial_fill = PartialFillConfig()
        if self.fees is None:
            self.fees = FeeConfig()
        if self.slippage is None:
            self.slippage = SlippageConfig()
        if self.latency is None:
            self.latency = LatencyConfig()
        if self.stop_loss is None:
            self.stop_loss = StopLossConfig()


# Default configuration instance
DEFAULT_CONFIG = ExecutionConfig()
