# FINDMY (FM) – Development Log (Day 02)

**Date:** Day 02
**Author:** Kai
**Environment:** GitHub Codespaces (VS Code Web on iPad)

---

## 🎯 Mục tiêu Day 02 (UPDATED)

Day 02 **KHÔNG tập trung vào PnL**.
Trọng tâm là **xây dựng nền tảng dữ liệu (Database & Auditability)** cho FINDMY.

### Mục tiêu chính

* Thiết kế **DB schema rõ ràng** cho paper trading
* Lưu **toàn bộ lệnh đã thực hiện**
* Ghi lại **lịch sử các yêu cầu từ Excel (sheet `purchase order`)**
* Đảm bảo **audit trail**: có thể truy vết lại *ai – khi nào – upload file nào – sinh ra lệnh gì*

> PnL & equity curve **để Day 03**

---

## 🧠 Phạm vi (Scope Control)

### ✅ Sẽ làm

* Order history (orders table)
* Trade history (trades table)
* Position snapshot (positions table)
* **Request history** từ Excel upload
* Execution run tracking

### ❌ Chưa làm

* PnL / equity
* Strategy logic
* Live trading
* Async execution

---

## 🗄️ 1. Thiết kế Database (Core của Day 02)

### 1.1 Bảng `execution_runs`

> Mỗi lần upload Excel = **1 execution run**

```sql
execution_runs
----------------------
id (PK)
run_id (UUID)
source_file_name
sheet_name
created_at
notes
```

Ý nghĩa:

* `run_id`: liên kết tất cả orders/trades của 1 lần chạy
* `source_file_name`: tên file Excel upload
* `sheet_name`: mặc định `purchase order`

---

### 1.2 Bảng `order_requests` (LỊCH SỬ EXCEL)

> Lưu **nguyên trạng dữ liệu đọc từ Excel** (chưa execution)

```sql
order_requests
----------------------
id (PK)
run_id (FK)
row_index
client_order_id
qty
price
symbol
raw_data (JSON)
created_at
```

Ý nghĩa:

* Ghi lại **mỗi dòng trong sheet purchase order**
* `raw_data`: lưu row gốc để audit/debug

---

### 1.3 Bảng `orders`

```sql
orders
----------------------
id (PK)
run_id (FK)
client_order_id
symbol
side
qty
price
status
created_at
```

---

### 1.4 Bảng `trades`

```sql
trades
----------------------
id (PK)
order_id (FK)
symbol
side
qty
price
ts
```

---

### 1.5 Bảng `positions`

```sql
positions
----------------------
id (PK)
symbol
size
avg_price
updated_at
```

---

## 🔄 2. Data Flow (Day 02)

```text
Excel Upload
   ↓
Create execution_run
   ↓
Parse sheet "purchase order"
   ↓
Insert order_requests (raw history)
   ↓
Create orders
   ↓
Simulate trades
   ↓
Update positions
```

---

## 🔧 3. Task Breakdown (Thực hiện trong ngày)

### 3.1 Persistence Layer

* [ ] Thêm model `ExecutionRun`
* [ ] Thêm model `OrderRequest`
* [ ] Gắn `run_id` cho orders & trades

### 3.2 Execution Layer

* [ ] Generate `run_id` mỗi lần execution
* [ ] Lưu order_requests trước khi execution
* [ ] Execution **KHÔNG phụ thuộc API**

### 3.3 API Layer

* [ ] `/paper-execution` trả về `run_id`
* [ ] Endpoint mới:

  * `GET /runs` (list execution runs)
  * `GET /runs/{run_id}` (chi tiết 1 run)

---

## 🧪 4. Test Plan

### Test case 1

* Upload 1 file Excel
* Expect:

  * 1 execution_run
  * N order_requests
  * N orders

### Test case 2

* Upload cùng file 2 lần
* Expect:

  * 2 execution_runs khác nhau
  * Dữ liệu không bị overwrite

---

## 🧠 5. Design Decisions (RẤT QUAN TRỌNG)

* Excel ingestion **luôn được lưu lại**, dù execution fail
* DB là **nguồn sự thật duy nhất**
* API chỉ trigger & query, không giữ state
* Strategy (Day 03) sẽ **đọc từ DB**, không từ Excel

---

## 📝 6. Lệnh dự kiến sử dụng

```bash
# start api
./scripts/start_api.sh

# test upload
curl -X POST http://localhost:8000/paper-execution \
  -F "file=@data/orders_v1.xlsx"

# inspect db
sqlite3 data/findmy_fm_paper.db
```

---

## 🔮 7. Ghi chú cho Day 03

* Dùng DB đã có để tính PnL
* Strategy engine chỉ sinh signal
* Không đọc Excel trực tiếp nữa

---

> *Day 02 đặt nền móng cho auditability và khả năng phân tích lại toàn bộ lịch sử giao dịch.*
