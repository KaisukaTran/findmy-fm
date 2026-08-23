# Đánh giá chiến lược KSS — 2026-08-22

> Nguồn: audit code A/B (read-only), phân tích outcome C, sweep phản-thực D (79 entries, 2 mô hình
> intrabar), nghiên cứu ngoài E1/E2/E3, và 2 vòng thẩm định độc lập G (data-fit + capital-safety).
> Dữ liệu: `data/findmy.db` mở read-only, cửa sổ **2026-07-20 → 2026-08-22** (33-34 ngày, 79 session).
> Mọi luận điểm gắn nhãn **[V]** = đã tự kiểm chứng bằng code/SQL, **[A]** = suy luận/giả định.
> `app/kss/pyramid.py` giữ nguyên FROZEN trong toàn bộ tài liệu này — **0 dòng** đề xuất sửa ở đó.

---

## 0. Tóm tắt điều hành (10 dòng)

1. KSS **đang có edge thật**: +$83.75 / 33 ngày trên $1k (+8.4%), PF 2.03, WR 81.2%, maxDD 3.42%. [V]
2. Nhưng edge đó **không đến từ DCA**: 43% số lệnh đóng là "vào 1 nhịp, bật lên, chốt trong <1 ngày". [V]
3. **TP 3% là code chết.** `kss_dynamic_tp_enabled=True` khiến `py.check_tp` không bao giờ chạm tới;
   TP thực tế là **+7.1%** (`SL×1.05 = avg×1.071`). Quét `tp_pct ∈ {1.5..5}` cho kết quả **giống hệt**. [V]
4. **Trail ATR cũng chết**: 8 giá trị `kss_trail_min_pct`/`kss_trail_atr_mult` đều ra đúng $93.21.
   Edge thật nằm ở **sàn chốt lời +2%** (ratchet), không phải ở khoảng trail. [V]
5. Đòn bẩy tốt nhất mà dữ liệu chống lưng: **`kss_tp_gap_pct` 5 → 8** — 0 LOC, +45%/+53% net ở cả
   hai mô hình, **vốn đỉnh không đổi ($831)**, drawdown không đổi. Duy nhất qua được luật 2-mô-hình. [V]
6. Lỗ hổng cấu trúc nghiêm trọng nhất: **nhịp nạp rung DCA 30 phút vs nhịp kiểm SL 90 giây** — cú sập
   22-08 giết 4 session khi chưa rung nào kịp khớp (`wave_num=1 status='cancelled'`). [V]
7. Câu chuyện "94% scan bị chặn vì tiền nằm không" **bị bác bỏ**: tiền mặt thực đã ở **108% ngân sách**
   suốt 25 ngày bão skip; đỉnh triển khai **$974.45 = 97% equity**. Dự phòng 25% **đã bị thủng rồi**. [V]
8. Phantom-lock của `_session_lock` là thật nhưng **nhỏ**: $88-108 (≈0.6-0.75 slot), không phải $340-390. [V]
9. Bậc thang đầy 4 rung là **nhóm đóng góp lời lớn nhất** (+$28.77 net), không chỉ là nhóm lỗ — mọi
   đề xuất cắt ngắn ladder (cap 10% equity, max_waves 2-3, SL 6%) đều đã bị đo và **âm**. [V]
10. n=69 là "chỉ đủ làm giả thuyết". Kế hoạch dưới đây: **1 thay đổi runtime ở P0**, phần còn lại là
    quan sát/đo lường sau cờ default-OFF, và một cổng review bắt buộc ở mốc ~100 lệnh đóng.

---

## 1. Đánh giá logic KSS hiện tại

### 1.1 Bảng chấm điểm từng khối

| Khối | Anchor | Phán quyết | Bằng chứng |
|---|---|---|---|
| Ladder math (giá/qty) | `app/kss/pyramid.py:209-239` | **Đúng & đang làm việc thật** | Trọng số `(n+1)` là edge thật: làm phẳng wave → −29% net, +21% vốn (D §5.6 ii) [V] |
| Re-anchor rung DCA | `app/kss/service.py:123-148` | **Điểm mạnh chưa được ghi nhận** | Ladder hình học thuần: $79.58 vs re-anchor $93.21 → **+17%** (D §5.6 vi) [V] |
| SL neo theo `avg` | `service.py:1450`, `pyramid.py:450` | Đúng nguyên lý, có **đuôi hở 5.74pp** | avg = entry×0.9606, rung sâu nhất entry×0.9412, sàn SL entry×0.8837 [V] |
| TP cố định 3% | `service.py:1543` | **CODE CHẾT** khi dyn ON | 42/42 TP là dynamic; 0 dòng frozen-path trong audit_log [V] |
| Ride & Trail (arm/lock/gap) | `app/kss/dynamic_exit.py:44-108` | **Nửa sống nửa chết** | Sàn +2% là edge; nhánh ATR bất động (8 knob → cùng 1 số) [V] |
| Guard SL nhanh 90s | `service.py:1437-1463`, `1624-1677` | Là **lưới**, không phải phanh tức thời | Vượt sàn −0.03%…−5.0% ngày 22-08 [V] |
| Guard nạp rung DCA | *không tồn tại* | **Lỗ hổng thiết kế** | Rung chỉ khớp qua `orders.auto_fill_due_orders` (`app/orders.py:233`) gọi 1 lần/`run_cycle` (`app/scheduler.py:164`) [V] |
| `_session_lock` | `app/scanner.py:925-935` | Ngưỡng 50%-**chi-phí** lệch pha đường cong chi phí | 3/4 rung = 60.8% cost → khóa full $144.09 khi mới tiêu 61% [V] |
| `_can_open` | `app/scanner.py:939-963` | Chặn đúng, nhưng **báo cáo mù** | Ngân sách tính trên equity, `locked` tính trên ladder-cost — hai xấp xỉ độc lập [V] |
| Gate expectancy/win_rate | `app/backtest.py:139`, `scanner.py:396-401` | **Bão hòa**, không phân biệt được | expectancy = 2.7 ở 14,582/29,281 dòng (trần `tp−cost`); AUC 0.466/0.422 [V] |
| Grok veto | `app/scanner.py:862` | **Cổng lọc lớn nhất thực tế**, vô hình với audit | 2,959-3,014 / 4,921 rớt cuối (60%), không set `decision='skip'` [V] |
| Regime gate BTC | `app/regime.py:37-92`, `scanner.py:513-531` | Đúng ý tưởng, **sai thang thời gian** | SMA 50/200 **ngày** không thể lật trong cửa sổ 2h20m của cụm 22-08 [V] |
| MAE gates | `scanner.py:483` | Tắt, và **nên tắt** | 0 hit / 30,042 candidate; AUC 0.326/0.441 (nghịch dấu) [V] |
| Guards K-1/K-2/K-trail, oversell, pyramid_up | `service.py:1143-1157`, `orders.py:569-580` | **Sạch** | 0 wave trùng, 0 oversell bất thường, 0 session pyramid_up mồ côi [V] |

### 1.2 Ba sự thật lật ngược trực giác (đọc kỹ phần này)

**(a) TP thực tế không phải 3%.** Chuỗi thoát thực sự:

```
arm   tại  avg × 1.05          (kss_trail_arm_pct = 5)
SL    tại  avg × 1.02          (kss_trail_lock_pct = 2 — SÀN CHỐT LỜI, đây mới là edge)
TP    tại  SL  × 1.05 = avg × 1.071   (kss_tp_gap_pct = 5)
```
DB xác nhận: **48/49** session đã arm có `trail_sl_price/avg_price = 1.0200 ± 0.0002`; TP thật khớp ở
1.0707 (ALGO#35) và 1.0741 (PENGU#22). [V] Hệ quả: mọi lần chỉnh `scan_tp_pct` trong 33 ngày qua là
**vô nghĩa**, và `docs/kss.md` đang mô tả một đường đi không bao giờ chạy.

**Bằng chứng quyết định** rằng edge là cái sàn chứ không phải cái trần (D §9): một TP **cố định** ở
cùng mức +6.86% chỉ kiếm $83.5 với drawdown −$57.9 và biến cả 4 loser ladder thành stop đầy −$12.
Cùng trần, khác sàn → khác hoàn toàn.

**(b) Tiền KHÔNG nằm không.** Dựng lại tiền mặt thực từ 216 dòng `fills`, weight theo thời gian:

| Cửa sổ | ngày | tiền mặt thực TB | `locked` (mô hình) | ngân sách (equity×0.75) | %tg thực > ngân sách | concurrent TB / max |
|---|---|---|---|---|---|---|
| FULL 07-20→08-23 | 33.3 | **$712.42** | $800.3 | $756.9 | **51.2%** | 9.2 / 13 |
| 07-20→07-25 | 4.6 | $397.3 | $435.8 | $751.6 | 6.2% | **11.8** / 12 |
| **07-25→08-19 (bão skip)** | 25.0 | **$814.6** | $922.6 | $754.1 | **67.0%** | 8.6 / 13 |
| 08-19→08-23 | 3.7 | $411.5 | $424.1 | $782.3 | 0.0% | 9.7 / 13 |

Đỉnh triển khai **$974.45 lúc 2026-08-14 19:16 = 97% equity**. [V] Nghĩa là: scanner skip **vì sách
đã đầy thật**, không phải vì mô hình reservation tưởng tượng. Và cái "dự phòng 25%" đã bị thủng
51.2% thời gian rồi. Phantom-lock thật sự chỉ **$87.9** (full) / **$108** (bão) — dưới 1 slot
($144.09). Cửa sổ "có thể mở session" nếu xóa sạch phantom: 5.3% → 10.4% (full), **0.5% → 7.3% (bão)**.

**(c) Ladder sâu là nhóm LỜI nhất, không phải nhóm lỗ nhất.**

| Số rung đã khớp | n | net $ | thắng | $thắng | thua | $thua |
|---|---|---|---|---|---|---|
| 1 | 39 | +16.94 | 33 | +31.64 | 6 | −14.70 |
| 2 | 9 | +16.08 | 8 | +20.03 | 1 | −3.95 |
| 3 | 8 | +21.96 | 6 | +36.53 | 2 | −14.58 |
| **4 (đầy)** | **13** | **+28.77** | 9 | **+77.14** | 4 | **−48.37** |

Nhóm 4-rung mang 59% tiền lỗ **và 47% tiền lời**, và là nhóm **đóng góp net lớn nhất**. [V]
Đối chiếu D §5.3: `max_waves` 4→3 kéo net từ $93.21 xuống **$45.45 (−51%)**. ⇒ Mọi đề xuất "cắt ngắn
ladder cho an toàn" đều đã bị đo và **âm tiền**.

### 1.3 Lỗ hổng cấu trúc — xếp theo mức độ

| # | Lỗ hổng | Anchor | Mức | Đo được |
|---|---|---|---|---|
| 1 | Rung DCA chỉ khớp mỗi 30' nhưng SL kiểm mỗi 90s | `service.py:1437` + `orders.py:233` + `scheduler.py:164` | **CRITICAL** | 4 session 22-08, `wave_num=1 status='cancelled'`, tổng −$6.13 [V] |
| 2 | `_session_lock` khóa full ở 50%-cost (đường cong chi phí dồn về cuối) | `scanner.py:925` | **CRITICAL** (hiệu suất vốn) | $87.9-108 phantom; 2/25 session thật rơi vào vùng idle-lock (không phải 10/16) [V] |
| 3 | Grok veto vô hình với cột `decision` | `scanner.py:862` | **CRITICAL** (đo lường) | 5,055 `decision='trade'` nhưng chỉ 79 (1.56%) mở session → sai số **63×** [V] |
| 4 | Backtest gate không mô phỏng Ride & Trail | `backtest.py:139` | HIGH | 7/67 lệnh đóng thật là `trail_sl` — một *loại* thoát mà sim không có [V] |
| 5 | expectancy/win_rate_lb bão hòa nhưng vẫn là hard gate | `scanner.py:396-401` | HIGH | 83% candidate (24,282/29,281) qua cả hai cùng lúc [V] |
| 6 | Session đã arm mất hẳn `_guard_hard_sl` | `service.py:1650-1661` | MEDIUM | OSMO#65: 15 lần `trailing_deferred` / 22 phút dưới avg của chính nó [V] |
| 7 | Sàn SL nằm 5.74pp dưới rung sâu nhất (đuôi không hedge) | `pyramid.py:209` | MEDIUM | 4 loser ladder đầy: thực lỗ −8.12%…−8.64% cost [V] |
| 8 | `max_session_deploy_usd` chỉ dư **$5.90** trên chi phí ladder | `service.py:210-213` | LOW nhưng là **bẫy im lặng** | $144.09 vs $150; vượt cap → cắt rung cuối chỉ qua 1 dòng audit [V] |
| 9 | Guard SL là lưới lấy mẫu 90s, không phải stop tức thì | `service.py:1589-1606` | LOW (đã chấp nhận) | Vượt sàn −0.03%…−5.0% [V] |

---

## 2. Bộ tham số TỐT NHẤT mà dữ liệu HIỆN TẠI chống lưng

### 2.1 Luật chấp nhận (D §1)

Sweep D chạy **hai** mô hình intrabar vì nến 15m không nói được high hay low đến trước:
*primary* (wick / SL-trước, bi quan) và *secondary* (poll / TP-trước, lạc quan).
**Một bộ tham số chỉ được tin khi cả hai đồng ý về hướng.** Baseline: **$93.21 / $116.32**.

| Thay đổi | primary | secondary | Đồng ý? | Vốn đỉnh | maxDD |
|---|---|---|---|---|---|
| **`kss_tp_gap_pct` 5→8** | **135.59 (+45%)** | **177.93 (+53%)** | **CÓ** | **$831 (y hệt)** | −35.02 (y hệt) |
| `sl_pct` 8→12 | 146.30 (+57%) | 130.78 (+12%) | có (lệch 5×) | **$974** | −10.61 |
| `scan_distance_pct` 2→3 | 100.29 (+8%) | **94.14 (−19%)** | **KHÔNG** | $778 | −11.47 |
| `scan_distance_pct` 2→4 | 97.02 (+4%) | **74.66 (−36%)** | **KHÔNG** | $620 | −6.18 |
| `sl12/d4/gap8` (rank-3 của D) | 117.29 | **107.75 (< baseline)** | **KHÔNG** | $620 | −5.64 |
| Chu kỳ nạp rung 5 phút | 128.05 (+37%) | 118.85 (+2%) | có (yếu) | $800 | −22.50 |
| `tp_pct` bất kỳ (dyn ON) | 93.21 | 116.32 | *bất động* | — | — |
| `trail_min`/`trail_atr_mult` bất kỳ | 93.21 | 116.32 | *bất động* | — | — |

### 2.2 Bộ khuyến nghị

> **BỘ TỐT NHẤT ĐƯỢC DỮ LIỆU CHỐNG LƯNG HÔM NAY = cấu hình hiện tại, đổi ĐÚNG MỘT giá trị:**
> **`kss_tp_gap_pct: 5.0 → 8.0`**
> Mọi knob khác **giữ nguyên**: `scan_distance_pct=2`, `scan_max_waves=4`, `sl_pct=8`,
> `kss_first_wave_usd=15`, `deadline_days=30`, `equity_backup_pct=25`, `kss_trail_arm_pct=5`,
> `kss_trail_lock_pct=2`, `max_session_deploy_usd=150`.

Vì sao chỉ đúng một:

- **Trung tính về vốn.** Vốn đỉnh $831 → $831, maxDD −34.26 → −35.02. Trên một cuốn sách đã ở 108%
  ngân sách suốt 25 ngày, tính trung-tính-vốn mới là thứ quyết định được ship hay không. [V]
- **Cơ chế rõ ràng**: sàn ratchet +2% (`dynamic_exit.compute_sl` → `lock_floor_price`, verified tại
  `service.py:1296-1304`) vẫn ghim đáy của mọi session đã arm; nới trần chỉ là thu thêm phần trên.
- Cơ cấu thoát dịch chuyển: tp 42→32, trail 17→27, hold +0.26 ngày. Đây là **kill-metric** cần theo dõi.
- **KHÔNG đuổi theo gap 12/20**: hai mô hình lệch 44%/63% → D tự xếp là "chưa chứng minh".

**Những thứ ĐÃ được đo và BÁC BỎ** (đừng đề xuất lại):

| Ý tưởng | Số giết nó |
|---|---|
| Siết SL 5-6% | `sl=6`: **$60.53 / $57.12** vs $93.21 / $116.32 — tệ hơn ở **cả hai** mô hình; SL exits 9→13 |
| Bỏ hard SL / chỉ trailing | Đẹp trên giấy ($148.41) nhưng `sl≥15` cho **0 SL exit** = cửa sổ này chưa từng rơi 15% dưới avg. Cửa sổ **kết thúc bằng hồi phục** |
| `max_waves` > 4 | W=6 **byte-identical** W=4 ($93.21) vì cap $150 cắt rung 5 (cần thêm $69.2) |
| `max_session_deploy_usd` = 10% equity đơn lẻ | $107.9 < $144.09 → cắt còn 3 rung → **−51% net** |
| Bật `mae_quartile_gate_enabled` | Bỏ quartile xấu nhất theo `avg_mae` = mất **$50.86 / $83.75 = 61% lợi nhuận** |
| Giới hạn universe về majors | 40 symbol thắng, top-3 chỉ 22.7% tiền thắng — lời **rộng và mỏng**; ADA/XLM/FIL là mid/large-cap |
| Hard gate "overbought + đảo chiều" | `rsi` AUC 0.468, `macd_h` 0.550, `bb_pct` 0.587 — không tách được 56 thắng / 13 thua |
| Circuit-breaker theo cụm stop_cooldown | Đo trên chính cửa sổ này: **−$6.60**. Cụm 22-08 mở lúc 02:51-04:53, SL nổ 05:11 → phanh chỉ bắt được BABY#73 (−$1.24), bỏ mất +$7.84 |
| Làm phẳng kích thước wave | −29% net, +21% vốn |
| Rút ngắn `deadline_days` | 3/7/14/30 = $22.88 / $38.40 / $78.87 / $93.21 — mọi lần rút ngắn đều tệ hơn |
| Nới `equity_backup_pct` xuống 15% | Ngân sách chỉ lên $917, vẫn dưới đỉnh $974 đã đạt → **0** lợi ích, mất nốt đệm |

---

## 3. Cải tiến đề xuất — ưu tiên theo bằng chứng

Mỗi mục: **bằng chứng → hiệu ứng kỳ vọng → rủi ro → anchor code → test → đo gì**.
Mọi mục có hành vi mới đều **default OFF** sau runtime knob. `pyramid.py` không đổi 1 dòng.

### I-1 · `kss_tp_gap_pct` 5 → 8 (TRIAL) — 0 LOC

- **Bằng chứng** [V]: D §5.2 — cả 2 mô hình +45%/+53%, vốn đỉnh và maxDD **không đổi**.
- **Hiệu ứng**: ≈ **+$40 / 33 ngày** trên sách $1k (+46% net), tuyến tính theo quy mô sách.
- **Rủi ro**: hai mô hình lệch 31% (baseline tự lệch 25%) → theo D caveat 5 là "chưa chứng minh"
  ⇒ ship như **thử nghiệm có ngày review**, không phải kết luận. Trong regime đi ngang, nhiều
  session sẽ thoát ở sàn +2% thay vì +7.1% — nhưng sàn cap đáy y hệt, nên vốn/DD không đổi.
- **Anchor**: `app/runtime.py:100` (`kss_tp_gap_pct`), `app/kss/dynamic_exit.py:92 compute_tp`.
- **Test**: 1 test trên `dynamic_exit.compute_tp(sl=..., avg=...)` tại gap=8 (TDD, ~15 LOC).
- **Đo**: tỉ lệ exit `trail_sl` (kỳ vọng 17→27 pattern), %lời trung bình của exit trail, hold-time.
  **Kill-metric**: sau +30 lệnh đóng, nếu net/session < baseline $1.21 → rollback tức thì.

### I-2 · Cho guard 90s nạp luôn các rung DCA đến hạn — ~12 LOC, default OFF

- **Bằng chứng** [V]: A §1 — rung chỉ khớp trong `orders.auto_fill_due_orders` (`app/orders.py:233`)
  gọi 1 lần/`run_cycle` (`app/scheduler.py:164`, `scan_interval_min=30`), trong khi `_guard_hard_sl`
  (`app/kss/service.py:1437-1463`) chạy mỗi `kss_exit_check_sec=90`s và khi nổ thì `_cancel_pending_waves`
  huỷ luôn rung chưa khớp. Cả 4 session 22-08 có `kss_waves.wave_num=1 status='cancelled', filled_at=NULL`.
  D §5.6(vii): chu kỳ nạp 5 phút = **$128.05 vs $93.21 (+37%)** với vốn đỉnh **thấp hơn** ($800 vs $831)
  và drawdown **thấp hơn** (−$22.50 vs −$34.26).
- **Hiệu ứng**: +$25-35 / 33 ngày **kèm** −34% max drawdown. Là đề xuất **duy nhất** cải thiện đồng
  thời net, vốn và drawdown; và là đề xuất duy nhất thực sự đổi được lớp lỗ 22-08 (rung wave-1 sẽ có
  2-3 cơ hội khớp trong 2h20m trước khi INJ/UNI dừng).
- **Rủi ro & ràng buộc bắt buộc**:
  1. **Phải** gọi `orders.auto_fill_due_orders(db)` bản có frozen-guard — **tuyệt đối không** dùng
     đường `reviewer="guard"` miễn-freeze (`service.py:1667-1676`), đường đó chỉ dành cho **exit**.
     Một lệnh BUY DCA là **rủi ro mới**, phải nằm sau circuit breaker + Guardian veto
     (`orders.py:252-254` bỏ qua BUY khi `auto_veto`).
  2. Chạy trên interval riêng `kss_guard_dca_fill_sec` **default 300** (chính là con số D mô phỏng),
     không phải 90s (chưa test, ăn ngân sách nhanh hơn).
  3. **Default OFF.**
  4. Nạp nhanh hơn = triển khai vốn nhanh hơn vào thị trường đang rơi → ghép với I-3/I-4 để có headroom.
- **Anchor**: `app/scheduler.py:210 _guard_once` / `:231 _guard_loop`; knob mới ở `app/config.py` +
  `app/runtime.py:52 KSS_SETTING_FIELDS`.
- **Test** (~40 LOC): (a) hệ thống frozen ⇒ **không** khớp BUY; (b) Guardian veto ⇒ **không** khớp;
  (c) rung đến hạn ⇒ khớp giữa hai `run_cycle`; (d) exit vẫn không bao giờ bị gate (never-gate-exits).
- **Đo**: số session thoát SL ở trạng thái wave-0 (kỳ vọng giảm), số rung khớp/ngày, vốn đỉnh.

### I-3 · `_session_lock` → `used + 1 rung kế` + trần next-rung toàn danh mục + sàn tiền mặt cứng — ~45 LOC, default OFF

- **Bằng chứng** [V]: `app/scanner.py:925-935` trả `used if used < 0.5*reserved else reserved`.
  Chi phí dồn cuối (`(n+1)·15·0.98ⁿ` = 15/29.4/43.2/56.5) ⇒ 3/4 rung = **60.8% cost** → khóa full
  $144.09 khi mới tiêu 61% (phantom ≈$56/session). Ví dụ sống: session #71 (RVN) khóa $144.09 / triển
  khai $85.55 → $58.5 phantom.
- **Hiệu ứng — nói thật, nhỏ hơn nhiều so với đồn đại**: phantom weight-theo-thời-gian chỉ **$87.9**
  (full) / **$108** (bão) = **0.6-0.75 slot**, không phải $340-390. Cửa sổ "mở được session":
  5.3%→10.4% (full), **0.5%→7.3% (bão)** — tăng 14× trong bão nhưng vẫn là con số nhỏ tuyệt đối.
  Ghép với ladder $96 (I-4): 29.2% / 11.2%.
- **Rủi ro — đây là mục DUY NHẤT nới lỏng một cơ chế kiểm soát**, nên **không được ship trần trụi**:
  1. Tiền mặt thực **đã** đạt đỉnh 97% equity và vượt vạch ngân sách 51.2% thời gian. Nới reservation
     chỉ **giải phóng một đặt chỗ, không tạo ra tiền** — nó cho phép triển khai thực leo lên 100% equity.
  2. **Bắt buộc kèm**: (i) gate mới `Σ next_rung_cost(active) ≤ equity × next_rung_exposure_pct`
     (default **20%** ≈ $216 hôm nay) đặt trong `_can_open`; (ii) **sàn tiền mặt cứng** —
     `cash_floor_usd` đã là knob sẵn (`app/runtime.py:64`) hoặc tính ngân sách trên tiền mặt tự do thật;
     (iii) `max_concurrent_sessions` giữ vai trò backstop cứng; (iv) `equity_backup_pct` **giữ 25**.
  3. Một cú sập hệ thống có thể gọi nhiều "rung kế" cùng lúc — chính pattern 22-08. Trần (i) là thứ chặn.
- **Anchor**: `app/scanner.py:925 _session_lock`, `:939 _can_open`, `:965 _has_open_capacity`;
  helper `_next_wave_cost` tái dùng `projected_ladder_cost`; knob `next_rung_reserve_enabled=false`.
- **Test** (~80 LOC, `tests/app/test_scanner_budget.py`): lock math tại 0/1/2/3 rung đã khớp; trần
  danh mục nổ đúng lúc; gate vẫn từ chối khi sàn tiền mặt sẽ vỡ; cờ OFF ⇒ hành vi **y hệt** hôm nay.
- **Đo**: `locked − used` theo thời gian; số scan skip lý do ngân sách; đỉnh tiền mặt thực (**không
  được vượt** đỉnh lịch sử $974.45); số session mở/ngày.

### I-4 · Hạ hình dạng vốn/session: `kss_first_wave_usd` 15→10 **cùng với** `max_session_deploy_usd` 150→~105 — 0 LOC (2 runtime value), nhưng là quyết định của Kai

- **Bằng chứng** [V]: D §5.5 — `first_wave=$10` có **hiệu suất y hệt** (0.00614 $/$-ngày) với
  **33% ít vốn hơn** (đỉnh $554 vs $831). Ladder mới = 10+19.6+28.81+37.65 = **$96.06**, cap ~$105
  giữ đúng **4 rung** (≥1.04× chi phí ladder). Lệnh wave-0 dưới $15 đã khớp bình thường trong thực tế:
  INJ **$10.39**, AVAX $13.11, VANA $14.01 — không có lỗi dust.
- **Hiệu ứng**: cùng $/$-ngày với ~1.5× số session đồng thời trên cùng lượng tiền; đuôi lỗ/session
  giảm từ ~$11.5 xuống ~$7.7; cửa sổ mở-được 5.3%→10.4% (và tới 29.2% nếu ghép I-3).
- **Rủi ro — chế độ hỏng phải canh**: hạ cap **một mình** (không hạ `first_wave`) ⇒ ladder bị cắt im
  lặng còn 3 rung qua nhánh `deploy_cap_hit` (`service.py:210-213`) ⇒ **−51% net**. Đây chính xác là
  lý do I-5 (assertion) phải lên trước.
  Sàn dưới: **không** hạ `first_wave` dưới $10 (lớp lỗi stepSize/min-notional, đã fix ở `7a7a264`,
  guard tại `pyramid.py:222-227`, cộng slippage sách mỏng chưa đo).
- **Anchor**: `app/runtime.py:116` (`kss_first_wave_usd`), `:73` (`max_session_deploy_usd`),
  `service.py:210-213` (nhánh cắt im lặng).
- **Test**: assertion của I-5 phải fail khi cap < 1.04 × `projected_ladder_cost`.
- **Đo**: số rung khớp trung bình/session (**phải giữ ≈1.85**, nếu tụt là ladder đang bị cắt), net/session.

### I-5 · Assertion mạch lạc tham số (WARN-only) — ~22 LOC, không đổi hành vi

- **Bằng chứng** [V]: chi phí ladder sống $144.09 vs cap $150 = **$5.90 headroom**; và
  `_anchor_dca_price` có thể định giá rung **thấp hơn** mục tiêu hình học, đẩy chi phí thật sát cap hơn.
  Thất bại **im lặng**: chỉ 1 dòng audit `deploy_cap_hit`, không lỗi nào nổi lên UI.
- **Nội dung**: khi `set_kss_settings` chạy và lúc startup, ghi audit **WARN** nếu
  (a) `projected_ladder_cost ≥ 0.97 × max_session_deploy_usd`, hoặc
  (b) `N × cap / equity × sl_pct > 6%` (trần CVaR — xem §5).
  Đồng thời: **nêu tên ràng buộc đang bind** trong `reason` của `_can_open` (`floor(budget/cap)` vs
  `max_concurrent_sessions`), thay cho 1,104 dòng "vượt ngân sách" mù.
- **Rủi ro**: không có (chỉ cảnh báo).
- **Anchor**: `app/runtime.py:282 set_kss_settings`, `app/scanner.py:939 _can_open`.
- **Test** (~25 LOC): cap=$150 + first_wave=$25 ⇒ WARN; N=12/cap=150/equity=1079/sl=8 ⇒ WARN CVaR;
  cấu hình hiện tại ⇒ WARN (đúng, vì $144.09/$150 = 96.1%... sát ngưỡng — chọn 0.97 có chủ đích).
- **Đo**: số WARN phát sinh; mỗi WARN là một lần suýt mất 51% net.

### I-6 · Vệ sinh đo lường Grok + tách `bb_pct>1` thành pre-filter tất định — ~16 LOC

- **Bằng chứng** [V]: `scanner.py:851-916` chỉ append vào `.reason`, **không** đổi `.decision`
  (khác với gate MAE và regime gate cùng hàm, hai gate đó *có* set `decision="skip"`).
  Kết quả: 5,055 dòng `decision='trade'` nhưng chỉ **79 (1.56%)** có `session_id`. Phân rã 4,976 còn
  lại: **Grok 3,014 (60.6%)**, per-symbol cap 832, per-scan cap 1,130 (cộng đúng bằng 4,976).
  **92.7%** veto Grok (2,795) thuộc đúng một họ: "bb_pct>1 / overbought" — một phép so sánh số học đã
  có sẵn trong `ta_bundle`, đang chạy trong một LLM trả phí, không test được.
- **Hiệu ứng**: $0 trực tiếp — nhưng nó gỡ sai số **63×** khỏi cột `decision`, mà **mọi** phân tích
  sau này (kể cả cổng review của I-9) đều đọc từ cột đó. Phụ: cắt ~2,000 lời gọi LLM/cửa sổ.
- **Rủi ro**: giữ **đúng ngưỡng >1.0** của Grok (không hạ xuống 0.90 để bắt cụm 22-08 — đó là ý tưởng
  đã bị bác bỏ ở §2.2). Ship pre-filter ở chế độ **shadow trước**, so bộ veto tất định vs bộ veto Grok,
  rồi mới cho nó shortcut lời gọi LLM.
- **Anchor**: `app/scanner.py:862` (nhánh Grok), `:591 _downtrend_veto` (chỗ đặt gate tất định mới),
  `app/orchestrator/grok.py:81-82` (prompt hiện tại).
- **Test** (~30 LOC): mọi nhánh chặn cuối phải set `decision='skip'`; pre-filter tất định trùng khớp
  với verdict Grok trên bộ mẫu ta_json; thêm 1 test **không mock** đo phân bố verdict định kỳ.
- **Đo**: sau khi ship, `decision='trade'` ≈ số session thật mở (sai số < 2×, không phải 63×).

### I-7 · Tài liệu hoá code chết & code bất động (không xoá) — 0 LOC prod

- **Bằng chứng** [V]: `_evaluate_dynamic_exit` trả False **chỉ khi** `price ≤ avg`, còn `check_tp` cần
  `price ≥ avg(1+tp%)` — loại trừ nhau tuyệt đối. audit: `dynamic_tp 42 / dynamic_trail 7 /
  stop_loss 12`, **0** dòng frozen-path. D §3 xác nhận độc lập: `tp_pct ∈ {1.5..5}` ra byte-identical.
- **Vì sao không phải mỹ phẩm**: tắt `kss_dynamic_tp_enabled` sẽ **âm thầm** đưa sách về máy TP cố
  định = **$57.15 vs $93.21 (−39%)**, ẩn sau đúng một boolean. Phải ghi cặp ràng buộc này **ngay cạnh
  cái toggle**, không chỉ trong `docs/kss.md`.
- **Phải ghi 3 thứ**: (1) TP cố định 3% không bao giờ chạy khi dyn ON — TP thực là **avg×1.071**;
  (2) nhánh ATR của trail **bất động** (8 knob → cùng 1 kết quả) — edge là **sàn +2%**;
  (3) `expectancy`/`win_rate_lb` bão hòa (trần `tp−cost`, AUC 0.466/0.422) → **giữ làm sàn rẻ tiền,
  đừng tune như knob chọn lọc**; `min_trials=15` mới là cái thực sự cắn (3,660 skip).
  (4) MAE evidence thu thập có chủ đích nhưng **cố ý không gate** (AUC 0.326/0.441, nghịch dấu).
- **KHÔNG xoá nhánh nào**: `check_tp`/`check_stop` là đường sống duy nhất khi toggle tắt.
- **Anchor**: `service.py:1543` (docstring), `docs/kss.md`, `docs/AGENTS.md`.

### I-8 · Đo dải "đã arm mà không có sàn cứng" (chỉ instrument, không đổi sàn) — ~6 LOC

- **Bằng chứng** [V]: khi `trail_active=True`, ladder DCA bị huỷ (`service.py:1259`) và
  `run_position_guard` **thôi** gọi `_guard_hard_sl` (`service.py:1650-1661`); sàn duy nhất còn lại là
  fallback `avg×(1−sl%)` tại `service.py:1300-1303`, và chỉ tới được qua nhánh `price ≤ carried_sl` +
  K-2 `_tp_clears_cost` **fail**. OSMO#65: 15 lần defer / 21 phút, giá 0.0361-0.0377 (dưới avg
  0.0376188 của chính nó), sàn fallback ở 0.03461 — còn ~4pp nữa mới có bảo vệ.
- **KHÔNG đổi `kss_trail_lock_pct` bây giờ**: D §5.5 cho hai mô hình chỉ **ngược chiều nhau**
  (lock 1/2/3/4 = 96.74/93.21/91.59/81.20 primary nhưng 103.27/116.32/121.66/125.92 secondary).
- **Đo**: đếm chuỗi defer và **PnL kết cục** của từng chuỗi. Chỉ khi tỉ lệ "defer rồi kết thúc lỗ" đủ
  cao mới bàn tới sàn trung gian.
- **Anchor**: `service.py:1295-1304`, `:1299` (`trailing_deferred`).

### I-9 · Kỷ luật cỡ mẫu: mọi thay đổi knob đi kèm review đã đăng ký trước — 0 LOC (tuỳ chọn ~6 LOC)

- **Bằng chứng** [V]: n=69 nằm trong dải "30-100 = chỉ là giả thuyết" (E1 nguồn 6). 12 SL exit mang
  **100%** tiền lỗ (−$81.55/−$81.59). Top-5 ngày = **131%** tổng lợi nhuận ⇒ 29 ngày còn lại net **−$26**.
  Top-6 symbol = 82% tiền lỗ. Sharpe-like 4.72 tự nó bị đánh dấu không robust trên 34 ngày.
  Và các metric của chính scanner: consensus AUC **0.396 (nghịch)**, win_rate_lb 0.422, expectancy 0.466.
- **Nội dung**: mỗi lần đổi knob ghi (a) giá trị **trước** khi đổi, (b) mốc review tại **+30 lệnh đóng**,
  (c) **kill-metric** đã nêu trước. Đây chính là thứ biến I-1 từ "cược" thành "thử nghiệm".
- **Anchor** (tuỳ chọn): audit 1 dòng `knob_change` trong `runtime.set_kss_settings` để review có mốc thời gian.

---

## 4. Việc CẦN ĐO (needs-data) — làm trước, đừng ship

| # | Câu hỏi | Thí nghiệm (read-only / scratch, 0 dòng repo) | Ngưỡng quyết định |
|---|---|---|---|
| M-1 | `sl_pct` 8→12 có thật sự tốt? | Chạy lại `D_engine.replay` trên một cửa sổ **drawdown** 2026 (hoặc analog 2022), kiểm dấu | Chỉ bàn lại khi cả 2 mô hình vẫn dương **và** equity ≥ $2,400 (để đỉnh $974 nằm trong reserve) |
| M-2 | Time-stop có điều kiện (đỏ **và** không có rung mới trong K ngày) | Thêm biến thể vào `D_engine` scratch (~20 LOC) | Dữ liệu nói thiệt hại nằm **sau ngày 14** (>7d: +$19.87, >10d: +$35.69, >14d: **−$22.80**) — mọi ngưỡng 7-10 ngày đã bị bác |
| M-3 | Giãn khoảng rung (flat d=3, hoặc ATR-scaled) | **Test flat d=3 trước (0 LOC)** rồi mới nghĩ tới đường cong per-wave | Cột secondary hiện đi **ngược** (94.14 / 74.66). Cần đồng thuận 2 mô hình. Lưu ý: `_anchor_dca_price` (`service.py:123-148`) đã làm ladder thích nghi thị trường, đáng +17% |
| M-4 | Nới/siết margin `rel_strength` theo regime | Shadow-log 2 tuần: margin 0%/1%/3% **sẽ** cho qua những gì, rồi so forward-return | Đây là filter lớn nhất (8,128 skip, 7,238 trong 7 ngày) — không đụng khi chưa có outcome data |
| M-5 | Breadth filter (>P% universe giảm >Q% trong H giờ) | Dựng lại tại 98 timestamp scan thật: tỉ lệ universe đã giảm >Q% (Q=3,5; H=2,4,6) | Chỉ ship nếu các scan trước cụm 22-08 là **outlier rõ rệt** so với scan trung vị |
| M-6 | Regime gate BTC | Bật `regime_gate_enabled=true` + `regime_gate_enforcing=false` (**shadow, 0 LOC**) để log duty-cycle | **Không enforce** cho tới khi shadow-log cho thấy risk-off trùng với cụm SL. Lưu ý: nó là SMA **ngày**, không thể chặn được cụm 22-08 |
| M-7 | Phanh cụm stop (`stop_cooldown:*`) | **Chỉ shadow-log** đếm số lần sẽ nổ | Bản enforcing **đã bị bác**: đo trên cửa sổ này ra **−$6.60** (bắt BABY −$1.24, bỏ +$7.84) |
| M-8 | Backtest gate mô phỏng Ride & Trail | ~60-90 LOC trong `app/backtest.py` | **Chỉ làm nếu** quyết định giữ expectancy/win_rate làm hard gate. Nhiều khả năng chỉ dời điểm bão hòa (TP thật +7.1% > 3%) |
| M-9 | Cohort overbought (RSI>60 ∧ %B>0.85) | Đã đo: 6/69 session, net **−$3.16** | Hai thẩm định **mâu thuẫn** (một bên: soft-penalty; một bên: reject). n=6 → chỉ shadow-log, **không** làm gate |

---

## 5. Bất đẳng thức an toàn vốn (dùng làm chuẩn, không dùng làm sizing)

```
N_max × cap_pct × sl_pct  ≤  6% equity          (trần CVaR, luôn giải cho N — KHÔNG BAO GIỜ giải cho cap_pct)
projected_ladder_cost     ≤  0.96 × max_session_deploy_usd     (giữ đủ 4 rung)
```

Số hiện tại [V]: lỗ thực tại SL ≈ `total_cost × sl_pct` ⇒ ladder đầy $144.09 @ 8% = **$11.53/session**
(khớp 4 loser thật: −$11.96 / −$12.01 / −$12.02 / −$12.39). Kịch bản xấu nhất đồng thời:
N=12 → $138 = **12.8% equity**; N=6 → $69 = 6.4%; N=5 → $58 = 5.3%.
**Nhưng** drawdown thực đã đo chỉ **3.42% ($34.97)**, và cụm SL đồng thời thật chỉ tốn **$6.13 (0.6%)** —
vì chúng là session 1-rung, không phải ladder đầy. Chỉ **13/69** session từng chạm 4 rung.

⇒ **Dùng bất đẳng thức làm assertion cảnh báo (I-5), KHÔNG dùng làm luật sizing cứng.** Giải nó cho
`cap_pct` sẽ ra $67/session → cắt ladder còn ~2 rung → D §5.3 định giá 2 rung ở **$26.64 vs $93.21 (−71%)**.

**Mâu thuẫn giữa hai thẩm định — cần Kai quyết** (xem §7, quyết định 4):
hạ `max_concurrent_sessions` 12 → 5-6.

| Bên | Lập luận | Số |
|---|---|---|
| capital-safety: **nên** | 12×$150 = $1,800 nominal vs ngân sách $809 = overcommit 2.2×; N=12 chỉ "thật" từ equity ≈$2,400 | `floor(809.25/150)=5`; CVaR `0.06×1079/11.53=5.6` |
| data-fit: **không nên** | Ở 2/3 cửa sổ con, **cap concurrency mới là cái bind** trong khi tiền mặt còn rảnh | 07-20→25: dùng $397 / ngân sách $752, concurrent **11.8/12**; 08-19→23: $411, concurrent 9.7 max **13** |

Phần **cả hai đồng ý**: (a) giữ `equity_backup_pct = 25`; (b) assertion cảnh báo (I-5);
(c) nêu **tên** ràng buộc đang bind trong audit thay vì 1,104 dòng "vượt ngân sách" mù.

---

## 6. Kế hoạch theo pha

### P0 — Sự thật & lưới an toàn (0 rủi ro, 0 thay đổi hành vi)
- I-7 tài liệu hoá code chết/bất động (TP 3%, trail ATR, gate bão hòa, MAE cố ý không gate).
- I-5 assertion mạch lạc (WARN-only) + nêu tên ràng buộc bind trong `_can_open`.
- I-6 nửa "vệ sinh": mọi nhánh chặn cuối set `decision='skip'` (Grok / per-symbol cap / per-scan cap).
- I-9 dựng cổng review: ghi giá trị trước, mốc +30 lệnh đóng, kill-metric.
- M-6 bật regime gate **shadow** (`enabled=true`, `enforcing=false`) — 0 LOC, chỉ để thu duty-cycle.

**Acceptance P0**: `decision='trade'` xấp xỉ số session mở thật (sai số < 2×); assertion nổ WARN đúng
trên cấu hình cố tình sai; `docs/kss.md` không còn mô tả đường TP không chạy; **0** thay đổi PnL.

### P1 — Một knob, có ngày review
- I-1 `kss_tp_gap_pct` 5 → 8, đăng ký review tại +30 lệnh đóng.

**Acceptance P1** (đo tại +30 lệnh đóng, tức n≈100): net/session **≥ $1.21**; tỉ trọng exit `trail_sl`
tăng (≈17→27 pattern); vốn đỉnh **không** vượt $974.45; maxDD **≤ 4.5%** equity.
**Kill**: net/session < $1.21 hoặc maxDD > 6% ⇒ rollback về 5.0 ngay (0 LOC).

### P2 — Nhịp nạp rung (đề xuất mạnh nhất về mặt cơ chế)
- I-2 guard nạp rung DCA @300s, **default OFF**, sau frozen-guard + Guardian veto.
- Bật ở paper trước, quan sát 2 tuần.

**Acceptance P2**: số session thoát SL ở trạng thái **wave-0** giảm rõ; vốn đỉnh **không tăng**
(D dự đoán $800 < $831); maxDD không xấu đi; **0** lệnh BUY nào khớp khi hệ thống frozen hoặc bị
Guardian veto (test bắt buộc).

### P3 — Hình dạng vốn (quyết định của Kai, xem §7)
- I-3 `_session_lock` = `used + 1 rung kế` **+ trần next-rung danh mục 20% + sàn tiền mặt cứng**,
  default OFF sau `next_rung_reserve_enabled`.
- I-4 `kss_first_wave_usd` 15→10 **cùng lúc** `max_session_deploy_usd` 150→~105 (cả hai hoặc không cái nào).

**Acceptance P3**: số rung khớp trung bình/session **giữ ≈1.85** (tụt = ladder bị cắt = hỏng);
đỉnh tiền mặt thực **≤ $974.45** (không được tệ hơn lịch sử); số session mở/ngày tăng;
`locked − used` weight-theo-thời-gian giảm về gần 0.

### P4 — Trung thực hoá scanner
- I-6 nửa còn lại: pre-filter `bb_pct>1` tất định ở **đúng ngưỡng >1.0**, shadow trước, so bộ veto.
- I-8 instrument dải defer của session đã arm.
- M-4 shadow-log margin `rel_strength`.

**Acceptance P4**: bộ veto tất định trùng ≥90% bộ veto Grok họ "overbought"; số lời gọi LLM/scan giảm;
có bảng "chuỗi defer → PnL kết cục" để quyết I-8 giai đoạn sau.

### P5 — Chỉ khi P0-P4 xong và n ≥ 100
- M-1 (`sl_pct` 12 trên cửa sổ drawdown), M-2 (time-stop có điều kiện sau ngày 14), M-3 (flat d=3),
  M-5 (breadth), M-8 (backtest mô phỏng Ride & Trail).

**Acceptance P5**: mỗi mục chỉ được lên P-tiếp-theo khi **cả hai** mô hình intrabar cùng dấu.

---

## 7. Quyết định cần Kai

> Đây là những thứ dữ liệu **không** quyết thay được — chúng là đánh đổi khẩu vị, không phải câu hỏi kỹ thuật.

**QĐ-1 · Có ship `kss_tp_gap_pct` 5→8 ngay không?**
Được cả hai thẩm định chấp nhận, 0 LOC, trung tính về vốn, +45%/+53% ở cả hai mô hình. Nhưng n=69 là
"chỉ đủ giả thuyết" và hai mô hình lệch 31%. → *Khuyến nghị: CÓ, dưới dạng trial có kill-metric (P1).*

**QĐ-2 · Chấp nhận rằng dự phòng 25% ĐANG bị thủng, hay siết lại trước?**
Tiền mặt thực đã đạt **97% equity** (đỉnh $974.45) và vượt vạch ngân sách **51.2%** thời gian — do
chính luật "cho mượn reservation nhàn rỗi" hiện có. Kai chọn một trong hai:
(a) coi đây là chấp nhận được và ship I-3 **kèm** sàn tiền mặt cứng; hoặc
(b) siết `cash_floor_usd` **trước**, chấp nhận ít session hơn. **Không được** ship I-3 mà không chọn.

**QĐ-3 · Có đổi hình dạng vốn/session sang $10 first-wave + cap $105 không?**
Cùng hiệu suất $/$-ngày, 33% ít vốn, ~1.5× số slot, đuôi lỗ/session $11.5→$7.7. Rủi ro: nếu chỉ hạ
cap mà quên hạ `first_wave` ⇒ ladder cắt còn 3 rung ⇒ **−51% net**, và thất bại đó **im lặng**.
Đây là thay đổi hình dạng sách, không phải tinh chỉnh — cần Kai gật, và I-5 phải lên trước.

**QĐ-4 · `max_concurrent_sessions`: giữ 12, hay hạ 5-6?**
**Hai thẩm định mâu thuẫn trực tiếp** (§5). An toàn vốn nói overcommit 2.2×; dữ liệu nói ở 2/3 cửa sổ
con chính cap này đang bind trong khi tiền còn rảnh, và drawdown thực chỉ 3.42% — nửa trần 6% đang
được bảo vệ. → *Khuyến nghị trung dung: giữ 12, ship assertion WARN + nêu tên ràng buộc bind (I-5),
xem lại sau P3.*

**QĐ-5 · Cho guard nạp rung DCA (P2) — bật ở paper khi nào?**
Đây là đề xuất duy nhất cải thiện đồng thời net (+37%), vốn đỉnh (−4%) và drawdown (−34%), và là thứ
duy nhất đổi được lớp lỗ 22-08. Nhưng nó cho bot **mua nhanh hơn vào thị trường đang rơi**. Kai quyết:
bật ngay sau P1, hay đợi P3 (có headroom) rồi mới bật.

**QĐ-6 · `sl_pct` 8→12: đóng băng cho tới khi có bằng chứng cửa sổ drawdown?**
Đây là knob đơn lẻ mạnh nhất ($93→$146 primary) nhưng cũng là mục **duy nhất cắt vào chính cái lồng**.
D caveat 1 quyết định: `sl≥15` cho 0 SL exit nghĩa là cửa sổ này **chưa từng** rơi 15% dưới avg —
cửa sổ kết thúc bằng hồi phục. Và XLM#3 tệ đi **48%** (−$11.73 → −$17.41). → *Khuyến nghị: đóng băng,
chạy M-1 trước, xem lại ở equity ≥ $2,400.*

**QĐ-7 · Kỷ luật cỡ mẫu: có đóng băng mọi knob khác cho tới ~100 lệnh đóng không?**
Với nhịp 44 lệnh mở / 4 ngày gần đây, mốc 100 lệnh đóng đạt được trong ~2-4 tuần.
→ *Khuyến nghị: CÓ — ngoài P0 (0 rủi ro) và QĐ-1.*

---

## 8. Những gì tuyệt đối KHÔNG làm (đã đo, đã bác)

1. **Không** xoá nhánh `py.check_tp`/`check_stop` — nó là đường sống duy nhất khi `kss_dynamic_tp_enabled=False`.
2. **Không** sửa `app/kss/pyramid.py`. Mọi thay đổi khoảng cách rung nằm ở `service.py:123-148
   _anchor_dca_price` (rung tự-chain **đã** bị ghi đè giá ở đó) — freeze không phải là rào cản, **bằng
   chứng mới là**.
3. **Không** rút ngắn `kss_exit_check_sec` xuống dưới 90s — thêm tải exchange + áp lực price-cache cho
   một lợi ích bị chặn trên; đòn bẩy đã đo là **nhịp nạp rung**, không phải nhịp kiểm.
4. **Không** ship gate cứng dựng từ metric không phân biệt được (RSI/MACD-h/%B: AUC 0.468/0.550/0.587).
5. **Không** nới `equity_backup_pct` như phản ứng đầu tiên với bão skip.
6. **Không** tune `scan_tp_pct`, `kss_trail_min_pct`, `kss_trail_atr_mult` — cả ba **bất động**.

---

## Phụ lục A: bảng số nền (tham chiếu nhanh)

| Chỉ số | Giá trị | Nguồn |
|---|---|---|
| Realized PnL 33-34 ngày | **+$83.75** (+8.4% trên $1,000) | 69 session đóng, `fills.realized_pnl` [V] |
| Profit factor / Win rate | **2.03** / **81.2%** (56/69) | [V] |
| Expectancy/session | +$1.21 (+3.54% vốn triển khai) | [V] |
| Max drawdown | **$34.97 (3.42%)** | [V] |
| Exit kinds | tp 48 (+$154.19) · trail_sl 7 (+$4.74) · sl 12 (−$81.55) · deadline 2 (+$6.37) | [V] |
| Hold: tp vs sl | 2.48d vs **7.02d** | [V] |
| Ngày idle (0 scan thật) | **25/34 (73.5%)** — 98/1,566 scan chạy thật (6.3%) | [V] |
| Skip: ngân sách / concurrency | 1,104 / 364 | [V] |
| Session mở trong 4 ngày cuối | 44 = 56% tổng | [V] |
| Tập trung ngày | Top-5 ngày = **131%** tổng lợi nhuận (29 ngày còn lại: −$26) | [V] |
| Tập trung lỗ | Top-6 symbol = **82%** tiền lỗ; 4 loser ladder đầy = 58% | [V] |
| Phí | **$7.57** trên $3,879 buy notional = 9% net; giảm 25% BNB ≈ $1.89/33 ngày | [V] |
| Ladder sống | 15 + 29.40 + 43.22 + 56.47 = **$144.09** vs cap $150 (**$5.90** dư) | [V] |
| TP hiệu lực thật | **avg × 1.071 (+7.1%)**, không phải 3% | [V] |
| Lỗ tối đa/session tại SL | ≈ `total_cost × sl_pct` = **$11.53** ladder đầy | [V] |

## Phụ lục B: file/dòng đã xác minh trong lần này

| Anchor | Nội dung |
|---|---|
| `app/scanner.py:925-935` | `_session_lock` — `used if used < 0.5*reserved else reserved` |
| `app/scanner.py:939-963` | `_can_open` — budget = equity×(100−backup)/100 |
| `app/scanner.py:513-531` | Regime gate (chỉ gate `to_open`, có sẵn shadow mode) |
| `app/scanner.py:591` / `:664` | `_downtrend_veto` / `_rel_strength_veto` |
| `app/scanner.py:747` | `_open_rank_key` — docstring tự thừa nhận gate bão hòa |
| `app/scanner.py:862` | Nhánh Grok veto — chỉ append `.reason`, không set `.decision` |
| `app/scanner.py:977` | `_in_stop_cooldown` (per-symbol, đã hoạt động đúng) |
| `app/kss/service.py:123-148` | `_anchor_dca_price` — điểm đổi khoảng cách rung KHÔNG cần unfreeze |
| `app/kss/service.py:210-213` | `deploy_cap_hit` — cắt rung **im lặng** |
| `app/kss/service.py:1225` / `:1299` | `_evaluate_dynamic_exit` / `trailing_deferred` |
| `app/kss/service.py:1437-1463` | `_guard_hard_sl` (+ `_cancel_pending_waves`) |
| `app/kss/service.py:1543` | `py.check_tp` — không thể chạm tới khi dyn ON |
| `app/kss/service.py:1624-1677` | `run_position_guard` (armed ⇒ bỏ qua hard-SL) |
| `app/kss/dynamic_exit.py:44/51/59/73/92` | `arm_threshold` / `should_arm` / `lock_floor_price` / `compute_sl` / `compute_tp` |
| `app/orders.py:233` | `auto_fill_due_orders` — nơi DUY NHẤT rung DCA khớp |
| `app/scheduler.py:164` / `:210` / `:231` | `run_cycle` gọi auto-fill / `_guard_once` / `_guard_loop` |
| `app/backtest.py:139` | `bar["high"] >= avg * tp_threshold_factor` — TP tĩnh, không có trail |
| `app/runtime.py:52/64/73/100/116/148-149/282` | `KSS_SETTING_FIELDS`, `cash_floor_usd`, `max_session_deploy_usd`, `kss_tp_gap_pct`, `kss_first_wave_usd`, `regime_gate_*`, `set_kss_settings` |

## Phụ lục: nguồn

### Nội bộ (scratchpad phiên 2026-08-22, read-only)
- `A_kss_logic_audit.md` — audit logic KSS (service/pyramid/dynamic_exit/scheduler/orders)
- `B_scanner_logic_audit.md` — audit scanner/backtest/agents/regime + phễu gate
- `C_outcomes_report.md` — outcome 79 session, AUC phân biệt, tập trung ngày/symbol
- `D_sweep_report.md` — sweep phản-thực 2 mô hình intrabar trên Binance 15m
- `E1_research_dca_bots.md`, `E2_research_quant.md`, `E3_research_risk.md`
- `G_judge_data-fit.md`, `G_judge_capital-safety.md`

### Ngoài (URL)
**DCA / safety-order bot**
- https://blockresearch.ai/blog/smart-safety-orders-explained — công thức deviation/step-scale/volume-scale
- https://help.3commas.io/articles/3108940 — DCA bot interface & main settings
- https://help.3commas.io/en/articles/7000384-dca-bot-reinvestment-and-risk-reduction-features — mô hình full-reservation
- https://www.pionex.com/blog/whats-martingale-bot/ — backtest 10% price-scale: +122.12% / −16.37% maxDD (18 tháng)
- https://support.pionex.com — DCA (Martingale) Bot: Trailing / DIY / Simple mode
- https://bitsgap.com/helpdesk/dca-bot/dca-bot-settings — Regular vs Trailing take-profit, SL mặc định là trailing
- https://bitsgap.com/blog/how-to-read-crypto-backtest-results-like-a-pro — PF ≥1.5; thang cỡ mẫu 30/100/300
- https://hummingbot.org/v2-strategies/executors/dcaexecutor/ — `time_limit` là field ngang hàng SL/TP
- https://www.freqtrade.io/en/stable/strategy-callbacks/ — `adjust_trade_position`, `max_entry_position_adjustment`
- https://github.com/iterativv/NostalgiaForInfinity — 6-12 trade đồng thời, pairlist 40-80
- https://wundertrading.com/en/dca-trading — thuật ngữ DCA bot chuẩn

**Quant / regime / breadth**
- https://research.artemis.ai/p/btc-regime-gated-alt-factor-strategy — gate BTC: 0% DD năm bear 2022, flat 83% số tuần
- https://quantpedia.com/trend-following-and-mean-reversion-in-bitcoin/ — MAX vs MIN, DD >80% của mean-reversion thuần
- https://www.kaiko.com/resources/gap-grows-between-bitcoin-and-altcoins — depth alt −31.3% vs BTC −18.05% khi stress
- https://www.researchgate.net/publication/401623458 — regime-switching; chu kỳ thanh khoản dẫn trước vol 3-5 ngày
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4322637 — cross-sectional momentum 30d/7d
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4675565 — CS momentum **yếu** sau phí; TS momentum bền
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4080253 — momentum & reversal intraday cùng tồn tại, phụ thuộc thanh khoản
- https://www.binance.com/en/blog/all/trader-series-part-1-bitcoin-correlations-421499824684900878 — tương quan BTC-alt trôi theo chu kỳ
- https://articles.stockcharts.com/article/can-market-breadth-help-identify-s-p-500-turning-points/ — breadth (analog cổ phiếu)
- https://www.thetrading.tools/market-breadth — % trên MA50/200 làm tín hiệu regime
- https://hudsonthames.org/does-meta-labeling-add-to-signal-efficacy-triple-barrier-method/ — triple-barrier: rào thời gian là exit hạng nhất
- https://quantstrategy.io/blog/the-triple-barrier-method-revolutionizing-how-we-label/ — cùng chủ đề

**Sizing / rủi ro / phí**
- https://experts.deriv.com/insights/kelly-criterion-position-sizing — Kelly = W − (1−W)/R
- https://www.quantt.co.uk/resources/kelly-criterion-explained — half-Kelly ≈75% tăng trưởng, DD thấp hơn nhiều
- https://coriva.eu.org/en/kelly-criterion-position-sizing/ — full-Kelly ⇒ DD 50%+ gần như chắc chắn
- https://www.mdpi.com/2227-7072/14/3/53 — CVaR trong danh mục crypto (đuôi béo)
- https://blog.quantinsti.com/cvar-expected-shortfall/ — định nghĩa/tính CVaR
- https://www.altrady.com/blog/crypto-bots/ai-crypto-trading-bot-risk-management — 5-10%/bot, 20-30% tổng, 70-80% reserve; band 3-5%/8-12%
- https://www.bitget.com/amp/academy/12560603876807 — cap 5%/bot, giới hạn số safety order
- https://portfoliooptimizer.io/blog/the-effective-number-of-bets-measuring-portfolio-diversification/ — N_eff = 1/Σw²
- https://binancemakertakerfee.org/ — Binance spot 0.1% maker & taker (không rebate)
- https://cryptopotato.com/binance-fees/ — giảm 25% khi trả phí bằng BNB
- https://github.com/binance/binance-spot-api-docs/blob/master/filters.md — MIN_NOTIONAL/stepSize
- https://www.kucoin.com/blog/how-the-martingale-strategy-works-in-crypto-trading-risks-and-rewards — martingale hỏng trong xu hướng một chiều

---

*Tài liệu này là phân tích backtest/paper-trading, không phải lời khuyên đầu tư.*
*Mọi số liệu gắn [V] có thể tái tạo bằng `sqlite3.connect('file:D:/FINDMY/data/findmy.db?mode=ro', uri=True)`
hoặc bằng các script `C_*.py` / `D_*.py` / `G_checks*.py` trong scratchpad phiên 2026-08-22.*
