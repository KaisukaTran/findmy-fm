"""Risk management service for position sizing and daily loss limits."""

from datetime import datetime, timedelta
from src.findmy.config import settings
from services.ts.db import SessionLocal
from services.ts.models import Trade


class RiskCheckResult:
    """Result of a risk check."""

    def __init__(self, passed: bool, violation: str = None):
        """
        Initialize risk check result.

        Args:
            passed: Whether risk check passed
            violation: Description of violation if failed
        """
        self.passed = passed
        self.violation = violation or ""

    def __bool__(self):
        """Allow using result in boolean context."""
        return self.passed

    def __str__(self):
        """String representation."""
        if self.passed:
            return "✓ Risk check passed"
        return f"✗ Risk check failed: {self.violation}"


def get_account_equity(db_session=None) -> float:
    """
    Get current account equity from trade service.

    Args:
        db_session: Optional database session. If None, creates new session.

    Returns:
        Account equity in USDT
    """
    close_session = db_session is None
    if close_session:
        db_session = SessionLocal()

    try:
        # For now, return a default value (10,000 USDT)
        # In production, this would fetch from account service
        return 10000.0
    finally:
        if close_session:
            db_session.close()


def get_current_exposure(symbol: str, db_session=None) -> tuple[float, float]:
    """
    Calculate current position exposure for a symbol.

    Args:
        symbol: Trading pair symbol
        db_session: Optional database session

    Returns:
        Tuple of (quantity, exposure_pct)
    """
    close_session = db_session is None
    if close_session:
        db_session = SessionLocal()

    try:
        # Query open trades for this symbol
        trades = db_session.query(Trade).filter(
            Trade.symbol == symbol,
            Trade.status == "OPEN"
        ).all()

        total_qty = sum(t.entry_qty for t in trades)
        equity = get_account_equity(db_session)

        # Calculate exposure as % of equity
        # Assume current price ≈ entry price for simplicity
        exposure_value = total_qty * (sum(t.entry_price * t.entry_qty for t in trades) / max(total_qty, 1))
        exposure_pct = (exposure_value / equity * 100) if equity > 0 else 0

        return total_qty, exposure_pct

    finally:
        if close_session:
            db_session.close()


def get_daily_loss(db_session=None) -> float:
    """
    Calculate realized loss from trades closed today.

    Args:
        db_session: Optional database session

    Returns:
        Daily realized loss in USDT (positive number for losses)
    """
    close_session = db_session is None
    if close_session:
        db_session = SessionLocal()

    try:
        today = datetime.utcnow().date()
        today_start = datetime.combine(today, datetime.min.time())
        today_end = datetime.combine(today, datetime.max.time())

        # Query closed trades from today
        trades = db_session.query(Trade).filter(
            Trade.status == "CLOSED",
            Trade.exit_time >= today_start,
            Trade.exit_time <= today_end,
            Trade.realized_pnl < 0  # Only losses
        ).all()

        total_loss = sum(abs(t.realized_pnl) for t in trades)
        return total_loss

    finally:
        if close_session:
            db_session.close()


def check_position_size(
    symbol: str, proposed_qty: float, db_session=None
) -> RiskCheckResult:
    """
    Check if adding position would exceed max position size %.

    Args:
        symbol: Trading pair symbol
        proposed_qty: Proposed additional quantity
        db_session: Optional database session

    Returns:
        RiskCheckResult with pass/fail and violation reason
    """
    close_session = db_session is None
    if close_session:
        db_session = SessionLocal()

    try:
        current_qty, current_exposure_pct = get_current_exposure(symbol, db_session)
        equity = get_account_equity(db_session)

        # Estimate new exposure (assume current price = entry price)
        # This is simplified; in production get current price
        new_qty = current_qty + proposed_qty
        # Rough estimate: new_exposure_pct = (new_qty / equity) * entry_price
        # For now, use simple assumption
        avg_price = 100  # Placeholder, should use actual market price

        new_exposure_value = new_qty * avg_price
        new_exposure_pct = (new_exposure_value / equity * 100) if equity > 0 else 0

        if new_exposure_pct > settings.max_position_size_pct:
            violation = (
                f"Position size {new_exposure_pct:.1f}% exceeds max "
                f"{settings.max_position_size_pct:.1f}%"
            )
            return RiskCheckResult(False, violation)

        return RiskCheckResult(True)

    finally:
        if close_session:
            db_session.close()


def check_daily_loss(db_session=None) -> RiskCheckResult:
    """
    Check if daily loss is within limits.

    Args:
        db_session: Optional database session

    Returns:
        RiskCheckResult with pass/fail and violation reason
    """
    close_session = db_session is None
    if close_session:
        db_session = SessionLocal()

    try:
        daily_loss = get_daily_loss(db_session)
        equity = get_account_equity(db_session)

        daily_loss_pct = (daily_loss / equity * 100) if equity > 0 else 0

        if daily_loss_pct > settings.max_daily_loss_pct:
            violation = (
                f"Daily loss {daily_loss_pct:.1f}% exceeds max "
                f"{settings.max_daily_loss_pct:.1f}%"
            )
            return RiskCheckResult(False, violation)

        return RiskCheckResult(True)

    finally:
        if close_session:
            db_session.close()


def check_all_risks(
    symbol: str, proposed_qty: float, db_session=None
) -> tuple[bool, list[str]]:
    """
    Run all risk checks before order execution.

    Args:
        symbol: Trading pair symbol
        proposed_qty: Proposed quantity
        db_session: Optional database session

    Returns:
        Tuple of (all_passed, [violations])
    """
    close_session = db_session is None
    if close_session:
        db_session = SessionLocal()

    try:
        violations = []

        # Check position size
        pos_check = check_position_size(symbol, proposed_qty, db_session)
        if not pos_check:
            violations.append(pos_check.violation)

        # Check daily loss
        loss_check = check_daily_loss(db_session)
        if not loss_check:
            violations.append(loss_check.violation)

        return len(violations) == 0, violations

    finally:
        if close_session:
            db_session.close()
