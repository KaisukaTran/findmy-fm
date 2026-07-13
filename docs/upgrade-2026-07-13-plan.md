# Plan nâng cấp 2026-07-13 — Bảo mật · Nâng cấp · Loại bỏ · Config 1000 USDT

> **Trạng thái: KAI ĐÃ DUYỆT 2026-07-13 — đang chạy liên tục A→F.** Nguồn: workflow 13 agents
> (5 recon song song + 8 verify đối kháng, 0 lỗi) chạy trên HEAD `c74dd35`, đối chiếu audit
> 2026-07-12 (16 findings) và DB thật. Mọi con số đều có anchor `file:line` đã verify chéo.
> Mô hình thực thi: **Opus phân việc + kiểm tra 2 lượt · Sonnet 5 code chính · Haiku việc cơ học.**
>
> **QUYẾT ĐỊNH KAI (2026-07-13):** (1) mục tiêu = **5-8%/tháng** theo dữ liệu paper đo được (bỏ
> 10-15%/ngày); (2) **D1 = sửa cơ chế lỗi defensive rung** (giữ chiến lược pyramid_up); (3) OPUS
> cascade = **chỉ disable** (không xóa code); (4) ml.py = **DELETE**; (5) **duyệt chạy liên tục A→F**
> (mỗi phase vẫn gated: full-suite-green + live-verify; commit theo concern chỉ khi Kai yêu cầu).

---

## 0. REALITY CHECK — mục tiêu 10-15%/ngày (bắt buộc đọc trước)

**Kết luận thẳng: 10-15%/ngày là bất khả thi về mặt toán học với chiến lược này — và với MỌI
chiến lược spot bền vững.** Chênh lệch so với năng lực đo được của bot là **~100×**, không phải
vấn đề tinh chỉnh config.

| Phép tính | Kết quả |
|---|---|
| 10%/ngày compound 1 tháng | 1.10³⁰ = **17.45×** → $1,000 → $17,449/tháng |
| 10%/ngày compound 1 năm | ~1.28e15× → con số tự phủ định |
| Ngày 1 cần | +$100 net = **35-45 chu kỳ TP thắng/ngày** (net ~$2.2-2.9/lệnh thắng, 0 lệnh thua) |
| Thực tế chiến lược | giữ lệnh **vài ngày-tuần** (deadline 30d, exit trung bình ở wave ~2.7), 5 slot |
| Lịch sử đo được | ~5-8%/tháng trên vốn ĐÃ TRIỂN KHAI; ~$300-450 triển khai bình quân trên $1k |
| **Kỳ vọng trung thực trên $1k** | **~$20-40/tháng (2-4%/tháng equity); trần lạc quan ~$60-100/tháng (0.3%/ngày)** |

Chính cái lồng an toàn của bot cũng cấm mục tiêu này: `DAILY_LOSS_HARD_PCT=3` đóng băng bot ở
−$30/ngày — một hệ chấp nhận lỗ tối đa 3%/ngày không thể nhất quán nhắm +10%/ngày. Ép mục tiêu đó
= dồn vốn 1-2 coin + coin biến động cao + bỏ dự phòng — đúng profile của 2 thảm họa chiếm 87% tổng
lỗ paper (C −$12,597, NFP −$8,139). Bất kỳ thứ gì hứa 10%/ngày ở cỡ vốn này là đòn bẩy (rủi ro
cháy tài khoản) hoặc lừa đảo.

**Khuyến nghị:** giữ mục tiêu Kai đã chốt 2026-07-12 — **3-5%/tháng trên equity**, tăng dần bằng
utilization + cắt đuôi lỗ (Phase D/E bên dưới), KHÔNG bằng đòn bẩy/cô đặc vốn.

---

## 1. CONFIG TỐI ƯU CHO 1000 USDT (instance LIVE)

Profile $1k đã áp sẵn vào `D:\FINDMY-live` ([live-readiness]) gần khớp với đạo hàm từ code —
**trừ 1 lỗ hổng CRITICAL** phát hiện hôm nay:

> ⚠️ **CRITICAL:** `live_max_order_notional` mặc định **$25** ([config.py:72-73](../app/config.py#L72-L73));
> [orders.py:348-353](../app/orders.py#L348-L353) từ chối mọi BUY live vượt mức này →
> **wave 2-3-4 ($29.40/$43.22/$56.47) đều bị TỪ CHỐI — ladder gãy ngay lần trung bình giá đầu tiên
> của MỌI session.** Phải đặt **`LIVE_MAX_ORDER_NOTIONAL=60`** trước khi chạy live. (Verify đối
> kháng đã xác nhận cả 2 lượt.)

Bảng ladder khuyến nghị (`W=$15, d=2%, N=4` — công thức FROZEN `cost(n)=(n+1)·W·(1−d/100)ⁿ`,
[pyramid.py:218](../app/kss/pyramid.py#L218), [:224-225](../app/kss/pyramid.py#L224-L225)):

| Wave | Cost | Lũy kế | Ghi chú |
|---|---|---|---|
| 1 | $15.00 | $15.00 | ≥ min notional $10 với biên 50% |
| 2 | $29.40 | $44.40 | > $25 → cần nâng `live_max_order_notional` |
| 3 | $43.22 | $87.62 | |
| 4 | $56.47 | **$144.09** | full ladder < deploy cap $150 |

5 session × $144.09 = **$720.45 ≤ $750** ngân sách (equity × 75%, [scanner.py:920-925](../app/scanner.py#L920-L925)), còn $250 backup + $29 slack.

| Knob | Giá trị | Lý do (code-derived) |
|---|---|---|
| `ACCOUNT_EQUITY` (env) | 1000 | gốc của `_free_cash` + risk equity |
| `kss_first_wave_usd` | 15 | wave 1 = $15 ≥ min notional, biên chống LOT_SIZE round-down ([execution.py:200-206](../app/execution.py#L200-L206)) |
| `scan_max_waves` | 4 | ladder $144; bằng chứng paper: TP exit trung bình wave 2.77, depth >4 chỉ cô đặc lỗ |
| `scan_distance_pct` | 2.0 | đáy ladder ≈ −5.9% entry; chờ E2 (ATR-spacing) rồi mới cá nhân hóa |
| `scan_tp_pct` | 3.0 | engine tự cộng buffer phí +0.24% → exit +3.24%, net ~+2.9% ([costengine.py:15-30](../app/costengine.py#L15-L30)) |
| `max_concurrent_sessions` | 5 | 5×$144 ≤ $750 |
| `max_new_sessions_per_scan` | 2 | ramp 5 slot qua ≥3 scan, không burst |
| `max_sessions_per_symbol` | 1 | K-1, chống blended basis |
| `equity_backup_pct` | 25 | $250 không bao giờ deploy |
| `max_session_deploy_usd` | 150 | tường cứng > $144.09 ([service.py:170-180](../app/kss/service.py#L170-L180)) |
| `cash_floor_usd` | 50 | chốt chặn phụ cho lệnh manual |
| **`live_max_order_notional`** | **60** | **⚠️ BẮT BUỘC — xem trên** |
| `sl_pct` | 8 | full-ladder SL = −$11.5 = 1.15% equity; 2.6 lần lỗ liên tiếp là breaker 3%/ngày chặn |
| `kss_dynamic_tp_enabled` | true | + knobs trail y hệt runtime paper đã soak (arm 5 / lock 2 / gap 5 / min 3 / atr_mult 1.0) |
| `deadline_days` | 30 | timeout do scanner tự suy ra ([scanner.py:1164-1166](../app/scanner.py#L1164-L1166)) |
| Breaker (env) | `MAX_DRAWDOWN_PCT=10, DAILY_LOSS_HARD_PCT=3, MAX_CONSECUTIVE_LOSSES=3` | đã áp trong live profile |
| `GUARDIAN_ENABLED` | **false** | xem R1 — đang gọi API fail mỗi cycle, 0 veto toàn lịch sử |

Phí: round-trip 0.2% (0.3% gồm slippage mô phỏng); TP 3% net ~2.9%; giữ TP ≥ 3% để phí không ăn
quá 10% gross win. Kỳ vọng tuần đầu live: **$5-15**, không phải $100/ngày.

---

## 2. BẢO MẬT — còn lại sau P0 (P0 `b74376f` đã merge live, đã re-verify đứng vững)

| # | Mức | Việc | Anchor | Owner |
|---|---|---|---|---|
| S1 | **HIGH** | Docker bake toàn bộ secrets: tạo `.dockerignore` (loại `.env*`, `data/`, `*.db*`, `*.log`, `.git`, `.venv`); sửa CMD stale `src.findmy.api.main:app` → `app.main:app`; bind loopback thay `0.0.0.0` | [Dockerfile:8,12](../Dockerfile), docker-compose.yml:12, scripts/start_api.sh:4 | Haiku (nội dung cho sẵn) |
| S2 | MED | `.gitignore` chưa cover backup DB thật: thêm `data/*.bak*` + `*.bak-*`; di chuyển 6 file `data/findmy.db*.bak-*` hiện có vào `data/backups/` (đã ignore) | .gitignore:11-17 | Haiku + Opus (move file) |
| S3 | MED | Rate-limit riêng cho endpoint mutating + auth-fail (hiện chỉ có global 200/min): `@limiter.limit("10/minute")` cho approve/approve-all/live-trading/close/KSS stop-TP + `/internal/telegram/command` | [security.py:31,140-142](../app/security.py#L31), [routes.py:695-706](../app/routes.py#L695-L706) | Sonnet 5 |
| S4 | LOW | `note` không giới hạn độ dài → `max_length=500` | [kss/routes.py:28](../app/kss/routes.py#L28) | Haiku |

Đã PASS (re-verified, không cần làm): auth default-on + boot-guard, CSRF/CSP, không log secret,
allowlist chat-id Telegram cho cả text lẫn callback, không raw-SQL/eval/SSRF, approval gate nguyên vẹn.

---

## 3. TÍNH NĂNG CẦN LOẠI BỎ (mỗi mục đã qua verify đối kháng — verdict trong ngoặc)

| # | Mục | Verdict | Hành động |
|---|---|---|---|
| R1 | **AI Guardian** — gọi Anthropic FAIL **mỗi cycle 15 phút NGAY HÔM NAY** (uvicorn.err.log 14:32/15:02), fail-open, **0 veto toàn lịch sử** = thuần latency + noise | CONFIRMED | **DISABLE NGAY**: `GUARDIAN_ENABLED=false` cả 2 `.env` (POST /api/guardian chỉ là in-memory). Không xóa code đợt này |
| R2 | **hyperopt.py** — chưa từng chạy (audit=0), objective mô phỏng CHIẾN LƯỢC KHÁC (không SL, không phí — [hyperopt.py:69-76](../app/hyperopt.py#L69-L76) vs [scanner.py:365-370](../app/scanner.py#L365-L370)), chỉ tune 3 coin/285 | CONFIRMED | **DELETE** trọn checklist (kèm addendum verify: routes.py:26,269,837-853; scanner.py:224-229; config.py:340-343; scheduler.py:101-105; PairParams; app.js:471-479; .env/.env.example; GIỮ class CSS `hyperopt-on` đang tái dụng) |
| R3 | **ml.py + ml_agent** — 55,591 vote confidence=0.0 (chưa từng ảnh hưởng 1 consensus nào), model stale 5 tuần, cùng lỗi objective-ảo | CONFIRMED | **DELETE** (checklist: agents/__init__.py:8,14,18; runtime.py:308; app.js:486,677; kss_settings.html:212 chia lại weight 0.30; routes.py:270,857-872; config.py:344-347; MlModel; GIỮ CSS `.ml-on`) |
| R4 | **`max_deployed_pct`** — dây an toàn GIẢ: persisted=90 nhưng **không code nào đọc để gate** ([config.py:223](../app/config.py#L223) tự nhận "legacy") | CONFIRMED | **DELETE** knob + .env:68 + .env.example:78 + 3 chỗ docs đang trích dẫn sai nó làm guard |
| R5 | **regime_ramp** — off từ khi sinh, 0 audit row, floor kép (0.2 × max(1,…)) nên **không bao giờ chặn được gì** | CONFIRMED | **DELETE** ~50 LOC ([scanner.py:644-686](../app/scanner.py#L644-L686), GIỮ `_nbar_return`) + UI knob; thay bằng D3 regime gate THẬT |
| R6 | **OPUS orchestrator cascade** — inert từ 2026-06-20, $0, 0 positions; NHƯNG package chứa grok.py (scanner gate ĐANG SỐNG) + ledger/models (portfolio/costs đang đọc) | CONFIRMED (disable-safe) | **Giữ `opus_mode=0` + ẩn panel.** Tùy chọn xóa ~950-1,600 LOC phần chết (loop/policy/watch/distill/consensus) — cần sửa kèm main.py:96-98 + routes.py:520, dời 2 import brain trong grok.py:222,238. **Khuyến nghị: để đợt sau** |
| — | GIỮ NGUYÊN | — | backtest.py (chính là trade gate), savings.py, notify_discord.py (fallback khi TG bị chặn — đã từng xảy ra), charts/auditview/pnlcal/costengine/costs (đều route-wired, costengine ≠ costs), **pyramid_up router** (lệnh Kai giữ — xem D1) |
| — | Kèm theo | — | prune `scripts/observe_full_auto.py` refs trong cùng commit R2/R3; thêm comment `.env` về FULL_AUTO bị runtime override |

---

## 4. TÍNH NĂNG CẦN NÂNG CẤP

### Bằng chứng mới từ loss re-scan hôm nay (2 lỗ mới ZBT #570 −$302 / MEME #450 −$121, đều hard-SL sau ~9 ngày)

- **N1 (HIGH — pattern MỚI):** rung phòng thủ pyramid_up size cứng `kss_first_wave_usd` ($1500)
  bất kể base — ZBT base $467, defensive $1,496 (**3.2×**), đẩy deploy lên **196% isolated_fund**
  (fund không được topup trên path này — [service.py:459-470](../app/kss/service.py#L459-L470),
  [:934-940](../app/kss/service.py#L934-L940)), phóng đại lỗ ~3× (−$302 vs ~−$100 nếu chỉ base).
  Path flip còn **ghi đè `entry_price`** làm mất entry gốc (lossautopsy phải join audit_log).
- **N2 (MED — tương tác không thiết kế):** deploy cap đã âm thầm vô hiệu hóa defensive-flip
  **1,114 lần/7 ngày** (`pyramid_defensive_capped`) → pyramid_up mất cơ chế phản ứng đảo chiều,
  ngồi chờ SL −15%. Hành vi mode đang phụ thuộc trạng thái ngân sách toàn cục một cách vô hình.
- **pyramid_up tổng thể:** toàn lịch sử 92 sell net **−$306.59**; 7 ngày qua 15 session đóng net
  **−$1,037.50** (4 loser đều ăn trọn −15% SL, winner trung bình chỉ ~+$95) vs dca_down 7 ngày
  **+$22,926**. Kai đã chốt 07-13 "giữ chạy để phân tích" — N1/N2 là **bằng chứng mới sau quyết
  định đó**, trình lại để Kai cân nhắc (chỉ fix cơ chế, không đụng chiến lược).

### Danh sách nâng cấp (đối chiếu HEAD — trạng thái verify từng mục)

| # | Việc | Trạng thái hiện tại | Owner |
|---|---|---|---|
| D1 | **Fix defensive rung (N1+N2):** size = `min(base_cost, headroom quỹ + deploy-cap)` thay vì $1500 cứng; chạy qua topup accounting; **không ghi đè `entry_price`** khi flip; quyết định tường minh tương tác cap×defensive | pattern mới, đang chảy máu (19 pyramid_up active) | Sonnet 5 (TDD) — **cần Kai gật vì đụng pyramid_up** |
| D2 | **Breaker absolute-USD + cửa sổ lỗ tuần** — hiện thuần % equity ([circuit.py:62-73](../app/circuit.py#L62-L73)), không có rolling window; trên $1k thì %-based tạm ổn ($30/ngày) nhưng thiếu tường tuần | NOT-STARTED | Sonnet 5 (TDD) |
| D3 | **Regime gate BTC 200d/50d + hysteresis** — chặn open mới + wave mới toàn thị trường, KHÔNG BAO GIỜ chặn exit; **shadow-mode (chỉ audit) trước**, replay vs loss cases rồi mới enforce. Đối chiếu `docs/regime-mae-plan.md` cũ | NOT-STARTED — hiện **không có thiết bị risk-off toàn thị trường nào đang bật** | Sonnet 5 (TDD, 2 bước) |
| E1 | **MFE analytics → TP theo dữ liệu** — `peak_price` đã persist nhưng chưa ai phân tích; đo phân phối MFE rồi mới đề xuất `scan_tp_pct` (không đoán) | PARTIAL (lossautopsy chỉ có MAE-side) | Sonnet 5 |
| E2 | **ATR-scaled ladder spacing** — `atr_pct` có sẵn trong TA bundle, chỉ dùng cho exit; entry vẫn 1 con số cứng cho mọi coin | NOT-STARTED | Sonnet 5 (shadow report trước) |
| E3 | Vol-targeted first-wave sizing | NOT-STARTED | Sonnet 5 (sau E2, tùy chọn) |
| — | `max_session_deploy_usd`: code DONE mọi path (kể cả manual DCA+ và nút Telegram) nhưng **paper chưa set** (0=off); live=150 ✓ | DONE-in-code | chỉ là quyết định giá trị (Kai đã chốt: live only) |

---

## 5. THỨ TỰ PHASE + PHÂN VIỆC

```
Phase A (0 code — env/runtime flips + dọn file)      ← làm NGAY sau duyệt
Phase B (bảo mật S1-S4)                               ← Haiku + Sonnet 5
Phase C (loại bỏ R2-R5, mỗi mục 1 commit riêng)       ← Sonnet 5
Phase D (an toàn D1-D3; D3 shadow trước)              ← Sonnet 5 TDD
Phase E (E1-E3 analytics/earn)                        ← Sonnet 5
Phase F (go-live $1k: LIVE_MAX_ORDER_NOTIONAL=60,
         verify auth/token, merge→live, restart, giám sát tuần 1) ← Opus + Kai
```

**Quy trình mỗi task (chuẩn §7 working-style):**
1. Opus viết brief: acceptance criteria + test spec + anchor `file:line` chính xác.
2. Sonnet 5 (hoặc Haiku với snippet gần hoàn chỉnh) code theo TDD, báo files changed + pass count,
   KHÔNG commit, KHÔNG sửa memory.
3. Opus review 2 lượt tách bạch: (i) đúng spec, đủ scope, không thừa; (ii) chất lượng code, edge
   cases, không làm yếu gate nào.
4. Opus tự chạy FULL suite (mốc hiện tại: **807 pass / 2 skip**) + ruff + boot app thật + đánh
   request thật vào path vừa đổi (checklist §3).
5. Paper restart qua `Start-ScheduledTask FINDMY-Restart`; verify trên process mới.
6. Commit theo concern khi Kai yêu cầu; promote live chỉ bằng git merge.

---

## 6. QUYẾT ĐỊNH CẦN KAI (chặn plan cho tới khi trả lời)

1. **Mục tiêu lợi nhuận:** xác nhận từ bỏ 10-15%/ngày, giữ 3-5%/tháng equity (đã chốt 07-12)?
   Plan này tối ưu cho "tối đa lợi nhuận TRONG lồng an toàn", không phá lồng.
2. **D1 pyramid_up defensive fix:** (a) fix sizing + accounting (khuyến nghị — chỉ sửa cơ chế lỗi,
   giữ nguyên chiến lược đang phân tích), (b) tắt defensive rung, (c) để nguyên theo dõi thêm.
3. **R6 OPUS cascade:** disable-only (khuyến nghị) hay xóa hẳn phần chết đợt này?
4. **R3 ml.py:** DELETE (khuyến nghị) hay chỉ DISABLE?
5. **Chạy liên tục A→F** theo delegation model sau khi duyệt, hay dừng duyệt từng phase?
