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

NOTE: This is the preserved core strategy logic from the original codebase.
Only the import paths were changed for the lean `app/` package. The math is
intentionally untouched — see the `kss-spec` skill for the canonical formulas.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from app.config import settings
from app.market import get_current_prices, get_exchange_info

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
    filled_time: datetime | None = None
    pending_order_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
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
    id: int | None = None
    status: PyramidSessionStatus = field(default=PyramidSessionStatus.PENDING)
    current_wave: int = 0
    waves: list[WaveInfo] = field(default_factory=list)

    # Calculated values
    avg_price: float = 0.0
    total_filled_qty: float = 0.0
    total_cost: float = 0.0

    # Risk exits (0.0 = disabled / fall back to settings defaults resolved by service)
    sl_pct: float = 0.0
    trailing_pct: float = 0.0
    peak_price: float = 0.0

    # Timestamps
    start_time: datetime | None = None
    last_fill_time: datetime | None = None
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
        """Calculate pip size.

        Default (legacy): ``pip_multiplier × minQty``.

        Opt-in override: when ``settings.kss_first_wave_usd > 0`` and
        ``self.entry_price > 0``, returns ``kss_first_wave_usd / entry_price``
        so that wave-0 qty = (0+1) × pip_size and its notional ≈ the configured
        USD value.  Later waves keep the ``(n+1)×`` pyramid shape unchanged.
        Setting ``kss_first_wave_usd`` to 0 (default) leaves all behaviour
        identical to the legacy pip-based formula.
        """
        if settings.kss_first_wave_usd > 0 and self.entry_price > 0:
            return settings.kss_first_wave_usd / self.entry_price
        return settings.pip_multiplier * self._min_qty

    def _tp_target_pct(self) -> float:
        """Effective take-profit %: the session's ``tp_pct`` PLUS a fee buffer so every TP
        clears its round-trip fee with a margin (``costengine.tp_fee_buffer_pct`` — default
        +120% of the total buy+sell fee). Applies to BOTH paper and live."""
        from app import costengine

        return self.tp_pct + costengine.tp_fee_buffer_pct()

    @property
    def estimated_tp_price(self) -> float:
        """Estimated TP price = avg (or entry) × (1 + effective TP %), where the effective TP
        adds the fee buffer on top of tp_pct (see _tp_target_pct)."""
        base = self.avg_price if self.avg_price > 0 else self.entry_price
        return base * (1 + self._tp_target_pct() / 100)

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

    def estimate_total_cost(self, num_waves: int | None = None) -> float:
        """Estimate total cost for N waves (for fund planning)."""
        n = num_waves or self.max_waves
        total = 0.0
        for i in range(n):
            wave = self.generate_wave(i)
            total += wave.quantity * wave.target_price
        return total

    def start(self) -> dict[str, Any] | None:
        """Start the pyramid session by generating and queuing wave 0."""
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

    def _wave_to_order(self, wave: WaveInfo) -> dict[str, Any]:
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
        current_market_price: float | None = None,
    ) -> dict[str, Any]:
        """Process a wave fill event."""
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

    def check_tp(self, current_market_price: float) -> dict[str, Any] | None:
        """Check if take profit condition is met."""
        if self.total_filled_qty <= 0:
            return None

        if current_market_price <= 0:
            return None

        tp_price = self.avg_price * (1 + self._tp_target_pct() / 100)

        if current_market_price >= tp_price:
            self.status = PyramidSessionStatus.TP_TRIGGERED

            logger.info(
                f"Pyramid {self.id} TP triggered: market {current_market_price} >= "
                f"TP {tp_price:.4f} (avg={self.avg_price:.4f}, tp%={self.tp_pct}+fee buffer)"
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

    def check_stop(self, current_price: float) -> dict[str, Any] | None:
        """
        Check stop-loss and trailing-stop exit conditions.

        Updates self.peak_price on every call (high-water mark tracking).
        Returns the same ``{"action": ..., "order": {...}}`` shape as check_tp,
        or None when nothing triggers.  Stop-loss takes precedence over
        trailing-stop when both conditions are satisfied simultaneously.
        """
        if self.total_filled_qty <= 0:
            return None

        sl = self.sl_pct
        trail = self.trailing_pct

        # Update high-water mark unconditionally so callers can persist it.
        self.peak_price = max(self.peak_price, current_price)

        if sl <= 0 and trail <= 0:
            return None

        triggered_action: str | None = None

        # Hard stop-loss: price dropped below avg by sl%.
        if sl > 0 and current_price <= self.avg_price * (1 - sl / 100):
            triggered_action = "stop_loss"

        # Trailing stop (only when no hard SL trigger): position must be in
        # profit (peak > avg) and current price fell trail% from the peak.
        if triggered_action is None and trail > 0:
            if self.peak_price > self.avg_price and current_price <= self.peak_price * (1 - trail / 100):
                triggered_action = "trailing_stop"

        if triggered_action is None:
            return None

        suffix = "sl" if triggered_action == "stop_loss" else "trailing"
        logger.info(
            f"Pyramid {self.id} {triggered_action}: market {current_price} "
            f"(avg={self.avg_price:.4f}, peak={self.peak_price:.4f}, "
            f"sl%={sl}, trail%={trail})"
        )

        stop_order = {
            "symbol": self.symbol,
            "side": "SELL",
            "quantity": self.total_filled_qty,
            "price": 0,  # Market order
            "order_type": "MARKET",
            "source": "kss",
            "source_ref": f"pyramid:{self.id}:{suffix}",
            "strategy_name": f"Pyramid_{self.symbol}",
            "note": (
                f"Pyramid {triggered_action}: sell {self.total_filled_qty} @ market "
                f"(avg={self.avg_price:.4f})"
            ),
        }

        return {
            "action": triggered_action,
            "order": stop_order,
            "message": (
                f"{triggered_action} triggered at {current_price}, "
                f"selling {self.total_filled_qty}"
            ),
        }

    def _check_timeout(self) -> bool:
        """Check if timeout condition is met."""
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

    def _get_wave(self, wave_num: int) -> WaveInfo | None:
        """Get wave by number."""
        for wave in self.waves:
            if wave.wave_num == wave_num:
                return wave
        return None

    def adjust_params(  # noqa: C901  (preserved verbatim — many independent guarded fields)
        self,
        max_waves: int | None = None,
        isolated_fund: float | None = None,
        tp_pct: float | None = None,
        distance_pct: float | None = None,
        timeout_x_min: float | None = None,
        gap_y_min: float | None = None,
    ) -> dict[str, Any]:
        """Adjust session parameters live."""
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

    def get_status(self) -> dict[str, Any]:
        """Get full session status for API/dashboard."""
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

    def to_dict(self) -> dict[str, Any]:
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
