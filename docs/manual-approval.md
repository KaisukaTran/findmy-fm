# Manual Order Approval System (v0.5.0)

## Overview

FINDMY FM now requires **manual user approval for ALL orders** before execution. This safety feature ensures that no order bypasses the approval queue, protecting against accidental executions and market manipulation.

### Key Features

- **Mandatory Approval Queue**: All orders (Excel uploads, strategy signals, backtest orders) queue to pending_orders
- **Dashboard Integration**: Visual pending orders queue with approve/reject buttons
- **REST API**: Programmatic approval/rejection of orders
- **Audit Trail**: All approvals logged with timestamp and reviewer
- **Source Tracking**: Know where each order originated (excel, strategy, backtest)
- **Safety First**: Orders cannot execute until explicitly approved by user

## Architecture

### Database Model

```python
# services/sot/pending_orders.py

class PendingOrder(Base):
    """Model representing an order awaiting user approval."""
    
    __tablename__ = "pending_orders"
    
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)  # e.g., "BTC/USD"
    side = Column(String(10), nullable=False)    # BUY or SELL
    quantity = Column(Float, nullable=False)     # Order size
    price = Column(Float, nullable=False)        # Limit price
    order_type = Column(String(10), default="LIMIT")
    source = Column(String(50), nullable=False)  # "excel", "strategy", "backtest"
    
    status = Column(String(20), default="pending")  # pending, approved, rejected
    created_at = Column(DateTime, default=datetime.utcnow)
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(String(255), nullable=True)
    note = Column(Text, nullable=True)
    
    strategy_name = Column(String(100), nullable=True)  # For strategy orders
    confidence = Column(Float, nullable=True)  # Signal confidence 0-1
```

### Service Layer

```python
# services/sot/pending_orders_service.py

# Queue a new order for approval
queue_order(symbol, side, quantity, price, order_type, source, **kwargs)
  → Returns: PendingOrder object with ID

# List pending orders (with filters)
get_pending_orders(status=None, symbol=None, source=None)
  → Returns: List of PendingOrder objects

# Approve an order (mark for execution)
approve_order(order_id, reviewed_by="system")
  → Returns: Updated PendingOrder with approved status

# Reject an order (prevent execution)
reject_order(order_id, reason=None, reviewed_by="system")
  → Returns: Updated PendingOrder with rejected status

# Count pending orders
count_pending(symbol=None)
  → Returns: Integer count
```

## API Endpoints

### List Pending Orders

```http
GET /api/pending?status=pending&symbol=BTC/USD
```

**Query Parameters:**
- `status` (optional): Filter by "pending", "approved", or "rejected"
- `symbol` (optional): Filter by trading pair (e.g., "BTC/USD")

**Response:**
```json
[
  {
    "id": 1,
    "symbol": "BTC/USD",
    "side": "BUY",
    "quantity": 0.5,
    "price": 43000.0,
    "order_type": "LIMIT",
    "source": "excel",
    "status": "pending",
    "created_at": "2024-01-15T10:30:45.123Z",
    "reviewed_at": null,
    "reviewed_by": null,
    "note": null,
    "strategy_name": null,
    "confidence": null
  }
]
```

### Approve Order

```http
POST /api/pending/approve/{order_id}
```

**Request Body:**
```json
{
  "note": "Checked market conditions, proceeding"
}
```

**Response:**
```json
{
  "id": 1,
  "symbol": "BTC/USD",
  "side": "BUY",
  "quantity": 0.5,
  "price": 43000.0,
  "status": "approved",
  "reviewed_at": "2024-01-15T10:31:20.456Z",
  "reviewed_by": "user@example.com",
  "note": "Checked market conditions, proceeding"
}
```

### Reject Order

```http
POST /api/pending/reject/{order_id}
```

**Request Body:**
```json
{
  "reason": "Market volatility too high"
}
```

**Response:**
```json
{
  "id": 1,
  "symbol": "BTC/USD",
  "side": "BUY",
  "quantity": 0.5,
  "price": 43000.0,
  "status": "rejected",
  "reviewed_at": "2024-01-15T10:31:45.789Z",
  "reviewed_by": "user@example.com",
  "note": "Market volatility too high"
}
```

## Dashboard Usage

### Pending Orders Queue Section

The dashboard displays a **"Pending Orders Queue"** table with the following columns:

| Column | Description |
|--------|-------------|
| Symbol | Trading pair (e.g., BTC/USD) |
| Side | BUY or SELL |
| Quantity | Order size |
| Price | Limit price |
| Order Type | LIMIT, MARKET, etc. |
| Source | Origin (excel, strategy, backtest) |
| Status | pending, approved, rejected |
| Created | Timestamp when order was queued |
| Actions | Approve/Reject buttons |

### Approve/Reject Orders

1. **Approve Button (✓)**: Mark order for execution
   - Confirms order details
   - Sets reviewed_at timestamp
   - Order becomes eligible for execution

2. **Reject Button (×)**: Prevent order execution
   - Prompts for rejection reason
   - Prevents execution indefinitely
   - Maintains audit trail

### Pending Count Badge

The header displays a badge showing the count of pending orders that need review. Badge updates in real-time via WebSocket or polling.

## Workflow Examples

### Example 1: Excel Upload → Approval

```
User Action          System Response
─────────────────────────────────────
Upload Excel file    → 3 orders queued to pending_orders
                     → Dashboard shows 3 pending

User reviews prices  → All look good
Click "Approve" ×3   → 3 orders moved to "approved" status
                     → Orders eligible for execution
```

### Example 2: Strategy Signal → Approval

```
User Action          System Response
─────────────────────────────────────
Strategy runs        → Generates 5 BUY signals
                     → 5 orders queued with source="strategy"

User checks dashboard → Shows pending orders with
                        strategy name and confidence

User approves 3/5    → 3 orders moved to "approved"
Rejects 2            → 2 orders marked "rejected" with reason
```

### Example 3: Backtest-Generated Orders

```
User Action          System Response
─────────────────────────────────────
Run backtest         → Generates 10 orders for execution
                     → All queued with source="backtest"

Review backtest stats → Check Sharpe ratio, drawdown
                     → Review pending orders list

Approve all          → Batch approve button (future enhancement)
                     → All 10 orders become "approved"
```

## Safety Features

### 1. Source Attribution
Every pending order includes its source, helping you understand where it came from:
- `excel`: Manually uploaded from spreadsheet
- `strategy`: Generated by an automated strategy
- `backtest`: From a backtest run

### 2. Audit Trail
All approvals are logged with:
- `reviewed_by`: User ID of approver
- `reviewed_at`: Timestamp of approval decision
- `note`: Optional notes/reasoning

### 3. No Silent Failures
If order approval fails, you'll see error messages explaining why:
- Order already approved
- Order already rejected
- Order ID not found
- Invalid order status

### 4. Confidence Scoring
Strategy-generated orders include confidence level (0-1):
- 0.9-1.0: High confidence signal
- 0.7-0.9: Medium confidence signal
- 0.5-0.7: Lower confidence signal
- Help prioritize which orders to approve

## cURL Examples

### List All Pending Orders

```bash
curl -X GET http://localhost:8000/api/pending \
  -H "Content-Type: application/json"
```

### List Pending BTC Orders

```bash
curl -X GET "http://localhost:8000/api/pending?status=pending&symbol=BTC/USD" \
  -H "Content-Type: application/json"
```

### Approve Order #5

```bash
curl -X POST http://localhost:8000/api/pending/approve/5 \
  -H "Content-Type: application/json" \
  -d '{"note": "Verified with market analysis"}'
```

### Reject Order #3

```bash
curl -X POST http://localhost:8000/api/pending/reject/3 \
  -H "Content-Type: application/json" \
  -d '{"reason": "Slippage risk too high"}'
```

## Testing

### Unit Tests

The manual approval system includes comprehensive test coverage:

```bash
# Run pending orders tests
pytest tests/test_pending_orders.py -v

# Test classes:
# - TestPendingOrdersService (5 tests)
# - TestPendingOrdersAPI (5 tests)
# - TestPaperExecutionQueues (2 tests)
```

### Key Test Scenarios

1. **Queue Order**: Create pending order → verify in database
2. **Get Pending**: Filter by status/symbol → correct results
3. **Approve Order**: Mark as approved → timestamp set
4. **Reject Order**: Mark as rejected → reason captured
5. **Double Approval Prevention**: Approve twice → error on second
6. **Excel Integration**: Upload file → orders queued (not executed)
7. **API Endpoints**: All CRUD operations tested

## Migration Guide

If upgrading from v0.4.0:

### Step 1: Run Database Migration

```bash
# Apply migration 006_pending_orders
python -c "from db.migrations import pending_orders; pending_orders.migrate_up()"
```

This creates the `pending_orders` table with necessary indexes.

### Step 2: Update Workflows

- **Excel Uploads**: No code changes needed
  - Orders still queue normally
  - Now require approval before execution
  
- **Strategy Signals**: No code changes needed
  - Signals queue as pending orders
  - Dashboard shows pending queue

- **Backtests**: No code changes needed
  - Test orders queue for approval
  - Approve before executing live

### Step 3: Approval Workflow

Establish a review process:
1. Check pending orders dashboard regularly
2. Review price levels and market conditions
3. Approve orders meeting criteria
4. Reject if conditions changed

## Best Practices

### 1. Regular Review
- Check dashboard at least hourly during trading hours
- Don't let pending orders accumulate for >4 hours
- Set a reminder if pending count exceeds threshold

### 2. Add Notes
- Include reasoning in approval notes
- Helps with audit trail and future analysis
- Example: "Confirmed resistance at 43000, good entry"

### 3. Rejection Reasons
- Be specific about rejection reasons
- Helps identify patterns in rejected orders
- Example: "Spread too wide, waiting for tighter"

### 4. Strategy Tuning
- If strategy rejects orders frequently, retune signals
- Use confidence scores to filter low-quality signals
- Adjust strategy parameters in next iteration

### 5. Batch Processing
- When uploading large Excel files, review in batches
- Approve in logical groups (same symbol, similar price)
- Prevents accidental approval of mixed signals

## Troubleshooting

### Orders Not Showing in Pending Queue

**Cause**: May not have loaded yet
**Solution**: Refresh dashboard, check network tab

### Approval Button Not Working

**Cause**: API endpoint unreachable
**Solution**: 
```bash
# Verify API is running
curl http://localhost:8000/api/pending
```

### Orders Stuck in Pending

**Cause**: May not be reviewed yet
**Solution**: Check dashboard for pending orders badge

### Can't Approve Order

**Cause**: Order already approved/rejected
**Solution**: Check order status, may need to find original

## Future Enhancements

Planned improvements for v0.5.x:

- [ ] Batch approve/reject button
- [ ] Keyboard shortcuts for quick approval
- [ ] Email notifications for pending orders
- [ ] Conditional approvals (price-based auto-approve)
- [ ] Order expiration (auto-reject after N hours)
- [ ] Approval workflow delegation (multiple users)
- [ ] Mobile app for on-the-go approvals

## Conclusion

The manual approval system is a critical safety layer protecting your trading operations. By requiring explicit approval before execution, you maintain control over every order and can respond to changing market conditions.

For questions or issues, please refer to [TROUBLESHOOTING.md](troubleshooting.md) or open an issue on GitHub.
