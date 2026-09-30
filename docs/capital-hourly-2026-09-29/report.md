# Nến giờ 2024-01..2026-07: coverage 1% vs 30% — kiểm chứng đối kháng (2026-09-29)

Bản đo (Sonnet, 10 seed) → kiểm chứng đối kháng (Opus, 220 lượt 1h, sửa 1 lỗi warmup, thêm seed 10–19).

## Tóm tắt

- **Khuyến nghị đứng vững: TRẢ `ladder_coverage_pct` của paper từ 1 về 30, giữ sàn tiền 20%.**
- Kết quả gốc TÁI LẬP ĐÚNG. Engine còn MỘT lỗi đơn vị trên nến giờ: `warmup = 24` vẫn đếm theo NẾN (= 24 GIỜ), nên coin mới
  niêm yết được mua ngay ngày thứ 2, trong khi production đòi 30 nến ngày (`app/scanner.py:42`). Đã sửa (test trước): trên nến giờ,
  warmup = số NGÀY kể từ ngày niêm yết (lấy từ bảng 1d); nến ngày giữ nguyên byte-for-byte. Sửa lỗi làm từng seed lệch tới ~10 điểm
  nhưng hầu như không đổi chênh cov1−cov30.
- Số đã sửa (40 phiên/ngày, 20 seed), CAGR trung vị / DD tối đa trung vị:
  - cov1: bi quan −23,6% / 72,8%; lạc quan −16,0% / 72,3%.
  - cov30: bi quan +10,6% / 34,4%; lạc quan +20,3% / 29,3%.
  - Chênh cặp cov1−cov30: **−32,8 [−38,2; −27,3]** (bi quan), **−36,9 [−42,4; −30,8]** (lạc quan); 20/20 seed âm ở cả hai cận.
- Cơ chế: coverage 1% chỉ giữ chỗ ~$11 cho thang ~$1,1k → cổng gần như chỉ đếm tiền đã tiêu → sổ mở tới ~80 phiên, tiền mặt nằm sát
  sàn → rung đói (1.878 rung riêng biệt vs 47) → không trung bình giá xuống được → phiên kẹt tới hạn 60 ngày rồi bán lỗ (6,9% phiên vs
  3,0%; chạy riêng lẻ chỉ 2%) → sổ chạy theo chu kỳ ~2 tháng: cả tháng không mở phiên nào rồi mở dồn 300–400 phiên.
- Gần như toàn bộ chênh lệch nằm ở **2025** (−48 điểm, 20/20 seed); 2026H1 −15 điểm; 2024 không có ý nghĩa.
- Sàn 0 vs 20: ở cov1 sàn 0 TỆ hơn (−11,2 [−16,1; −5,5]) vì sàn 20% là thứ duy nhất còn giữ tiền mặt; ở cov30 sàn không ảnh hưởng
  (+0,7, không ý nghĩa). Ngược dấu với nến ngày vì nến ngày không phân xử được (khoảng cách hai cận 154 điểm).
- Nến giờ thu hẹp khoảng cách hai cận: cov1 +9,0, cov30 +13,1 điểm (nến ngày +154 / +47).
- Cảnh báo: cov30 cũng yếu — 2025 −5,5% (bi quan), âm sau 10/10/2025, 20% seed kết thúc dưới $7k (bi quan). Khuyến nghị là "bớt tệ
  hơn nhiều", không phải "có lãi". Seed chỉ xáo lại lựa chọn coin trên MỘT lịch sử giá.

## 1. Thiết lập

$7k; gate=reserve; wave0 0,4% equity, trần $40, sàn $10; 10 rung @7%; TP 5% +0,5%/rung +0,24% đệm phí; SL 0; hạn 60 ngày; không trail;
≤80 phiên; dự phòng 24,8%; deep-lock 4; không quỹ ngoài; chi phí vòng 0,30%. Vũ trụ: 599 cặp USDT spot có nến 1d trong kỳ (131 chết
trong kỳ); vào lệnh ngẫu nhiên. **warmup = tuổi niêm yết ≥ 24 ngày trên cả hai khung (sửa 2026-09-29).** Thống kê cặp: bootstrap 95%
(10.000 lần) trên chênh lệch theo seed; khoảng t có trong output `verify_analyze.py`.

## 2. Kết quả (engine đã sửa, 1h)

CAGR trung vị % / DD tối đa trung vị % / % seed kết thúc dưới $7k. N=20 nơi ghi, còn lại 10.

| ô | 40/ngày bi quan | 40/ngày lạc quan | 5/ngày bi quan | 5/ngày lạc quan |
|---|---|---|---|---|
| cov1 sàn20 | −23.6 / 72.8 / 100% (N20) | −16.0 / 72.3 / 90% (N20) | −15.2 / 56.0 / 100% | +4.7 / 40.6 / 30% |
| cov1 sàn0 | −34.8 / 84.0 / 100% | −22.7 / 78.5 / 100% | −8.2 / 54.9 / 90% | +5.0 / 53.4 / 40% |
| cov30 sàn20 | +10.6 / 34.4 / 20% (N20) | +20.3 / 29.3 / 0% (N20) | +6.8 / 34.9 / 30% | +13.8 / 35.3 / 20% |
| cov30 sàn0 | +7.0 / 44.7 / 30% | +24.6 / 28.8 / 10% | (code cũ +15.3 / 31.5) | (code cũ +18.6 / 32.3) |

Chênh cặp, trung bình [KTC95%] (số seed âm):

| so sánh | bi quan | lạc quan |
|---|---|---|
| cov1−cov30 sàn20, 40/ngày, N20 | −32.8 [−38.2, −27.3] (20/20) | −36.9 [−42.4, −30.8] (20/20) |
| … lợi nhuận 2024 | −7.2 [−16.0, +1.7] | +2.8 [−7.3, +13.0] |
| … lợi nhuận 2025 | −48.1 [−57.4, −39.3] | −55.8 [−66.3, −45.0] |
| … lợi nhuận 2026H1 | −15.4 [−22.7, −8.3] | −17.3 [−22.2, −12.8] |
| … DD tối đa | +37.3 [+32.9, +41.4] | +39.8 [+35.6, +43.9] |
| cov1−cov30 sàn20, 5/ngày, N10 | −21.4 [−30.9, −10.7] (9/10) | −6.6 [−20.1, +5.9] (5/10) |
| cov1−cov30 sàn0, 40/ngày | −42.4 [−47.8, −37.0] | −46.7 [−56.2, −36.6] |
| sàn0−sàn20 @cov1, 40/ngày | −11.2 [−16.1, −5.5] | −10.5 [−19.8, −1.8] |
| sàn0−sàn20 @cov30, 40/ngày | +0.7 [−8.3, +8.8] | −0.7 [−6.7, +3.9] |
| sàn0−sàn20 @cov1, 5/ngày | +4.8 [−0.4, +9.4] | +0.5 [−10.9, +14.1] |
| khoảng cách lạc quan−bi quan, 40/ngày, N20 | cov1 +9.0 [+3.3, +14.6] | cov30 +13.1 [+7.0, +19.3] |

Code gốc (warmup 24 nến), 20 seed: cov1−cov30 bi quan −35.0 [−40.8, −29.1], lạc quan −41.8 [−47.9, −35.4]; trung vị cov1 −24.9 / −18.9,
cov30 +11.9 / +25.0. Cùng seed, đã sửa − gốc: cov1 −0.7 / +1.7; cov30 −3.0 / −3.2 (đều không ý nghĩa).

## 3. Cơ chế (seed 0, bi quan, 40/ngày)

| | cov1 sàn20 | cov30 sàn20 |
|---|---|---|
| số phiên đã mở | 4.700 | 1.395 |
| rung đói riêng biệt | 1.878 | 47 |
| thoát do hết hạn (lỗ, $) | 323 (299, −$17,4k) | 42 (36, −$8,5k) |
| thoát hết hạn / số phiên | 6,9% | 3,0% (chạy riêng lẻ: 2,0%) |
| lãi TP / vốn triển khai | ~5,9% | ~6,7% |
| tiền mặt so với sàn 2025-03-01 | $1.038 vs $1.087 (50 phiên mở) | $5.813 vs $1.868 (10 phiên mở) |

111 lần thoát hết hạn của cov1 đã khớp đủ 10 rung; lỗ trung bình −10,2% trên $443 triển khai. Mở phiên dừng hẳn khoảng một tháng
(0 phiên mở trong 2025-01, -03, -06, -08, -10, -12), rồi khi hạn giải phóng tiền thì 300–400 phiên mở cùng lúc và lại kẹt.
cov30 giữ $4–7k tiền rảnh suốt 2025. Lãi mỗi lần TP tương đương nhau — chênh lệch nằm ở lỗ hết hạn, không ở lệnh thắng.

## 4. Kiểm chứng engine

- Đúng trên 1h: hạn, đồng hồ TP, days_to_tp, capital-days tính theo mili giây (`capital_portfolio_study.py:643`, `:809`, `:835`);
  deep-lock đếm rung; cổng dự trữ và sàn tính lại mỗi nến từ NAV nến trước (`:1054`); số phiên mở tính theo ngày lịch (`:997–1002`);
  chỉ số lấy mẫu một lần/ngày (`:1197–1206`, năm CAGR từ dòng ngày `:1264`); delist chỉ khi `ts > nến cuối`, thiếu một giờ thì chờ
  (`:1062–1066`), không coin nào thiếu quá 2% số giờ.
- Parity: 504/504 khớp tuyệt đối giữa `step_session` và `simulate_kss` trên nến 1h thật, cả hai cận.
- Lỗi đã sửa: warmup đếm nến → nay là số ngày từ ngày niêm yết (`listing_ts` từ bảng 1d). Test `TestWarmupIsListingAgeInDaysOnHourlyBars`.
- Nhỏ, chưa sửa: độ trễ nhả sàn khi sập = 1 nến (1 giờ trên 1h, vẫn nhân quả); `crash_release_active_days` đếm giờ trên 1h; breadth
  1h dùng đỉnh 24h trước; chạy 1h kết thúc 2026-07-31 00:00; `analyze.py` lấy phần tử giữa-dưới làm trung vị khi n chẵn.
- 1d: 30/30 khớp từng seed với `docs/cash-floor-release-2026-09-28/verify_p2_lag1.json`, 0 khác sau khi sửa. Cùng 599 coin trên 1h và 1d.
- Lỗi dữ liệu: ACEUSDT, ACHUSDT, AEVOUSDT chỉ có 1h tới 2024-03 (tải dừng) → bị coi là delist 2024-04-01; 3/~460 coin, nạp lại bằng
  `build_universe.py --run`.

## 5. Cảnh báo

Một lịch sử giá; seed chỉ xáo lại lựa chọn coin; hiệu ứng dồn vào đợt gấu altcoin 2025 và cú sập 10/10/2025. Vào lệnh ngẫu nhiên,
không cổng lọc. Sàn production neo vốn với vùng chết 10%; engine dùng NAV nến trước. Bootstrap n=10 hẹp hơn khoảng t ~10–20%.
cov30 không có lãi sau 10/2025 — khuyến nghị là tương đối.

## 6. Khuyến nghị

- **GO: đặt `kss:ladder_coverage_pct` về 30.** Giữ `cash_floor_pct` 20 (vô hại ở cov30, bảo vệ ở cov1).
- Không hạ sàn khi coverage đang là 1.
- Trước khi tăng vốn, đo lại cov30 trên top-100 với cổng vào lệnh.

## Tái lập

```
.venv/Scripts/python.exe -m pytest tests/app/test_capital_portfolio.py -c tests/app/pytest.ini -q
.venv/Scripts/python.exe scripts/capital_hourly_grid.py --intervals 1h --workers-1h 1 \
    --cells cov1_floor20,cov30_floor20 --max-new-per-day 40 --seeds 20 --tag fix20
.venv/Scripts/python.exe docs/capital-hourly-2026-09-29/verify_analyze.py   # đọc verify_1h.json + results_main.json
```

Dữ liệu: `verify_1h.json` (220 lượt; mode `fix` và `old`), `verify_trace.json`, `results_main.json` (code gốc).
