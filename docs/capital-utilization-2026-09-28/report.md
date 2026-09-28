# Tiền làm việc bao nhiêu? — nghiên cứu sử dụng vốn đã kiểm chứng (2026-09-28)

Paper $7.000, 10 rung @7%, TP 5% +0,5%/rung (+0,24% đệm phí), SL 0, hạn 60 ngày, không trail, ≤80 phiên.
Nến 1d 2021-01..2026-07, 641 coin (173 coin chết/delist), vào lệnh ngẫu nhiên có seed. **Mọi số = trung vị
30 seed (0–29) [p10, p90]; không số nào từ một seed.** Mặc định bound **bi quan**; bound lạc quan chỉ dùng cho DD.
Engine: `scripts/capital_portfolio_study.py` (đã sửa 9 lỗi, xem cuối), lưới: `scripts/capital_utilization_grid.py`.
Kết quả: `results_verified.json`, `results_followup.json`. `results.json`/`run.log` là bản nháp engine chưa sửa — **không trích dẫn**.

## Tóm tắt

1. **"Sử dụng vốn trung bình" đánh lừa**: thị trường gấu thổi nó lên (rung sâu khớp, equity co lại → tiền "làm việc"
   trong lệnh lỗ). Dùng thêm **"ngày bình thường"** = ngày unit-NAV cách đỉnh ≤ 5%. Cấu hình hiện tại
   (cov 30%, 0,4%, trần $40, 10 rung): ngày bình thường **25%**, trung vị theo ngày 27%, nửa đầu 2026 37%, trung bình cả lịch sử 34%.
2. **Không cấu hình nào đạt ≥ 50% trong ngày bình thường mà DD còn chấp nhận được.**
   Tắt dự phòng: các cấu hình đạt ngưỡng đều cần coverage 1–5%, bỏ trần wave, 5–7 rung → DD bi quan **68–86%**
   (hiện tại 44%) và CAGR **thấp hơn** hiện tại (8–15% so với 16,5%). Bật dự phòng: **mọi** cấu hình đạt ngưỡng phá sản
   ở 27–100% seed, chủ nợ quỹ ngoài **$48k–$485k** (trung vị).
3. **Giới hạn cứng:** ngày bình thường, dùng vốn ≈ `số phiên × wave0 ÷ NAV` (phiên chốt TP sau vài ngày ở rung 0, coverage
   hầu như không đụng tới). 80 phiên × $40 = $3.200 ≈ 46% của $7k.
4. **Quỹ ngoài KHÔNG tăng dùng vốn** (25% → 25%), chỉ tăng lãi và DD. Với cấu hình hiện tại cần sẵn **$6,2k trung vị /
   $10,6k p90 / $13,3k max**, đỉnh rút thường 2024-07 hoặc 2025-10.
5. **Khuyến nghị:** `ladder_coverage_pct` 30 → **1**, giữ nguyên mọi thứ khác, **không** dùng quỹ ngoài tự động.
   Dùng vốn: theo ngày 27% → **58%**, nửa đầu 2026 37% → **65%**, ngày bình thường 25% → **41%**. CAGR 16,5% → **25%** [16, 34],
   DD bi quan 44% → **56%** [52, 62], 0/30 seed dưới vốn. Tiền phải chuẩn bị từ ngoài: **$0**. Giá: 2022 −43% (thay vì −17%),
   từ 10/10/2025 −22% (thay vì −9%). Phụ thuộc tốc độ mở phiên (xem độ nhạy).

## Bảng chính (bi quan, trung vị [p10, p90])

"util" = giá vốn đang triển khai ÷ unit-NAV của chủ. "ruined" = seed có unit-NAV ≤ 0. "on − off" = chênh CAGR/DD giữa bật và tắt dự phòng.
Deadline losses = số lần / tổng $ thoát lỗ do hết hạn 60 ngày trong 5,5 năm.

| cov | wave0 | cap | rungs | dự phòng | util ngày bình thường % | tỉ lệ ngày BT | util trung vị ngày % | util TB cả kỳ % | util 2026-01..07 % | CAGR vốn chủ % | DD bi quan / lạc quan % | seed < $7k | seed phá sản | đỉnh vay ngoài $ med / p90 / max | tháng đỉnh vay | on − off | deadline n / $ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **30%** | **0.4%** | **$40** | **10** | tắt | 25 [23, 26] | 32% | 27 | 34 | 37 | 16.5 [10.8, 25.7] | 44 / 29 | 0% | 0% | – | – | CAGR +3.7, DD +4.3 | 106 / −30.535 |
| **30%** | **0.4%** | **$40** | **10** | bật | 25 [23, 26] | 36% | 27 | 34 | 37 | 20.3 [9.9, 27.5] | 48 / 29 | 0% | 0% | 6.150 / 10.616 / 13.293 | 2025-10 (6), 2024-07 (5) | CAGR +3.7, DD +4.3 | 109 / −31.046 |
| 5% | 0.4% | không | 7 | tắt | 51 [50, 53] | 14% | 81 | 75 | 81 | 12.9 [5.7, 19.0] | 68 / 57 | 3% | 0% | – | – | CAGR −36.2, DD +43.3 | 387 / −111.366 |
| 1% | 0.4% | không | 5 | tắt | 58 [57, 59] | 12% | 86 | 81 | 86 | 8.6 [4.4, 13.3] | 69 / 56 | 3% | 0% | – | – | CAGR −40.5, DD +35.6 | 567 / −109.935 |
| 5% | 0.4% | không | 5 | tắt | 55 [54, 56] | 12% | 85 | 79 | 84 | 8.0 [−0.2, 14.2] | 70 / 56 | 13% | 0% | – | – | CAGR −18.8, DD +24.6 | 502 / −96.909 |
| 5% | 1.2% | không | 5 | tắt | 58 [56, 60] | 11% | 85 | 81 | 84 | 9.5 [1.1, 22.2] | 70 / 57 | 10% | 0% | – | – | CAGR −37.0, DD +32.7 | 210 / −111.623 |
| 5% | 0.8% | không | 5 | tắt | 58 [56, 60] | 12% | 86 | 82 | 84 | 11.8 [3.9, 18.7] | 70 / 56 | 0% | 0% | – | – | CAGR −23.9, DD +30.3 | 300 / −124.839 |
| 5% | 0.8% | không | 7 | tắt | 53 [51, 55] | 12% | 83 | 77 | 81 | 12.2 [−1.6, 22.9] | 72 / 55 | 17% | 0% | – | – | CAGR −60.2, DD +39.6 | 213 / −110.635 |
| 5% | 1.2% | không | 7 | tắt | 52 [49, 54] | 13% | 83 | 77 | 81 | 9.8 [3.3, 18.9] | 72 / 56 | 7% | 0% | – | – | CAGR −109.8, DD +58.0 | 156 / −109.132 |
| 1% | 0.4% | không | 7 | tắt | 57 [55, 59] | 13% | 86 | 80 | 84 | 11.6 [4.4, 19.9] | 72 / 58 | 3% | 0% | – | – | CAGR −111.6, DD +60.5 | 505 / −126.781 |
| 1% | 0.4% | không | 10 | tắt | 51 [49, 53] | 14% | 85 | 77 | 81 | 9.5 [−1.5, 19.4] | 73 / 57 | 13% | 0% | – | – | CAGR −109.5, DD +107.6 | 443 / −117.580 |
| 1% | 0.8% | không | 5 | tắt | 65 [63, 67] | 14% | 89 | 88 | 89 | 15.4 [3.2, 21.9] | 75 / 58 | 7% | 0% | – | – | CAGR −115.4, DD +50.3 | 421 / −158.624 |
| 1% | 1.2% | không | 5 | tắt | 66 [64, 68] | 13% | 88 | 88 | 89 | 12.9 [4.2, 24.0] | 76 / 58 | 0% | 0% | – | – | CAGR −112.9, DD +49.5 | 294 / −168.841 |
| 1% | 1.2% | không | 7 | tắt | 61 [59, 64] | 11% | 88 | 87 | 89 | 8.1 [−3.0, 23.7] | 77 / 57 | 13% | 0% | – | – | CAGR −108.1, DD +90.6 | 260 / −136.791 |
| 1% | 0.8% | không | 7 | tắt | 62 [59, 63] | 13% | 88 | 86 | 88 | 11.3 [0.1, 21.1] | 80 / 57 | 10% | 0% | – | – | CAGR −111.3, DD +73.2 | 368 / −158.419 |
| 1% | 1.2% | không | 10 | tắt | 55 [47, 59] | 12% | 87 | 83 | 85 | 2.1 [−16.1, 29.4] | 85 / 57 | 47% | 0% | – | – | CAGR −102.1, DD +110.1 | 225 / −127.253 |
| 1% | 0.8% | không | 10 | tắt | 55 [51, 57] | 13% | 87 | 82 | 84 | 0.9 [−12.7, 13.6] | 86 / 58 | 40% | 0% | – | – | CAGR −100.9, DD +153.9 | 311 / −139.039 |
| 5% | 0.4% | không | 5 | bật | 59 [58, 61] | 10% | 88 | 126 | 132 | −10.9 [−26.4, −0.9] | 95 / 77 | 90% | 27% | 47.958 / 58.884 / 64.987 | 2021-09 (29) | | 422 / −84.925 |
| 5% | 0.8% | không | 5 | bật | 65 [62, 68] | 10% | 90 | 128 | 124 | −12.1 [−100, 10.7] | 101 / 85 | 70% | 53% | 70.569 / 112.097 / 169.237 | 2021-09 (21), 2024-12 (6) | | 212 / −97.598 |
| 5% | 1.2% | không | 5 | bật | 64 [59, 67] | 10% | 91 | 134 | 129 | −27.5 [−100, 14.4] | 103 / 88 | 77% | 60% | 81.019 / 137.991 / 452.128 | 2021-09 (10), 2022-04 (8) | | 142 / −91.499 |
| 1% | 0.4% | không | 5 | bật | 63 [62, 64] | 10% | 97 | 181 | 171 | −31.9 [−100, −18.0] | 105 / 78 | 100% | 70% | 58.132 / 74.869 / 78.500 | 2021-09 (17), 2022-01 (9) | | 432 / −72.663 |
| 5% | 0.8% | không | 7 | bật | 57 [48, 60] | 12% | 77 | 129 | 106 | −48.0 [−100, 28.4] | 111 / 93 | 83% | 77% | 87.891 / 150.428 / 235.091 | 2022-04 (7), 2022-01 (6) | | 112 / −110.160 |
| 5% | 0.4% | không | 7 | bật | 56 [54, 58] | 12% | 76 | 122 | 127 | −23.3 [−100, 10.5] | 112 / 101 | 83% | 73% | 66.503 / 116.318 / 188.512 | 2022-01 (8), 2022-04 (7) | | 262 / −89.220 |
| 1% | 0.8–1.2% | không | 5 | bật | 74–76 | 9–10% | 0* | – | 0* | −100 | 125 / 101–104 | 90–97% | 97% | 140k–167k / 180k–240k / 272k–626k | 2021-09, 2022-01 | | – |
| 1–5% | 0.4–1.2% | không | 7–10 | bật | 57–74 | 11–14% | 0* | – | 0* | −100 | 130–240 | 77–100% | 90–100% | 112k–485k (med), max tới $40M | 2022-01..05 | | – |

\* Seed phá sản thì ngừng giao dịch, trung vị "0" vô nghĩa. Hai dòng cuối gộp 8 cấu hình.

**Không cấu hình nào vừa ≥ 50% ngày bình thường vừa có DD bi quan < 68%, và không cái nào có CAGR cao hơn cấu hình hiện tại** → đường biên dưới đây.

### Đường biên: dùng vốn ngày bình thường so với DD (không bị áp đảo)

Tắt dự phòng:

| cov | wave0 | cap | rungs | DD bi quan % | util ngày BT % | util trung vị ngày % | CAGR vốn chủ % |
|---|---|---|---|---|---|---|---|
| 30% | 0.8% | $40 | 10 | 43 | 24 | 28 | 17.4 [8.6, 23.9] |
| **30%** | **0.4%** | **$40** | **10** (hiện tại) | 44 | 25 | 27 | 16.5 [10.8, 25.7] |
| 30% | 0.4% | $40 | 7 | 49 | 31 | 42 | 9.7 [2.8, 15.8] |
| 15% | 0.4% | $40 | 10 | 52 | 33 | 40 | 19.4 [11.3, 25.4] |
| 30% | 0.4% | không | 5 | 56 | 38 | 56 | 6.7 [1.0, 12.7] |
| **1%** | **0.4%** | **$40** | **10** (khuyến nghị) | **56** | **41** | **58** | **25.0 [15.7, 34.4]** |
| 1% | 0.8% | $40 | 10 | 57 | 43 | 59 | 24.1 [16.3, 36.9] |
| 1% | 0.4% | $40 | 7 | 57 | 45 | 66 | 20.7 [11.4, 25.9] |
| 5% | 0.4% | $40 | 7 | 58 | 46 | 66 | 20.0 [10.9, 27.0] |
| 1% | 0.8% | $40 | 7 | 59 | 46 | 68 | 21.7 [14.4, 24.7] |
| 1% | 0.8% | $40 | 5 | 62 | 50 | 76 | 13.6 [8.8, 18.4] |
| 5% | 0.4% | không | 7 | 68 | 51 | 81 | 12.9 [5.7, 19.0] |
| 5% | 0.8% | không | 5 | 70 | 58 | 86 | 11.8 [3.9, 18.7] |
| 1% | 0.8% | không | 5 | 75 | 65 | 89 | 15.4 [3.2, 21.9] |

Bật dự phòng (không phá sản tới dòng DD 73%):

| cov | wave0 | cap | rungs | DD bi quan % | util ngày BT % | CAGR vốn chủ % | CAGR trên vốn + đỉnh vay % | đỉnh vay med / p90 |
|---|---|---|---|---|---|---|---|---|
| 30% | 0.8% | $40 | 10 | 46 | 25 | 21.3 | – | 7.072 / 10.085 |
| 30% | 0.4% | $40 | 7 | 51 | 32 | 15.3 | – | 9.224 / 11.290 |
| 1% | 0.4% | $40 | 10 | 57 | 35 | 52.1 [41.6, 59.8] | **14.4 [10.8, 18.4]** | 46.938 / 60.926 |
| 1% | 0.8% | $40 | 7 | 63 | 41 | 36.4 | – | 31.925 / 38.393 |
| 1% | 0.8% | $40 | 5 | 73 | 50 | 15.1 | – | 21.611 / 24.376 |

CAGR 52% ở cov 1% + dự phòng là **đòn bẩy**: vay trung vị $47k trên $7k. Tính trên (vốn + đỉnh vay) chỉ 14,4% — thấp hơn cấu hình hiện tại không vay.

## 2022 và cú sập 10/10/2025 (bi quan)

| cấu hình | dự phòng | cửa sổ | NAV return % | DD trong cửa sổ % | util TB % | rút $ | nợ đỉnh $ |
|---|---|---|---|---|---|---|---|
| **hiện tại** (30/0.4/$40/10) | tắt | 2022 | −17 [−31, −6] | 41 [37, 50] | 44 | 0 | 0 |
| | tắt | 2025-10..12 | −1 [−8, 3] | 13 [9, 16] | 39 | 0 | 0 |
| | tắt | 2025-10-10..2026-07 | −9 [−17, 1] | 19 [15, 26] | 37 | 0 | 0 |
| | bật | 2022 | −20 [−38, −5] | 47 [39, 56] | 47 | 6.950 | 3.765 |
| | bật | 2025-10..12 | −4 [−12, 3] | 13 [9, 20] | 41 | 3.065 | 3.065 |
| | bật | 2025-10-10..2026-07 | −11 [−15, 0] | 22 [16, 26] | 39 | 4.873 | 3.401 |
| **khuyến nghị** (1/0.4/$40/10) | tắt | 2022 | −43 [−52, −31] | 56 [52, 61] | 77 | 0 | 0 |
| | tắt | 2025-10..12 | −8 [−18, −2] | 19 [16, 28] | 67 | 0 | 0 |
| | tắt | 2025-10-10..2026-07 | −22 [−37, −9] | 30 [20, 42] | 65 | 0 | 0 |
| | bật | 2022 | −16 [−34, −2] | 57 [41, 77] | 70 | 90.315 | 33.214 |
| | bật | 2025-10..12 | −1 [−8, 3] | 13 [9, 18] | 47 | 32.228 | 32.228 |
| ≥50% DD thấp nhất (5/0.4/không/7) | tắt | 2022 | −52 | 62 | 89 | 0 | 0 |
| | tắt | 2025-10-10..2026-07 | −44 | 47 | 84 | 0 | 0 |
| | bật | 2022 | −92 | 112 | 197 | 147.923 | 61.926 |
| ≥50% CAGR tốt nhất (5/0.8/không/5) | tắt | 2022 | −53 | 62 | 93 | 0 | 0 |
| | tắt | 2025-10-10..2026-07 | −42 | 47 | 88 | 0 | 0 |
| | bật | 2022 | −93 | 101 | 217 | 116.348 | 51.017 |

## Độ nhạy (bi quan, 30 seed)

Lưới dùng 40 phiên mới/ngày. Paper thật 24–27/09: **22–48 coin/ngày qua cổng vào lệnh** ("trade"), bị chặn chỉ vì ngân sách
(~90 lượt/ngày "vượt ngân sách"), nên nguồn ứng viên thật nằm giữa 5 và 40/ngày trong thị trường này.

| cấu hình | dự phòng | biến thể | util ngày BT % | DD bi quan % | CAGR vốn chủ % | đỉnh vay med / p90 |
|---|---|---|---|---|---|---|
| hiện tại | tắt | gốc (40/ngày, floor 20%, deep-lock 4) | 25 | 44 | 16.5 | – |
| | | 5/ngày | 22 | 41 | 16.8 | – |
| | | cash floor 0 | 25 | 46 | 19.2 | – |
| | | deep-lock tắt | 26 | 45 | 18.4 | – |
| hiện tại | bật | gốc | 25 | 48 | 20.3 | 6.150 / 10.616 |
| | | 5/ngày | 22 | 38 | 18.7 | 5.417 / 7.611 |
| | | cash floor 0 | 25 | 48 | 20.3 | 3.617 / 6.483 |
| | | deep-lock tắt | 27 | 49 | 22.1 | 12.135 / 17.248 |
| khuyến nghị (1/0.4/$40/10) | tắt | gốc | 41 | 56 | 25.0 | – |
| | | **5/ngày** | **25** | 44 | **12.6** (10% seed < $7k) | – |
| | | cash floor 0 | 42 | 61 | 32.0 | – |
| | | deep-lock tắt | 47 | 60 | 18.9 | – |
| 5/0.4/không/7 | tắt | gốc / **5/ngày** | 51 / **26** | 68 / 55 | 12.9 / 2.9 (33% < $7k) | – |
| 5/0.8/không/5 | tắt | gốc / **5/ngày** | 58 / **33** | 70 / 65 | 11.8 / −4.6 (73% < $7k) | – |

**Tốc độ mở phiên quyết định tất cả.** Cash floor 20% làm tăng tiền vay ngoài ~$2,5k; deep-lock 4 giảm một nửa tiền vay.

## Khuyến nghị cho $7.000

**1 (chính):** `ladder_coverage_pct = 1`; giữ `first_wave_pct 0.4`, `first_wave_max_usd 40`, `scan_max_waves 10`,
`deep_ladder_lock_rungs 4`, cash floor 20%, `equity_backup_pct 24.8`; không dùng quỹ ngoài tự động.
Dùng vốn theo ngày 58%, nửa đầu 2026 65%, ngày bình thường 41%. CAGR 25% [16, 34], DD 56% [52, 62]. Quỹ ngoài cần: **$0**
(rung sâu bị bỏ đói khi hết tiền — chính điều đó giữ DD 56%). Nếu muốn quỹ ngoài lấp rung: cần $47k / $61k / $64k,
lãi trên tổng vốn 14%/năm — **không nên**. Chấp nhận: năm kiểu 2022 mất ~43% (~−$3.000).

**2 (nếu muốn dùng quỹ ngoài):** giữ cấu hình hiện tại, bật dự phòng. Không tăng dùng vốn (25%); CAGR 16,5 → 20,3%, DD 44 → 48%.
Cần sẵn **$6.150 / $10.616 / $13.293** (med / p90 / max); cash floor 0 thì $3,6k / $6,5k. Trả hết trước cuối kỳ ở 30/30 seed.

**Chỉ chấp nhận ≥ 50% ngày bình thường:** bỏ trần wave + 5–7 rung, không quỹ ngoài; tốt nhất 5/0.8/không/5: DD 70%, CAGR 11,8%,
2022 −53%; sụp nếu thực tế chỉ mở 5 phiên/ngày.

## Kiểm chứng và lỗi đã sửa (so với bản nháp)

1. Deep-ladder lock (`app/scanner.py:1340-1342`, live = 4) chưa mô hình hoá → thêm `deep_lock_rungs`.
2. Cash floor cứng 20% (`app/orders.py:153-172`) chưa mô hình hoá → thêm `cash_floor_pct`; lát < $10 bị từ chối như production.
3. Dự phòng rút giữa bar trước khi lệnh thoát khác cùng bar được ghi → `bar_debt` + `settle_backstop` sau mọi lệnh thoát.
4. Trả nợ quá chậm (giữ cả lock đã tiêu) → chỉ giữ phần lock tương lai + floor.
5. Tiền vay làm to sổ (ngân sách và wave theo equity gộp) → dùng unit-NAV của chủ.
6. "CAGR trên vốn + đỉnh vay" bỏ phần đã trả → sửa `_cagr_total`. 7. Seed phá sản báo CAGR 0% → −100%.
8. Coin delist (173/641) giữ phiên mở mãi, đếm giá vốn là "tiền làm việc" → chốt ở giá đóng cuối.
9. "Coverage 0" production coi là 100% (`scanner.py:1344`, `:1371`) → lưới dùng 1%.
Đúng từ bản nháp: ngân sách theo MTM equity với 24,8% dự phòng, lock `min(fund, spent + cov·fund)` cho mọi phiên, đệm phí TP,
unit-NAV trừ nợ, test parity xanh. Bound lạc quan trên nến ngày không đáng tin (CAGR hiện tại 77% vs 16,5%) — chỉ dùng DD.
Vào lệnh ngẫu nhiên trên 641 coin đánh giá cao nguồn ứng viên — xem độ nhạy.

## Tái lập

```
.venv/Scripts/python.exe -m pytest tests/app/test_capital_portfolio.py -c tests/app/pytest.ini   # 30 passed (gồm parity)
.venv/Scripts/python.exe scripts/capital_utilization_grid.py --stage grid --seeds 30 --workers 8       # 7.260 lượt ~52 phút
.venv/Scripts/python.exe scripts/capital_utilization_grid.py --stage followup --seeds 30 --workers 8 \
    --cells cov30_w0.4_cap40_r10 cov1_w0.4_cap40_r10 cov5_w0.8_nocap_r5 cov5_w0.4_nocap_r7
```
