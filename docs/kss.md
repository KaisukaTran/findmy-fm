# KSS (Kai Strategy Service) - Pyramid DCA Strategy

> **Version**: v0.10.0  
> **Module**: `src/findmy/kss/`

## Overview

KSS (Kai Strategy Service) implements a **Pyramid DCA (Dollar Cost Averaging)** strategy for systematic position building during market dips. The strategy places progressively larger orders at predetermined price levels, automatically managing entries and exits.

## Key Concepts

### Pyramid DCA Pattern

Instead of buying at a single price, Pyramid DCA:

1. Divides capital into multiple **waves**
2. Each wave triggers at a lower price than the previous
3. Later waves are **larger** (pyramid shape)
4. Position size increases as price decreases
5. Average entry price improves with each fill

```
Entry Price ━━━━━━━━━━━━━━━━━━━━━━ Wave 0: 1 pip
             ↓ -2%
            ━━━━━━━━━━━━━━━━━━━━━━ Wave 1: 2 pips
             ↓ -2%
           ━━━━━━━━━━━━━━━━━━━━━━━ Wave 2: 3 pips
             ↓ -2%
          ━━━━━━━━━━━━━━━━━━━━━━━━ Wave 3: 4 pips
             ...
```

### Terminology

| Term | Description |
|------|-------------|
| **Pip** | Minimum tradeable unit: `pip_multiplier × minQty` |
| **Wave** | A single DCA order with target price & quantity |
| **Distance%** | Price drop between consecutive waves |
| **Isolated Fund** | Maximum capital allocated to session |
| **TP%** | Take profit percentage above average price |
| **Timeout** | Inactivity period after which session auto-closes |

## Configuration

### Session Parameters

| Parameter | Type | Description | Default |
|-----------|------|-------------|---------|
| `symbol` | str | Trading pair (e.g., "BTC", "ETH") | Required |
| `entry_price` | float | First wave target price | Required |
| `distance_pct` | float | % drop between waves (0.1-50) | 2.0 |
| `max_waves` | int | Maximum number of waves (1-100) | 10 |
| `isolated_fund` | float | Max capital for session | Required |
| `tp_pct` | float | Take profit % above avg price | 3.0 |
| `timeout_x_min` | float | Minutes of inactivity before timeout | 30.0 |
| `gap_y_min` | float | Min minutes between waves | 5.0 |

### Global Settings

In `src/findmy/config.py`:

```python
# KSS Configuration
pip_multiplier: float = 2.0  # 1 pip = 2 × minQty from Binance
```

## Wave Calculations

### Wave Quantity

Each wave's quantity follows a pyramid pattern:

```
qty(n) = (n + 1) × pip_size
```

Where:
- `n` = wave number (0, 1, 2, ...)
- `pip_size` = `pip_multiplier × minQty`

**Example** (pip_size = 0.00002):
- Wave 0: 0.00002 BTC
- Wave 1: 0.00004 BTC
- Wave 2: 0.00006 BTC
- Wave 9: 0.00020 BTC

### Wave Price

Each wave's target price decreases by `distance_pct`:

```
price(n) = entry_price × (1 - distance_pct/100)^n
```

**Example** (entry=50000, distance=2%):
- Wave 0: 50,000.00
- Wave 1: 49,000.00
- Wave 2: 48,020.00
- Wave 5: 45,099.80

### Total Cost Estimation

Before starting, estimate capital needed:

```
total_cost = Σ price(n) × qty(n)  for n = 0 to max_waves-1
```

## Session Lifecycle

```
┌─────────┐     ┌────────┐     ┌───────────┐
│ PENDING │────▶│ ACTIVE │────▶│ COMPLETED │
└─────────┘     └────────┘     └───────────┘
                    │               ▲
                    │               │
                    ▼               │
              ┌─────────────┐       │
              │ TP_TRIGGERED│───────┘
              └─────────────┘
                    │
                    ▼
              ┌─────────┐
              │ STOPPED │
              └─────────┘
```

### States

| State | Description |
|-------|-------------|
| `PENDING` | Created but not started |
| `ACTIVE` | Running, waves being placed |
| `TP_TRIGGERED` | Take profit order placed |
| `STOPPED` | Manually stopped |
| `TIMEOUT` | Closed due to inactivity |
| `COMPLETED` | All waves filled or TP executed |

### Flow

1. **Create** → Session in `PENDING` state
2. **Start** → Places Wave 0 order, moves to `ACTIVE`
3. **Fill Event** → Updates avg price, generates next wave
4. **Check TP** → If price > TP threshold, sell all
5. **Timeout** → If no fills for `timeout_x_min`, auto-close

## API Endpoints

### Create Session

```http
POST /kss/sessions

{
  "symbol": "BTC",
  "entry_price": 50000.0,
  "distance_pct": 2.0,
  "max_waves": 10,
  "isolated_fund": 1000.0,
  "tp_pct": 3.0,
  "timeout_x_min": 30.0,
  "gap_y_min": 5.0
}
```

Response:
```json
{
  "id": 1,
  "symbol": "BTC",
  "status": "pending",
  "entry_price": 50000.0,
  "estimated_cost": 892.45,
  "estimated_tp_price": 51500.0
}
```

### Start Session

```http
POST /kss/sessions/{id}/start
```

Response:
```json
{
  "message": "Session started",
  "order": {
    "symbol": "BTC",
    "side": "BUY",
    "quantity": 0.00002,
    "price": 50000.0
  }
}
```

### Stop Session

```http
POST /kss/sessions/{id}/stop

{
  "reason": "manual"  // optional
}
```

### Adjust Parameters

```http
PATCH /kss/sessions/{id}

{
  "max_waves": 15,
  "tp_pct": 4.0
}
```

### List Sessions

```http
GET /kss/sessions?symbol=BTC&status=active
```

### Check Take Profit

```http
POST /kss/sessions/{id}/check-tp

{
  "current_price": 52000.0
}
```

### Delete Session

```http
DELETE /kss/sessions/{id}
```

### Get Summary

```http
GET /kss/summary
```

Response:
```json
{
  "total_sessions": 5,
  "active_sessions": 2,
  "pending_sessions": 1,
  "completed_sessions": 2,
  "total_isolated_fund": 5000.0,
  "active_isolated_fund": 2000.0
}
```

## Dashboard Integration

The KSS section appears on the dashboard with:

1. **Summary Cards** - Total/Active/Pending sessions, Total Fund
2. **Sessions Table** - All sessions with status, waves, avg price, P&L
3. **Create Modal** - Form to configure new sessions
4. **Actions** - Start, Stop, Delete, Check TP buttons

## Integration with Pending Orders

KSS integrates with the existing pending orders system:

1. Wave orders are submitted as pending orders
2. Approved orders are filled through the executor
3. Fill events trigger `kss.hooks.handle_fill_event()`
4. Hook updates session state and generates next wave

### Source Reference Format

Orders are tracked using source references:

```
pyramid:{session_id}:wave:{wave_num}
```

Example: `pyramid:5:wave:3` = Session 5, Wave 3

## Database Schema

### kss_sessions

```sql
CREATE TABLE kss_sessions (
    id INTEGER PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    entry_price DECIMAL(20, 8) NOT NULL,
    distance_pct DECIMAL(8, 4) NOT NULL,
    max_waves INTEGER NOT NULL,
    isolated_fund DECIMAL(20, 8) NOT NULL,
    tp_pct DECIMAL(8, 4) NOT NULL,
    timeout_x_min DECIMAL(10, 2) NOT NULL,
    gap_y_min DECIMAL(10, 2) NOT NULL,
    pip_multiplier DECIMAL(10, 4) DEFAULT 2.0,
    status VARCHAR(20) DEFAULT 'pending',
    current_wave INTEGER DEFAULT 0,
    total_filled_qty DECIMAL(20, 8) DEFAULT 0,
    total_cost DECIMAL(20, 8) DEFAULT 0,
    avg_price DECIMAL(20, 8) DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    start_time TIMESTAMP,
    last_fill_time TIMESTAMP
);
```

### kss_waves

```sql
CREATE TABLE kss_waves (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    wave_num INTEGER NOT NULL,
    quantity DECIMAL(20, 8) NOT NULL,
    target_price DECIMAL(20, 8) NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    filled_qty DECIMAL(20, 8),
    filled_price DECIMAL(20, 8),
    filled_time TIMESTAMP,
    pending_order_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES kss_sessions(id)
);
```

## Example Scenario

### Setup

- Symbol: BTC
- Entry Price: $50,000
- Distance: 2%
- Max Waves: 10
- Isolated Fund: $1,000
- TP: 3%

### Execution

1. **Wave 0** @ $50,000 → Buy 0.00002 BTC ($1.00)
2. Price drops to $49,000
3. **Wave 1** @ $49,000 → Buy 0.00004 BTC ($1.96)
4. Price drops to $48,020
5. **Wave 2** @ $48,020 → Buy 0.00006 BTC ($2.88)
6. ...
7. Price rises to $52,000
8. **TP Triggered** → Sell all @ $52,000

### Result

- Total bought: 0.00012 BTC
- Total cost: $5.84
- Avg price: ~$48,667
- Sell @ $52,000: $6.24
- Profit: $0.40 (6.8%)

## Best Practices

1. **Set Realistic Distance** - 1-5% typical; too small = fills too fast, too large = may miss dips
2. **Size Isolated Fund** - Use `estimate_total_cost()` before starting
3. **Monitor Timeout** - Increase for volatile assets, decrease for stable ones
4. **Use Gap Time** - Prevents rapid-fire orders during flash crashes
5. **Start Small** - Test with 1-3 waves before scaling up

## Error Handling

| Error | Cause | Resolution |
|-------|-------|------------|
| `Session not found` | Invalid session ID | Check session exists |
| `Session already started` | Double start attempt | Session is already active |
| `Session not active` | Stop on non-active | Session already stopped |
| `Invalid wave number` | Out of range | Check max_waves setting |
| `Insufficient fund` | Cost > isolated_fund | Reduce waves or increase fund |

## Related Documentation

- [Pending Orders](./SOT.md) - Order approval workflow
- [Risk Management](./risk-management.md) - Position limits
- [Configuration](./configuration.md) - Global settings
- [API Reference](./api.md) - Full endpoint documentation
