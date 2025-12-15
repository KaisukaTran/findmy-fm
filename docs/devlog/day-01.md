# FINDMY (FM) – Development Log (Day 01)

**Date:** Day 01
**Author:** Kai
**Environment:** GitHub Codespaces (VS Code Web on iPad)

---

## 🎯 Mục tiêu trong ngày

* Thiết lập môi trường phát triển **không cần máy tính cá nhân**
* Khởi tạo dự án FINDMY (FM)
* Xây dựng **paper trading execution engine (v1)**
* Tạo **FastAPI backend** để upload Excel và trigger execution
* Chuẩn hoá tài liệu & workflow GitHub

---

## 🧱 1. Thiết lập môi trường (GitHub Codespaces)

### 1.1 Tạo repository

* Tạo repo GitHub: `findmy-fm`
* Không chọn README mặc định (tự chuẩn hoá sau)

### 1.2 Mở Codespaces

* GitHub → Repo → **Code → Codespaces → Create codespace**
* VS Code Web mở trực tiếp trên trình duyệt (iPad)

---

## 🐍 2. Thiết lập Python environment

### 2.1 Tạo virtual environment

```bash
python -m venv .venv
source .venv/bin/activate
```

> `.venv` dùng để cô lập dependencies cho dự án

### 2.2 Cài package cần thiết

```bash
pip install pandas sqlalchemy openpyxl fastapi uvicorn python-multipart
pip freeze > requirements.txt
```

---

## 📁 3. Cấu trúc project ban đầu

```bash
mkdir -p src/findmy/{api,execution}
mkdir -p data/uploads
mkdir -p scripts

touch src/findmy/__init__.py
```

Cấu trúc chính:

```
findmy-fm/
├─ src/findmy/
│  ├─ api/
│  └─ execution/
├─ data/
├─ scripts/
```

---

## 📦 4. Fix import chuẩn cho Python (`src/` layout)

### 4.1 Lỗi gặp phải

```
ModuleNotFoundError: No module named 'findmy'
```

### 4.2 Cách fix (chuẩn production)

Tạo file `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "findmy"
version = "0.1.0"
requires-python = ">=3.10"

[tool.setuptools.packages.find]
where = ["src"]
```

Cài package ở chế độ editable:

```bash
pip install -e .
```

---

## 📊 5. Paper Trading Execution Engine (v1)

### 5.1 Mục tiêu

* Đọc file Excel
* Sheet name: `purchase order`
* Hỗ trợ Excel:

  * Có header
  * Không header
  * Header sai → fallback A,B,C,D
* BUY only
* Immediate full-fill
* Lưu Orders / Trades / Positions vào SQLite

### 5.2 File chính

```
src/findmy/execution/paper_execution.py
```

### 5.3 Lỗi & Fix quan trọng

#### ❌ `'int' object has no attribute 'lower'`

* Nguyên nhân: Excel không có header
* Fix: detect `df.columns` là `int` → positional mapping

#### ❌ `missing required columns`

* Nguyên nhân: header Excel không khớp
* Fix: fallback positional mapping

#### ❌ `return outside function`

* Nguyên nhân: sai indentation khi paste code
* Fix: replace toàn bộ function, không vá từng dòng

---

## 🌐 6. FastAPI Backend

### 6.1 Tạo FastAPI app

File:

```
src/findmy/api/main.py
```

Health check:

```http
GET /
```

Paper execution:

```http
POST /paper-execution
```

---

### 6.2 Chạy server

```bash
PYTHONPATH=src uvicorn findmy.api.main:app --host 0.0.0.0 --port 8000 --reload
```

Swagger UI:

```
/docs
```

---

## 🔁 7. Script khởi động FastAPI

Tạo file:

```
scripts/start_api.sh
```

Nội dung:

```bash
#!/bin/bash
source .venv/bin/activate
export PYTHONPATH=src
uvicorn findmy.api.main:app --host 0.0.0.0 --port 8000 --reload
```

```bash
chmod +x scripts/start_api.sh
```

Chạy:

```bash
./scripts/start_api.sh
```

---

## 🧪 8. Test end-to-end

* Mở `/docs`
* Upload file Excel
* Execute
* Nhận JSON:

  * orders
  * trades
  * positions

---

## 🧾 9. Git workflow trong ngày

```bash
git status
git add .
git commit -m "feat: paper trading execution + fastapi upload"
git push
```

---

## 📚 10. Documentation

* Chuẩn hoá `README.md`
* Tạo structure `docs/`
* Devlog theo ngày (`docs/devlog/day-01.md`)

---

## 🧠 Ghi chú cho ngày mai (Day 02)

* Bắt đầu từ `paper_execution.py`
* Thêm PnL & equity curve
* Thiết kế Strategy interface
* Không đụng lại FastAPI nếu không cần

---

> *Day 01 tập trung vào nền móng: môi trường, execution, API, và tài liệu. Hệ thống đã sẵn sàng để phát triển chiến lược.*
