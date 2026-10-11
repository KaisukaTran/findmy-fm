# Cổng "đừng mua coin vừa pump" — đo lại trên 2,6 năm nến 1h (2026-10-11)

*Nghiên cứu, không đổi gì trong `app/`. Mọi số dưới đây sinh ra từ `results_20261011.json` (hạt chính) và `results_20261012.json` (hạt lặp, đổi giờ vào lệnh).*

## 0. Kết luận trước

**Câu hỏi:** cổng "coin vừa pump / RSI cao" hoặc cổng hướng-thị-trường có thật sự cải thiện kết quả, hay là nhiễu như 6 cổng đo 2026-09-10?

1. **Hiệu ứng pump là THẬT ở chiều cắt-ngang (so coin này với coin khác CÙNG NGÀY).** Bốn cổng pre-register G2 (pump24h ≥ 6%), G3 (pump7d ≥ 15%), G4 (RSI ngày ≥ 60) và — yếu hơn — G1 (pump24h ≥ 10%) cùng dấu ở cả hai cận, cả ba năm, cả hai hạt giờ-vào-lệnh; hoán vị trong-ngày với Westfall-Young (đã studentise) cho FWER 0,0003–0,04. Khác hẳn 2026-09-10 (không cổng nào hơn ngẫu nhiên). Độ lớn: lọc G2 nâng ppdd của pool từ **1.73 lên 2.04** (lạc quan) / **1.02 lên 1.31** (bi quan), tức +18% / +29% — mất 15% số cơ hội (~6/ngày trên ~41/ngày).
2. **Không có bằng chứng ở chiều THỜI ĐIỂM.** Hoán vị dịch-ngày (kiểm tra "cổng bật đúng lúc xấu") cho p = 0,16–0,57 và FWER ≥ 0,5 với mọi cổng pump. Cổng G6 (BTC 24h ≤ −2%, chặn mua khi BTC đang rơi) **không giúp**: hạt 1 nói "gây hại" (−0,36, FWER 0,05/0,01), hạt 2 nói −0,12/−0,18 và không có ý nghĩa → **không ổn định, không dùng**. Mua ngay sau khi BTC rơi là nhóm tốt nhất trong hạt 1 (ppdd 5,2 vs 1,4), không phải tệ nhất.
3. **G7 (rel-strength đã ship, mặc định TẮT) đảo dấu: chặn coin yếu hơn BTC làm TỆ đi** (lift −0,39…−0,52, cả hai hạt, p trong-ngày 0,0003, nhưng năm 2026 đổi dấu). Đây là cùng một yếu tố đảo chiều ngắn hạn với dấu ngược: coin mạnh tương đối → vào lệnh DCA tệ; coin yếu tương đối → tốt hơn. Đừng bật `rel_strength_enabled`.
4. **G8 (sanity ngược: chặn pump24h < 0)** hạt 1 xấu đi như kỳ vọng (−0,43/−0,42, CI loại 0) nhưng hạt 2 ≈ 0 (+0,02/−0,07) → hiệu ứng pump **nằm ở đuôi phải (≥ 3–6%)**, không phải một đường tuyến tính đi qua 0. Bảng bucket phía dưới cho thấy đúng vậy.
5. **Mẫu paper (276 phiên) có hướng đúng nhưng mức sai:** fell-rate theo pump24h: < 0%: paper 20% → đo 33% (hạt 2: 31%); ≥ 10%: paper 38% → đo 58%. RSI ngày < 50 "ít rơi" (1/17) **không lặp lại** (RSI < 40: 36%, 40–50: 31%, 50–60: 34%); chỉ RSI ≥ 70 xấu rõ (46%). Vùng RSI ngày tốt nhất đổi theo hạt (hạt 1: 40–50 với ppdd 2,99; hạt 2: < 40 với 3,13) → không kết luận về vùng "RSI thấp là tốt".
6. **Bot thật đang nghiêng về phía xấu của các cổng này:** trung vị lúc mở của paper là RSI ngày 59, +3,6%/24h, +7,1%/7d, còn trung vị của pool ứng viên là RSI 52, +0.2%/24h, +2.7%/7d. Tức scanner đang chọn coin nằm ở nhóm pump cao hơn nhiều so với lấy ngẫu nhiên trong pool; lift tính dưới đây (giả định chọn ngẫu nhiên trong pool) **có thể là cận dưới** của lợi ích với cách chọn thực tế (chưa đo — xem §9).
7. **Đừng đọc quá mức:** (a) chiều độ lớn nhỏ so với nhiễu theo thời gian — khoảng tin cậy khối-ngày của lift G2 là [0.10; 0.62] trên nền 1.73; (b) hiệu ứng thu hẹp mạnh ở 2026 Jan–Jul (G2: +0,14/+0,21 hạt 1, +0,06/+0,09 hạt 2) — năm mà chính chiến lược lỗ (ppdd nền −0,30/−0,41; BTC −29%); (c) mẫu thưa 1-trên-7-ngày/coin (n = 5,506) cùng dấu nhưng CI chứa 0 ở G1/G2/G3/G5 — hiệu ứng cần nhiều quan sát mới thấy; (d) hai hạt dùng CHUNG một lịch sử giá, nên hạt 2 chỉ kiểm tra độ nhạy theo giờ vào lệnh chứ không phải một mẫu độc lập.
8. **Gợi ý (không phải đã duyệt):** nếu muốn thử một cổng trên paper, ứng viên có bằng chứng nhất là G2 hoặc G4 (lift ~+0,2…+0,33 ppdd, FWER < 0,005 ở cả hai hạt, cùng dấu mọi năm). Chúng chồng lấn (tương quan 0,31) và **chưa đo kết hợp**. Đây là việc của vòng sau (A/B trên paper theo ngày xen kẽ); báo cáo này không chọn ngưỡng nào ngoài 8 cổng đã đăng ký trước.

## 1. Cấu hình dùng (đọc từ `data/findmy.db` bảng `runtime_config`, giá trị mặc định từ `app/config.py`)

| tham số | giá trị | nguồn |
|---|---|---|
| khoảng cách rung | 7.0% (rung k ở entry×0,93^k) | `kss:scan_distance_pct` |
| số sóng | 10 | `kss:scan_max_waves` |
| trọng số sóng | (n+1) — wave0=1, wave1=2, … | `app/kss/pyramid.py::generate_wave` = `simulate_kss` (đã đối chiếu) |
| TP danh nghĩa + đệm phí | 5.0% + 0.24% = 5.24% | `kss:scan_tp_pct`, `costengine.tp_fee_buffer_pct()` (1,2 × 2 × 0,1%) |
| TP leo thang | +0.5% cho mỗi rung đã khớp (trên trung bình giá) | `kss:kss_tp_step_per_rung_pct`; `app/kss/service.py:93` |
| stop-loss | 0.0 (tắt) | `kss:sl_pct` |
| hạn | 60 ngày (bán MTM theo đóng cửa nến) | `kss:deadline_days` |
| chi phí vòng | 0.30% (2×taker 0,1 + 2×trượt giá 0,05) | `costengine.round_trip_cost_pct()` |
| trail sau TP | tắt | `kss:kss_trail_after_tp_pct` = 0 |
| wave-0 | **$28** (= first_wave_pct 0,4% × neo vốn $7.000, trần $40; `kss_first_wave_usd`=28) → thang đầy 10 rung ≈ $1.013 | `kss:kss_first_wave_usd`, `capital_scale_anchor` |
| vũ trụ scanner | top-100 theo khối lượng 24h, ≥ $1M/24h, ≥ 30 nến ngày | `scan_max_symbols`, `min_quote_volume`, `scanner._MIN_CANDLES` |
| cổng ship sẵn | downtrend (HTF down & ST down & ADX ≥ 25), falling-knife (ST down & MACD-h < 0), consensus ≥ 45 | `app/scanner.py`, `app/agents` |
| `rel_strength_enabled` | **tắt** (G7 được bật tạm trong tiến trình đo, y như công cụ 09-10) | `app/config.py` |
| bỏ qua | `cash_floor` = 0 (đã đặt 10/10), trần 150 phiên, hàng đợi duyệt, Grok (tắt) | — |

## 2. Dữ liệu và mẫu

* `data/research/market.db`: nến 1h 599 mã USDT (2024-01-01 → 2026-07-31 23:00), trong đó 131 mã đã chết — GIỮ NGUYÊN, không sống-sót-thiên-lệch. Nến ngày từ bảng `1d`. Kiểm tra chéo 1h↔1d (BTC/SOL/PEPE, 936 ngày mỗi mã): đóng/cao/khối lượng khớp tuyệt đối.
* Loại tối đa 38 mã không phải coin giao dịch được (stablecoin/neo giá: BFUSD, EURI, KGST, RLUSD, USDE, U…; vàng XAUT/PAXG; wrapped WBTC/WBETH/BNSOL; cổ phiếu token hoá `…BUSDT`; LUNA/LUNC theo asset-guard) — danh sách trong `universe.py::EXCLUDED`.
* **Điểm vào lệnh:** mỗi ngày 2024-01-10 → 2026-07-31 (934 ngày) có MỘT giờ vào lệnh ngẫu nhiên (hạt 20261011 chính; 20261012 lặp), CHUNG cho mọi coin ngày đó (một cụm hoán vị = một lần quét). Tại giờ đó lấy top-100 theo tổng quote-volume 24 nến 1h gần nhất (kể cả nến vào lệnh, đã đóng), ≥ $1M. Mỗi (ngày, coin) = một phiên giả định; **các phiên của cùng một coin chồng thời gian lên nhau** (không giới hạn "một phiên mở/coin" để tránh chọn theo kết quả) → xử lý bằng bootstrap khối-ngày và mẫu thưa 1-trên-7.
* **Phễu:** 93,385 cơ hội → 89,135 có ≥ 30 nến ngày đóng → trừ downtrend (20,894 bị chặn) / falling-knife (24,766) / consensus < 45 (20,222; các tập chồng lấn) → **pool chính 38,625 (41.4%), 41.4 cơ hội/ngày, 517 coin**. Biến thể "nến ngày đang hình thành": pool 39,412 (giao với pool chính 35,387).
* Gate `backtest_evidence` (win-rate/expectancy walk-forward) KHÔNG nằm trong pool (đề bài chỉ nêu veto + consensus); `bev_C` chặn thêm 13,261 mục nếu bật.
* Mẫu chạy hết dữ liệu: 2.0% phiên (768) còn mở ở 2026-07-31 → MTM theo đóng cửa cuối; mã chết → MTM theo đóng cửa cuối của mã. Có cột robustness loại các phiên đó (§7). Thiếu giá trị (pool): pump24h 1, pump7d 1, RSI 0.

## 3. Phương pháp

* **Kết quả mỗi phiên:** `app.backtest.simulate_kss` (nguyên bản, import trực tiếp) với cấu hình trên, CẢ HAI cận trong-nến (`pessimistic_intrabar` False/True). Chỉ số chính **ppdd = $ lãi ròng / $1000 vốn-ngày** = 1000·Σpnl/Σcapital_days (tỉ số của tổng, không phải trung bình tỉ số). Phụ: P(rung 2 khớp trong 48h) — mốc −7%, nến theo timestamp; P(TP ≤ 72h); $/phiên (wave-0 $28); % timeout 60 ngày và lỗ MTM TB của chúng; ngày giữ TB; MAE xấu nhất.
* **Cổng (đăng ký trước, không dò ngưỡng):** G1 pump24h ≥ 10%; G2 pump24h ≥ 6%; G3 pump7d ≥ 15%; G4 RSI14 ngày ≥ 60 (nến ngày đóng + nến đang hình thành tại giá vào lệnh, `indicators.rsi`); G5 RSI14 1h ≥ 70 (200 nến 1h cuối); G6 BTC 24h ≤ −2%; G7 `_rel_strength_veto` nguyên bản (7 nến ngày vs BTC, biên 2%, nến ngày ĐÃ ĐÓNG); G8 sanity pump24h < 0. Họ FWER = G1–G7.
* **Pool/cổng ship sẵn dùng đúng hàm của app:** `scripts/measure_entry_gates.gate_snapshot` (downtrend, falling-knife, rel-strength, consensus với `DEFAULT_WEIGHTS`) trên cửa sổ 365 nến ngày đóng trước ngày vào lệnh (biến thể F: thêm nến ngày đang hình thành dựng từ nến 1h đến nến vào lệnh).
* **Chống nhìn trước:** (i) mọi đầu vào của một phiên lấy từ nến có `ts ≤ ts nến vào lệnh` (nến vào lệnh đã ĐÓNG, giá vào = đóng cửa của nó, như `simulate_kss`); mỗi phiên tính `max_input_ts` và `assert max_input_ts ≤ ts` — kết quả: **0 vi phạm / 93,385 phiên**; (ii) `test_entry_features_are_immune_to_the_future` làm hỏng toàn bộ dữ liệu sau nến vào lệnh (kể cả nến ngày của chính ngày đó) và khẳng định mọi đặc trưng/cờ không đổi; đã kiểm tra bằng đột biến (đổi `bisect_left`→`bisect_right` làm test đỏ); (iii) top-100 tính từ khối lượng ≤ giờ vào lệnh.
* **Ba góc nhìn thống kê, tách bạch:**
  1. *Hoán vị trong-ngày* (hàm gốc `cluster_permutation_pvalues`, nhãn xáo trong từng ngày) → chỉ đo kỹ năng CẮT-NGANG; cổng cấp-ngày (G6) có điểm số không đổi dưới null này (đo được SD = 0) nên **vô quyền lực theo thiết kế**.
  2. *Hoán vị dịch-ngày theo coin* (mới, `stats_ext.day_shift_null`): dịch vòng nhãn của từng coin cùng một số ngày s ∈ [30, N−30] (875 độ dịch, xác định, không dùng ngẫu nhiên) cho mọi coin và cổng, giữ nguyên kết quả → phá quan hệ nhãn–kết quả nhưng giữ tự tương quan và tương quan chéo → thấy được **thời điểm** (thị trường + trong-coin).
  3. *Bootstrap CI* của lift (lọt − nền): khối-ngày vòng (30 ngày; 7 ngày trong JSON) = ước lượng hiệu ứng gộp có tính phụ thuộc thời gian; và cụm theo coin (chỉ bắt dị biệt giữa coin).
* **Westfall-Young step-down max-T** trên cùng một bộ hoán vị dùng chung cho cả họ. **Sửa phương pháp (sau khi thấy kết quả, công khai):** công cụ gốc dùng |lift| theo đơn vị gốc; G6 (cấp-ngày) có |lift| lớn mà null trong-ngày không xê dịch, nên một mình G6 đặt ngưỡng cho cả họ (FWER của G1–G5, G7 bị đẩy lên 0,21 / 0,88 — đúng bằng p của G6). Cột "đơn vị gốc" trong bảng §4 giữ nguyên số của công cụ gốc để đối chiếu; cột "studentised" chia |T| cho SD của null từng cổng, và với null trong-ngày bỏ cổng không có quyền lực (G6). Kết luận ở §0 dựa trên cột studentised (tốt nhất của hai lược đồ). *Đây là điều chỉnh thống kê sau-sự-kiện: số liệu thô vẫn nằm trong JSON.*
* **min_lift:** lift nhỏ nhất mà mẫu này có thể gọi là có ý nghĩa sau 7 lần nhìn (95% của max-T studentised nhân SD của cổng). Một cổng có lift thật dưới mức này "không đo được", không phải "vô tội".
* Kiểm thử: `test_study.py` (13 test, pytest): ppdd là tỉ số tổng; bootstrap phủ 0 dưới null và loại 0 khi có hiệu ứng; WY đơn điệu; hoán vị dịch-ngày phát hiện chế độ ngày thật & không bắt nhiễu; null trong-ngày khớp công cụ gốc; studentise sửa lỗi "cổng ồn át cổng yên"; không nhìn trước; rung-2 theo timestamp.

## 4. Kết quả chính — hạt 1 (giờ vào lệnh A)

Đơn vị ppdd = $ ròng / $1000 vốn-ngày. "lift" = ppdd(lọt) − ppdd(nền); dương = cổng giúp. Số đến từ **cùng một** lần chạy cho cả hai cận.

**Cận lạc quan** — đơn vị: $ lãi ròng / $1000 vốn-ngày (ppdd). Baseline pool = 1.730.

| cổng | bị chặn (% pool; /ngày) | ppdd bị chặn | ppdd lọt | lift (lọt−nền) | CI khối-ngày 30d | CI theo coin | lọt−chặn |
|---|---|---|---|---|---|---|---|
| G1 pump24h >= 10%  | 2,737 (7.1%; 2.9) | -0.183 | 1.876 | **+0.145** | [+0.03; +0.30] | [+0.05; +0.25] | +2.058 |
| G2 pump24h >= 6%  | 5,993 (15.5%; 6.4) | 0.172 | 2.040 | **+0.310** | [+0.10; +0.62] | [+0.18; +0.46] | +1.869 |
| G3 pump7d >= 15%  | 6,578 (17.0%; 7.0) | 0.516 | 1.989 | **+0.258** | [+0.06; +0.57] | [+0.10; +0.42] | +1.473 |
| G4 daily RSI14 >= 60 (forming bar)  | 6,829 (17.7%; 7.3) | 0.431 | 2.059 | **+0.329** | [+0.08; +0.61] | [+0.15; +0.52] | +1.628 |
| G5 1h RSI14 >= 70  | 2,158 (5.6%; 2.3) | -0.002 | 1.863 | **+0.133** | [+0.05; +0.26] | [+0.06; +0.21] | +1.865 |
| G6 BTC 24h ret <= -2%  | 5,185 (13.4%; 5.6) | 5.248 | 1.375 | **-0.355** | [-0.65; -0.15] | [-0.44; -0.27] | -3.873 |
| G7 shipped rel-strength veto (daily, closed)  | 14,703 (38.1%; 15.7) | 2.403 | 1.344 | **-0.386** | [-1.04; +0.03] | [-0.66; -0.09] | -1.058 |
| G8 SANITY: pump24h < 0% *(sanity)* | 18,479 (47.8%; 19.8) | 2.221 | 1.302 | **-0.428** | [-0.89; -0.13] | [-0.67; -0.18] | -0.919 |

**Cận bi quan** — đơn vị: $ lãi ròng / $1000 vốn-ngày (ppdd). Baseline pool = 1.018.

| cổng | bị chặn (% pool; /ngày) | ppdd bị chặn | ppdd lọt | lift (lọt−nền) | CI khối-ngày 30d | CI theo coin | lọt−chặn |
|---|---|---|---|---|---|---|---|
| G1 pump24h >= 10%  | 2,737 (7.1%; 2.9) | -0.466 | 1.133 | **+0.116** | [+0.02; +0.24] | [+0.03; +0.21] | +1.599 |
| G2 pump24h >= 6%  | 5,993 (15.5%; 6.4) | -0.452 | 1.315 | **+0.297** | [+0.09; +0.61] | [+0.16; +0.44] | +1.766 |
| G3 pump7d >= 15%  | 6,578 (17.0%; 7.0) | -0.239 | 1.290 | **+0.272** | [+0.08; +0.52] | [+0.11; +0.45] | +1.528 |
| G4 daily RSI14 >= 60 (forming bar)  | 6,829 (17.7%; 7.3) | -0.287 | 1.348 | **+0.330** | [+0.10; +0.54] | [+0.16; +0.52] | +1.635 |
| G5 1h RSI14 >= 70  | 2,158 (5.6%; 2.3) | -0.151 | 1.103 | **+0.086** | [+0.00; +0.20] | [+0.02; +0.16] | +1.255 |
| G6 BTC 24h ret <= -2%  | 5,185 (13.4%; 5.6) | 4.901 | 0.639 | **-0.379** | [-0.65; -0.19] | [-0.45; -0.30] | -4.262 |
| G7 shipped rel-strength veto (daily, closed)  | 14,703 (38.1%; 15.7) | 1.936 | 0.501 | **-0.517** | [-1.06; -0.09] | [-0.77; -0.26] | -1.435 |
| G8 SANITY: pump24h < 0% *(sanity)* | 18,479 (47.8%; 19.8) | 1.499 | 0.599 | **-0.419** | [-0.82; -0.13] | [-0.64; -0.18] | -0.900 |

### Có ý nghĩa thống kê không? (hoán vị + họ G1–G7)

*p/FWER hai phía theo |lift(lọt−chặn)|. "đơn vị gốc" = bản của công cụ 09-10; "studentised" = bản đã sửa (xem §3). Hàng G8 là sanity, ngoài họ.*
| cổng | cận | p trong-ngày | FWER trong-ngày: đơn vị gốc G1–7 (công cụ 09-10) | FWER trong-ngày: studentised, bỏ G6 | p dịch-ngày | FWER dịch-ngày: đơn vị gốc | FWER dịch-ngày: studentised | lift tối thiểu phát hiện (trong-ngày / dịch-ngày, studentised) |
|---|---|---|---|---|---|---|---|---|
| G1 | lạc quan | 0.0003 | 0.213 | 0.0030 | 0.345 | 0.558 | 0.614 | 1.44 / 4.59 |
| G1 | bi quan | 0.011 | 0.881 | 0.024 | 0.470 | 0.723 | 0.650 | 1.42 / 4.06 |
| G2 | lạc quan | 0.0003 | 0.213 | 0.0013 | 0.175 | 0.558 | 0.540 | 1.43 / 3.72 |
| G2 | bi quan | 0.0003 | 0.881 | 0.0017 | 0.220 | 0.682 | 0.533 | 1.31 / 3.58 |
| G3 | lạc quan | 0.0003 | 0.213 | 0.0003 | 0.333 | 0.558 | 0.614 | 1.01 / 3.20 |
| G3 | bi quan | 0.0003 | 0.881 | 0.0003 | 0.348 | 0.723 | 0.650 | 0.84 / 3.74 |
| G4 | lạc quan | 0.0030 | 0.213 | 0.0030 | 0.184 | 0.558 | 0.540 | 1.31 / 3.31 |
| G4 | bi quan | 0.0020 | 0.881 | 0.0020 | 0.164 | 0.723 | 0.524 | 1.21 / 3.14 |
| G5 | lạc quan | 0.0080 | 0.213 | 0.0080 | 0.182 | 0.558 | 0.589 | 1.53 / 4.30 |
| G5 | bi quan | 0.086 | 0.881 | 0.086 | 0.333 | 0.723 | 0.650 | 1.38 / 3.33 |
| G6 | lạc quan | 0.025 | 0.213 | – | 0.0068 | 0.097 | 0.051 | – / 3.87 |
| G6 | bi quan | 0.493 | 0.881 | – | 0.0011 | 0.061 | 0.013 | – / 3.62 |
| G7 | lạc quan | 0.0003 | 0.213 | 0.0003 | 0.299 | 0.558 | 0.614 | 0.72 / 2.56 |
| G7 | bi quan | 0.0003 | 0.881 | 0.0003 | 0.147 | 0.723 | 0.508 | 0.68 / 2.74 |
| G8 (sanity) | lạc quan | 0.283 | 0.283 | 0.283 | 0.171 | 0.171 | 0.171 | 1.15 / 1.35 |
| G8 (sanity) | bi quan | 0.530 | 0.530 | 0.530 | 0.160 | 0.160 | 0.160 | 1.25 / 1.27 |

### Kết luận máy móc theo quy tắc `analyze.verdict` (hạt 1)
| cổng | kết luận (quy tắc ghi trong analyze.verdict) |
|---|---|
| G1 pump24h >= 10% | helps: FWER<0.05 both bounds + CI + all years |
| G2 pump24h >= 6% | helps: FWER<0.05 both bounds + CI + all years |
| G3 pump7d >= 15% | helps: FWER<0.05 both bounds + CI + all years |
| G4 daily RSI14 >= 60 (forming bar) | helps: FWER<0.05 both bounds + CI + all years |
| G5 1h RSI14 >= 70 | suggestive helps: day-block CI excludes 0, FWER (o/p) = 0.01/0.09 |
| G6 BTC 24h ret <= -2% | suggestive hurts: day-block CI excludes 0, FWER (o/p) = 0.05/0.01 |
| G7 shipped rel-strength veto (daily, closed) | hurts: FWER<0.05 but day-block CI includes 0 |
| G8 SANITY: pump24h < 0% | suggestive hurts: day-block CI excludes 0, FWER (o/p) = 0.17/0.16 |

*Quy tắc: "helps/hurts" cần FWER < 0,05 ở CẢ HAI cận (lược đồ studentised tốt nhất) + CI khối-ngày loại 0 ở cả hai cận + cùng dấu mọi năm; thiếu FWER mà CI loại 0 → "suggestive"; hai cận trái dấu → "undecided".*

### Chỉ số phụ (chặn V / lọt L / nền N)

**Cận lạc quan** — chặn (V) vs lọt (L) vs nền (N).

| cổng | nhóm | n | P(rung2≤48h) | P(TP≤72h) | $/phiên | timeout % | $ TB phiên timeout | ngày giữ TB | MAE xấu nhất % |
|---|---|---|---|---|---|---|---|---|---|
| nền | N | 38,625 | 33.4% | 54.1% | 3.21 | 2.6% | -175 | 7.3 | -100 |
| G1 | V | 2,737 | 57.7% | 73.4% | -0.34 | 2.3% | -279 | 4.9 | -85 |
| G1 | L | 35,888 | 31.5% | 52.6% | 3.48 | 2.6% | -168 | 7.5 | -100 |
| G2 | V | 5,993 | 47.2% | 67.0% | 0.34 | 2.6% | -246 | 5.8 | -91 |
| G2 | L | 32,632 | 30.8% | 51.7% | 3.74 | 2.6% | -161 | 7.6 | -100 |
| G3 | V | 6,578 | 49.0% | 67.0% | 0.99 | 2.6% | -223 | 5.8 | -97 |
| G3 | L | 32,047 | 30.2% | 51.4% | 3.67 | 2.6% | -165 | 7.6 | -100 |
| G4 | V | 6,829 | 37.7% | 59.0% | 0.92 | 3.7% | -141 | 7.7 | -92 |
| G4 | L | 31,796 | 32.4% | 53.0% | 3.71 | 2.3% | -186 | 7.2 | -100 |
| G5 | V | 2,158 | 41.4% | 61.6% | -0.01 | 3.5% | -180 | 7.5 | -100 |
| G5 | L | 36,467 | 32.9% | 53.7% | 3.40 | 2.5% | -174 | 7.3 | -100 |
| G6 | V | 5,185 | 39.6% | 58.3% | 6.66 | 1.4% | -156 | 6.1 | -100 |
| G6 | L | 33,440 | 32.4% | 53.5% | 2.68 | 2.8% | -176 | 7.5 | -100 |
| G7 | V | 14,703 | 32.5% | 53.7% | 4.27 | 2.1% | -210 | 6.8 | -100 |
| G7 | L | 23,922 | 33.9% | 54.3% | 2.56 | 2.9% | -158 | 7.6 | -100 |
| G8 | V | 18,479 | 32.9% | 53.1% | 4.02 | 2.5% | -170 | 7.2 | -100 |
| G8 | L | 20,146 | 33.8% | 55.0% | 2.48 | 2.7% | -179 | 7.4 | -100 |

**Cận bi quan** — chặn (V) vs lọt (L) vs nền (N).

| cổng | nhóm | n | P(rung2≤48h) | P(TP≤72h) | $/phiên | timeout % | $ TB phiên timeout | ngày giữ TB | MAE xấu nhất % |
|---|---|---|---|---|---|---|---|---|---|
| nền | N | 38,625 | 33.4% | 53.6% | 2.08 | 2.9% | -195 | 7.5 | -100 |
| G1 | V | 2,737 | 57.7% | 72.4% | -0.97 | 2.6% | -281 | 5.2 | -87 |
| G1 | L | 35,888 | 31.5% | 52.2% | 2.31 | 2.9% | -189 | 7.7 | -100 |
| G2 | V | 5,993 | 47.2% | 66.3% | -1.00 | 3.0% | -262 | 6.1 | -98 |
| G2 | L | 32,632 | 30.8% | 51.3% | 2.64 | 2.8% | -182 | 7.8 | -100 |
| G3 | V | 6,578 | 49.0% | 66.2% | -0.51 | 2.9% | -248 | 6.1 | -98 |
| G3 | L | 32,047 | 30.2% | 51.0% | 2.61 | 2.9% | -183 | 7.8 | -100 |
| G4 | V | 6,829 | 37.7% | 58.4% | -0.67 | 4.1% | -167 | 8.0 | -98 |
| G4 | L | 31,796 | 32.4% | 52.6% | 2.66 | 2.6% | -204 | 7.4 | -100 |
| G5 | V | 2,158 | 41.4% | 60.9% | -0.38 | 3.7% | -187 | 7.7 | -100 |
| G5 | L | 36,467 | 32.9% | 53.2% | 2.22 | 2.8% | -195 | 7.5 | -100 |
| G6 | V | 5,185 | 39.6% | 57.7% | 6.62 | 1.5% | -149 | 6.2 | -100 |
| G6 | L | 33,440 | 32.4% | 53.0% | 1.37 | 3.1% | -198 | 7.7 | -100 |
| G7 | V | 14,703 | 32.5% | 53.2% | 3.74 | 2.3% | -219 | 7.0 | -100 |
| G7 | L | 23,922 | 33.9% | 53.8% | 1.05 | 3.2% | -184 | 7.9 | -100 |
| G8 | V | 18,479 | 32.9% | 52.6% | 2.97 | 2.7% | -191 | 7.4 | -100 |
| G8 | L | 20,146 | 33.8% | 54.5% | 1.25 | 3.0% | -198 | 7.6 | -100 |

## 5. Lặp lại với giờ vào lệnh khác (hạt 2) — cùng lịch sử giá, KHÔNG phải mẫu độc lập
Lift (lọt − nền) ppdd: hạt 1 (giờ vào lệnh A) | hạt 2 (giờ vào lệnh B); FWER = studentised tốt nhất (trong-ngày bỏ G6 / dịch-ngày).

| cổng | lq hạt1 | lq hạt2 | bq hạt1 | bq hạt2 | FWER lq h1 / h2 | FWER bq h1 / h2 |
|---|---|---|---|---|---|---|
| G1 | +0.145 | +0.087 | +0.116 | +0.089 | 0.0030 / 0.041 | 0.024 / 0.024 |
| G2 | +0.310 | +0.236 | +0.297 | +0.214 | 0.0013 / 0.0003 | 0.0017 / 0.0007 |
| G3 | +0.258 | +0.212 | +0.272 | +0.233 | 0.0003 / 0.0010 | 0.0003 / 0.0007 |
| G4 | +0.329 | +0.308 | +0.330 | +0.305 | 0.0030 / 0.0040 | 0.0020 / 0.0023 |
| G5 | +0.133 | +0.069 | +0.086 | +0.083 | 0.0080 / 0.067 | 0.086 / 0.024 |
| G6 | -0.355 | -0.122 | -0.379 | -0.180 | 0.051 / 0.866 | 0.013 / 0.685 |
| G7 | -0.386 | -0.404 | -0.517 | -0.507 | 0.0003 / 0.0003 | 0.0003 / 0.0003 |
| G8 | -0.428 | +0.020 | -0.419 | -0.073 | 0.171 / 0.859 | 0.160 / 0.729 |

Hạt 2: pool 38,675, ppdd nền 1.87 / 1.18.

**Cận lạc quan** — đơn vị: $ lãi ròng / $1000 vốn-ngày (ppdd). Baseline pool = 1.872.

| cổng | bị chặn (% pool; /ngày) | ppdd bị chặn | ppdd lọt | lift (lọt−nền) | CI khối-ngày 30d | CI theo coin | lọt−chặn |
|---|---|---|---|---|---|---|---|
| G1 pump24h >= 10%  | 2,737 (7.1%; 2.9) | 0.696 | 1.959 | **+0.087** | [-0.00; +0.23] | [+0.00; +0.18] | +1.263 |
| G2 pump24h >= 6%  | 5,745 (14.9%; 6.2) | 0.620 | 2.109 | **+0.236** | [+0.04; +0.55] | [+0.11; +0.37] | +1.488 |
| G3 pump7d >= 15%  | 6,442 (16.7%; 6.9) | 0.833 | 2.084 | **+0.212** | [+0.01; +0.58] | [+0.06; +0.38] | +1.251 |
| G4 daily RSI14 >= 60 (forming bar)  | 6,582 (17.0%; 7.0) | 0.630 | 2.180 | **+0.308** | [+0.08; +0.61] | [+0.15; +0.49] | +1.550 |
| G5 1h RSI14 >= 70  | 2,137 (5.5%; 2.3) | 0.807 | 1.942 | **+0.069** | [-0.01; +0.16] | [+0.01; +0.14] | +1.135 |
| G6 BTC 24h ret <= -2%  | 6,344 (16.4%; 6.8) | 2.673 | 1.750 | **-0.122** | [-0.33; +0.05] | [-0.21; -0.02] | -0.923 |
| G7 shipped rel-strength veto (daily, closed)  | 14,664 (37.9%; 15.7) | 2.575 | 1.468 | **-0.404** | [-1.06; +0.02] | [-0.67; -0.12] | -1.107 |
| G8 SANITY: pump24h < 0% *(sanity)* | 19,126 (49.5%; 20.5) | 1.850 | 1.892 | **+0.020** | [-0.47; +0.46] | [-0.20; +0.26] | +0.042 |

**Cận bi quan** — đơn vị: $ lãi ròng / $1000 vốn-ngày (ppdd). Baseline pool = 1.183.

| cổng | bị chặn (% pool; /ngày) | ppdd bị chặn | ppdd lọt | lift (lọt−nền) | CI khối-ngày 30d | CI theo coin | lọt−chặn |
|---|---|---|---|---|---|---|---|
| G1 pump24h >= 10%  | 2,737 (7.1%; 2.9) | 0.007 | 1.272 | **+0.089** | [+0.01; +0.23] | [+0.01; +0.17] | +1.264 |
| G2 pump24h >= 6%  | 5,745 (14.9%; 6.2) | 0.038 | 1.397 | **+0.214** | [+0.04; +0.52] | [+0.09; +0.33] | +1.358 |
| G3 pump7d >= 15%  | 6,442 (16.7%; 6.9) | 0.042 | 1.416 | **+0.233** | [+0.04; +0.54] | [+0.06; +0.43] | +1.374 |
| G4 daily RSI14 >= 60 (forming bar)  | 6,582 (17.0%; 7.0) | -0.059 | 1.488 | **+0.305** | [+0.10; +0.56] | [+0.14; +0.49] | +1.547 |
| G5 1h RSI14 >= 70  | 2,137 (5.5%; 2.3) | -0.067 | 1.266 | **+0.083** | [+0.01; +0.20] | [+0.02; +0.15] | +1.333 |
| G6 BTC 24h ret <= -2%  | 6,344 (16.4%; 6.8) | 2.432 | 1.003 | **-0.180** | [-0.34; -0.05] | [-0.26; -0.10] | -1.429 |
| G7 shipped rel-strength veto (daily, closed)  | 14,664 (37.9%; 15.7) | 2.071 | 0.676 | **-0.507** | [-1.04; -0.10] | [-0.75; -0.26] | -1.395 |
| G8 SANITY: pump24h < 0% *(sanity)* | 19,126 (49.5%; 20.5) | 1.264 | 1.110 | **-0.073** | [-0.51; +0.26] | [-0.31; +0.18] | -0.154 |

| cổng | kết luận (quy tắc ghi trong analyze.verdict) |
|---|---|
| G1 pump24h >= 10% | helps: FWER<0.05 but day-block CI includes 0 |
| G2 pump24h >= 6% | helps: FWER<0.05 both bounds + CI + all years |
| G3 pump7d >= 15% | helps: FWER<0.05 both bounds + CI + all years |
| G4 daily RSI14 >= 60 (forming bar) | helps: FWER<0.05 both bounds + CI + all years |
| G5 1h RSI14 >= 70 | no evidence (CI includes 0) |
| G6 BTC 24h ret <= -2% | no evidence (CI includes 0) |
| G7 shipped rel-strength veto (daily, closed) | hurts: FWER<0.05 but day-block CI includes 0 |
| G8 SANITY: pump24h < 0% | undecided (bounds disagree in sign) |

## 6. Bucket (bản sao 2,6 năm của gợi ý trên paper)

Pool chính, hạt 1. "fell" của paper = P(rung 2 trong 48h); "good" của paper ≈ P(TP ≤ 72h và không rung 2).

**pump24h — hạt 1** (pool chính; ppdd lạc quan / bi quan; $/phiên lạc quan / bi quan)

| nhóm | n (%) | P(rung2≤48h) | P(TP≤72h) | P(TP≤72h & không rung2) | ppdd (lq / bq) | $/phiên (lq / bq) | timeout % |
|---|---|---|---|---|---|---|---|
| <0 | 18,479 (47.8%) | 32.9% | 53.1% | 38.4% | 2.22 / 1.50 | 4.02 / 2.97 | 2.5% |
| 0-3 | 9,198 (23.8%) | 25.9% | 46.6% | 37.2% | 2.23 / 1.62 | 4.20 / 3.30 | 2.8% |
| 3-6 | 4,954 (12.8%) | 32.2% | 56.2% | 42.6% | 1.04 / 0.10 | 1.90 / 0.20 | 2.4% |
| 6-10 | 3,256 (8.4%) | 38.3% | 61.6% | 43.6% | 0.43 / -0.44 | 0.91 / -1.02 | 2.9% |
| >=10 | 2,737 (7.1%) | 57.7% | 73.4% | 36.4% | -0.18 / -0.47 | -0.34 / -0.97 | 2.3% |

**pump24h — hạt 2** (pool chính; ppdd lạc quan / bi quan; $/phiên lạc quan / bi quan)

| nhóm | n (%) | P(rung2≤48h) | P(TP≤72h) | P(TP≤72h & không rung2) | ppdd (lq / bq) | $/phiên (lq / bq) | timeout % |
|---|---|---|---|---|---|---|---|
| <0 | 19,126 (49.5%) | 30.8% | 52.3% | 39.0% | 1.85 / 1.26 | 3.32 / 2.48 | 2.5% |
| 0-3 | 9,220 (23.8%) | 26.6% | 48.0% | 38.2% | 2.74 / 1.89 | 5.17 / 3.96 | 2.9% |
| 3-6 | 4,583 (11.9%) | 31.4% | 56.7% | 44.0% | 1.90 / 0.97 | 3.78 / 2.07 | 2.4% |
| 6-10 | 3,008 (7.8%) | 38.6% | 62.3% | 44.1% | 0.56 / 0.06 | 1.22 / 0.15 | 2.8% |
| >=10 | 2,737 (7.1%) | 58.3% | 72.6% | 34.9% | 0.70 / 0.01 | 1.27 / 0.01 | 2.3% |

**RSI14 ngày (nến đang hình thành) — hạt 1** (pool chính; ppdd lạc quan / bi quan; $/phiên lạc quan / bi quan)

| nhóm | n (%) | P(rung2≤48h) | P(TP≤72h) | P(TP≤72h & không rung2) | ppdd (lq / bq) | $/phiên (lq / bq) | timeout % |
|---|---|---|---|---|---|---|---|
| <40 | 1,976 (5.1%) | 36.4% | 57.7% | 38.2% | 1.77 / 1.79 | 2.59 / 2.65 | 1.8% |
| 40-50 | 13,373 (34.6%) | 30.6% | 51.0% | 38.0% | 2.99 / 2.28 | 4.61 / 3.87 | 2.0% |
| 50-60 | 16,447 (42.6%) | 33.5% | 54.1% | 39.9% | 1.51 / 0.74 | 3.11 / 1.68 | 2.7% |
| 60-70 | 5,987 (15.5%) | 36.6% | 57.7% | 39.0% | 0.51 / -0.20 | 1.08 / -0.46 | 3.7% |
| >=70 | 842 (2.2%) | 45.6% | 68.6% | 36.7% | -0.13 / -0.89 | -0.27 / -2.15 | 4.3% |

**RSI14 ngày (nến đang hình thành) — hạt 2** (pool chính; ppdd lạc quan / bi quan; $/phiên lạc quan / bi quan)

| nhóm | n (%) | P(rung2≤48h) | P(TP≤72h) | P(TP≤72h & không rung2) | ppdd (lq / bq) | $/phiên (lq / bq) | timeout % |
|---|---|---|---|---|---|---|---|
| <40 | 1,976 (5.1%) | 34.0% | 58.4% | 39.9% | 3.13 / 3.21 | 3.99 / 4.15 | 1.4% |
| 40-50 | 13,567 (35.1%) | 30.2% | 51.2% | 38.6% | 2.41 / 1.92 | 3.83 / 3.33 | 2.1% |
| 50-60 | 16,550 (42.8%) | 32.5% | 53.9% | 40.4% | 1.96 / 1.10 | 4.02 / 2.49 | 2.7% |
| 60-70 | 5,782 (15.0%) | 35.3% | 57.4% | 39.4% | 0.65 / -0.04 | 1.46 / -0.09 | 3.9% |
| >=70 | 800 (2.1%) | 45.9% | 69.2% | 37.1% | 0.44 / -0.26 | 0.78 / -0.54 | 3.2% |

**pump24h theo năm (hạt 1)** — ppdd lạc quan / bi quan, P(rung2≤48h)

| nhóm | 2024 | 2025 | 2026 Jan–Jul |
|---|---|---|---|
| <0 | n=7,525: 3.61 / 2.46; 38% | n=6,400: 3.98 / 2.47; 34% | n=4,554: -0.17 / -0.19; 24% |
| 0-3 | n=3,349: 3.12 / 1.98; 28% | n=3,390: 4.38 / 3.42; 29% | n=2,459: -0.12 / -0.17; 19% |
| 3-6 | n=1,902: 1.47 / 0.14; 33% | n=1,792: 1.98 / 0.44; 33% | n=1,260: -0.23 / -0.29; 30% |
| 6-10 | n=1,340: 1.08 / 0.55; 34% | n=1,147: 0.98 / -0.58; 42% | n=769: -0.85 / -1.29; 40% |
| >=10 | n=1,082: 1.00 / 0.95; 52% | n=1,049: -0.06 / -0.29; 62% | n=606: -1.57 / -2.09; 61% |

**RSI ngày theo năm (hạt 1)** — ppdd lạc quan / bi quan, P(rung2≤48h)

| nhóm | 2024 | 2025 | 2026 Jan–Jul |
|---|---|---|---|
| <40 | n=663: 7.52 / 7.57; 43% | n=779: 0.75 / 0.95; 37% | n=534: -0.54 / -0.67; 26% |
| 40-50 | n=4,823: 4.31 / 3.74; 34% | n=5,121: 4.59 / 2.76; 33% | n=3,429: 0.50 / 0.43; 22% |
| 50-60 | n=6,777: 2.28 / 1.08; 35% | n=5,684: 2.76 / 1.47; 36% | n=3,986: -0.35 / -0.35; 27% |
| 60-70 | n=2,574: 1.46 / 0.06; 36% | n=1,935: 2.03 / 1.43; 38% | n=1,478: -1.06 / -1.29; 35% |
| >=70 | n=362: 2.39 / 2.57; 43% | n=259: 0.30 / -1.52; 44% | n=221: -1.47 / -2.06; 52% |

**Đọc:** P(rung2≤48h) tăng đơn điệu khi pump24h ≥ 3% (26% → 32% → 38% → 58%); ppdd giảm từ nhóm 0–3% trở lên (đơn điệu ở hạt 1 cả hai cận và hạt 2 cận bi quan; hạt 2 lạc quan có ngoại lệ ≥ 10%: 0,70 > 6–10%: 0,56) và ở nhóm ≥ 10% rơi về ≤ 0,7 (hạt 1 âm). Trong nhóm < 0% và 0–3% hai hạt không đồng ý nhau (hạt 1: 2,2/2,2; hạt 2: 1,85/2,74) → phần hiệu ứng "pump thấp" là nhiễu. Theo năm: nhóm ≥ 10% xấu dần (2024 +1,0 → 2025 −0,1 → 2026 −1,6); năm 2026 hầu hết mọi nhóm đều ≤ 0 (ngoại lệ RSI 40–50: +0,5).

## 7. Lift theo năm (hạt 1) và kiểm tra độ bền
Lift (lọt − nền) theo năm; ô: lạc quan / bi quan.

| cổng | 2024 | 2025 | 2026 (Jan–Jul) | cùng dấu mọi năm & cả hai cận? |
|---|---|---|---|---|
| G1 | +0.13 / +0.06 | +0.32 / +0.21 | +0.08 / +0.11 | CÓ |
| G2 | +0.35 / +0.19 | +0.66 / +0.60 | +0.14 / +0.21 | CÓ |
| G3 | +0.33 / +0.35 | +0.59 / +0.46 | +0.08 / +0.11 | CÓ |
| G4 | +0.32 / +0.37 | +0.23 / +0.14 | +0.26 / +0.33 | CÓ |
| G5 | +0.06 / +0.01 | +0.19 / +0.10 | +0.13 / +0.13 | CÓ |
| G6 | -0.43 / -0.48 | -0.43 / -0.48 | -0.10 / -0.11 | CÓ |
| G7 | -0.48 / -0.75 | -0.83 / -0.89 | +0.04 / +0.03 | KHÔNG |
| G8 (sanity) | -0.76 / -0.64 | -0.63 / -0.49 | -0.13 / -0.21 | CÓ |

Hạt 2 (cùng định dạng):
Lift (lọt − nền) theo năm; ô: lạc quan / bi quan.

| cổng | 2024 | 2025 | 2026 (Jan–Jul) | cùng dấu mọi năm & cả hai cận? |
|---|---|---|---|---|
| G1 | +0.08 / +0.02 | +0.34 / +0.30 | +0.01 / +0.05 | CÓ |
| G2 | +0.31 / +0.17 | +0.70 / +0.62 | +0.06 / +0.09 | CÓ |
| G3 | +0.28 / +0.35 | +0.62 / +0.53 | +0.04 / +0.02 | CÓ |
| G4 | +0.30 / +0.33 | +0.41 / +0.36 | +0.15 / +0.17 | CÓ |
| G5 | +0.08 / +0.03 | +0.09 / +0.18 | +0.08 / +0.08 | CÓ |
| G6 | -0.16 / -0.24 | -0.13 / -0.18 | -0.18 / -0.18 | CÓ |
| G7 | -0.61 / -0.85 | -0.87 / -0.83 | +0.10 / +0.09 | KHÔNG |
| G8 (sanity) | -0.35 / -0.23 | -0.15 / -0.29 | +0.07 / -0.01 | KHÔNG |

**Độ bền** (chỉ lift + CI khối-ngày 30; hạt 1):

**pool_F_forming_bar** (n=39412)

| cổng | n chặn | lift lq | CI30d lq | lift bq | CI30d bq |
|---|---|---|---|---|---|
| G1 | 2,391 | +0.115 | [+0.02; +0.24] | +0.095 | [+0.01; +0.21] |
| G2 | 5,740 | +0.236 | [+0.04; +0.50] | +0.222 | [+0.01; +0.49] |
| G3 | 6,524 | +0.190 | [+0.01; +0.43] | +0.209 | [+0.02; +0.42] |
| G4 | 6,331 | +0.270 | [+0.04; +0.51] | +0.274 | [+0.04; +0.46] |
| G5 | 2,050 | +0.092 | [+0.03; +0.19] | +0.061 | [+0.00; +0.14] |
| G6 | 4,956 | -0.314 | [-0.54; -0.13] | -0.339 | [-0.55; -0.18] |
| G7 | 14,281 | -0.434 | [-1.01; -0.10] | -0.511 | [-0.96; -0.18] |
| G8 | 18,898 | -0.336 | [-0.73; -0.06] | -0.344 | [-0.68; -0.07] |

**sparse_1in7_per_coin** (n=5506)

| cổng | n chặn | lift lq | CI30d lq | lift bq | CI30d bq |
|---|---|---|---|---|---|
| G1 | 401 | +0.206 | [-0.02; +0.47] | +0.174 | [-0.04; +0.40] |
| G2 | 844 | +0.260 | [-0.15; +0.82] | +0.230 | [-0.16; +0.70] |
| G3 | 957 | +0.256 | [-0.15; +0.81] | +0.185 | [-0.13; +0.61] |
| G4 | 1,008 | +0.419 | [+0.07; +0.76] | +0.431 | [+0.04; +0.74] |
| G5 | 310 | +0.159 | [-0.04; +0.39] | +0.092 | [-0.07; +0.27] |
| G6 | 722 | -0.355 | [-0.70; -0.12] | -0.395 | [-0.74; -0.16] |
| G7 | 2,108 | -0.257 | [-1.20; +0.51] | -0.399 | [-1.16; +0.21] |
| G8 | 2,627 | -0.797 | [-1.65; -0.20] | -0.836 | [-1.62; -0.31] |

**horizon_complete_ts_le_2026-06-01** (n=35933)

| cổng | n chặn | lift lq | CI30d lq | lift bq | CI30d bq |
|---|---|---|---|---|---|
| G1 | 2,580 | +0.146 | [+0.03; +0.30] | +0.116 | [+0.02; +0.25] |
| G2 | 5,678 | +0.317 | [+0.12; +0.62] | +0.303 | [+0.08; +0.63] |
| G3 | 6,215 | +0.262 | [+0.08; +0.57] | +0.277 | [+0.10; +0.52] |
| G4 | 6,517 | +0.333 | [+0.10; +0.61] | +0.333 | [+0.11; +0.52] |
| G5 | 2,038 | +0.127 | [+0.04; +0.26] | +0.080 | [-0.01; +0.19] |
| G6 | 4,739 | -0.359 | [-0.66; -0.13] | -0.378 | [-0.68; -0.17] |
| G7 | 13,881 | -0.392 | [-1.00; +0.03] | -0.529 | [-1.04; -0.13] |
| G8 | 17,039 | -0.420 | [-0.88; -0.10] | -0.413 | [-0.78; -0.12] |

**G7_with_forming_daily_context** (n=38625)

| cổng | n chặn | lift lq | CI30d lq | lift bq | CI30d bq |
|---|---|---|---|---|---|
| G7 | 14,659 | -0.481 | [-1.09; -0.13] | -0.586 | [-1.08; -0.25] |

**ex_BTC_ETH** (n=37691)

| cổng | n chặn | lift lq | CI30d lq | lift bq | CI30d bq |
|---|---|---|---|---|---|
| G1 | 2,736 | +0.153 | [+0.04; +0.31] | +0.119 | [+0.02; +0.25] |
| G2 | 5,971 | +0.327 | [+0.11; +0.64] | +0.308 | [+0.09; +0.63] |
| G3 | 6,556 | +0.275 | [+0.06; +0.59] | +0.283 | [+0.07; +0.52] |
| G4 | 6,529 | +0.322 | [+0.09; +0.60] | +0.329 | [+0.10; +0.54] |
| G5 | 2,097 | +0.136 | [+0.05; +0.27] | +0.087 | [+0.00; +0.20] |
| G6 | 5,056 | -0.362 | [-0.65; -0.15] | -0.386 | [-0.65; -0.19] |
| G7 | 14,573 | -0.395 | [-1.06; +0.01] | -0.537 | [-1.09; -0.12] |
| G8 | 18,029 | -0.432 | [-0.89; -0.13] | -0.421 | [-0.82; -0.14] |

## 8. THĂM DÒ (EXPLORATORY, ngoài họ FWER): bao nhiêu phần của "fell" là do thị trường?

Dùng TƯƠNG LAI của BTC (48h sau vào lệnh) làm biến chẩn đoán — không bao giờ đưa vào cổng. Hạt 1.
| BTC 48h SAU vào lệnh | n | P(rung2≤48h) | pump24<0 | 0–6 | ≥6 |
|---|---|---|---|---|---|
| BTC48h down (-17.1…-0.9%) | 12,854 | 59.1% | 60% (n=6,406) | 54% (n=4,513) | 68% (n=1,934) |
| flat (-0.9…+1.2%) | 12,886 | 24.8% | 23% (n=6,113) | 20% (n=4,847) | 42% (n=1,926) |
| up (+1.2…+12.1%) | 12,810 | 16.3% | 14% (n=5,928) | 12% (n=4,758) | 33% (n=2,124) |

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
