# Holdout G4 (RSI14 ngày, kể cả nến đang hình thành, ≥ 60) — dữ liệu SAU cửa sổ gốc (2026-10-11)

*Luật quyết định đăng ký trước, không đổi: lift ppdd của G4 ≥ +0,15 ở CẢ HAI cận → **keep**; ≤ 0 ở MỘT cận bất kỳ → **drop**; còn lại → **inconclusive**.*

## 0. Kết quả

**Theo đúng luật: DROP** (cả hai hạt giờ-vào-lệnh, cả ba tập con; lift âm ở cả hai cận). Nhưng đọc kèm ba điều:

1. **Lift G4 trên mọi phiên (tập a, hạt 1): -1.26 (lạc quan) / -1.23 (bi quan)** ppdd; CI khối-ngày-14 [-1.75; +0.64] / [-1.73; +0.61]; p hoán vị trong-ngày 0.17 / 0.16. Hạt 2: -0.58 / -0.57, p 0.59. **Không có ý nghĩa thống kê theo hướng nào.**
2. **Mẫu gần như không có quyền lực.** 71 ngày; MDE 80% ≈ 1.7 ppdd (hạt 1), 1.4 (hạt 2) — gấp ~4–5 lần hiệu ứng cần xác nhận (+0,33 ở cửa sổ gốc) và ~25% ppdd nền của holdout (6.5). CI chứa cả +0,15 lẫn −1,2: dữ liệu này **không phân biệt được** "G4 vô dụng" với "G4 còn nguyên hiệu ứng".
3. **Kết quả "drop" do MỘT coin.** Loại KAITO (6–8 phiên chồng nhau, đủ 10 rung, timeout 60 ngày, tổng −$1,3k…−$1,8k; thuộc nhóm RSI 40–50 nên nằm bên "lọt") thì lift G4 thành +0.19/+0.19 (hạt 1) và +0.50/+0.48 (hạt 2) — sẽ là "keep" theo luật. **THĂM DÒ, chọn coin SAU khi thấy dữ liệu, không thay đổi quyết định.** Nhưng nó cho thấy kết luận 71 ngày này mong manh theo một cụm phiên.

**Tóm lại:** quyết định đăng ký trước = *drop*; bằng chứng hàm ý = *không kết luận được, nghiêng trung tính* (không phải bằng chứng mạnh chống G4). G4 không được xác nhận trên holdout.

## 1. Thiết kế (giống nghiên cứu chính)

* Dữ liệu `data/research/market_holdout.db` (chỉ-đọc): 1h từ 2026-07-01, 1d từ 2025-07-01. **Tháng 7/2026 chỉ làm lịch sử/khởi động, không có phiên nào vào lệnh trong đó.** Vào lệnh 2026-08-01 → 2026-10-10 (71 ngày). 53 mã chỉ có nến ngày (đã hủy niêm yết trước 07/2026) không có nến 1h nên không vào được vũ trụ.
* Pipeline y hệt: top-100 theo khối lượng 24h điểm-thời-gian (≥ $1M, bỏ stable/wrapped/token hoá), ≥ 30 nến ngày đóng, downtrend + falling-knife + consensus ≥ 45 trên 365 nến ngày đóng; cùng `simulate_kss`, cùng cấu hình (rung 7%, 10 sóng, TP 5,24% + 0,5%/rung, SL 0, 60 ngày, chi phí 0,30%, wave-0 $28 — in lại từ `runtime_config`, không đổi); cả hai cận trong-nến; giờ vào lệnh ngẫu nhiên mỗi ngày với hạt 20261011 (chính) và 20261012. Mã mới: `holdout.py` (vá hằng số của `entry_study.py`/`universe.py`, không sửa file đã commit). Look-ahead: 0 vi phạm.
* Thống kê: lift = ppdd(lọt) − ppdd(nền); CI bootstrap khối-ngày vòng. **Độ dài khối đổi từ 30 (nghiên cứu chính) xuống 14 vì mẫu chỉ 71 ngày**; CI với khối 7 và 30 trong JSON (kết luận không đổi). p = hoán vị trong-ngày (một cổng, không cần họ). MDE 80% ≈ 2,8 × SE (SE suy từ độ rộng CI khối-14) — xấp xỉ.

## 2. Mẫu

| | hạt 1 | hạt 2 |
|---|---|---|
| cơ hội trong vũ trụ | 7,098 | 7,098 |
| pool (qua downtrend/FK/consensus) | **3,811** (53.7/ngày) | **3,791** (53.4/ngày) |
| (a) tất cả (gồm MTM tại nến cuối) | 3,811 | 3,791 |
| trong đó chưa thoát hết dữ liệu (MTM, có cờ) | 390 (10.2%) | 398 (10.5%) |
| (b) đã thoát trước hết dữ liệu (TP hoặc timeout) | **3,421** | **3,392** |
| (c) đủ 60 ngày (vào ≤ 2026-08-11) | 548 (12 ngày) | 498 |
| mã đã chết cuối chuỗi | 8 | 7 |
| tỉ lệ G4 chặn | 33.4% | 30.2% |

**Cảnh báo tập (b):** chỉ giữ phiên đã thoát nghĩa là bỏ các phiên còn kẹt — đúng loại phiên thua nặng nhất — nên (b) thiên lệch lạc quan và ppdd nền tăng (6.5 → 8.0). Tập (c) chỉ ~12 ngày, ~500 phiên và ~70 phiên bị chặn: bootstrap suy biến, không dùng để kết luận.

## 3. G4 (quyết định) và G2 (hàng phụ, KHÔNG thuộc quyết định)

**G4 — RSI14 ngày ≥ 60** (lq/bq = cận lạc quan/bi quan; lift tính trên ppdd = $ ròng/$1000 vốn-ngày):
| hạt | tập | n phiên (chặn) | lift lq | lift bq | CI khối-ngày14 lq | CI khối-ngày14 bq | p trong-ngày (lq/bq) | MDE 80% | luật |
|---|---|---|---|---|---|---|---|---|---|
| 1 | (a) tất cả | 3,811 (1,273) | -1.255 | -1.228 | [-1.75; +0.64] | [-1.73; +0.61] | 0.167 / 0.157 | 1.70 | **drop** |
| 1 | (b) đã thoát trước hết dữ liệu | 3,421 (1,160) | -1.739 | -1.688 | [-2.67; +0.82] | [-2.64; +0.81] | 0.359 / 0.337 | 2.49 | **drop** |
| 1 | (c) đủ 60 ngày (vào ≤ 11/08) | 548 (74) | -0.718 | -0.750 | [-0.72; -0.72] | [-0.75; -0.75] | 0.461 / 0.423 | 0.00 | **drop** |
| 2 | (a) tất cả | 3,791 (1,145) | -0.577 | -0.575 | [-1.14; +0.82] | [-1.14; +0.79] | 0.590 / 0.580 | 1.40 | **drop** |
| 2 | (b) đã thoát trước hết dữ liệu | 3,392 (1,039) | -1.049 | -1.041 | [-1.89; +0.74] | [-1.86; +0.66] | 0.327 / 0.293 | 1.88 | **drop** |
| 2 | (c) đủ 60 ngày (vào ≤ 11/08) | 498 (72) | -1.012 | -1.056 | [-1.01; -1.01] | [-1.06; -1.06] | 0.181 / 0.158 | 0.00 | **drop** |

**G2 — pump24h ≥ 6% (phụ):**
| hạt | tập | n phiên (chặn) | lift lq | lift bq | CI khối-ngày14 lq | CI khối-ngày14 bq | p trong-ngày (lq/bq) | MDE 80% | luật |
|---|---|---|---|---|---|---|---|---|---|
| 1 | (a) tất cả | 3,811 (637) | -0.856 | -0.819 | [-1.13; -0.49] | [-1.09; -0.41] | 0.000 / 0.001 | 0.45 | **drop** |
| 1 | (b) đã thoát trước hết dữ liệu | 3,421 (615) | -1.033 | -0.967 | [-1.64; -0.52] | [-1.56; -0.38] | 0.002 / 0.023 | 0.80 | **drop** |
| 1 | (c) đủ 60 ngày (vào ≤ 11/08) | 548 (50) | -0.409 | -0.439 | [-0.41; -0.41] | [-0.44; -0.44] | 0.085 / 0.063 | 0.00 | **drop** |
| 2 | (a) tất cả | 3,791 (575) | -0.595 | -0.592 | [-0.85; +0.00] | [-0.87; +0.04] | 0.043 / 0.043 | 0.61 | **drop** |
| 2 | (b) đã thoát trước hết dữ liệu | 3,392 (542) | -0.878 | -0.866 | [-1.40; -0.28] | [-1.38; -0.24] | 0.012 / 0.019 | 0.80 | **drop** |
| 2 | (c) đủ 60 ngày (vào ≤ 11/08) | 498 (61) | -0.617 | -0.685 | [-0.62; -0.62] | [-0.68; -0.68] | 0.003 / 0.002 | 0.00 | **drop** |

G2 trong holdout **hại rõ ràng** (hạt 1: p = 0.0002; loại KAITO vẫn -0.65/-0.57): coin pump ≥ 6% có ppdd 14.3 so với 5.6 của phần còn lại. Đây là chế độ tăng mạnh (xem §5), nơi động lượng thắng đảo chiều — ngược với 2024–2025 ở cửa sổ gốc. Cổng pump đổi dấu theo chế độ; G4 yếu hơn và không đổi dấu rõ ràng sau khi bỏ KAITO.

## 4. Bucket RSI ngày (holdout, nến đang hình thành)

Hạt 1, tất cả phiên:
| RSI ngày | n | P(rung2≤48h) | ppdd lq / bq | $/phiên lq / bq |
|---|---|---|---|---|
| <40 | 112 | 15.2% | 14.67 / 11.48 | 2.95 / 3.05 |
| 40-50 | 875 | 13.1% | 0.58 / 0.66 | 0.28 / 0.32 |
| 50-60 | 1,551 | 20.1% | 9.19 / 9.17 | 2.57 / 2.56 |
| 60-70 | 1,050 | 30.8% | 8.94 / 8.93 | 2.91 / 2.95 |
| >=70 | 223 | 50.7% | 10.84 / 9.68 | 2.80 / 2.85 |

Hạt 1, chỉ phiên đã thoát (b):
| RSI ngày | n | P(rung2≤48h) | ppdd lq / bq | $/phiên lq / bq |
|---|---|---|---|---|
| <40 | 106 | 16.0% | 14.81 / 11.55 | 3.09 / 3.20 |
| 40-50 | 782 | 12.5% | 0.67 / 0.75 | 0.33 / 0.38 |
| 50-60 | 1,373 | 19.4% | 11.98 / 11.96 | 3.03 / 3.02 |
| 60-70 | 948 | 31.8% | 11.72 / 11.67 | 3.37 / 3.41 |
| >=70 | 212 | 51.9% | 14.50 / 12.56 | 3.18 / 3.24 |

Hạt 2, tất cả phiên:
| RSI ngày | n | P(rung2≤48h) | ppdd lq / bq | $/phiên lq / bq |
|---|---|---|---|---|
| <40 | 91 | 16.5% | 12.24 / 12.15 | 2.87 / 2.87 |
| 40-50 | 850 | 14.4% | 2.34 / 2.37 | 1.00 / 1.04 |
| 50-60 | 1,705 | 19.9% | 9.35 / 9.34 | 2.61 / 2.62 |
| 60-70 | 1,011 | 27.0% | 8.27 / 8.24 | 2.62 / 2.62 |
| >=70 | 134 | 42.5% | 9.55 / 9.62 | 2.86 / 2.92 |

* P(rung 2 trong 48h) tăng theo RSI từ nhóm 40–50 trở lên (hạt 1: 13% → 20% → 31% → 51%) — giống cửa sổ gốc; nhưng ở holdout rung 2 khớp KHÔNG kéo theo lỗ (thị trường đang lên, rung khớp rồi hồi) nên ppdd không giảm theo RSI.
* Nhóm 40–50 có ppdd bất thường thấp (0,58 hạt 1; 2,34 hạt 2) — do KAITO (phiên timeout −$420…−$460 mỗi phiên, xoá gần hết lãi của nhóm). Đây chính là điểm khiến lift G4 âm.

## 5. Bối cảnh thị trường

BTC trong cửa sổ vào lệnh (08-01 → 10-11): **+32.1%** (62,938 → 83,171), theo tháng: 08 +24.9%, 09 +6.3%, 10 (đến 11/10) -0.4%; sụt tối đa -7.7% — **một nhịp tăng gần như một chiều**, trái với cửa sổ gốc (BTC +112% / −7% / −29% theo năm, ppdd nền 1,7). Ppdd nền holdout 6.5 lạc quan / 6.4 bi quan — gấp ~4 lần — nên mọi nhóm đều lãi và hiệu ứng "tránh phiên xấu" ít có chỗ để hiện ra.

## 6. Quyền lực và giới hạn (rõ ràng)

* **71 ngày × ~54 phiên/ngày, nhưng phiên chồng nhau tới 60 ngày** → chỉ ~2–3 khối độc lập 30 ngày. SE lift G4 ≈ 0.61 ppdd (hạt 1); MDE 80% ≈ 1.7. Ngưỡng 95% của |lift lọt−chặn| dưới null hoán vị là 4.3 ppdd. So với +0,15 (ngưỡng keep) và +0,33 (hiệu ứng gốc), mẫu **không thể xác nhận cũng không thể bác bỏ** hiệu ứng cỡ đó.
* ~10% phiên bị cắt ở cuối dữ liệu (MTM), tập trung ở tháng 9–10; tập (b) loại chúng nhưng thiên lệch.
* Hai hạt dùng chung một lịch sử giá và đều bị chi phối bởi cùng cụm KAITO.
* Chưa mô phỏng sàn tiền mặt, trần phiên, thứ hạng scanner; lift giả định chọn ngẫu nhiên trong pool.
* Phép loại KAITO và đổi khối bootstrap 30→14 là lựa chọn của người phân tích, ghi rõ; luật quyết định không bị đổi.
* Gợi ý bước sau (không thuộc luật): theo dõi G4 trên paper thật (`entry_rsi_max` ghi sẵn các phiên bị chặn) và đánh giá lại khi có ≥ 6 tháng dữ liệu ở nhiều chế độ; xét tách theo chế độ thị trường vì dấu của cổng pump đã đổi giữa hai cửa sổ.

Tệp: `holdout.py` (build/analyze), `build_holdout_report.py`, `results_holdout.json`, `cache_holdout_20261011.pkl`, `cache_holdout_20261012.pkl`, `run_holdout_*.log`.
