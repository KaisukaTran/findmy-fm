"""Pip sizing service for calculating order quantities based on pips and min lot size."""

from src.findmy.config import settings
from src.findmy.services.market_data import get_exchange_info


def calculate_order_qty(symbol: str, pips: float = 1.0) -> float:
    """
    Calculate order quantity based on pips and minimum lot size.

    Formula: qty = pips × pip_multiplier × minQty, rounded to stepSize

    Args:
        symbol: Trading pair symbol (e.g., "BTC", "ETH")
        pips: Number of pips (default: 1.0)

    Returns:
        Order quantity rounded to exchange stepSize

    Example:
        # With pip_multiplier=2.0, BTC minQty=0.00001
        qty = calculate_order_qty("BTC", pips=1.0)
        # Returns: 0.00002 (1 × 2.0 × 0.00001)

        qty = calculate_order_qty("BTC", pips=5.0)
        # Returns: 0.0001 (5 × 2.0 × 0.00001)
    """
    # Get exchange info (lot size, step size)
    info = get_exchange_info(symbol)
    min_qty = info.get("minQty", 0.00001)
    step_size = info.get("stepSize", 0.00001)

    # Calculate base quantity: pips × multiplier × minQty
    qty = pips * settings.pip_multiplier * min_qty

    # Round to step size
    qty = round(qty / step_size) * step_size

    # Ensure minimum qty
    if qty < min_qty:
        qty = min_qty

    return qty


def get_pip_value(symbol: str, quantity: float, current_price: float) -> float:
    """
    Calculate the value (in USDT) of one pip for a given quantity.

    One pip = pip_multiplier × minQty

    Args:
        symbol: Trading pair symbol
        quantity: Order quantity
        current_price: Current price in USDT

    Returns:
        Value in USDT of one pip
    """
    info = get_exchange_info(symbol)
    min_qty = info.get("minQty", 0.00001)
    one_pip_qty = settings.pip_multiplier * min_qty
    pip_value = one_pip_qty * current_price
    return pip_value


def validate_order_qty(symbol: str, quantity: float) -> tuple[bool, str]:
    """
    Validate order quantity against exchange limits.

    Args:
        symbol: Trading pair symbol
        quantity: Order quantity to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    info = get_exchange_info(symbol)
    min_qty = info.get("minQty", 0.00001)
    max_qty = info.get("maxQty", 10000.0)
    step_size = info.get("stepSize", 0.00001)

    # Check minimum
    if quantity < min_qty:
        return False, f"Quantity {quantity} below minimum {min_qty}"

    # Check maximum
    if quantity > max_qty:
        return False, f"Quantity {quantity} exceeds maximum {max_qty}"

    # Check step size alignment
    # Use division to avoid floating point issues with modulo
    if step_size > 0:
        quotient = quantity / step_size
        # Check if it's close to an integer (allowing small floating point errors)
        if abs(quotient - round(quotient)) > 1e-9:
            return False, f"Quantity {quantity} not aligned with step size {step_size}"

    return True, ""
