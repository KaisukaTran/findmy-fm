"""
Pyramid DCA Strategy Session.

Implements a wave-based dollar-cost averaging strategy where:
- Each wave increases quantity by 1 pip
- Each wave price decreases by distance_pct
- Take profit triggers when market price > avg_price * (1 + tp_pct)
- Timeout stops new waves if fills are too slow

Example:
    session = PyramidSession(
        symbol="BTC",
        entry_price=50000.0,
        distance_pct=2.0,
        max_waves=10,
        isolated_fund=1000.0,
        tp_pct=3.0,
        timeout_x_min=30,
        gap_y_min=5,
    )
    session.start()  # Queue wave 0
    session.on_fill(fill_event)  # Process fill, queue next wave
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, List, Dict, Any
import logging

from src.findmy.services.market_data import get_exchange_info, get_current_prices
from src.findmy.config import settings

logger = logging.getLogger(__name__)


class PyramidSessionStatus(Enum):
    """Status of a pyramid session."""
    PENDING = "pending"        # Created but not started
    ACTIVE = "active"          # Running, waiting for fills
    STOPPED = "stopped"        # Stopped by timeout or manual stop
    COMPLETED = "completed"    # All waves filled or TP triggered
    TP_TRIGGERED = "tp_triggered"  # Take profit executed


@dataclass
class WaveInfo:
    """Information about a single wave in the pyramid."""
    wave_num: int
    quantity: float
    target_price: float
    status: str = "pending"      # pending, sent, filled, cancelled
    filled_qty: float = 0.0
    filled_price: float = 0.0
    filled_time: Optional[datetime] = None
    pending_order_id: Optional[int] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API/storage."""
        return {
            "wave_num": self.wave_num,
            "quantity": self.quantity,
            "target_price": self.target_price,
            "status": self.status,
            "filled_qty": self.filled_qty,
            "filled_price": self.filled_price,
            "filled_time": self.filled_time.isoformat() if self.filled_time else None,
            "pending_order_id": self.pending_order_id,
        }


@dataclass
class PyramidSession:
    """
    Pyramid DCA Strategy Session.
    
    Manages a series of buy orders (waves) with increasing quantity and decreasing price.
    Each wave qty = (wave_num + 1) * pip_size.
    Each wave price = entry_price * (1 - distance_pct/100) ^ wave_num.
    
    Attributes:
        id: Unique session identifier (set by manager/DB)
        symbol: Trading pair symbol (e.g., "BTC")
        entry_price: Starting price for wave 0
        distance_pct: Price decrease % per wave (e.g., 2.0 for 2%)
        max_waves: Maximum number of waves to send
        isolated_fund: Fund allocated for this session (not deducted from main)
        tp_pct: Take profit % above avg price (e.g., 3.0 for 3%)
        timeout_x_min: Stop if no fill for X minutes
        gap_y_min: Minimum time between fills before timeout applies
    """
    
    # Session parameters
    symbol: str
    entry_price: float
    distance_pct: float
    max_waves: int
    isolated_fund: float
    tp_pct: float
    timeout_x_min: float
    gap_y_min: float
    
    # Session state
    id: Optional[int] = None
    status: PyramidSessionStatus = field(default=PyramidSessionStatus.PENDING)
    current_wave: int = 0
    waves: List[WaveInfo] = field(default_factory=list)
    
    # Calculated values
    avg_price: float = 0.0
    total_filled_qty: float = 0.0
    total_cost: float = 0.0
    
    # Timestamps
    start_time: Optional[datetime] = None
    last_fill_time: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    
    # Exchange info (cached)
    _min_qty: float = field(default=0.00001, repr=False)
    _step_size: float = field(default=0.00001, repr=False)
    _price_precision: int = field(default=2, repr=False)
    
    def __post_init__(self):
        """Validate inputs and initialize exchange info."""
        self._validate_inputs()
        self._load_exchange_info()
    
    def _validate_inputs(self) -> None:
        """Validate session parameters."""
        if not self.symbol:
            raise ValueError("Symbol is required")
        if self.entry_price <= 0:
            raise ValueError(f"Entry price must be positive: {self.entry_price}")
        if self.distance_pct <= 0 or self.distance_pct >= 100:
            raise ValueError(f"Distance must be 0-100%: {self.distance_pct}")
        if self.max_waves < 1:
            raise ValueError(f"Max waves must be >= 1: {self.max_waves}")
        if self.isolated_fund <= 0:
            raise ValueError(f"Isolated fund must be positive: {self.isolated_fund}")
        if self.tp_pct <= 0:
            raise ValueError(f"TP % must be positive: {self.tp_pct}")
        if self.timeout_x_min <= 0:
            raise ValueError(f"Timeout must be positive: {self.timeout_x_min}")
        if self.gap_y_min < 0:
            raise ValueError(f"Gap must be non-negative: {self.gap_y_min}")
    
    def _load_exchange_info(self) -> None:
        """Load exchange info for symbol (lot size, precision)."""
        try:
            info = get_exchange_info(self.symbol)
            self._min_qty = info.get("minQty", 0.00001)
            self._step_size = info.get("stepSize", 0.00001)
            # Calculate price precision from entry price
            self._price_precision = self._calculate_price_precision()
        except Exception as e:
            logger.warning(f"Failed to load exchange info for {self.symbol}: {e}")
    
    def _calculate_price_precision(self) -> int:
        """Calculate price precision based on entry price magnitude."""
        if self.entry_price >= 10000:
            return 2  # BTC-like
        elif self.entry_price >= 100:
            return 4  # ETH-like
        else:
            return 6  # Small altcoins
    
    @property
    def pip_size(self) -> float:
        """Calculate pip size: pip_multiplier × minQty."""
        return settings.pip_multiplier * self._min_qty
    
    @property
    def estimated_tp_price(self) -> float:
        """Calculate estimated TP price based on current avg price."""
        if self.avg_price <= 0:
            return self.entry_price * (1 + self.tp_pct / 100)
        return self.avg_price * (1 + self.tp_pct / 100)
    
    @property
    def used_fund(self) -> float:
        """Calculate total fund used (total cost of filled waves)."""
        return self.total_cost
    
    @property
    def remaining_fund(self) -> float:
        """Calculate remaining available fund."""
        return max(0, self.isolated_fund - self.total_cost)
    
    def generate_wave(self, wave_num: int) -> WaveInfo:
        """
        Generate wave order parameters.
        
        Formula:
            qty = (wave_num + 1) × pip_size
            price = entry_price × (1 - distance_pct/100)^wave_num
        
        Args:
            wave_num: Wave number (0-indexed)
        
        Returns:
            WaveInfo with calculated qty and price
        """
        # Calculate quantity: (wave_num + 1) pips
        raw_qty = (wave_num + 1) * self.pip_size
        # Round to step size
        qty = round(raw_qty / self._step_size) * self._step_size
        qty = max(qty, self._min_qty)
        
        # Calculate price: entry × (1 - distance%)^wave_num
        distance_factor = 1 - (self.distance_pct / 100)
        raw_price = self.entry_price * (distance_factor ** wave_num)
        price = round(raw_price, self._price_precision)
        
        return WaveInfo(
            wave_num=wave_num,
            quantity=qty,
            target_price=price,
            status="pending",
        )
    
    def estimate_total_cost(self, num_waves: Optional[int] = None) -> float:
        """
        Estimate total cost for N waves (for fund planning).
        
        Args:
            num_waves: Number of waves to estimate (default: max_waves)
        
        Returns:
            Estimated total cost in quote currency
        """
        n = num_waves or self.max_waves
        total = 0.0
        for i in range(n):
            wave = self.generate_wave(i)
            total += wave.quantity * wave.target_price
        return total
    
    def start(self) -> Optional[Dict[str, Any]]:
        """
        Start the pyramid session by generating and queuing wave 0.
        
        Returns:
            Order dict for wave 0 to be queued, or None if cannot start
        """
        if self.status != PyramidSessionStatus.PENDING:
            logger.warning(f"Session {self.id} already started (status={self.status})")
            return None
        
        # Validate fund is sufficient for at least wave 0
        wave_0 = self.generate_wave(0)
        wave_0_cost = wave_0.quantity * wave_0.target_price
        
        if wave_0_cost > self.isolated_fund:
            logger.error(
                f"Insufficient fund for wave 0: need {wave_0_cost:.4f}, have {self.isolated_fund:.4f}"
            )
            return None
        
        self.start_time = datetime.utcnow()
        self.status = PyramidSessionStatus.ACTIVE
        self.current_wave = 0
        
        # Store wave info
        wave_0.status = "sent"
        self.waves.append(wave_0)
        
        logger.info(
            f"Starting pyramid session {self.id}: {self.symbol} @ {self.entry_price}, "
            f"waves=0/{self.max_waves}, fund={self.isolated_fund}"
        )
        
        # Return order dict for pending queue
        return self._wave_to_order(wave_0)
    
    def _wave_to_order(self, wave: WaveInfo) -> Dict[str, Any]:
        """Convert wave to order dict for pending queue."""
        return {
            "symbol": self.symbol,
            "side": "BUY",
            "quantity": wave.quantity,
            "price": wave.target_price,
            "order_type": "LIMIT",
            "source": "kss",
            "source_ref": f"pyramid:{self.id}:wave:{wave.wave_num}",
            "strategy_name": f"Pyramid_{self.symbol}",
            "note": f"Pyramid wave {wave.wave_num}/{self.max_waves}",
        }
    
    def on_fill(
        self, 
        wave_num: int, 
        filled_qty: float, 
        filled_price: float,
        current_market_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Process a wave fill event.
        
        Updates session state, recalculates avg price, checks TP condition,
        and generates next wave if applicable.
        
        Args:
            wave_num: Wave number that was filled
            filled_qty: Actual filled quantity
            filled_price: Actual fill price
            current_market_price: Current market price for TP check (fetched if None)
        
        Returns:
            Dict with action results:
            {
                "action": "next_wave" | "tp_triggered" | "stopped" | "completed" | "none",
                "order": order dict if next wave or TP order needed,
                "message": description of what happened,
            }
        """
        if self.status != PyramidSessionStatus.ACTIVE:
            return {"action": "none", "message": f"Session not active: {self.status}"}
        
        # Find the wave
        wave = self._get_wave(wave_num)
        if not wave:
            return {"action": "none", "message": f"Wave {wave_num} not found"}
        
        # Update wave info
        now = datetime.utcnow()
        wave.status = "filled"
        wave.filled_qty = filled_qty
        wave.filled_price = filled_price
        wave.filled_time = now
        
        # Update session totals
        fill_cost = filled_qty * filled_price
        self.total_filled_qty += filled_qty
        self.total_cost += fill_cost
        
        # Recalculate average price
        if self.total_filled_qty > 0:
            self.avg_price = self.total_cost / self.total_filled_qty
        
        self.last_fill_time = now
        
        logger.info(
            f"Pyramid {self.id} wave {wave_num} filled: "
            f"{filled_qty} @ {filled_price}, avg={self.avg_price:.4f}"
        )
        
        # Get current market price for TP check
        if current_market_price is None:
            prices = get_current_prices([self.symbol])
            current_market_price = prices.get(self.symbol, 0)
        
        # Check TP condition
        tp_result = self.check_tp(current_market_price)
        if tp_result:
            return tp_result
        
        # Check timeout condition
        if self._check_timeout():
            self.status = PyramidSessionStatus.STOPPED
            return {
                "action": "stopped",
                "message": f"Session stopped: timeout ({self.timeout_x_min} min without fill)",
            }
        
        # Generate next wave if not at max
        next_wave_num = wave_num + 1
        if next_wave_num >= self.max_waves:
            # All waves sent, wait for fills or TP
            return {
                "action": "none",
                "message": f"All {self.max_waves} waves sent, waiting for fills or TP",
            }
        
        # Check fund availability for next wave
        next_wave = self.generate_wave(next_wave_num)
        next_cost = next_wave.quantity * next_wave.target_price
        
        if next_cost > self.remaining_fund:
            logger.warning(
                f"Insufficient fund for wave {next_wave_num}: "
                f"need {next_cost:.4f}, have {self.remaining_fund:.4f}"
            )
            return {
                "action": "none",
                "message": f"Insufficient fund for wave {next_wave_num}",
            }
        
        # Queue next wave
        self.current_wave = next_wave_num
        next_wave.status = "sent"
        self.waves.append(next_wave)
        
        return {
            "action": "next_wave",
            "order": self._wave_to_order(next_wave),
            "message": f"Queued wave {next_wave_num} @ {next_wave.target_price}",
        }
    
    def check_tp(self, current_market_price: float) -> Optional[Dict[str, Any]]:
        """
        Check if take profit condition is met.
        
        TP triggers when: current_price > avg_price × (1 + tp_pct/100)
        
        Args:
            current_market_price: Current market price
        
        Returns:
            Dict with TP order if triggered, None otherwise
        """
        if self.total_filled_qty <= 0:
            return None
        
        if current_market_price <= 0:
            return None
        
        tp_price = self.avg_price * (1 + self.tp_pct / 100)
        
        if current_market_price >= tp_price:
            self.status = PyramidSessionStatus.TP_TRIGGERED
            
            logger.info(
                f"Pyramid {self.id} TP triggered: market {current_market_price} >= "
                f"TP {tp_price:.4f} (avg={self.avg_price:.4f}, tp%={self.tp_pct})"
            )
            
            # Generate market sell order (bypass wave limit, taker)
            tp_order = {
                "symbol": self.symbol,
                "side": "SELL",
                "quantity": self.total_filled_qty,
                "price": 0,  # Market order
                "order_type": "MARKET",
                "source": "kss",
                "source_ref": f"pyramid:{self.id}:tp",
                "strategy_name": f"Pyramid_{self.symbol}",
                "note": f"Pyramid TP: sell {self.total_filled_qty} @ market (avg={self.avg_price:.4f})",
            }
            
            return {
                "action": "tp_triggered",
                "order": tp_order,
                "message": f"TP triggered at {current_market_price}, selling {self.total_filled_qty}",
            }
        
        return None
    
    def _check_timeout(self) -> bool:
        """
        Check if timeout condition is met.
        
        Timeout triggers when:
        - Time since last fill > timeout_x_min
        - AND last fill gap < gap_y_min
        
        Returns:
            True if session should be stopped due to timeout
        """
        if not self.last_fill_time:
            return False
        
        now = datetime.utcnow()
        time_since_last_fill = (now - self.last_fill_time).total_seconds() / 60  # minutes
        
        if time_since_last_fill <= self.timeout_x_min:
            return False
        
        # Check gap condition (time between last two fills)
        filled_waves = [w for w in self.waves if w.status == "filled" and w.filled_time]
        if len(filled_waves) < 2:
            return True  # No gap to check, timeout applies
        
        # Sort by fill time
        filled_waves.sort(key=lambda w: w.filled_time)
        last_two = filled_waves[-2:]
        gap_minutes = (last_two[1].filled_time - last_two[0].filled_time).total_seconds() / 60
        
        return gap_minutes < self.gap_y_min
    
    def _get_wave(self, wave_num: int) -> Optional[WaveInfo]:
        """Get wave by number."""
        for wave in self.waves:
            if wave.wave_num == wave_num:
                return wave
        return None
    
    def adjust_params(
        self,
        max_waves: Optional[int] = None,
        isolated_fund: Optional[float] = None,
        tp_pct: Optional[float] = None,
        distance_pct: Optional[float] = None,
        timeout_x_min: Optional[float] = None,
        gap_y_min: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Adjust session parameters live.
        
        Only allows changes that make sense for an active session:
        - max_waves: Can increase/decrease (but not below current wave)
        - isolated_fund: Can increase (adding more fund)
        - tp_pct: Can adjust up/down
        - distance_pct: Can adjust (affects future waves only)
        - timeout_x_min: Can adjust
        - gap_y_min: Can adjust
        
        Args:
            max_waves: New max waves limit
            isolated_fund: New isolated fund amount
            tp_pct: New take profit percentage
            distance_pct: New distance percentage
            timeout_x_min: New timeout minutes
            gap_y_min: New gap minutes
        
        Returns:
            Dict with changes made
        """
        changes = {}
        
        if max_waves is not None:
            if max_waves < self.current_wave + 1:
                logger.warning(
                    f"Cannot set max_waves={max_waves} below current wave {self.current_wave}"
                )
            else:
                self.max_waves = max_waves
                changes["max_waves"] = max_waves
        
        if isolated_fund is not None:
            if isolated_fund < self.total_cost:
                logger.warning(
                    f"Cannot set isolated_fund={isolated_fund} below used cost {self.total_cost}"
                )
            else:
                self.isolated_fund = isolated_fund
                changes["isolated_fund"] = isolated_fund
        
        if tp_pct is not None:
            if tp_pct <= 0:
                logger.warning(f"Invalid tp_pct={tp_pct}, must be positive")
            else:
                self.tp_pct = tp_pct
                changes["tp_pct"] = tp_pct
        
        if distance_pct is not None:
            if distance_pct <= 0 or distance_pct >= 100:
                logger.warning(f"Invalid distance_pct={distance_pct}, must be 0-100")
            else:
                self.distance_pct = distance_pct
                changes["distance_pct"] = distance_pct
        
        if timeout_x_min is not None:
            if timeout_x_min <= 0:
                logger.warning(f"Invalid timeout_x_min={timeout_x_min}, must be positive")
            else:
                self.timeout_x_min = timeout_x_min
                changes["timeout_x_min"] = timeout_x_min
        
        if gap_y_min is not None:
            if gap_y_min < 0:
                logger.warning(f"Invalid gap_y_min={gap_y_min}, must be non-negative")
            else:
                self.gap_y_min = gap_y_min
                changes["gap_y_min"] = gap_y_min
        
        if changes:
            logger.info(f"Pyramid {self.id} params adjusted: {changes}")
        
        return changes
    
    def stop(self, reason: str = "manual") -> None:
        """Stop the session manually."""
        if self.status == PyramidSessionStatus.ACTIVE:
            self.status = PyramidSessionStatus.STOPPED
            logger.info(f"Pyramid {self.id} stopped: {reason}")
    
    def get_status(self) -> Dict[str, Any]:
        """
        Get full session status for API/dashboard.
        
        Returns:
            Dict with all session info
        """
        filled_waves = [w for w in self.waves if w.status == "filled"]
        pending_waves = [w for w in self.waves if w.status in ("pending", "sent")]
        
        # Calculate unrealized PnL if we have positions
        unrealized_pnl = 0.0
        current_price = 0.0
        if self.total_filled_qty > 0:
            prices = get_current_prices([self.symbol])
            current_price = prices.get(self.symbol, 0)
            if current_price > 0:
                market_value = self.total_filled_qty * current_price
                unrealized_pnl = market_value - self.total_cost
        
        return {
            "id": self.id,
            "symbol": self.symbol,
            "status": self.status.value,
            
            # Parameters
            "entry_price": self.entry_price,
            "distance_pct": self.distance_pct,
            "max_waves": self.max_waves,
            "isolated_fund": self.isolated_fund,
            "tp_pct": self.tp_pct,
            "timeout_x_min": self.timeout_x_min,
            "gap_y_min": self.gap_y_min,
            
            # Progress
            "current_wave": self.current_wave,
            "filled_waves_count": len(filled_waves),
            "pending_waves_count": len(pending_waves),
            
            # Position
            "total_filled_qty": self.total_filled_qty,
            "avg_price": self.avg_price,
            "total_cost": self.total_cost,
            "used_fund": self.used_fund,
            "remaining_fund": self.remaining_fund,
            
            # Market & PnL
            "current_price": current_price,
            "estimated_tp_price": self.estimated_tp_price,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": (unrealized_pnl / self.total_cost * 100) if self.total_cost > 0 else 0,
            
            # Timestamps
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "last_fill_time": self.last_fill_time.isoformat() if self.last_fill_time else None,
            "created_at": self.created_at.isoformat(),
            
            # Waves detail
            "waves": [w.to_dict() for w in self.waves],
        }
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage/serialization."""
        return {
            "id": self.id,
            "symbol": self.symbol,
            "entry_price": self.entry_price,
            "distance_pct": self.distance_pct,
            "max_waves": self.max_waves,
            "isolated_fund": self.isolated_fund,
            "tp_pct": self.tp_pct,
            "timeout_x_min": self.timeout_x_min,
            "gap_y_min": self.gap_y_min,
            "status": self.status.value,
            "current_wave": self.current_wave,
            "avg_price": self.avg_price,
            "total_filled_qty": self.total_filled_qty,
            "total_cost": self.total_cost,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "last_fill_time": self.last_fill_time.isoformat() if self.last_fill_time else None,
            "created_at": self.created_at.isoformat(),
            "waves": [w.to_dict() for w in self.waves],
        }
