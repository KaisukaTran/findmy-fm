# Sample Purchase Order for FINDMY Paper Trading

This is a sample Excel file that demonstrates the expected format for the FINDMY paper trading execution engine.

## File Format

**Sheet Name:** `purchase order` (required)

### With Headers (Recommended)

The system supports English column names:

| Order ID | Quantity | Price | Trading Pair |
|---|---|---|---|
| 001 | 10.5 | 50000 | BTC/USD |
| 002 | 20.0 | 3000 | ETH/USD |

**Alternative English Headers:**
| Client ID | Quantity | Price | Symbol |
|---|---|---|---|
| 001 | 10.5 | 50000 | BTC/USD |

### Without Headers

If no header is present, the system expects columns in this order (A, B, C, D):
1. **Column A:** Client Order ID
2. **Column B:** Quantity
3. **Column C:** Price
4. **Column D:** Symbol

## Requirements

- **File Format:** .xlsx or .xls (Excel)
- **Max File Size:** 10 MB
- **Sheet Name:** Must be "purchase order"
- **Required Columns:** Client ID, Quantity, Price, Symbol
- **Numeric Fields:** Quantity and Price must be numeric values

## Notes

- Orders are processed in the order they appear
- Duplicate client order IDs are treated as existing orders (no duplicate execution)
- All prices are assumed to be in USD
- This version (v1) supports **BUY orders only**
