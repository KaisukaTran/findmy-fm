"""Assemble report.md (Vietnamese) from results_<seed>.json + narrative. python build_report.py"""
from __future__ import annotations

import common  # noqa: F401

import make_tables as T
from common import STUDY

r1, r2 = T.load(20261011), T.load(20261012)
cfg = r1["cfg"]
ps = r1["pool_sizes"]

TXT = f"""# Cổng "đừng mua coin vừa pump" — đo lại trên 2,6 năm nến 1h (2026-10-11)

*Nghiên cứu, không đổi gì trong `app/`. Mọi số dưới đây sinh ra từ `results_20261011.json` (hạt chính) và `results_20261012.json` (hạt lặp, đổi giờ vào lệnh).*

## 0. Kết luận trước

**Câu hỏi:** cổng "coin vừa pump / RSI cao" hoặc cổng hướng-thị-trường có thật sự cải thiện kết quả, hay là nhiễu như 6 cổng đo 2026-09-10?

1. **Hiệu ứng pump là THẬT ở chiều cắt-ngang (so coin này với coin khác CÙNG NGÀY).** Bốn cổng pre-register G2 (pump24h ≥ 6%), G3 (pump7d ≥ 15%), G4 (RSI ngày ≥ 60) và — yếu hơn — G1 (pump24h ≥ 10%) cùng dấu ở cả hai cận, cả ba năm, cả hai hạt giờ-vào-lệnh; hoán vị trong-ngày với Westfall-Young (đã studentise) cho FWER 0,0003–0,04. Khác hẳn 2026-09-10 (không cổng nào hơn ngẫu nhiên). Độ lớn: lọc G2 nâng ppdd của pool từ **{T.f(r1['baseline']['o']['ppdd'],2)} lên {T.f(r1['table']['o']['G2']['passed']['ppdd'],2)}** (lạc quan) / **{T.f(r1['baseline']['p']['ppdd'],2)} lên {T.f(r1['table']['p']['G2']['passed']['ppdd'],2)}** (bi quan), tức +18% / +29% — mất 15% số cơ hội (~6/ngày trên ~41/ngày).
2. **Không có bằng chứng ở chiều THỜI ĐIỂM.** Hoán vị dịch-ngày (kiểm tra "cổng bật đúng lúc xấu") cho p = 0,16–0,57 và FWER ≥ 0,5 với mọi cổng pump. Cổng G6 (BTC 24h ≤ −2%, chặn mua khi BTC đang rơi) **không giúp**: hạt 1 nói "gây hại" (−0,36, FWER 0,05/0,01), hạt 2 nói −0,12/−0,18 và không có ý nghĩa → **không ổn định, không dùng**. Mua ngay sau khi BTC rơi là nhóm tốt nhất trong hạt 1 (ppdd 5,2 vs 1,4), không phải tệ nhất.
3. **G7 (rel-strength đã ship, mặc định TẮT) đảo dấu: chặn coin yếu hơn BTC làm TỆ đi** (lift −0,39…−0,52, cả hai hạt, p trong-ngày 0,0003, nhưng năm 2026 đổi dấu). Đây là cùng một yếu tố đảo chiều ngắn hạn với dấu ngược: coin mạnh tương đối → vào lệnh DCA tệ; coin yếu tương đối → tốt hơn. Đừng bật `rel_strength_enabled`.
4. **G8 (sanity ngược: chặn pump24h < 0)** hạt 1 xấu đi như kỳ vọng (−0,43/−0,42, CI loại 0) nhưng hạt 2 ≈ 0 (+0,02/−0,07) → hiệu ứng pump **nằm ở đuôi phải (≥ 3–6%)**, không phải một đường tuyến tính đi qua 0. Bảng bucket phía dưới cho thấy đúng vậy.
5. **Mẫu paper (276 phiên) có hướng đúng nhưng mức sai:** fell-rate theo pump24h: < 0%: paper 20% → đo 33% (hạt 2: 31%); ≥ 10%: paper 38% → đo 58%. RSI ngày < 50 "ít rơi" (1/17) **không lặp lại** (RSI < 40: 36%, 40–50: 31%, 50–60: 34%); chỉ RSI ≥ 70 xấu rõ (46%). Vùng RSI ngày tốt nhất đổi theo hạt (hạt 1: 40–50 với ppdd 2,99; hạt 2: < 40 với 3,13) → không kết luận về vùng "RSI thấp là tốt".
6. **Bot thật đang nghiêng về phía xấu của các cổng này:** trung vị lúc mở của paper là RSI ngày 59, +3,6%/24h, +7,1%/7d, còn trung vị của pool ứng viên là RSI {T.f(r1['median_at_entry']['rsiD_F'],0)}, {T.f(r1['median_at_entry']['pump24'],1,True)}%/24h, {T.f(r1['median_at_entry']['pump7d'],1,True)}%/7d. Tức scanner đang chọn coin nằm ở nhóm pump cao hơn nhiều so với lấy ngẫu nhiên trong pool; lift tính dưới đây (giả định chọn ngẫu nhiên trong pool) **có thể là cận dưới** của lợi ích với cách chọn thực tế (chưa đo — xem §9).
7. **Đừng đọc quá mức:** (a) chiều độ lớn nhỏ so với nhiễu theo thời gian — khoảng tin cậy khối-ngày của lift G2 là [{T.f(r1['table']['o']['G2']['ci_day30']['pb'][0],2)}; {T.f(r1['table']['o']['G2']['ci_day30']['pb'][1],2)}] trên nền {T.f(r1['baseline']['o']['ppdd'],2)}; (b) hiệu ứng thu hẹp mạnh ở 2026 Jan–Jul (G2: +0,14/+0,21 hạt 1, +0,06/+0,09 hạt 2) — năm mà chính chiến lược lỗ (ppdd nền −0,30/−0,41; BTC −29%); (c) mẫu thưa 1-trên-7-ngày/coin (n = {r1['robustness']['sparse_1in7_per_coin']['n']:,}) cùng dấu nhưng CI chứa 0 ở G1/G2/G3/G5 — hiệu ứng cần nhiều quan sát mới thấy; (d) hai hạt dùng CHUNG một lịch sử giá, nên hạt 2 chỉ kiểm tra độ nhạy theo giờ vào lệnh chứ không phải một mẫu độc lập.
8. **Gợi ý (không phải đã duyệt):** nếu muốn thử một cổng trên paper, ứng viên có bằng chứng nhất là G2 hoặc G4 (lift ~+0,2…+0,33 ppdd, FWER < 0,005 ở cả hai hạt, cùng dấu mọi năm). Chúng chồng lấn (tương quan 0,31) và **chưa đo kết hợp**. Đây là việc của vòng sau (A/B trên paper theo ngày xen kẽ); báo cáo này không chọn ngưỡng nào ngoài 8 cổng đã đăng ký trước.

## 1. Cấu hình dùng (đọc từ `data/findmy.db` bảng `runtime_config`, giá trị mặc định từ `app/config.py`)

| tham số | giá trị | nguồn |
|---|---|---|
| khoảng cách rung | {cfg['distance']}% (rung k ở entry×0,93^k) | `kss:scan_distance_pct` |
| số sóng | {cfg['waves']} | `kss:scan_max_waves` |
| trọng số sóng | (n+1) — wave0=1, wave1=2, … | `app/kss/pyramid.py::generate_wave` = `simulate_kss` (đã đối chiếu) |
| TP danh nghĩa + đệm phí | {cfg['tp_nominal']}% + {cfg['tp_eff']-cfg['tp_nominal']:.2f}% = {cfg['tp_eff']:.2f}% | `kss:scan_tp_pct`, `costengine.tp_fee_buffer_pct()` (1,2 × 2 × 0,1%) |
| TP leo thang | +{cfg['tp_step']}% cho mỗi rung đã khớp (trên trung bình giá) | `kss:kss_tp_step_per_rung_pct`; `app/kss/service.py:93` |
| stop-loss | {cfg['sl']} (tắt) | `kss:sl_pct` |
| hạn | {cfg['deadline']:.0f} ngày (bán MTM theo đóng cửa nến) | `kss:deadline_days` |
| chi phí vòng | {cfg['cost']:.2f}% (2×taker 0,1 + 2×trượt giá 0,05) | `costengine.round_trip_cost_pct()` |
| trail sau TP | tắt | `kss:kss_trail_after_tp_pct` = 0 |
| wave-0 | **${cfg['wave0']:.0f}** (= first_wave_pct 0,4% × neo vốn $7.000, trần $40; `kss_first_wave_usd`=28) → thang đầy 10 rung ≈ $1.013 | `kss:kss_first_wave_usd`, `capital_scale_anchor` |
| vũ trụ scanner | top-100 theo khối lượng 24h, ≥ $1M/24h, ≥ 30 nến ngày | `scan_max_symbols`, `min_quote_volume`, `scanner._MIN_CANDLES` |
| cổng ship sẵn | downtrend (HTF down & ST down & ADX ≥ 25), falling-knife (ST down & MACD-h < 0), consensus ≥ 45 | `app/scanner.py`, `app/agents` |
| `rel_strength_enabled` | **tắt** (G7 được bật tạm trong tiến trình đo, y như công cụ 09-10) | `app/config.py` |
| bỏ qua | `cash_floor` = 0 (đã đặt 10/10), trần 150 phiên, hàng đợi duyệt, Grok (tắt) | — |

## 2. Dữ liệu và mẫu

* `data/research/market.db`: nến 1h 599 mã USDT (2024-01-01 → 2026-07-31 23:00), trong đó 131 mã đã chết — GIỮ NGUYÊN, không sống-sót-thiên-lệch. Nến ngày từ bảng `1d`. Kiểm tra chéo 1h↔1d (BTC/SOL/PEPE, 936 ngày mỗi mã): đóng/cao/khối lượng khớp tuyệt đối.
* Loại tối đa 38 mã không phải coin giao dịch được (stablecoin/neo giá: BFUSD, EURI, KGST, RLUSD, USDE, U…; vàng XAUT/PAXG; wrapped WBTC/WBETH/BNSOL; cổ phiếu token hoá `…BUSDT`; LUNA/LUNC theo asset-guard) — danh sách trong `universe.py::EXCLUDED`.
* **Điểm vào lệnh:** mỗi ngày 2024-01-10 → 2026-07-31 (934 ngày) có MỘT giờ vào lệnh ngẫu nhiên (hạt 20261011 chính; 20261012 lặp), CHUNG cho mọi coin ngày đó (một cụm hoán vị = một lần quét). Tại giờ đó lấy top-100 theo tổng quote-volume 24 nến 1h gần nhất (kể cả nến vào lệnh, đã đóng), ≥ $1M. Mỗi (ngày, coin) = một phiên giả định; **các phiên của cùng một coin chồng thời gian lên nhau** (không giới hạn "một phiên mở/coin" để tránh chọn theo kết quả) → xử lý bằng bootstrap khối-ngày và mẫu thưa 1-trên-7.
* **Phễu:** {ps['universe']:,} cơ hội → {ps['ok_C']:,} có ≥ 30 nến ngày đóng → trừ downtrend ({ps['dt_C']:,} bị chặn) / falling-knife ({ps['fk_C']:,}) / consensus < 45 ({ps['cons_C']:,}; các tập chồng lấn) → **pool chính {ps['pool_C']:,} ({ps['pool_C']/ps['universe']*100:.1f}%), {r1['baseline']['o']['n']/r1['pool_days']:.1f} cơ hội/ngày, 517 coin**. Biến thể "nến ngày đang hình thành": pool {ps['pool_F']:,} (giao với pool chính {ps['both']:,}).
* Gate `backtest_evidence` (win-rate/expectancy walk-forward) KHÔNG nằm trong pool (đề bài chỉ nêu veto + consensus); `bev_C` chặn thêm {ps['bev_C']:,} mục nếu bật.
* Mẫu chạy hết dữ liệu: {r1['baseline']['o']['trunc_share']*100:.1f}% phiên (768) còn mở ở 2026-07-31 → MTM theo đóng cửa cuối; mã chết → MTM theo đóng cửa cuối của mã. Có cột robustness loại các phiên đó (§7). Thiếu giá trị (pool): pump24h 1, pump7d 1, RSI 0.

## 3. Phương pháp

* **Kết quả mỗi phiên:** `app.backtest.simulate_kss` (nguyên bản, import trực tiếp) với cấu hình trên, CẢ HAI cận trong-nến (`pessimistic_intrabar` False/True). Chỉ số chính **ppdd = $ lãi ròng / $1000 vốn-ngày** = 1000·Σpnl/Σcapital_days (tỉ số của tổng, không phải trung bình tỉ số). Phụ: P(rung 2 khớp trong 48h) — mốc −7%, nến theo timestamp; P(TP ≤ 72h); $/phiên (wave-0 $28); % timeout 60 ngày và lỗ MTM TB của chúng; ngày giữ TB; MAE xấu nhất.
* **Cổng (đăng ký trước, không dò ngưỡng):** G1 pump24h ≥ 10%; G2 pump24h ≥ 6%; G3 pump7d ≥ 15%; G4 RSI14 ngày ≥ 60 (nến ngày đóng + nến đang hình thành tại giá vào lệnh, `indicators.rsi`); G5 RSI14 1h ≥ 70 (200 nến 1h cuối); G6 BTC 24h ≤ −2%; G7 `_rel_strength_veto` nguyên bản (7 nến ngày vs BTC, biên 2%, nến ngày ĐÃ ĐÓNG); G8 sanity pump24h < 0. Họ FWER = G1–G7.
* **Pool/cổng ship sẵn dùng đúng hàm của app:** `scripts/measure_entry_gates.gate_snapshot` (downtrend, falling-knife, rel-strength, consensus với `DEFAULT_WEIGHTS`) trên cửa sổ 365 nến ngày đóng trước ngày vào lệnh (biến thể F: thêm nến ngày đang hình thành dựng từ nến 1h đến nến vào lệnh).
* **Chống nhìn trước:** (i) mọi đầu vào của một phiên lấy từ nến có `ts ≤ ts nến vào lệnh` (nến vào lệnh đã ĐÓNG, giá vào = đóng cửa của nó, như `simulate_kss`); mỗi phiên tính `max_input_ts` và `assert max_input_ts ≤ ts` — kết quả: **0 vi phạm / {r1['n_universe_entries']:,} phiên**; (ii) `test_entry_features_are_immune_to_the_future` làm hỏng toàn bộ dữ liệu sau nến vào lệnh (kể cả nến ngày của chính ngày đó) và khẳng định mọi đặc trưng/cờ không đổi; đã kiểm tra bằng đột biến (đổi `bisect_left`→`bisect_right` làm test đỏ); (iii) top-100 tính từ khối lượng ≤ giờ vào lệnh.
* **Ba góc nhìn thống kê, tách bạch:**
  1. *Hoán vị trong-ngày* (hàm gốc `cluster_permutation_pvalues`, nhãn xáo trong từng ngày) → chỉ đo kỹ năng CẮT-NGANG; cổng cấp-ngày (G6) có điểm số không đổi dưới null này (đo được SD = 0) nên **vô quyền lực theo thiết kế**.
  2. *Hoán vị dịch-ngày theo coin* (mới, `stats_ext.day_shift_null`): dịch vòng nhãn của từng coin cùng một số ngày s ∈ [30, N−30] (875 độ dịch, xác định, không dùng ngẫu nhiên) cho mọi coin và cổng, giữ nguyên kết quả → phá quan hệ nhãn–kết quả nhưng giữ tự tương quan và tương quan chéo → thấy được **thời điểm** (thị trường + trong-coin).
  3. *Bootstrap CI* của lift (lọt − nền): khối-ngày vòng (30 ngày; 7 ngày trong JSON) = ước lượng hiệu ứng gộp có tính phụ thuộc thời gian; và cụm theo coin (chỉ bắt dị biệt giữa coin).
* **Westfall-Young step-down max-T** trên cùng một bộ hoán vị dùng chung cho cả họ. **Sửa phương pháp (sau khi thấy kết quả, công khai):** công cụ gốc dùng |lift| theo đơn vị gốc; G6 (cấp-ngày) có |lift| lớn mà null trong-ngày không xê dịch, nên một mình G6 đặt ngưỡng cho cả họ (FWER của G1–G5, G7 bị đẩy lên 0,21 / 0,88 — đúng bằng p của G6). Cột "đơn vị gốc" trong bảng §4 giữ nguyên số của công cụ gốc để đối chiếu; cột "studentised" chia |T| cho SD của null từng cổng, và với null trong-ngày bỏ cổng không có quyền lực (G6). Kết luận ở §0 dựa trên cột studentised (tốt nhất của hai lược đồ). *Đây là điều chỉnh thống kê sau-sự-kiện: số liệu thô vẫn nằm trong JSON.*
* **min_lift:** lift nhỏ nhất mà mẫu này có thể gọi là có ý nghĩa sau 7 lần nhìn (95% của max-T studentised nhân SD của cổng). Một cổng có lift thật dưới mức này "không đo được", không phải "vô tội".
* Kiểm thử: `test_study.py` ({'13'} test, pytest): ppdd là tỉ số tổng; bootstrap phủ 0 dưới null và loại 0 khi có hiệu ứng; WY đơn điệu; hoán vị dịch-ngày phát hiện chế độ ngày thật & không bắt nhiễu; null trong-ngày khớp công cụ gốc; studentise sửa lỗi "cổng ồn át cổng yên"; không nhìn trước; rung-2 theo timestamp.

## 4. Kết quả chính — hạt 1 (giờ vào lệnh A)

Đơn vị ppdd = $ ròng / $1000 vốn-ngày. "lift" = ppdd(lọt) − ppdd(nền); dương = cổng giúp. Số đến từ **cùng một** lần chạy cho cả hai cận.
{T.table_main(r1)}

### Có ý nghĩa thống kê không? (hoán vị + họ G1–G7)

*p/FWER hai phía theo |lift(lọt−chặn)|. "đơn vị gốc" = bản của công cụ 09-10; "studentised" = bản đã sửa (xem §3). Hàng G8 là sanity, ngoài họ.*
{T.table_sig(r1)}

### Kết luận máy móc theo quy tắc `analyze.verdict` (hạt 1)
{T.table_verdict(r1)}

*Quy tắc: "helps/hurts" cần FWER < 0,05 ở CẢ HAI cận (lược đồ studentised tốt nhất) + CI khối-ngày loại 0 ở cả hai cận + cùng dấu mọi năm; thiếu FWER mà CI loại 0 → "suggestive"; hai cận trái dấu → "undecided".*

### Chỉ số phụ (chặn V / lọt L / nền N)
{T.table_secondary(r1)}

## 5. Lặp lại với giờ vào lệnh khác (hạt 2) — cùng lịch sử giá, KHÔNG phải mẫu độc lập
{T.table_replicate(r1, r2)}

Hạt 2: pool {r2['pool_sizes']['pool_C']:,}, ppdd nền {T.f(r2['baseline']['o']['ppdd'],2)} / {T.f(r2['baseline']['p']['ppdd'],2)}.
{T.table_main(r2)}

{T.table_verdict(r2)}

## 6. Bucket (bản sao 2,6 năm của gợi ý trên paper)

Pool chính, hạt 1. "fell" của paper = P(rung 2 trong 48h); "good" của paper ≈ P(TP ≤ 72h và không rung 2).
{T.table_buckets(r1, 'buckets_pump24', 'pump24h — hạt 1')}
{T.table_buckets(r2, 'buckets_pump24', 'pump24h — hạt 2')}
{T.table_buckets(r1, 'buckets_rsiD', 'RSI14 ngày (nến đang hình thành) — hạt 1')}
{T.table_buckets(r2, 'buckets_rsiD', 'RSI14 ngày (nến đang hình thành) — hạt 2')}
{T.table_buckets_year(r1, 'buckets_pump24_by_year', 'pump24h theo năm (hạt 1)')}
{T.table_buckets_year(r1, 'buckets_rsiD_by_year', 'RSI ngày theo năm (hạt 1)')}

**Đọc:** P(rung2≤48h) tăng đơn điệu khi pump24h ≥ 3% (26% → 32% → 38% → 58%); ppdd giảm từ nhóm 0–3% trở lên (đơn điệu ở hạt 1 cả hai cận và hạt 2 cận bi quan; hạt 2 lạc quan có ngoại lệ ≥ 10%: 0,70 > 6–10%: 0,56) và ở nhóm ≥ 10% rơi về ≤ 0,7 (hạt 1 âm). Trong nhóm < 0% và 0–3% hai hạt không đồng ý nhau (hạt 1: 2,2/2,2; hạt 2: 1,85/2,74) → phần hiệu ứng "pump thấp" là nhiễu. Theo năm: nhóm ≥ 10% xấu dần (2024 +1,0 → 2025 −0,1 → 2026 −1,6); năm 2026 hầu hết mọi nhóm đều ≤ 0 (ngoại lệ RSI 40–50: +0,5).

## 7. Lift theo năm (hạt 1) và kiểm tra độ bền
{T.table_years(r1)}

Hạt 2 (cùng định dạng):
{T.table_years(r2)}

**Độ bền** (chỉ lift + CI khối-ngày 30; hạt 1):
{T.table_robust(r1)}

## 8. THĂM DÒ (EXPLORATORY, ngoài họ FWER): bao nhiêu phần của "fell" là do thị trường?

Dùng TƯƠNG LAI của BTC (48h sau vào lệnh) làm biến chẩn đoán — không bao giờ đưa vào cổng. Hạt 1.
{T.table_expl(r1)}

BTC sau vào lệnh là yếu tố trội: P(rung 2) 59% khi BTC giảm trong 48h kế tiếp, 25% khi đi ngang, 16% khi tăng (khớp nhận định của kiểm toán paper: "động lực chính là BTC rơi SAU khi vào"). Nhưng **trong từng tam phân vị BTC, coin pump ≥ 6% vẫn rơi nhiều hơn 8–22 điểm** → hiệu ứng pump không chỉ là BTC. Vì BTC tương lai không biết trước, đây là lý do cổng cấp-coin (G1–G4) hữu ích còn cổng hướng-thị-trường thì không (G6 chỉ nhìn BTC quá khứ 24h; nó không phân biệt được các tam phân vị trên).

## 9. Hạn chế / chưa đo

* **Chọn ngẫu nhiên trong pool.** Lift giả định bot lấy mục trong pool mà không xét đặc trưng. Bot thật xếp hạng ứng viên (và nghiêng về coin pump: trung vị +3,6% vs +0,2%) nên lợi ích thật có thể lớn hơn — hoặc nhỏ hơn nếu thứ hạng đã loại phần xấu. Cần đo trên chính các phiên paper hoặc mô phỏng đúng thứ hạng scanner.
* **Chi phí cơ hội.** Cổng bỏ 2–7 cơ hội/ngày (7–18% pool, pool ~41/ngày) — paper mở ~14 phiên/ngày, còn dư. Nhưng khi tiền mặt là ràng buộc thì mới tính được; không mô phỏng sàn tiền mặt/trần phiên/ngân sách 25% ở đây.
* **Khớp rung theo chạm giá (low ≤ mục tiêu)** ở cả hai cận; paper dùng `paper_fill_needs_trade_through=True` (khó khớp hơn) → cả hai cận hơi lạc quan về tốc độ khớp rung.
* **Đuôi:** ppdd bị chi phối bởi vài % phiên timeout (TB −$175…−$195/phiên, tổng timeout ≈ −$175k so với tổng lãi ròng +$124k ở hạt 1, cận lạc quan). Các cổng G1–G4 không giảm tỉ lệ timeout (G4 thậm chí 3,7% vs 2,6%); lợi ích đến từ vốn bị trói lâu hơn / rung sâu hơn / TP kém nhanh hơn ở nhóm bị chặn, không phải từ tránh timeout. Phiên chết-niêm-yết MTM ở đóng cửa cuối có thể đánh giá thấp lỗ thật.
* **Một lịch sử giá, ba chế độ** (2024 +112% BTC, 2025 −7%, 2026 Jan–Jul −29%). Hiệu ứng mạnh nhất ở 2024–2025, yếu ở 2026; không biết nó có tồn tại trong chế độ mới.
* **Pump có thể chỉ là đại diện của biến động.** Chưa kiểm xem sau khi kiểm soát độ biến động (ATR) hiệu ứng còn không; nếu mục tiêu là "bỏ coin nhiễu" thì cổng ATR có thể rẻ hơn.
* **Họ cổng tương quan** (G1–G2 0,64; G2–G5 0,41): bốn cổng pump không phải bốn bằng chứng độc lập; kết hợp chưa đo.
* **Consensus** dùng `DEFAULT_WEIGHTS` và `MlAgent` không có mô hình (giống công cụ 09-10), không dùng trọng số runtime.
* **Bước thời gian:** `days_to_tp` đo từ mở-nến vào lệnh (lệch +1h) → P(TP ≤ 72h) hơi lạc quan 1 giờ.
* Không mô phỏng Grok, ML, hàng đợi duyệt, trượt giá lúc thoát khẩn, phí funding.

## 10. Tệp và cách chạy lại

Mọi thứ trong `docs/entry-pump-gate-2026-10-11/`: `common.py` (cấu hình, ép DB tạm TRƯỚC khi import `app`), `universe.py` (top-100 điểm-thời-gian), `entry_study.py` (đặc trưng + mô phỏng, ≤ 4 tiến trình), `stats_ext.py` (thống kê thuần), `analyze.py` (bảng, hoán vị, bucket, độ bền), `make_tables.py`/`build_report.py` (render), `test_study.py` (13 test), `results_20261011.json`, `results_20261012.json`, log `run_*.log`.
```
python universe.py 20261011 && python entry_study.py 20261011 4 && python analyze.py 20261011 3000
python -m pytest -c pytest.ini test_study.py -q
```
(`cache_*.pkl/.npz` là bộ nhớ đệm có thể tái tạo, ~40MB mỗi hạt; chưa commit gì.)
"""

(STUDY / "report.md").write_text(TXT, encoding="utf-8")
print("report.md", len(TXT))
