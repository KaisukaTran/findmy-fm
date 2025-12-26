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
  "service": "FINDMY FM API",
  "version": "0.1.0"
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

**Example Request** (curl):
```bash
curl -X POST http://localhost:8000/paper-execution \
  -F "file=@orders.xlsx"
```

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
