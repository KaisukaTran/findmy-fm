# UI Rebuild Brief — FINDMY-FM Dashboard (bản giao cho phiên Opus điều phối)

> **Ngày viết:** 2026-08-23 · **Nhánh:** `kss-capital-auto-sizing` · **Trạng thái:** CHỜ KAI DUYỆT
> **Người đọc:** một phiên Claude **Opus** mới, vai *orchestrator*, điều phối **Sonnet** (viết code)
> + **Haiku** (việc cơ học). Tài liệu tự đủ — không giả định đã đọc context nào khác ngoài repo.
> **Nguồn:** audit UI đầy đủ (19 partial, `routes.py` 1246 dòng, `app.js` 743, `style.css` 417) +
> kiểm chứng trực tiếp trên instance paper `127.0.0.1:8000` (chỉ GET). Mọi khẳng định là
> **VERIFIED** (đã đọc code/dữ liệu) trừ khi ghi rõ **ASSUMED**.

## 0. Đọc gì trước khi bắt đầu

Bắt buộc, theo thứ tự: (1) tài liệu này; (2) `app/templates/dashboard.html` (248 dòng — khung tab +
toàn bộ `hx-trigger`); (3) `app/templates/partials/kss_settings.html` (318 dòng/45KB — bề mặt sửa
nặng nhất); (4) `app/static/app.js` **dòng 572–641** (nhánh `kss-settings-form`, gốc lỗi P-1);
(5) `app/routes.py` **390–470** (`KssSettingsBody`, 89 field) và **816–1246** (toàn bộ `ui_router`);
(6) skill `htmx-dashboard` + `fm-conventions`.

Thêm: `docs/strategy-eval-2026-08-22.md` (582 dòng) — đọc **trước P2**, xem §10.
**KHÔNG** đọc cả `app/kss/service.py` (2011 dòng) hay `app/scanner.py` (1248 dòng) — việc này không
chạm logic chiến lược. Chỉ mở khi cần *tên trường* của dict trả về partial.

## 1. Bối cảnh

FINDMY-FM là bot crypto paper/live (FastAPI + SQLAlchemy + SQLite). Chiến lược lõi là **KSS Pyramid
DCA** (mua-thang-xuống). Dashboard hiện tại: server-render Jinja2 + HTMX polling + Alpine. Nó **chạy
được** và không có route chết — nhưng kiến trúc thông tin đã lệch:

- **Chiến lược không ở trung tâm.** Câu hỏi Kai hỏi mỗi ngày — *"tại sao bot không vào lệnh?"* —
  **không chỗ nào trả lời được**. Bằng chứng: 1468/1566 scan_runs (94%) có `universe_size=0`, bị
  chặn bởi "vượt ngân sách triển khai" 1104 lần và "max concurrent 12" 364 lần; UI chỉ hiện **một
  lần scan gần nhất** → thông tin quan trọng nhất chỉ nằm trong audit log thô.
- **OPUS chiếm tab top-level ngang hàng tab chiến lược lõi**, dù lợi nhuận hiện tại $0 (5 blocker).
- **Form Cấu hình có lỗ hổng an toàn vốn thật** (P-1) — sửa knob nhưng không lưu, im lặng.

Mục tiêu: **đưa chiến lược ra mặt tiền**, sửa dứt điểm P-1/P-2 bằng cấu trúc (không vá danh sách),
gom taxonomy trùng lặp, chuẩn hóa format/poll/badge.

## 2. Ràng buộc CỨNG (không thương lượng)

| # | Ràng buộc | Ghi chú |
|---|---|---|
| C1 | **Không sửa `app/kss/pyramid.py`** — công thức thang FROZEN | Cả `pyramid_up.py` cũng không |
| C2 | Không sửa logic trong `app/kss/service.py`, `app/scanner.py`, `app/backtest.py` | Chỉ được *đọc* để biết schema dict |
| C3 | Không đụng instance **live** (worktree riêng, cổng `:8001`) | Chỉ làm trên worktree paper `:8000` |
| C4 | **Không commit / không push** trừ khi Kai yêu cầu rõ | Cứ để working tree bẩn, báo cáo diff |
| C5 | Không phá **API contract** hiện có | Xem §2.1 |
| C6 | Tables, **không phải cards** cho mọi danh sách >1 dòng | Kai đã bác card-per-row: "quá xấu" |
| C7 | Mọi knob chiến lược phải **hiện + sửa được lúc runtime + có tooltip + hiện config-key** | Xem §7 |
| C8 | CSP chặt phải giữ nguyên, không `unsafe-inline` | Xem §9 |
| C9 | Nhãn UI tiếng Việt; identifier/path/config-key giữ tiếng Anh | Đang đúng, giữ |

### 2.1 API contract KHÔNG được phá

- **59 route `@api_router`** (`app/routes.py`) + **14 route** (`app/kss/routes.py`:
  `/api/kss/sessions`, `/sessions/{id}/start|stop|take-profit|dca-next|check-tp|dca-preview`, …).
  `app.js` gọi 25 endpoint `/api/*`: `autotrade, autoapprove, full-auto, breaker/reset,
  consensus-weights, grok, grok-scanner, guardian, kss-settings, kss/preview, kss/sessions,
  live-trading, opus, opus/shadow, orders, pending/approve-all, pending/reject-all, positions/close,
  savings, scan, scheduler, ta-source, telegram, telegram/test, withdrawals`.
  **Được thêm route mới; KHÔNG đổi/xóa route cũ.**
- **Telegram** (`app/notify.py`) gọi **hàm Python trong process** (`_cmd_summary/_cmd_kss/…`, nhận
  `db`), **không** qua HTTP nội bộ → đổi template không ảnh hưởng Telegram. **NHƯNG** nó POST sang
  instance anh em qua `settings.telegram_sibling_url` (`notify.py:705-709`) — giữ nguyên endpoint đó.
- `GET /ws` (`routes.py:1237`) phát tick "refresh" mỗi 10s; `app.js` biến tick thành event
  `refresh-*` cho HTMX. Đổi tên event phải đổi **cả hai đầu**.

## 3. Hiện trạng — inventory (VERIFIED)

9 tab (`dashboard.html:32-40`) → 19 partial trong `app/templates/partials/` → 17 endpoint riêng biệt,
**tất cả trả 200** khi kiểm live. Không có partial/route chết.

| Tab (`data-tab`) | Partial → endpoint (poll hiện tại) |
|---|---|
| Tổng quan (`overview`) | `summary` → `/partials/summary` (15s) · `performance` → `/partials/performance?period=` (20s, tự bake `period` vào `hx-get`) · `scanner` → `/partials/scanner` (30s) · `scanner_stats` → `/partials/scanner-stats` (30s) |
| Giao dịch (`trading`) | `kss` → `/partials/kss?page=` (10s) · `positions` → `/partials/positions?page=&sort=&dir=` (15s) · `pending` → `/partials/pending?page=` (10s) · `trades` → `/partials/trades?page=&side=` (15s) |
| OPUS (`opus`) | `opus` → `/partials/opus` (10s) |
| Phân tích lỗ (`losses`) | `losses` → `/partials/losses` (30s) · `lossautopsy` → `/partials/lossautopsy` (**chỉ event — bất đối xứng!**) |
| Lịch P&L (`calendar`) | `calendar` → `/partials/calendar?view=&year=&month=` (không poll) · `calendar_day` → `/partials/calendar/day?d=` (drill-down) |
| Chi phí (`costs`) | `costs` → `/partials/costs?period=` (không poll) |
| Tích trữ (`savings`) | `savings` → `/partials/savings` (30s — thừa, dữ liệu nhập tay) |
| Chiến lược (`strategy`) | `kss_settings` → `/partials/kss-settings` (**chỉ `load`**, cố ý) · `live_trading` → `/partials/live-trading` (chỉ event) |
| Nhật ký (`logs`) | `audit` → `/partials/audit?category=&page=` (15s) |
| luôn bật / modal | `status` → `/partials/status` (5s) · `ladder` → `/partials/ladder?session=\|symbol=` (theo click) |

### 3.1 Danh sách vấn đề đã xác định (dùng làm backlog)

- **P-1 (CRITICAL)** — Handler lưu form ở `app.js:572-641` **liệt kê tay** từng field POST lên
  `/api/kss-settings`. **16 field ĐANG render** trong template nhưng **thiếu trong danh sách tay** →
  sửa xong bấm "Lưu", hiện toast thành công, **giá trị bị vứt im lặng**: `loss_reentry_enabled,
  loss_reentry_weeks_1, loss_reentry_weeks_2, loss_reentry_blacklist_after, loss_reentry_pardon,
  max_session_deploy_usd, maxdca_allow_add, maxdca_max_underwater_pct, overextension_penalty_enabled,
  overextension_penalty_weight, overextension_lookback_bars, regime_gate_enabled,
  regime_gate_enforcing, regime_sma_fast, regime_sma_slow, regime_hysteresis_pct`.
  Trong đó có **`max_session_deploy_usd`** — chính là bức tường an toàn vốn (trần deploy/phiên).
  **Đây là bug an toàn vốn, không phải bug thẩm mỹ. Sửa đầu tiên, sửa bằng cấu trúc.**
- **P-2 (HIGH)** — `max_new_sessions_per_scan` có trong `KssSettingsBody` (89 field) nhưng **không
  có `<input>` nào** → vi phạm C7.
- **P-3 (HIGH)** — Hai taxonomy nguyên nhân lỗ độc lập, không tham chiếu nhau, trên **cùng một tab**:
  `losses.html` (`OPUS/KSS-SL/KSS-Trail/KSS-TP?/Khác`) vs `lossautopsy.html`
  (`dup_wave/size_outlier/pyramid_up_reversal/deep_ladder_sl/orphan/reversal`). Tệ hơn: cả hai **mượn
  class badge** đã mang nghĩa khác nơi khác — `.badge.ml-on` = "vị thế do OPUS quản" ở
  `positions.html` nhưng = "nguyên nhân deep_ladder_sl" ở `lossautopsy.html`; `.badge.hyperopt-on`
  gắn nhãn `pyramid_up_reversal` **dù không có feature hyperopt nào trong `app/`** (grep 0 hit).
- **P-4 (MEDIUM)** — Format tiền không nhất quán: `fee` ở `trades.html`/`losses.html` là
  `"%.4f"|format(...)` trần (không `$`, không filter); bảng ngày `opus.html` (`gross/cost/net`) và
  ô ngày/tuần `calendar.html` là `%+.2f`/`%+.0f` trần — trong khi tổng ở header cùng file lại có
  `$…|money_kmb`.
- **P-4b (MEDIUM, phát hiện mới)** — **Không có filter `price`.** `positions.html:36-37` render
  `avg_entry_price`/`current_price` bằng `| money` = `f"{v:,.2f}"` (`routes.py:49`) → **mọi coin giá
  < $0.005 hiển thị `0.00`**. Với universe altcoin, đây là lỗi hiển thị thật.
- **P-5 (MEDIUM)** — `.cards` dùng ở 6 partial, **4 ngoài Overview** (`kss`, `opus`, `losses`,
  `lossautopsy`) → cần Kai chốt (§16 Q2).
- **P-6 (MEDIUM)** — `#kss-settings-form` có ~66 field và **không rule grid/multi-column nào** trong
  `style.css` (chỉ `label{display:flex;flex-direction:column}`) → **một cột dọc dài kể cả desktop
  1280px+**. Tooltip `title="…"` **chỉ hover** → **vô dụng trên điện thoại**, dù Kai yêu cầu dùng
  được trên phone.
- **P-7 (LOW)** — Code chết: `.statusbar-hyperopt`/`.statusbar-ml` (`style.css:57`) không template
  nào dùng; **đúng 1 inline `style=` trong cả cây template** (`kss_settings.html:55`) — vừa bị CSP
  chặn vừa vô nghĩa (`grid-column` nhưng cha không `display:grid`).
- **P-8 (LOW)** — Poll tùy hứng 5/10/15/20/30s, không theo tầng nào. **P-9 (LOW)** — `status.html`
  **nhân đôi toàn bộ dải badge** (`.auto-mobile-wrap` + `.auto-inline`, ~60 dòng gần y hệt) chỉ để
  xử lý breakpoint CSS. **P-10 (INFO)** — hai cơ chế refresh chồng nhau: partial tự poll **và**
  `/ws` tick 10s bắn `refresh-*` (vd `status.html` poll 5s **cộng** WS refresh 10s).

## 4. Kiến trúc thông tin MỤC TIÊU (chiến lược ở mặt tiền)

Từ 9 tab → **7 tab**, sắp theo mức độ Kai thực sự nhìn:
**1. Chiến lược** (LANDING mới — Overview + funnel scanner) ← trọng tâm · **2. Sổ đang mở**
(KSS sessions + Positions; Pending/Trades thành sub-view) · **3. Phễu quét** (scanner gate-by-gate
theo thời gian) · **4. Phân tích lỗ** (một taxonomy duy nhất) · **5. Lịch P&L** · **6. Cấu hình**
(settings + live toggle + OPUS hạ cấp vào đây) · **7. Nhật ký** (audit; Chi phí + Tích trữ giữ
riêng hoặc thành sub-view — Q4).

**OPUS bị hạ từ tab top-level xuống một section trong Cấu hình** (hoặc tab "Nâng cao/Thử nghiệm" thu
gọn), giữ nguyên `/partials/opus` — chỉ giảm trọng số điều hướng cho khớp đóng góp $0 hiện tại.

## 5. Spec từng tab

### 5.1 Tab **Chiến lược** (landing)

| Khối | Nguồn → việc phải làm |
|---|---|
| Dải KPI 7 ô | `/partials/summary` → **tái dùng nguyên trạng**; đây là chỗ DUY NHẤT `.cards` được phép (Q2) |
| Equity + win/loss + toggle kỳ | `/partials/performance?period=` → tái dùng, giữ pattern bake `period` vào `hx-get` |
| **Funnel cổng scanner (MỚI)** | **cần endpoint mới** → §5.1.1 |
| Ứng viên scan gần nhất | `/partials/scanner` → tái dùng; thêm cột "Lý do" rút gọn (chip) thay vì bắt mở từng `<details>` |
| Timing/cache scan | `/partials/scanner-stats` → tái dùng, đẩy xuống cuối trang |

#### 5.1.1 Funnel cổng scanner — **hạng mục giá trị cao nhất của cả bản rebuild**

Trả lời câu "tại sao bot không vào lệnh?" bằng **một bảng** (không card, không đồ họa funnel cầu
kỳ), trailing **24h / 7d** (toggle như `performance`). Cột: `Giai đoạn` (nhãn cố định) ·
`Số lượng` (đếm) · `% còn lại` (tính) · `Ghi chú` (ngưỡng hiện hành, đọc từ `settings`).

Các dòng, đúng thứ tự pipeline `app/scanner.py`: `ứng viên vào` → `thin-skip` → `pre-blocked` →
`skipped_rel_strength` → `skipped_downtrend` → `skipped_entry_timing` → `scanner_veto` → `chặn vì
ngân sách (equity_backup_pct)` → `chặn vì max concurrent` → `chặn vì per-scan cap` → **`đã mở phiên`**.

- **Endpoint MỚI:** `GET /partials/scan-funnel?window=24h|7d`, poll **T2 (30s)**. **Nguồn:** rollup
  trên `scan_runs` (đã có `universe_size` + lý do skip) và `AuditLog` (category đã tag sẵn
  `skipped_rel_strength`, `scanner_veto`, `skipped_downtrend`, `skipped_entry_timing`); dùng
  `app/auditview.py:recent_by_category` làm mẫu — nó đã có `scan_cap` để truy vấn rẻ.
- **ASSUMED:** tên cột chính xác của `scan_runs` — Sonnet phải đọc `app/models.py` xác nhận trước
  khi viết query. **Không đoán.**
- **Dòng "chặn vì ngân sách" phải nổi bật** khi >50% tổng skip — đã xảy ra 1104 lần trong 33 ngày mà
  UI hoàn toàn im lặng.

### 5.2 Tab **Sổ đang mở**

- **Bảng chính: phiên KSS** (`/partials/kss?page=`), cột giữ nguyên (coin, status, waves, uPnL,
  ladder link, actions Start/DCA+/TakeProfit/Stop/Delete). **Bỏ dải 5 card con** → thành **một dòng
  tóm tắt mảnh** (sessions/active/deployed/reserved/free-cash) trên bảng, hoặc đẩy vào KPI Overview.
  Badge `strategy_mode` (PYR↑/DCA↓) **giữ nguyên** — `pyramid_up` ngủ đông theo config
  (`strategy_router_enabled=false`), **không phải code chết**.
- **Bảng thứ hai: vị thế** (`/partials/positions?page=&sort=&dir=`). Giữ pattern sort server-side
  qua querystring (state sống sót qua poll — nên nhân rộng). Badge `Src` đổi sang class riêng (§8).
- **Pending + Trades hạ xuống sub-view** (tab-trong-tab hoặc drawer "Lịch sử") — hiện là 4 widget
  phân trang độc lập trên cùng màn hình. Poll: KSS + Positions **T1 (10s)**; Pending/Trades chỉ load
  khi mở sub-view.

### 5.3 Tab **Phễu quét** (tùy chọn, xem Q3)

Nếu funnel §5.1.1 làm landing quá dày, tách tab riêng: funnel + breakdown theo symbol theo thời gian
+ xu hướng "budget skip vs concurrency skip". Tái dùng `/partials/scanner`,
`/partials/scanner-stats`, `/partials/scan-funnel`.

### 5.4 Tab **Phân tích lỗ**

- **Hợp nhất taxonomy (P-3):** chọn `root_cause` của `lossautopsy` (mịn hơn) làm **chuẩn duy nhất**;
  view cấp fill gom theo cùng nhãn đó, hoặc **bỏ hẳn dải card `by_cause`** và chỉ giữ bảng
  log-theo-fill + bảng theo-cặp. **Badge nguyên nhân lỗ phải có class riêng** (`.cause-*`), không
  mượn `.badge.ml-on/.hyperopt-on/.breaker-frozen/.veto`.
- Giữ nguyên bảng phân biệt thắng-thua (consensus/win_rate_lb/expectancy/worst_mae/avg_mae theo
  percentile) và bảng 120 case xấu nhất — đã tốt.
- Sửa refresh bất đối xứng: hai partial cùng **T2 (30s)** hoặc cùng chỉ-event.

### 5.5–5.8 Các tab còn lại

- **Lịch P&L** — giữ nguyên; chỉ sửa P-4 (thêm `$` + filter cho ô ngày/tuần).
- **Cấu hình** — xem §7. Gồm form KSS chính, 3 form riêng (live-exec, consensus-weights,
  grok-fail-mode), 2 panel toggle (Grok scanner, pandas-ta), `live_trading.html`, **+ section OPUS
  bị hạ cấp**.
- **Nhật ký** — giữ nguyên; là **pattern mẫu** (lọc category server-side + phân trang) để dựng bảng
  funnel và bảng nguyên nhân lỗ mới.
- **Chi phí / Tích trữ** — giữ nội dung; `savings.html` bỏ poll 30s → `load` + `refresh-savings`.

## 6. So sánh PAPER / LIVE (hạng mục MỚI, không phải đổi tên)

Hiện chỉ có badge `mode.label`/`mode.cls` ở header (`routes.py:877-885`) + panel
`live_trading.html`. **Không tồn tại view so sánh song song** hai instance. Đây là **việc mới, cần
fetch chéo process** (hai process, hai DB): instance này gọi HTTP sang sibling —
`settings.telegram_sibling_url` là tiền lệ cấu hình sẵn có. → **Xếp vào P4, chỉ làm nếu Kai xác
nhận Q5.** Không đoán schema.

## 7. Quy tắc FORM CẤU HÌNH (bắt buộc, đây là phần an toàn vốn)

**R1 — Submit sinh từ chính form, KHÔNG liệt kê tay.** Handler duyệt `new FormData(form)` /
`form.elements` và build payload từ đó → thêm `<input name="X">` vào template là **tự động** được
gửi. Đóng vĩnh viễn P-1 **và** P-2, thay vì vá danh sách hôm nay rồi vỡ lại tháng sau.
- Ép kiểu: number → `Number()`, checkbox/toggle → boolean, text → string. Gợi ý: gắn
  `data-type="num|bool|str"` lên input để handler khỏi phải đoán.
- Field rỗng/không đổi → gửi `null`; schema đã là `X | None = Field(None, …)` nên `null` = "không
  đổi" (**VERIFIED**: cả 89 field đều Optional).

**R2 — Phủ 1:1 với schema.** Mọi field trong `KssSettingsBody` (**89**, `routes.py:390`) phải có
đúng một input render; nếu cố tình ẩn, phải có comment code giải thích trỏ về brief này. Chiều
ngược lại: field trong form mà không có trong schema chỉ được phép nếu thuộc schema tách riêng cố ý
— hiện chỉ có `ConsensusWeightsBody` (`trend/dip/volatility/liquidity`). Tên lạ khác = bug.

**R3 — Mỗi knob: nhãn VN + `<span class="varname">config_key</span>` + tooltip.** Pattern `varname`
đã phủ 100% field hiện tại (`kss_settings.html:5,7,9,…`) — giữ.

**R4 — Tooltip phải chạm được không cần hover.** Nút "ⓘ" focusable/tappable hiện giải thích khi tap
(≤768px). Giữ `title=` cho desktop cũng được, nhưng **không được là đường duy nhất**.

**R5 — Nhóm thành section điều hướng được** (`<details>` accordion hoặc tab-trong-tab), thay
`<h3 class="section-subhead">` thuần văn bản. Nhóm đề xuất (số knob ước tính): `Thang & TP/SL lõi`
(8) · `Vốn & giới hạn phiên` (7) · `Chặn tái vào sau lỗ` (7) · `Cổng scanner` (9) ·
`Ride & Trail động` (10) · `Lọc coin nâng cao` (8) · `Pyramid-UP` (7, badge **TẮT**) ·
`Regime gate BTC` (5, badge **SHADOW**) · `OPUS God Mode` (20, badge blocked) ·
`Live exec / Grok / consensus` (3 form riêng — **giữ riêng**, chúng có action lưu riêng, thế là đúng).

**R6 — Layout nhiều cột trên desktop.** ≥1280px không được là một cột dọc 66 dòng.

**R7 — Chỉ dấu "có thể đã cũ".** Form cố ý chỉ `load` một lần (tránh ghi đè khi đang gõ — comment
trong template giải thích, **giữ hành vi này**), nhưng phải có affordance nhẹ "cấu hình có thể đã
đổi ở nơi khác — tải lại?" thay vì im lặng.

**R8 — Xóa inline `style=` ở `kss_settings.html:55`** (CSP-chết + layout-chết).

## 8. Quy tắc THỊ GIÁC

- **Bảng, không phải card.** Mọi danh sách >1 dòng thực thể so sánh được → `<table>`. Zero
  card-per-row (hiện đang đúng — không được thụt lùi).
- **Dải KPI `.cards` chỉ ở tab Chiến lược (landing).** Nơi khác: dòng tóm tắt mảnh, badge inline,
  hoặc footer bảng. Chờ Kai chốt Q2 — nếu Kai cho ngoại lệ, ghi rõ **từng vị trí** "giữ như ngoại lệ
  đã duyệt" trong plan, không để mơ hồ.
- **Tiền — quy tắc thống nhất, ghi vào comment đầu block filter của `routes.py`:** `money_kmb` →
  số tổng hợp/KPI/tiêu đề (equity, cash, tổng P&L, tổng chi phí) · `money` → số theo dòng (P&L từng
  lệnh, phí, giá trị vị thế) · **`price` (filter MỚI, sửa P-4b)** → giá coin, chính xác thích ứng
  (≥$1 → 2 chữ số; $0.01–$1 → 4; <$0.01 → 6–8 chữ số có nghĩa), áp cho `avg_entry_price`,
  `current_price`, giá thang. **Mọi số tiền có tiền tố `$`**; không còn `%.2f`/`%.4f` trần ở đâu.
- **Badge: class ngữ nghĩa riêng theo miền, không dùng chéo** — `.badge.auto-*` (tự động hóa) ·
  `.badge.src-*` (nguồn lệnh KSS/OPUS) · `.cause-*` (nguyên nhân lỗ). Xóa
  `.statusbar-hyperopt`/`.statusbar-ml` (`style.css:57`) và mọi class `hyperopt-*`.
- **Badge PAPER/LIVE** ở header luôn nhìn thấy, tương phản mạnh (LIVE = đỏ/cảnh báo). Nguồn:
  `settings.live_trading` + `settings.live_use_testnet` (`routes.py:877-885`).
- **Nhãn VN, key tiếng Anh.** Không dịch config-key.
- **Dark/light:** hiện **chỉ dark** — `style.css` có `:root` nhưng **không có
  `prefers-color-scheme`** (VERIFIED). Light theme là **tùy chọn**, chỉ ở P4, và chỉ khi làm được
  bằng cách hoán token màu trong `:root` (không rải màu cứng khắp file).

## 9. Quy tắc KỸ THUẬT

- **Stack:** Jinja2 (server-render) + HTMX (poll/swap partial) + Alpine (tương tác nhỏ). **Không**
  thêm framework/build step/npm. **Assets chỉ trong `app/static/`** (`htmx.min.js`, `alpine.min.js`
  đã có sẵn); **tuyệt đối không CDN ngoài** — CSP chặn, và không được nới CSP.
- **CSP hiện hành (`app/security.py:36`), giữ nguyên:**
  `default-src 'self'; img-src 'self' data:; style-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'`
  → **không inline `<script>`, không `onclick=`, không inline `style=`.** Toàn bộ JS ở
  `/static/app.js`, nạp bằng `<script defer src="/static/…">`.
- **Auto-reload:** Jinja2 `Environment.auto_reload` mặc định True và `app/` **không override**
  (grep 0 hit) → **sửa `.html` thấy ngay, không cần restart**; `StaticFiles` đọc từ đĩa nên
  `.css`/`.js` cũng vậy. **Sửa `.py` thì PHẢI restart paper:** `Start-ScheduledTask FINDMY-Restart`
  (chạy được ở quyền non-elevated — agent tự làm, không hỏi Kai). Chỉ restart **paper `:8000`**.
- **Tầng poll chuẩn hóa (P-8) — chọn từ đây, không chế số mới:** **T0 = 5s** (trạng thái tự động hóa
  live: `status`) · **T1 = 10s** (dữ liệu giao dịch đang chạy: KSS sessions, positions) ·
  **T2 = 30s** (phân tích/scanner/funnel/losses) · **T3 = load + event** (dữ liệu nhập tay hoặc lịch
  sử: savings, costs, calendar, settings).
- **Guard poll bắt buộc.** Trong panel: `[tabActive(this) && document.visibilityState==='visible']`.
  Ngoài panel (status/summary): `[document.visibilityState==='visible']`. **Không bao giờ**
  `every Ns` trần.
- **`/ws` tick (P-10):** phân công rõ — đề xuất WS chỉ bắn `refresh-*` cho partial ở **T3**; T0–T2
  tự poll. Ghi quyết định vào comment trong `app.js`.

## 10. ĐIỂM MỞ RỘNG (chờ `docs/strategy-eval-2026-08-22.md`)

`docs/strategy-eval-2026-08-22.md` (582 dòng) được viết **song song** với brief này và **chưa được
đọc khi soạn brief** — nội dung §2 (bộ tham số tốt nhất), §3 (cải tiến đề xuất) và §7 (quyết định
cần Kai) của nó **sẽ** sinh ra knob/metric mới. **Chỉ thị: Opus PHẢI đọc file đó trước P2, và KHÔNG
ĐOÁN trước khi đọc.** Chừa sẵn điểm mở rộng có đánh dấu, để cắm vào sau mà không sửa kiến trúc:

| ID | Vị trí | Chừa sẵn cái gì |
|---|---|---|
| **EXT-1** | Form Cấu hình | Một nhóm section rỗng có tên tạm `Tinh chỉnh từ đánh giá chiến lược`. Nhờ R1 (submit sinh từ form), thêm knob mới **chỉ cần thêm 1 `<input>` + 1 field vào `KssSettingsBody`** — không đụng JS. |
| **EXT-2** | Tab Chiến lược | Chừa một hàng trống dưới dải KPI cho metric mới (ví dụ payoff ratio, MAE distribution) — **hiện đo được: WR 81%, avg win $2.95 vs avg loss -$6.28, payoff 0.47** — nhưng đợi báo cáo chốt metric nào lên mặt tiền. |
| **EXT-3** | Phân tích lỗ | Taxonomy `root_cause` phải đọc từ **một hằng số Python duy nhất** (không hard-code nhãn trong template), để báo cáo thêm nguyên nhân mới mà không sửa template. |
| **EXT-4** | Funnel scanner | Danh sách "giai đoạn" cũng lấy từ một hằng số duy nhất, cùng lý do. |

Opus đọc báo cáo rồi mở **một phase phụ** cắm vào EXT-1..4. Không chặn P1; có thể chạy cùng P2–P3.

## 11. Mô hình ỦY QUYỀN

**Opus (orchestrator) — tự làm, không giao:** (1) đọc §0, dựng plan theo phase, **trình Kai duyệt
trước khi code** (quy trình dự án: analysis → plan → approve → build); (2) viết spec từng partial cho
Sonnet — đúng cột, sort, endpoint, tầng poll; (3) **review hai lượt** mỗi diff: lượt 1 đối chiếu
spec, lượt 2 đối chiếu §2 (ràng buộc cứng) + §13 (acceptance); (4) **xác minh live** — chạy test,
restart paper nếu sửa `.py`, gọi HTTP thật vào `:8000`, screenshot 1280px + 375px; (5) giữ ngân sách
context, **không** đổ cả `service.py`/`scanner.py` vào context.

**Sonnet — viết code:** template, CSS, JS, endpoint mới, test. Mỗi lần một phase, một partial. TDD:
viết test trước. **Haiku — việc cơ học:** trích `name="…"` từ template; trích field từ
`KssSettingsBody`; chạy set-difference; grep inline `style=`/`<script`/`onclick=`; đếm/đổi tên class
badge hàng loạt; kiểm tra mọi số tiền đều có filter.

## 12. TDD

Test mới ở `tests/app/test_ui_*.py`, dùng FastAPI `TestClient`. Fixture mẫu đã có ở
`tests/app/conftest.py` (DB tạm, `REQUIRE_AUTH=false`, schema tạo/xóa mỗi test). Lệnh chạy
(VERIFIED): `D:/FINDMY/.venv/Scripts/python.exe -m pytest tests/app -o addopts="" -p no:cacheprovider -q`

Bộ test tối thiểu bắt buộc:

- `test_ui_partials.py` — mọi endpoint partial trả **200 với DB rỗng** (không 500 khi 0 dòng) và
  chứa chuỗi khóa đặc trưng. Hiện 17 endpoint; rebuild không được làm hỏng cái nào (gộp/bỏ thì
  phải ghi rõ trong plan + cập nhật test).
- `test_ui_settings_coverage.py` — **test hồi quy quan trọng nhất**: (1) tập `name="…"` render trong
  form == tập key handler gửi đi — hôm nay lệch **16**, sau rebuild phải **0**; (2) mọi field trong
  `KssSettingsBody` (89) có input — hôm nay thiếu **1** (`max_new_sessions_per_scan`), sau rebuild
  **0**; (3) mọi input có `varname` + nguồn tooltip khác rỗng.
- `test_ui_csp.py` — grep template: **0** inline `style=`, **0** `<script>` có thân, **0**
  `onclick=`/`onload=`; header response vẫn đúng chuỗi CSP ở §9.
- `test_ui_money_format.py` — không template nào còn `%.2f`/`%.4f` cho số tiền mà thiếu `$`+filter.
- `test_ui_funnel.py` — endpoint funnel mới: 200 với DB rỗng, tổng các dòng nhất quán, toggle
  `window=24h|7d` hoạt động.

## 13. PHASE + tiêu chí nghiệm thu

**P1 — Khung + tab Chiến lược.** Khung 7 tab mới, nav, badge PAPER/LIVE, `status.html` gộp còn
**một** khối DOM (P-9); tab Chiến lược = KPI + performance + scanner (tái dùng nguyên trạng); chuẩn
hóa tầng poll T0–T3 cho mọi partial hiện có.
→ *Nghiệm thu:* suite xanh · 17 endpoint cũ vẫn 200 · `test_ui_csp.py` xanh ·
`grep -rn 'style="' app/templates/` trả **0** (nay 1) · không partial nào còn `every Ns` trần.

**P2 — Sổ đang mở + Cấu hình** *(phase giá trị cao nhất)*. Sửa **P-1 bằng R1** (submit sinh từ
FormData) + render `max_new_sessions_per_scan` (P-2); accordion R5, đa cột R6, tooltip tap-được R4,
xóa inline style R8; Sổ đang mở: KSS + Positions là bảng chính, Pending/Trades thành sub-view, bỏ
dải card của `kss`.
→ *Nghiệm thu:* `test_ui_settings_coverage.py` xanh cả 3 mệnh đề · **thử tay: đổi
`max_session_deploy_usd` trên UI → POST thật → GET lại → giá trị đã đổi** (bằng chứng P-1 đã chết) ·
form không còn một-cột ở 1280px · tooltip đọc được ở 375px khi vô hiệu hóa `:hover`.

**P3 — Funnel scanner + Phân tích lỗ.** Endpoint `/partials/scan-funnel` + bảng funnel trên landing
(§5.1.1); hợp nhất taxonomy lỗ về `root_cause` + class `.cause-*` riêng + sửa refresh bất đối xứng;
sửa P-4 và thêm filter `price` (P-4b) trên toàn bộ template.
→ *Nghiệm thu:* `test_ui_funnel.py` + `test_ui_money_format.py` xanh · funnel hiển thị đúng "chặn vì
ngân sách" là nguyên nhân skip lớn nhất trên dữ liệu paper thật · coin giá <$0.01 hiện khác `0.00`.

**P4 — Polish.** Mobile 375px, xóa CSS chết (P-7), hạ cấp OPUS vào Cấu hình, (tùy chọn) light theme,
(tùy chọn, chờ Q5) view song song PAPER/LIVE, cắm EXT-1..4 nếu báo cáo chiến lược đã xong.
→ *Nghiệm thu:* ở 375px `<body>` **không** có thanh cuộn ngang (bảng rộng cuộn trong `.scroll`
riêng — pattern đã có) · `.statusbar-hyperopt`/`.statusbar-ml` đã xóa hoặc dùng thật · screenshot
1280px + 375px cho cả 7 tab.

## 14. Checklist XÁC MINH (chạy cuối mỗi phase, không bỏ bước)

1. `pytest tests/app -o addopts="" -p no:cacheprovider -q` (venv ở §12) — **xanh toàn bộ**, không chỉ test mới.
2. Nếu có sửa `.py`: `Start-ScheduledTask FINDMY-Restart`, rồi xác nhận server boot sạch (không traceback trong log).
3. HTTP thật vào `127.0.0.1:8000`: `GET /` + **mọi** endpoint partial → 200; `curl -D-` xác nhận header CSP còn nguyên chuỗi ở §9.
4. Grep cơ học (giao Haiku): inline style = 0 · `<script>` có thân = 0 · `onclick=` = 0 · số tiền thiếu filter = 0.
5. Screenshot 1280px + 375px cho mọi tab đã đụng; xác nhận **không commit nào được tạo** (trừ khi Kai bảo).

## 15. DANH SÁCH "TUYỆT ĐỐI KHÔNG"

- ❌ Sửa `app/kss/pyramid.py` (math FROZEN) hay `pyramid_up.py`; sửa logic trong `service.py` /
  `scanner.py` / `backtest.py` / `dynamic_exit.py`.
- ❌ Commit / push / tạo PR — trừ khi Kai yêu cầu rõ ràng.
- ❌ Đụng instance live `:8001` (sửa file, restart, đọc-ghi worktree của nó).
- ❌ Đổi giá trị runtime setting để "test cho tiện" — paper đang chạy với vị thế mở (10 phiên
  active, ~$278 đã triển khai).
- ❌ Đổi/xóa route `/api/*` đang tồn tại; đổi tên event `refresh-*` ở chỉ một đầu.
- ❌ Thêm CDN / npm / build step / framework mới; nới CSP.
- ❌ Xóa `pyramid_up` UI/settings — nó **ngủ đông theo config**, không phải code chết.
- ❌ Đoán knob/metric của báo cáo chiến lược — dùng EXT-1..4.
- ❌ Dùng card-per-row ở bất kỳ đâu.

## 16. CÂU HỎI KAI PHẢI CHỐT TRƯỚC KHI ĐỘNG VÀO UI

| # | Câu hỏi | Đề xuất mặc định |
|---|---|---|
| **Q1** | Gộp 9 tab → 7 và **hạ OPUS** xuống section trong Cấu hình — đồng ý? | Đồng ý (OPUS đang $0 lợi nhuận) |
| **Q2** | Dải KPI `.cards`: **chỉ** cho tab Chiến lược, hay cho phép dải mảnh ở mỗi tab? | Chỉ landing; nơi khác dùng dòng tóm tắt mảnh |
| **Q3** | Funnel scanner: nhúng vào landing, hay tách tab "Phễu quét" riêng? | Nhúng landing trước; tách nếu quá dày |
| **Q4** | "Chi phí" + "Tích trữ" giữ tab riêng hay gộp thành sub-view? | Giữ riêng (ít động, nhưng Kai xem định kỳ) |
| **Q5** | Có làm view **so sánh song song PAPER/LIVE** không? Đây là việc mới, cần fetch chéo process | Hoãn tới P4; chỉ làm nếu Kai cần |
| **Q6** | Light theme: có làm không, hay giữ dark-only? | Giữ dark-only; light là tùy chọn P4 |
| **Q7** | Sau khi sửa P-1, có muốn **audit lại toàn bộ 89 knob runtime** xem giá trị thật khớp ý Kai không (16 knob có thể đã bị vứt âm thầm nhiều tháng)? | **Có — nên làm ngay sau P2** |
