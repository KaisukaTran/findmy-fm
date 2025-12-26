# FINDMY – API Reference

## Overview

The FINDMY API is a RESTful service built with **FastAPI** that provides endpoints for:
- Uploading Excel files with trading orders
- Executing paper trades
- Querying execution results
- Health checks and system status

**Base URL**: `http://localhost:8000` (development)

---

## Endpoints

### 1. Health Check

**Request**:
```http
GET /
```

**Response** (200 OK):
```json
{
  "status": "ok",
  "service": "FINDMY FM API"
}
```

**Use Case**: Verify API is running and healthy.

---

### 2. Paper Trading Execution

**Request**:
```http
POST /paper-execution
Content-Type: multipart/form-data

file: <Excel file>
```

**File Requirements:**
- **Format**: .xlsx or .xls (Excel)
- **Max Size**: 10 MB
- **Sheet Name**: Must be "purchase order"
- **MIME Types**: 
  - `application/vnd.ms-excel`
  - `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`

**Example Request** (curl):
```bash
curl -X POST http://localhost:8000/paper-execution \
  -F "file=@examples/sample_purchase_order_with_header.xlsx"
```

**Excel Format - Required Columns:**

| Vietnamese | English | Type | Description |
|---|---|---|---|
| Số Thứ Tự Lệnh | Client ID | String | Unique order identifier |
| Khối Lượng Mua | Quantity | Number | Order quantity |
| Giá Đặt Lệnh | Price | Number | Price in USD |
| Cặp Tiền Ảo Giao Dịch | Symbol | String | Trading pair (BTC/USD, ETH/USD, etc.) |

See [examples/](../examples/) for sample files with different formats.

**Response** (200 OK):
```json
{
  "status": "success",
  "result": {
    "orders": 3,
    "trades": 3,
    "positions": [
      {
        "symbol": "BTC/USD",
        "size": 0.5,
        "avg_price": 50000.0,
        "updated_at": "2025-01-15T10:30:45"
      },
      {
        "symbol": "ETH/USD",
        "size": 1.0,
        "avg_price": 3000.0,
        "updated_at": "2025-01-15T10:30:46"
      }
    ],
    "errors": null
  }
}
```

**Response Fields:**
- **orders**: Number of orders processed
- **trades**: Number of successfully executed trades
- **positions**: Array of current positions with symbol, size, and average price
- **errors**: Array of processing errors (null if none)

---

## Error Responses

### 400 Bad Request - Invalid MIME Type
```json
{
  "detail": "Invalid file type: text/plain. Only Excel files are supported."
}
```

### 400 Bad Request - File Too Large
```json
{
  "detail": "File too large. Maximum size is 10MB"
}
```

### 400 Bad Request - Invalid Excel Format
```json
{
  "detail": "Invalid Excel file: Sheet 'purchase order' not found"
}
```

### 400 Bad Request - Invalid Data (Rows Skipped)
```json
{
  "status": "success",
  "result": {
    "orders": 3,
    "trades": 1,
    "positions": [...],
    "errors": [
      {"row": 2, "error": "Invalid numeric values: qty=invalid, price=..."},
      {"row": 3, "error": "Invalid numeric values: qty=..., price=invalid"}
    ]
  }
}
```

### 422 Unprocessable Entity - Missing File
```json
{
  "detail": [
    {
      "loc": ["body", "file"],
      "msg": "Field required",
      "type": "missing"
    }
  ]
}
```

### 500 Internal Server Error
```json
{
  "detail": "Processing error: [description]"
}
```

---

## Usage Examples

### Python
```python
import requests

with open("examples/sample_purchase_order_with_header.xlsx", "rb") as f:
    files = {
        "file": (
            "orders.xlsx",
            f,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    }
    response = requests.post(
        "http://localhost:8000/paper-execution",
        files=files
    )
    result = response.json()
    print(f"Processed {result['result']['orders']} orders")
    print(f"Executed {result['result']['trades']} trades")
    for pos in result['result']['positions']:
        print(f"  {pos['symbol']}: {pos['size']} @ {pos['avg_price']}")
```

### JavaScript/Fetch
```javascript
const formData = new FormData();
const fileInput = document.querySelector('input[type="file"]');
formData.append('file', fileInput.files[0]);

fetch('http://localhost:8000/paper-execution', {
  method: 'POST',
  body: formData
})
  .then(response => response.json())
  .then(data => {
    console.log(`Processed ${data.result.orders} orders`);
    console.log(`Executed ${data.result.trades} trades`);
    data.result.positions.forEach(pos => {
      console.log(`  ${pos.symbol}: ${pos.size} @ ${pos.avg_price}`);
    });
  })
  .catch(error => console.error('Error:', error));
```

### CURL
```bash
# Basic request
curl -X POST http://localhost:8000/paper-execution \
  -F "file=@my_orders.xlsx"

# Pretty print response
curl -X POST http://localhost:8000/paper-execution \
  -F "file=@my_orders.xlsx" | python -m json.tool
```

---

## Database Schema

The API persists data in SQLite with the following tables:

### Orders Table
```sql
CREATE TABLE orders (
  id INTEGER PRIMARY KEY,
  client_order_id VARCHAR UNIQUE NOT NULL,
  symbol VARCHAR NOT NULL,
  side VARCHAR NOT NULL,  -- BUY (SELL in v2+)
  qty NUMERIC NOT NULL,
  price NUMERIC NOT NULL,
  status VARCHAR NOT NULL,  -- NEW, FILLED, CANCELLED
  created_at DATETIME DEFAULT NOW(),
  updated_at DATETIME DEFAULT NOW()
);
```

### Trades Table
```sql
CREATE TABLE trades (
  id INTEGER PRIMARY KEY,
  order_id INTEGER FOREIGN KEY,
  symbol VARCHAR NOT NULL,
  side VARCHAR NOT NULL,
  qty NUMERIC NOT NULL,
  price NUMERIC NOT NULL,
  ts DATETIME DEFAULT NOW()
);
```

### Positions Table
```sql
CREATE TABLE positions (
  id INTEGER PRIMARY KEY,
  symbol VARCHAR UNIQUE NOT NULL,
  size NUMERIC NOT NULL,
  avg_price NUMERIC NOT NULL,
  updated_at DATETIME DEFAULT NOW()
);
```

---

## Security Features

✅ **File Type Validation**: Only Excel files allowed (MIME type + extension check)  
✅ **File Size Limits**: Maximum 10MB per upload  
✅ **Safe Filenames**: UUID-based naming to prevent collisions/overwrites  
✅ **Temporary File Cleanup**: Automatic deletion after processing  
✅ **Input Validation**: Numeric field validation with graceful error handling  
✅ **Isolation**: Bad data in one row doesn't crash the entire batch  

---

## Environment Configuration

### Upload Directory
```bash
# Set custom upload directory
export UPLOAD_DIR=/var/uploads

# Default: data/uploads
```

### Database Path
The database is stored at `data/findmy_fm_paper.db` by default.

---

## Future Features (v2+)

- 🚀 SELL orders and position reduction
- 🚀 Async batch processing
- 🚀 Order status tracking and history
- 🚀 Position P&L calculations
- 🚀 WebSocket real-time updates
- 🚀 Rate limiting and authentication
- 🚀 Trade audit logging
- 🚀 Strategy backtesting integration

**Response** (200 OK):
```json
{
  "status": "success",
  "execution_id": "exec-20250101-123456",
  "summary": {
    "orders_received": 5,
    "orders_executed": 5,
    "orders_rejected": 0,
    "total_cost": 325000.00,
    "execution_time_ms": 45
  },
  "positions": [
    {
      "symbol": "BTC/USDT",
      "size": 0.3,
      "avg_cost": 65000.00,
      "unrealized_pnl": 1500.00
    },
    {
      "symbol": "ETH/USDT",
      "size": 5.0,
      "avg_cost": 3500.00,
      "unrealized_pnl": 250.00
    }
  ],
  "trades": [
    {
      "order_id": "ORD-001",
      "symbol": "BTC/USDT",
      "side": "BUY",
      "size": 0.3,
      "price": 65000.00,
      "executed_at": "2025-01-01T12:34:56Z"
    }
  ]
}
```

**Error Response** (400 Bad Request):
```json
{
  "status": "error",
  "error_code": "INVALID_EXCEL_FORMAT",
  "message": "Missing required sheet 'purchase order'"
}
```

**Error Codes**:
| Code | Meaning | Resolution |
|------|---------|-----------|
| `INVALID_EXCEL_FORMAT` | Sheet or columns missing | Verify Excel matches specification |
| `INVALID_ORDER_DATA` | Order validation failed | Check quantities, prices are positive |
| `EXECUTION_ERROR` | Execution engine failed | Check logs for details |
| `DATABASE_ERROR` | SOT write failed | Verify database connectivity |

---

### 3. Execution History (Future)

**Request**:
```http
GET /executions/{execution_id}
```

**Response** (200 OK):
```json
{
  "execution_id": "exec-20250101-123456",
  "status": "completed",
  "orders": [...],
  "fills": [...],
  "positions": [...]
}
```

---

## Request Schemas

### Upload Execution

**File Requirements**:
- Format: `.xlsx` (Excel)
- Sheet name: `purchase order`
- Columns (A–D):
  - **A**: Order ID (string or number)
  - **B**: Quantity (positive float)
  - **C**: Price (positive float)
  - **D**: Symbol (string, e.g., "BTC/USDT")

**Example Excel Layout**:
```
| Order ID  | Quantity | Price   | Symbol    |
|-----------|----------|---------|-----------|
| ORD-001   | 0.5      | 65000   | BTC/USDT  |
| ORD-002   | 5.0      | 3500    | ETH/USDT  |
| ORD-003   | 100      | 50      | SOL/USDT  |
```

**Header Row**: Optional. If missing or incorrect, falls back to positional mapping (A, B, C, D).

---

## Response Schemas

### ExecutionResult

```python
class ExecutionResult(BaseModel):
    status: str  # "success" or "error"
    execution_id: str
    summary: ExecutionSummary
    positions: List[Position]
    trades: List[Trade]
    errors: List[str]  # If any orders failed
```

### ExecutionSummary

```python
class ExecutionSummary(BaseModel):
    orders_received: int
    orders_executed: int
    orders_rejected: int
    total_cost: float
    execution_time_ms: int
```

### Position

```python
class Position(BaseModel):
    symbol: str
    size: float
    avg_cost: float
    unrealized_pnl: float
    last_updated: datetime
```

### Trade

```python
class Trade(BaseModel):
    order_id: str
    symbol: str
    side: str  # "BUY" or "SELL"
    size: float
    price: float
    executed_at: datetime
    fill_quantity: float
```

---

## Error Handling

### General Error Response

```json
{
  "status": "error",
  "error_code": "ERROR_CODE",
  "message": "Human-readable error message",
  "details": {
    "field": "error details"
  }
}
```

### HTTP Status Codes

| Status | Meaning | Example |
|--------|---------|---------|
| 200 | Success | Execution completed |
| 400 | Bad Request | Invalid Excel format |
| 422 | Validation Error | Missing required field |
| 500 | Server Error | Database connection failed |
| 503 | Service Unavailable | Database down |

---

## Rate Limiting

**Current**: No rate limiting (v1).

**Future**: Per-IP rate limits may be implemented:
- 100 requests/minute per IP
- 1000 orders/minute per IP

---

## Authentication & Security

**v1**: No authentication required.

**Future** (before live trading):
- API key authentication
- OAuth 2.0 support
- Rate limiting
- Request signing

---

## Examples

### Example 1: Execute Small Order

```bash
# Create sample Excel file
cat > orders.xlsx << EOF
Order ID,Quantity,Price,Symbol
ORD-001,0.1,65000,BTC/USDT
EOF

# Upload and execute
curl -X POST http://localhost:8000/paper-execution \
  -F "file=@orders.xlsx" \
  -H "Accept: application/json" | jq .
```

### Example 2: Multiple Assets

```bash
cat > portfolio.xlsx << EOF
Order ID,Quantity,Price,Symbol
BUY-BTC,0.5,65000,BTC/USDT
BUY-ETH,5.0,3500,ETH/USDT
BUY-SOL,100,50,SOL/USDT
EOF

curl -X POST http://localhost:8000/paper-execution \
  -F "file=@portfolio.xlsx" | jq '.positions'
```

### Example 3: Python Client

```python
import requests

files = {'file': open('orders.xlsx', 'rb')}
response = requests.post('http://localhost:8000/paper-execution', files=files)

result = response.json()
print(f"Executed {result['summary']['orders_executed']} orders")
for position in result['positions']:
    print(f"{position['symbol']}: {position['size']} @ {position['avg_cost']}")
```

---

## OpenAPI/Swagger

Interactive API documentation available at:
- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`
- **OpenAPI JSON**: `http://localhost:8000/openapi.json`

Visit `/docs` while server is running to test endpoints interactively.

---

## Related Documentation

- **Architecture**: See [architecture.md](architecture.md)
- **Execution Details**: See [execution.md](execution.md)
- **Data Model**: See [SOT.md](SOT.md)
