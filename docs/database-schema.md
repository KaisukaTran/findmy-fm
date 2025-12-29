# Database Schema Documentation

## Overview

FINDMY uses SQLite for persistent storage of orders, trades, and positions. The database is automatically initialized on first run.

**Default Location**: `data/findmy_fm_paper.db`

---

## Tables

### 1. Orders Table

Stores all order requests submitted by users.

```sql
CREATE TABLE orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_order_id VARCHAR UNIQUE NOT NULL,
  symbol VARCHAR NOT NULL,
  side VARCHAR NOT NULL,
  qty NUMERIC NOT NULL,
  price NUMERIC NOT NULL,
  status VARCHAR NOT NULL DEFAULT 'NEW',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME
);
```

**Columns:**

| Column | Type | Description | Example |
|--------|------|-------------|---------|
| id | INTEGER | Primary key (auto-increment) | 1 |
| client_order_id | VARCHAR | Unique order ID from client | "ORD001" |
| symbol | VARCHAR | Trading pair | "BTC/USD", "ETH/USD" |
| side | VARCHAR | Order side | "BUY" (SELL in v2+) |
| qty | NUMERIC | Quantity/size | 0.5, 10.0 |
| price | NUMERIC | Price per unit | 50000.0, 3000.5 |
| status | VARCHAR | Current status | NEW, FILLED, CANCELLED |
| created_at | DATETIME | Creation timestamp | 2025-01-15 10:30:45 |
| updated_at | DATETIME | Last update timestamp | 2025-01-15 10:30:50 |

**Constraints:**
- `client_order_id` must be unique
- No duplicates allowed (prevents double-execution)

**Example Record:**
```json
{
  "id": 1,
  "client_order_id": "ORD001",
  "symbol": "BTC/USD",
  "side": "BUY",
  "qty": 0.5,
  "price": 50000.0,
  "status": "FILLED",
  "created_at": "2025-01-15T10:30:45",
  "updated_at": "2025-01-15T10:30:50"
}
```

---

### 2. Trades Table

Stores executed trades (one trade per filled order in v1).

```sql
CREATE TABLE trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL,
  symbol VARCHAR NOT NULL,
  side VARCHAR NOT NULL,
  qty NUMERIC NOT NULL,
  price NUMERIC NOT NULL,
  ts DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (order_id) REFERENCES orders(id)
);
```

**Columns:**

| Column | Type | Description | Example |
|--------|------|-------------|---------|
| id | INTEGER | Primary key | 1 |
| order_id | INTEGER | Reference to orders table | 1 |
| symbol | VARCHAR | Trading pair | "BTC/USD" |
| side | VARCHAR | Trade side | "BUY" |
| qty | NUMERIC | Trade quantity | 0.5 |
| price | NUMERIC | Execution price | 50000.0 |
| ts | DATETIME | Execution timestamp | 2025-01-15 10:30:50 |

**Constraints:**
- Foreign key to orders.id

**Example Record:**
```json
{
  "id": 1,
  "order_id": 1,
  "symbol": "BTC/USD",
  "side": "BUY",
  "qty": 0.5,
  "price": 50000.0,
  "ts": "2025-01-15T10:30:50"
}
```

---

### 3. Positions Table

Stores current holdings (continuously updated as orders are filled).

```sql
CREATE TABLE positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol VARCHAR UNIQUE NOT NULL,
  size NUMERIC NOT NULL,
  avg_price NUMERIC NOT NULL,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**Columns:**

| Column | Type | Description | Example |
|--------|------|-------------|---------|
| id | INTEGER | Primary key | 1 |
| symbol | VARCHAR | Trading pair (unique) | "BTC/USD" |
| size | NUMERIC | Current position size | 1.5 |
| avg_price | NUMERIC | Average entry price | 49000.0 |
| updated_at | DATETIME | Last update timestamp | 2025-01-15 10:32:00 |

**Constraints:**
- `symbol` is unique (one position per trading pair)

**Example Record:**
```json
{
  "id": 1,
  "symbol": "BTC/USD",
  "size": 1.5,
  "avg_price": 49000.0,
  "updated_at": "2025-01-15T10:32:00"
}
```

---

## Relationships

```
orders (1) ──── (M) trades
  ↑
  └─── positions
```

**One-to-Many Relationship:**
- One order can have multiple associated trades (v2+ when partial fills are supported)
- Currently, one order = one trade (immediate full fill in v1)

**Orders to Positions:**
- Orders update position records
- Positions track current holdings by symbol
- Average price is calculated cumulatively

---

## Data Flow Example

### Scenario: Execute 3 buy orders for BTC/USD

**Input Excel:**
```
Order ID | Quantity | Price | Trading Pair
ORD001   | 0.5      | 50000 | BTC/USD
ORD002   | 0.5      | 51000 | BTC/USD
ORD003   | 0.5      | 52000 | BTC/USD
```

**Orders Table After Processing:**
```
id | client_order_id | symbol   | qty | price | status | created_at
1  | ORD001         | BTC/USD  | 0.5 | 50000 | FILLED | 2025-01-15 10:30:45
2  | ORD002         | BTC/USD  | 0.5 | 51000 | FILLED | 2025-01-15 10:30:46
3  | ORD003         | BTC/USD  | 0.5 | 52000 | FILLED | 2025-01-15 10:30:47
```

**Trades Table After Processing:**
```
id | order_id | symbol  | qty | price | ts
1  | 1        | BTC/USD | 0.5 | 50000 | 2025-01-15 10:30:45
2  | 2        | BTC/USD | 0.5 | 51000 | 2025-01-15 10:30:46
3  | 3        | BTC/USD | 0.5 | 52000 | 2025-01-15 10:30:47
```

**Positions Table After Processing:**
```
id | symbol  | size | avg_price | updated_at
1  | BTC/USD | 1.5  | 51000.0   | 2025-01-15 10:30:47
```

**Calculation:**
```
avg_price = (0.5×50000 + 0.5×51000 + 0.5×52000) / 1.5
          = (25000 + 25500 + 26000) / 1.5
          = 76500 / 1.5
          = 51000.0
```

---

## Querying Examples

### Get all orders for a symbol
```sql
SELECT * FROM orders WHERE symbol = 'BTC/USD' ORDER BY created_at DESC;
```

### Calculate P&L for a position (when sell prices are available in v2+)
```sql
SELECT 
  symbol,
  size,
  avg_price,
  (final_price - avg_price) * size as unrealized_pnl
FROM positions
WHERE symbol = 'BTC/USD';
```

### Get recent trades (last 10)
```sql
SELECT * FROM trades ORDER BY ts DESC LIMIT 10;
```

### Check order execution status
```sql
SELECT client_order_id, status, created_at FROM orders WHERE client_order_id = 'ORD001';
```

### Get all unfilled orders
```sql
SELECT * FROM orders WHERE status != 'FILLED';
```

### Calculate total invested by symbol
```sql
SELECT 
  symbol,
  SUM(qty * price) as total_invested
FROM trades
WHERE side = 'BUY'
GROUP BY symbol;
```

---

## Indexing Strategy

For optimal query performance (add to schema if needed):

```sql
-- Index for fast order lookup by client_order_id
CREATE INDEX idx_orders_client_order_id ON orders(client_order_id);

-- Index for fast symbol lookups
CREATE INDEX idx_orders_symbol ON orders(symbol);

-- Index for time-based queries
CREATE INDEX idx_trades_ts ON trades(ts);
```

---

## Backup and Maintenance

### Backup Database
```bash
# Copy database file
cp data/findmy_fm_paper.db data/findmy_fm_paper.backup.db
```

### Export to CSV
```bash
sqlite3 data/findmy_fm_paper.db <<EOF
.mode csv
.output orders.csv
SELECT * FROM orders;
.output trades.csv
SELECT * FROM trades;
.output positions.csv
SELECT * FROM positions;
EOF
```

---

## Future Changes (v2+)

- **Partial Fills**: Support partial order fills (qty field will have meaning across multiple trades)
- **SELL Orders**: Track selling side (negative position changes)
- **Order Status**: Support PENDING, CANCELLED, REJECTED states
- **Trade Fees**: Add commission/fee tracking
- **P&L Tracking**: Realized and unrealized gains/losses
- **Audit Log**: Track all data modifications with user/reason
- **Archive**: Old positions and trades moved to archive tables
