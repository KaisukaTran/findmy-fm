# Go-Live Runbook — Copilot Prompt

> **Mục đích:** File này là hướng dẫn từng bước để go-live hệ thống FINDMY FM.
> Copilot / AI assistant có thể đọc file này và thực hiện từng task theo thứ tự.
> Mỗi task có checkbox `[ ]` — đánh dấu `[x]` khi hoàn thành.

---

## Trạng thái code hiện tại (branch `claude/review-progress-todos-X5uh0`)

- 38/38 integration tests passing
- 13 Alembic migrations (latest: `0012_ai_agent_tables`)
- AI agent infrastructure hoàn chỉnh
- CSRF middleware, modal dialogs, Prometheus metrics, Sentry integration đã có
- Trade-close PnL tracking wired vào paper_report

---

## PHASE 1 — Cấu hình môi trường (làm TRƯỚC tất cả mọi thứ)

### Task 1.1 — Tạo file `.env` từ template

```bash
cp .env.example .env
```

### Task 1.2 — Sinh APP_SECRET_KEY mạnh và ghi vào `.env`

```bash
python -c "import secrets; print('APP_SECRET_KEY=' + secrets.token_urlsafe(48))"
```

Mở `.env` và thay dòng `APP_SECRET_KEY=...` bằng giá trị vừa sinh ra.

**Yêu cầu:** tối thiểu 32 ký tự, không dùng giá trị mặc định.

### Task 1.3 — Ghi các biến bắt buộc vào `.env`

Mở file `.env` và điền đầy đủ các giá trị sau:

```dotenv
# === BẮT BUỘC ===
APP_SECRET_KEY=<giá trị từ task 1.2>
ANTHROPIC_API_KEY=<lấy từ console.anthropic.com>
DATABASE_URL=sqlite:///data/findmy_fm_paper.db
SOT_DATABASE_URL=sqlite:///data/findmy_fm_paper.db

# === Binance (paper mode — dùng testnet) ===
BROKER_API_KEY=<Binance API key>
BROKER_API_SECRET=<Binance API secret>

# === Chế độ trading ===
LIVE_TRADING=false
LIVE_TRADING_DRY_RUN=true

# === AI Agent ===
AI_WATCHLIST=BTC/USDT,ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT
AI_MAX_SPEND_USDT=50
AI_DAILY_TARGET_PCT=1.0
AI_LOOP_INTERVAL_SECONDS=300
AI_PAPER_MIN_DAYS=7

# === Observability (tuỳ chọn nhưng khuyến nghị) ===
SENTRY_DSN=<lấy từ sentry.io — bỏ trống nếu không dùng>
APP_ENV=production
APP_VERSION=1.0.0
```

**Lưu ý:** KHÔNG commit file `.env` vào git. File `.gitignore` đã exclude nó.

---

## PHASE 2 — Database & Schema

### Task 2.1 — Tạo thư mục data nếu chưa có

```bash
mkdir -p data/uploads
```

### Task 2.2 — Apply tất cả migrations

```bash
alembic upgrade head
```

**Kết quả mong đợi:** `Running upgrade ... -> 0012_ai_agent_tables, AI agent tables`

Nếu lỗi `ModuleNotFoundError`: chạy `pip install -r requirements-prod.txt` trước.

### Task 2.3 — Seed admin user

```bash
ADMIN_USERNAME=admin ADMIN_PASSWORD=<mật_khẩu_mạnh_tối_thiểu_8_ký_tự> python scripts/seed_admin.py
```

**Quan trọng:** Đổi mật khẩu ngay sau lần đăng nhập đầu tiên.

### Task 2.4 — Xoá demo users (trader1, trader2)

```bash
python - << 'EOF'
import sys
sys.path.insert(0, 'src')
from services.auth.user_repository import ensure_table
import sqlite3

db_path = 'data/findmy_fm_paper.db'
con = sqlite3.connect(db_path)
deleted = con.execute("DELETE FROM users WHERE username IN ('trader1', 'trader2')").rowcount
con.commit()
con.close()
print(f"Deleted {deleted} demo user(s)")
EOF
```

---

## PHASE 3 — Kiểm tra preflight

### Task 3.1 — Chạy preflight check

```bash
python scripts/preflight_check.py
```

**Yêu cầu:** Tất cả items phải là `[PASS]`. Không được có `[FAIL]`.
`[WARN]` về Binance key là chấp nhận được ở paper mode.

### Task 3.2 — Chạy integration test suite

```bash
pytest tests/integration/ --no-cov -v
```

**Yêu cầu:** `38 passed`. Không có failures.

---

## PHASE 4 — Cài đặt dependencies production

### Task 4.1 — Cài requirements production

```bash
pip install -r requirements-prod.txt
```

**Bao gồm:** FastAPI, SQLAlchemy, Alembic, Anthropic SDK, Sentry SDK, Prometheus.

### Task 4.2 — Verify Sentry SDK (nếu dùng SENTRY_DSN)

```bash
python -c "import sentry_sdk; print('sentry-sdk OK:', sentry_sdk.VERSION)"
```

---

## PHASE 5 — Infrastructure (ngoài code — làm ở server)

### Task 5.1 — Cấu hình TLS (nginx hoặc Cloudflare)

API **phải không** reachable qua plain HTTP. Cấu hình nginx upstream:

```nginx
server {
    listen 443 ssl;
    server_name yourdomain.com;
    ssl_certificate     /etc/ssl/certs/yourdomain.crt;
    ssl_certificate_key /etc/ssl/private/yourdomain.key;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# Redirect HTTP → HTTPS
server {
    listen 80;
    server_name yourdomain.com;
    return 301 https://$host$request_uri;
}
```

Sau khi cấu hình, đổi `secure=True` trong CSRF cookie (file `src/findmy/api/main.py` dòng có `secure=False`).

### Task 5.2 — Cấu hình cron backup database hàng ngày

```bash
# Thêm vào crontab (crontab -e)
0 2 * * * cp /path/to/findmy-fm/data/findmy_fm_paper.db /backup/findmy_fm_$(date +\%Y\%m\%d).db
# Giữ 30 ngày gần nhất
0 3 * * * find /backup/ -name "findmy_fm_*.db" -mtime +30 -delete
```

### Task 5.3 — Cấu hình Prometheus alerts

Copy file alert rules vào Prometheus config:

```bash
cp monitoring/ai_agent_alerts.yml /etc/prometheus/rules/ai_agent.yml
# Reload Prometheus
curl -X POST http://localhost:9090/-/reload
```

Các alerts đã cấu hình:
- `AIAgentHighErrorRate`: >0.1 errors/s trong 5 phút → warning
- `AIAgentLoopStalled`: Không có iteration trong 10 phút → critical
- `AIAgentHighSpend`: SpendLimitError >5 lần/giờ → warning

---

## PHASE 6 — Khởi động server

### Task 6.1 — Khởi động production server

```bash
gunicorn src.findmy.api.main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers 2 \
    --bind 127.0.0.1:8000 \
    --timeout 120 \
    --access-logfile /var/log/findmy/access.log \
    --error-logfile /var/log/findmy/error.log
```

### Task 6.2 — Kiểm tra health endpoint

```bash
curl -s http://localhost:8000/health | python -m json.tool
```

**Kết quả mong đợi:** `"status": "ok"` và tất cả components xanh.

### Task 6.3 — Kiểm tra metrics endpoint

```bash
curl -s http://localhost:8000/metrics | grep ai_agent
```

**Kết quả mong đợi:** các metric `ai_agent_errors_total`, `ai_loop_iterations_total`, `ai_agent_signal_confidence` xuất hiện.

---

## PHASE 7 — Paper mode validation (BẮT BUỘC trước khi live)

### Task 7.1 — Đăng nhập dashboard

Mở `https://yourdomain.com` → đăng nhập bằng admin user vừa seed.

### Task 7.2 — Kiểm tra AI tab

Vào tab **AI Agent** trên dashboard. Verify:
- Status hiện `mode: paper`
- `LIVE_TRADING` hiện `false`

### Task 7.3 — Start AI agent (paper mode)

Nhấn nút **"Start AI Agent"** trên dashboard hoặc:

```bash
curl -X POST https://yourdomain.com/api/ai/start \
  -H "Authorization: Bearer <admin_token>"
```

**Kết quả mong đợi:** `{"started": true}`

### Task 7.4 — Monitor trong 7+ ngày

Kiểm tra hàng ngày:

```bash
# Xem decisions gần nhất
curl -s https://yourdomain.com/api/ai/decisions \
  -H "Authorization: Bearer <admin_token>" | python -m json.tool | head -60

# Kiểm tra không có ERROR action lặp lại trên cùng symbol
curl -s https://yourdomain.com/api/ai/decisions \
  -H "Authorization: Bearer <admin_token>" | python -m json.tool | grep '"action"'

# Kiểm tra spend hàng ngày
curl -s https://yourdomain.com/api/ai/status \
  -H "Authorization: Bearer <admin_token>" | python -m json.tool
```

**Tiêu chí pass:**
- Không có `ERROR` action lặp lại trên cùng symbol
- `today.spent_usdt` trong giới hạn (`AI_MAX_SPEND_USDT × 10`)
- 7 ngày chạy liên tục không crash

### Task 7.5 — Xem paper report

```bash
curl -s https://yourdomain.com/api/ai/paper-report \
  -H "Authorization: Bearer <admin_token>" | python -m json.tool
```

Verify `estimated_win_rate` không còn `null` sau khi có closed trades.

### Task 7.6 — Thêm ít nhất 1 consultant agent (khuyến nghị)

```bash
curl -X POST https://yourdomain.com/api/ai/consultants \
  -H "Authorization: Bearer <admin_token>" \
  -H "Content-Type: application/json" \
  -d '{"name": "technical-analyst", "type": "technical", "enabled": true}'
```

---

## PHASE 8 — Promote to live (sau 7+ ngày paper mode)

### Task 8.1 — Verify promotion eligibility

```bash
curl -s https://yourdomain.com/api/ai/paper-report \
  -H "Authorization: Bearer <admin_token>" | python -m json.tool
```

Cần: `period_days >= 7`, `estimated_win_rate` không null.

### Task 8.2 — Promote qua API

```bash
curl -X POST https://yourdomain.com/api/ai/promote-to-live \
  -H "Authorization: Bearer <admin_token>"
```

**Kết quả mong đợi:** `{"promoted": true, "message": "AI mode set to 'live'..."}`

### Task 8.3 — Bật LIVE_TRADING

Trong file `.env`:
```dotenv
LIVE_TRADING=true
LIVE_TRADING_DRY_RUN=false
```

Sau đó restart server (gunicorn):
```bash
kill -HUP $(cat /var/run/gunicorn.pid)
# hoặc
systemctl restart findmy-fm
```

### Task 8.4 — Verify live mode

```bash
curl -s https://yourdomain.com/api/ai/status \
  -H "Authorization: Bearer <admin_token>" | python -m json.tool
```

**Kết quả mong đợi:** `"mode": "live"` và `"running": true`.

---

## Rollback (nếu có sự cố sau promote)

```bash
# 1. Emergency halt — dừng mọi approval/execution ngay lập tức
curl -X POST https://yourdomain.com/api/emergency-stop \
  -H "Authorization: Bearer <admin_token>"

# 2. Stop AI agent loop
curl -X POST https://yourdomain.com/api/ai/stop \
  -H "Authorization: Bearer <admin_token>"

# 3. Revert mode về paper trong DB
sqlite3 data/findmy_fm_paper.db \
  "UPDATE ai_agent_state SET value='paper' WHERE key='mode'"

# 4. Đổi lại .env
# LIVE_TRADING=false
# LIVE_TRADING_DRY_RUN=true

# 5. Restart server
systemctl restart findmy-fm

# 6. Resume sau khi fix xong
curl -X POST https://yourdomain.com/api/emergency-resume \
  -H "Authorization: Bearer <admin_token>"

# 7. Nếu nghi migration xấu
alembic downgrade -1
```

---

## Checklist cuối — trước khi announce go-live

- [ ] Task 1.2: APP_SECRET_KEY đã sinh ngẫu nhiên ≥ 32 chars
- [ ] Task 1.3: ANTHROPIC_API_KEY đã set
- [ ] Task 1.3: BROKER_API_KEY + BROKER_API_SECRET đã set
- [ ] Task 2.2: `alembic upgrade head` → migration 0012 pass
- [ ] Task 2.3: Admin user đã seed, mật khẩu đã đổi
- [ ] Task 2.4: Demo users (trader1, trader2) đã xoá
- [ ] Task 3.1: `preflight_check.py` → tất cả PASS
- [ ] Task 3.2: `pytest tests/integration/` → 38 passed
- [ ] Task 5.1: TLS đã cấu hình, HTTP redirect → HTTPS
- [ ] Task 5.2: Cron backup database đã set
- [ ] Task 5.3: Prometheus alert rules đã load
- [ ] Task 6.2: `/health` → status ok
- [ ] Task 7.3: AI agent đang chạy paper mode
- [ ] Task 7.4: Paper mode chạy ≥ 7 ngày không crash
- [ ] Task 7.6: Ít nhất 1 consultant agent đã thêm
- [ ] Task 8.2: Promote to live pass
- [ ] Task 8.3: LIVE_TRADING=true + server restart
- [ ] Task 8.4: `/api/ai/status` → mode: live, running: true
