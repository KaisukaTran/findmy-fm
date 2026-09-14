# Timeout của thang 30 rung / 4% / không stop-loss — có phải 30 ngày là quá ngắn?

Câu hỏi của chủ dự án: một phiên KSS 30 rung, cách nhau 4%, **không có stop-loss**, hiện đang
bị bán thị trường sau **30 ngày** nếu chưa chạm take-profit. Liệu 30 ngày có quá ngắn?

Đo bằng `scripts/ladder_grid_study.py`, cùng bộ mô phỏng/thước đo với `ladder-grid-2026-09-13`,
chỉ đổi `deadline` (14 → 3650 ngày) ở 30 rung và 60 rung, `sl=0` (không stop) trong cả hai
trường hợp.

- **Panel ngày** (2021–2026, có gấu 2022): `--interval 1d --symbols 641 --min-years 2 --every 7
  --workers 8` → 388 coin đủ ≥2 năm dữ liệu, 85.5k–86.1k lượt vào lệnh/cấu hình. Chạy xong ~19s.
- **Panel giờ** (2024–2026): `--interval 1h --symbols 641 --min-years 1 --every 168 --workers 8`
  → 98 coin, 10.7k–10.9k lượt/cấu hình. Chạy xong ~30s.

Lệnh đầy đủ:
```
.venv/Scripts/python.exe scripts/ladder_grid_study.py --interval 1d --symbols 641 --min-years 2 --every 7 --workers 8 --skip-infinite \
  --only "30:0:14,30:0:30,30:0:45,30:0:60,30:0:90,30:0:120,30:0:180,30:0:365,30:0:3650,60:0:30,60:0:60,60:0:90,60:0:180" \
  --out docs/ladder-timeout-2026-09-14-1d > docs/ladder-timeout-2026-09-14-1d.txt 2>&1

.venv/Scripts/python.exe scripts/ladder_grid_study.py --interval 1h --symbols 641 --min-years 1 --every 168 --workers 8 --skip-infinite \
  --only "30:0:14,30:0:30,30:0:45,30:0:60,30:0:90,30:0:120,30:0:180,30:0:365,30:0:3650,60:0:30,60:0:60,60:0:90,60:0:180" \
  --out docs/ladder-timeout-2026-09-14-1h > docs/ladder-timeout-2026-09-14-1h.txt 2>&1
```
Cả hai chạy sạch, không lỗi. `%/$-day` là số gốc script tính (`pct_per_dollar_day`); cột
**"%/năm trên vốn"** là số suy ra thêm theo yêu cầu: `mean_usd / mean_capital_days × 365 × 100`
— về bản chất chỉ là `pct_per_dollar_day × 365`, đổi đơn vị sang %/năm cho dễ so.

## Panel ngày (1d, 2021–2026, 388 coin, sl=0)

### 30 rung

| deadline | MTM opt/pess | $/trial (pess) | tp% | horizon% | worst $ | %/$-ngày | %/năm trên vốn | vốn-ngày/trial | open | open $ |
|---|---|---|---|---|---|---|---|---|---|---|
| 14   | 88.2 / 68.4 | +18.89 | 89.5  | 10.5 | -14,980 | +0.5410 | +197.5 |  3,492 | 312 | -74,368 |
| 30   | 86.2 / 71.3 | +32.31 | 95.6  | 4.4  | -15,151 | +0.4720 | +172.3 |  6,846 | 456 | -207,278 |
| 45   | 82.1 / 67.5 | +33.17 | 97.2  | 2.8  | -15,687 | +0.3499 | +127.7 |  9,479 | 484 | -294,523 |
| 60   | 82.5 / 67.2 | +35.97 | 98.0  | 2.0  | -15,906 | +0.3044 | +111.1 | 11,815 | 512 | -320,765 |
| 90   | 79.8 / 67.0 | +41.90 | 99.0  | 1.1  | -15,950 | +0.2772 | +101.2 | 15,118 | 674 | -545,486 |
| 120  | 81.7 / 70.0 | +49.28 | 99.3  | 0.7  | -15,972 | +0.2729 |  +99.6 | 18,061 | 686 | -601,826 |
| 180  | 80.6 / 70.0 | +52.02 | 99.5  | 0.5  | -15,775 | +0.2277 |  +83.1 | 22,844 | 697 | -652,188 |
| 365  | 81.5 / 69.7 | +59.74 | 99.8  | 0.2  | -16,165 | +0.2077 |  +75.8 | 28,768 | 783 | -1,147,397 |
| 3650 | 82.5 / 70.5 | +84.05 | 100.0 | 0.0  |      +0 | +0.3526 | +128.7 | 23,837 | 907 | -3,014,883 |

### 60 rung

| deadline | MTM opt/pess | $/trial (pess) | tp% | horizon% | worst $ | %/$-ngày | %/năm trên vốn | vốn-ngày/trial | open | open $ |
|---|---|---|---|---|---|---|---|---|---|---|
| 30  | 87.7 / 73.0 | +39.11 | 95.6 | 4.4 | -27,430 | +0.5575 | +203.5 |  7,015 | 455 | -262,187 |
| 60  | 84.5 / 70.0 | +45.19 | 98.0 | 2.0 | -30,400 | +0.3743 | +136.6 | 12,073 | 509 | -368,414 |
| 90  | 82.0 / 69.4 | +51.60 | 99.0 | 1.0 | -30,574 | +0.3303 | +120.6 | 15,621 | 670 | -610,006 |
| 180 | 82.3 / 72.9 | +65.14 | 99.5 | 0.5 | -29,883 | +0.2694 |  +98.3 | 24,180 | 692 | -728,589 |

## Panel giờ (1h, 2024–2026, 98 coin, sl=0)

### 30 rung

| deadline | MTM opt/pess | $/trial (pess) | tp% | horizon% | worst $ | %/$-ngày | %/năm trên vốn | vốn-ngày/trial | open | open $ |
|---|---|---|---|---|---|---|---|---|---|---|
| 14   | 81.0 / 79.0 | +31.50 | 87.8  | 12.2 |  -4,041 | +1.0302 | +376.0 |  3,058 | 103 |   -1,646 |
| 30   | 80.8 / 77.7 | +42.78 | 95.1  | 4.9  |  -7,507 | +0.6629 | +242.0 |  6,454 | 145 |   -8,000 |
| 45   | 77.3 / 75.2 | +44.37 | 96.8  | 3.2  |  -9,398 | +0.4924 | +179.7 |  9,011 | 154 |  -11,596 |
| 60   | 80.5 / 78.1 | +52.54 | 97.9  | 2.1  | -11,319 | +0.4773 | +174.2 | 11,008 | 177 |  -17,257 |
| 90   | 76.0 / 73.9 | +54.57 | 99.1  | 0.9  | -12,503 | +0.4206 | +153.5 | 12,974 | 225 |  -58,726 |
| 120  | 75.5 / 73.5 | +57.74 | 99.4  | 0.6  | -13,054 | +0.3759 | +137.2 | 15,362 | 230 |  -74,966 |
| 180  | 78.4 / 76.9 | +65.22 | 99.6  | 0.4  | -14,721 | +0.3344 | +122.1 | 19,504 | 233 |  -83,434 |
| 365  | 72.6 / 71.2 | +71.14 | 99.9  | 0.1  | -15,913 | +0.3848 | +140.5 | 18,485 | 261 | -226,162 |
| 3650 | 71.9 / 70.4 | +84.29 | 100.0 | 0.0  |      +0 | +0.6311 | +230.4 | 13,357 | 271 | -380,022 |

### 60 rung

| deadline | MTM opt/pess | $/trial (pess) | tp% | horizon% | worst $ | %/$-ngày | %/năm trên vốn | vốn-ngày/trial | open | open $ |
|---|---|---|---|---|---|---|---|---|---|---|
| 30  | 84.3 / 81.3 | +57.13 | 95.1 | 4.9 |  -7,632 | +0.8855 | +323.2 |  6,452 | 145 |   -8,000 |
| 60  | 83.6 / 81.1 | +68.08 | 98.0 | 2.0 | -13,972 | +0.6149 | +224.5 | 11,071 | 177 |  -17,257 |
| 90  | 79.6 / 77.1 | +71.22 | 99.1 | 0.9 | -17,395 | +0.5424 | +198.0 | 13,130 | 225 |  -58,726 |
| 180 | 83.3 / 81.1 | +87.57 | 99.6 | 0.4 | -25,737 | +0.4346 | +158.6 | 20,150 | 233 |  -98,317 |

## Đọc số

1. **MTM share không tăng theo timeout — nó đạt đỉnh sớm rồi đi ngang/giảm.** Ở cả hai panel,
   MTM (opt) cao nhất ngay tại deadline ngắn nhất đo được (14 ngày: 88.2%/1d, 81.0%/1h); MTM
   (pess) đạt đỉnh ở 14–30 ngày (1d: 71.3% tại 30; 1h: 79.0% tại 14) rồi dao động trong khoảng
   67–71% (1d) / 70–79% (1h) cho mọi deadline dài hơn — không có xu hướng tăng rõ ràng, chỉ có
   nhiễu.
2. **%/$-ngày (và %/năm trên vốn) đạt đỉnh ở deadline ngắn và giảm đều khi kéo dài** — ngoại lệ
   duy nhất là deadline=3650 (thực chất vô hạn: horizon=0%, không bao giờ cắt), nơi vốn còn kẹt
   lại tự "biến mất" khỏi mẫu số vì phần lớn lệnh mở đã kịp chạm TP đâu đó trong lịch sử dài.
   Từ 30→90 ngày (1d/30 rung): %/năm rơi từ +172% xuống +101% (giảm ~41%). Từ 30→180 ngày:
   rơi xuống +83% (giảm ~52%). Panel giờ cùng hướng: +242%→+153% (30→90 ngày).
3. **Đánh đổi cốt lõi**: chờ lâu hơn giảm tỷ lệ bị cắt vì hết hạn (horizon%) — 1d/30 rung từ
   10.5% (14 ngày) xuống 0.2% (365 ngày) — nhưng đổi lại vốn bị găm gấp 8 lần lâu hơn
   (3,492 → 28,768 vốn-ngày/lệnh) và số lệnh còn treo cuối dữ liệu tăng vọt cả về số lượng
   (312→783) lẫn giá trị âm treo lại (-74k→-1,15 triệu $). Đây là tiền chưa thực hiện lỗ, không
   phải lỗ thật, nhưng là rủi ro đuôi bị dồn vào các lệnh chưa đóng ở cuối mẫu.
4. **30 vs 60 vs 90 ngày có khác biệt vật chất, nhưng theo hướng ngược lại giả thuyết ban đầu.**
   MTM share giữa ba mốc gần như bằng nhau (1d/30 rung: 71.3/67.2/67.0%; 1h/30 rung:
   77.7/78.1/73.9%) — sai số nằm trong biên nhiễu. Cái khác biệt thật là hiệu suất vốn: %/năm
   giảm hơn 40% khi đi từ 30 lên 90 ngày ở cả hai panel. Vậy 30↔60↔90 không đổi tỷ lệ thắng đáng
   kể, chỉ đổi tốc độ quay vòng vốn.
5. **Con số chủ dự án nên cân**: ở deadline=30 ngày, tỷ lệ lệnh bị cắt vì hết hạn là
   **4.4% (panel ngày, 30 rung) / 4.9% (panel giờ)**. Kéo dài hạn tới 1 năm (365 ngày) chuyển
   gần như **toàn bộ** phần đó thành TP thắng: tp% đi từ 95.6%→99.8% (1d, +4.2 điểm % trong
   4.4% khả dụng ≈ 95% được chuyển hoá) và từ 95.1%→99.9% (1h, +4.8/4.9 ≈ 98%). Nhưng cái giá
   là vốn-ngày/lệnh tăng ~4.2 lần (1d) / ~2.9 lần (1h), và đây là dữ liệu không-stop-loss trong
   một thị trường có thiên hướng tăng dài hạn — gần như mọi lệnh cuối cùng đều chạm TP nếu chờ
   đủ lâu, nên "chuyển hoá gần 100%" một phần là hệ quả của chính cách dựng thí nghiệm (không
   trần), không hẳn là bằng chứng 30 ngày sai.

**Đề xuất**: 30 ngày không phải là điểm bị lỗ hiệu suất — nó gần với điểm hiệu quả vốn cao nhất
đã đo (%/năm cao thứ nhì sau chính 14 ngày, và MTM cao thứ nhì). Nếu muốn giảm cảm giác "bị cắt
oan" ở đúng lúc, nới lên **45 ngày** là một bước đệm rẻ (mất ~26% %/năm ở panel ngày,
mất ~26% ở panel giờ, nhưng horizon% đã giảm hơn một nửa: 4.4%→2.8% và 4.9%→3.2%) — vượt quá đó
(60–90+ ngày) đổi hiệu suất vốn lấy rất ít MTM share tăng thêm, đa phần trong biên nhiễu.
