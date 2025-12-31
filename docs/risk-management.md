# Risk Management & Pip Sizing (v0.6.0)

## Overview

v0.6.0 introduces comprehensive **risk management** and **pip-based order sizing** for safer trading. The system enforces position limits and daily loss limits before orders are queued for approval.

## Pip Sizing System

### What are Pips?

In FINDMY FM, **1 pip** = **pip_multiplier × minQty**

- **pip_multiplier**: Configurable multiplier (default: 2.0)
- **minQty**: Binance's minimum order quantity for the symbol

**Example:**
- BTC/USDT has minQty = 0.00001
- With pip_multiplier = 2.0, 1 pip = 2.0 × 0.00001 = **0.00002 BTC**
- 5 pips = 5 × 0.00002 = **0.0001 BTC**

### Configuration

Edit your `.env` file or set environment variables:

```env
# Pip Sizing (v0.6.0)
PIP_MULTIPLIER=2.0

# Risk Management (v0.6.0)
MAX_POSITION_SIZE_PCT=10.0
MAX_DAILY_LOSS_PCT=5.0
```

### Calculating Order Quantity

#### Option 1: Direct Quantity

```python
from services.sot.pending_orders_service import queue_order

order, risk_note = queue_order(
    symbol="BTC",
    side="BUY",
    quantity=0.5,        # 0.5 BTC
    price=65000.0,
    source="excel",
)
```

#### Option 2: Pip-Based (Recommended)

```python
order, risk_note = queue_order(
    symbol="BTC",
    side="BUY",
    pips=10.0,          # 10 pips = 10 × 2.0 × 0.00001 = 0.0002 BTC
    price=65000.0,
    source="strategy",
)
```

### Pip Sizing API

```python
from services.risk import calculate_order_qty, get_pip_value, validate_order_qty

# Calculate quantity for N pips
qty = calculate_order_qty("BTC", pips=5.0)
# Returns: 0.0001 (5 × 2.0 × 0.00001)

# Get pip value in USD
from src.findmy.services.market_data import get_current_prices
prices = get_current_prices(["BTC"])
pip_value = get_pip_value("BTC", qty, prices["BTC"])
# Returns: dollar value of 1 pip

# Validate quantity against exchange limits
is_valid, error_msg = validate_order_qty("BTC", 0.0001)
# Returns: (True, "") if valid
```

## Risk Management System

### Position Size Limits

The system enforces **maximum position size as % of account equity**.

**Configuration:**
```env
MAX_POSITION_SIZE_PCT=10.0    # Max 10% of equity in one position
```

**How it works:**
1. Query current open positions for the symbol
2. Calculate exposure: `(position_value / account_equity) × 100%`
3. Reject if: `new_exposure_pct > MAX_POSITION_SIZE_PCT`

**Example:**
```
Account Equity: $10,000
Max Position Size: 10% = $1,000

Current BTC Position: $500 (5%)
New Order: $600
New Exposure: $1,100 (11%)
Result: ✗ REJECTED - Exceeds 10% limit
```

### Daily Loss Limits

The system enforces **maximum daily loss as % of account equity**.

**Configuration:**
```env
MAX_DAILY_LOSS_PCT=5.0        # Max 5% daily loss
```

**How it works:**
1. Query trades closed today with realized losses
2. Calculate daily loss: `sum(losses) / account_equity × 100%`
3. Reject new orders if: `daily_loss_pct > MAX_DAILY_LOSS_PCT`

**Example:**
```
Account Equity: $10,000
Max Daily Loss: 5% = $500

Trades Closed Today:
  - Trade 1: -$150 loss
  - Trade 2: -$200 loss
  Total: -$350 (3.5%)

New Order Would Loss: -$200
New Daily Loss: -$550 (5.5%)
Result: ✗ REJECTED - Would exceed 5% daily loss limit
```

### Risk Check Workflow

When creating an order:

1. **Calculate Quantity** (from pips or use provided qty)
2. **Run Risk Checks**:
   - Check position size limit
   - Check daily loss limit
3. **Queue Order**:
   - If all checks pass: Queue with `risk_note = None`
   - If check fails: Queue with `risk_note = "violation message"`
4. **Display in Pending Queue**:
   - Show risk violation in order notes
   - User can approve despite warning or reject

### Risk Check API

```python
from services.risk import (
    check_position_size,
    check_daily_loss,
    check_all_risks,
    RiskCheckResult,
)

# Check position size
result = check_position_size("BTC", proposed_qty=0.5)
if result.passed:
    print("Position size OK")
else:
    print(f"RISK: {result.violation}")

# Check daily loss
result = check_daily_loss()
if result.passed:
    print("Daily loss OK")
else:
    print(f"RISK: {result.violation}")

# Run all checks at once
all_passed, violations = check_all_risks("BTC", proposed_qty=0.5)
if not all_passed:
    for violation in violations:
        print(f"⚠️  {violation}")
```

## Dashboard Risk Metrics

The dashboard displays a **Risk Metrics** card showing:

### Portfolio Exposure
- **Current**: Current % of equity in open positions
- **Max**: Configured maximum (10%)
- **Progress bar**: Green < 5%, Yellow 5-8%, Red > 8%

### Daily Loss
- **Current**: Current realized loss today ($)
- **% of Equity**: Daily loss as % (max 5%)
- **Progress bar**: Green < 2.5%, Yellow 2.5-4%, Red > 4%

**Information Panel:**
- Pip Multiplier setting
- Position Size Limit
- Daily Loss Limit

## Example Workflows

### Workflow 1: Order Sizing with Pips

```python
from services.sot.pending_orders_service import queue_order

# User wants to trade 5 pips of BTC
# pip_multiplier = 2.0, BTC minQty = 0.00001
# qty = 5 × 2.0 × 0.00001 = 0.0001 BTC

order, risk_note = queue_order(
    symbol="BTC",
    side="BUY",
    pips=5.0,
    price=65000.0,
    source="strategy",
    strategy_name="MovingAverageStrategy",
    confidence=0.85,
)

# Dashboard shows:
# - Order: "BTC BUY 0.0001 @ $65,000"
# - Pips: 5.0
# - Confidence: 85%
# - Risk: "OK" (if no violations) or "Position size 15% exceeds max 10%" (if violation)
```

### Workflow 2: Risk Violation Handling

```python
# User attempts large order that violates position size limit
order, risk_note = queue_order(
    symbol="BTC",
    side="BUY",
    quantity=10.0,  # Very large order
    price=65000.0,
    source="excel",
)

# risk_note = "Position size 45.5% exceeds max 10%"
# Order is still queued, but with warning in notes
# User sees in dashboard:
#   - Order marked as "PENDING" with risk violation
#   - Can approve (override risk) or reject
#   - Risk note visible in order details
```

### Workflow 3: Daily Loss Limit

```python
# After several losing trades today
daily_loss = 600  # $600 loss (6% of $10k equity)

# New order triggers daily loss check
order, risk_note = queue_order(
    symbol="ETH",
    side="BUY",
    quantity=5.0,
    price=3500.0,
    source="strategy",
)

# risk_note = "Daily loss 6.0% exceeds max 5.0%"
# Order queued but marked with risk violation
# User must consciously approve or reject
```

## Configuration Guide

### Via Environment Variables

```bash
export PIP_MULTIPLIER=2.0
export MAX_POSITION_SIZE_PCT=10.0
export MAX_DAILY_LOSS_PCT=5.0
python -m uvicorn src.findmy.api.main:app
```

### Via .env File

```env
# Risk Management Settings
PIP_MULTIPLIER=2.0
MAX_POSITION_SIZE_PCT=10.0
MAX_DAILY_LOSS_PCT=5.0
```

### Via Config Class

```python
from src.findmy.config import settings

# Read current settings
print(settings.pip_multiplier)           # 2.0
print(settings.max_position_size_pct)    # 10.0
print(settings.max_daily_loss_pct)       # 5.0
```

## Testing

### Unit Tests

Run risk management tests:

```bash
pytest tests/test_risk_management.py -v

# Test categories:
# - TestPipSizing: 4 tests
# - TestRiskManagement: 9 tests
# - TestPendingOrdersWithPips: 3 tests
# - TestPytestTimeout: 3 tests
```

### Test Execution with Timeout

```bash
# All tests with 30s default timeout
pytest tests/ --timeout=30

# Specific test with extended timeout
pytest tests/test_risk_management.py::TestPipSizing -v --timeout=60

# Mark slow tests with @pytest.mark.timeout(300)
# Backtesting and Binance tests use 5-minute timeout
```

## Pytest Timeout Configuration

### Global Timeout: 30 seconds

Default timeout for all tests: **30 seconds**

Set in `pytest.ini`:
```ini
[pytest]
timeout = 30
timeout_method = thread
addopts = --timeout=30
```

### Marking Slow Tests

For tests that need more time:

```python
import pytest

# 60-second timeout
@pytest.mark.timeout(60)
def test_strategy_execution():
    pass

# 5-minute timeout for backtesting
@pytest.mark.timeout(300)
def test_backtest_simulation():
    pass

# No timeout
@pytest.mark.timeout(0)
def test_no_timeout_needed():
    pass
```

### Common Timeout Settings

- **Fast unit tests**: 10s (default applies)
- **Integration tests**: 30s (default applies)
- **Strategy backtests**: 60s
- **Binance API calls**: 30-60s
- **Full backtesting suite**: 300s (5 minutes)

## Best Practices

### 1. Use Pips for Consistency

```python
# ✓ Good: Consistent order sizing
order = queue_order("BTC", "BUY", pips=5.0, price=65000.0, source="strategy")

# ✗ Bad: Manual calculation prone to errors
qty = 5 * 2.0 * 0.00001  # 0.0001
order = queue_order("BTC", "BUY", quantity=qty, price=65000.0, source="strategy")
```

### 2. Monitor Risk Metrics

- Check dashboard Risk Metrics card regularly
- Alert if exposure approaches 8-9% of equity
- Review daily losses before market close

### 3. Set Appropriate Limits

- **Conservative**: 5% position size, 2% daily loss
- **Moderate**: 10% position size, 5% daily loss (default)
- **Aggressive**: 15% position size, 10% daily loss

### 4. Override Carefully

When risk check fails:
- Review the violation reason
- Verify market conditions haven't changed
- Add approval note explaining override
- Monitor closely after approval

## Future Enhancements

Planned improvements for v0.7.0:

- [ ] Account-level equity tracking (not hardcoded)
- [ ] Real-time portfolio exposure calculations
- [ ] Volatility-based dynamic position sizing
- [ ] Stop-loss automation based on position size
- [ ] Risk dashboard with historical charts
- [ ] Per-symbol risk limits (different for BTC vs altcoins)
- [ ] Risk exposure heatmap
- [ ] Correlation-based position limits
- [ ] VaR (Value at Risk) calculations

## Support

For issues or questions:
1. Check test examples in `tests/test_risk_management.py`
2. Review dashboard Risk Metrics card
3. Check pending orders for risk violation notes
4. Run risk checks manually to debug:

```python
from services.risk import check_all_risks

passed, violations = check_all_risks("BTC", 0.5)
for v in violations:
    print(f"Violation: {v}")
```
