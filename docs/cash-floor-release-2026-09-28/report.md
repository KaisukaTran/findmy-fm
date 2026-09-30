# Có nên nhả sàn tiền mặt 20%? — kiểm chứng đối kháng (2026-09-28)

Bản nháp (Sonnet) → kiểm chứng đối kháng (Opus). `results.json` / `results_sens.json` là bản nháp TRƯỚC khi sửa lỗi
(nhìn trộm tín hiệu sập + cộng lặp tiền nhả sàn) — **không trích dẫn**. Số dưới đây lấy từ `verify_*.json`.

## Tóm tắt

- **Kết luận của bản nháp KHÔNG đứng vững.** Ô "thắng" `A_crash_K6_R50_W7` không tốt hơn cả hai mốc: so cặp cùng seed với
  floor20, CAGR +3,87 điểm, KTC95% [−0,29; +8,19] — không có ý nghĩa; so với floor0 CAGR còn thấp hơn (−0,46).
- **Ba lỗi đã sửa:** (1) tín hiệu sập nhìn trộm cùng nến (breadth của nến t dùng để nhả tiền cho rung khớp trên chính nến t);
  (2) `floor_release_usd` cộng lặp phần thâm hụt đã nằm dưới sàn → sổ $7k "nhả" $0,5–7,7 triệu; sau sửa: trung vị $62k luân
  chuyển trong 5,5 năm (~$175/lệnh); (3) `starved_usd` cộng mỗi lần thử lại hằng ngày (số rung đói riêng biệt: trung vị 1.491).
- **Tín hiệu bền vững duy nhất: chính cái sàn 20% làm mất ~6–10 điểm CAGR**, chỉ đổi lấy 1–3,5 điểm DD, và phần bảo vệ đó
  không có ý nghĩa ngoài mẫu. Nhả sàn khi có tín hiệu sập (K1, R100, W7, trễ 1 nến) lấy lại phần CAGR đó mà không tăng DD —
  đúng trong mẫu, trên seed mới và ở cả hai giai đoạn, với 40 phiên mở/ngày; KHÔNG đúng ở 5 phiên/ngày trên seed mới.
- **So với chỉ đặt sàn = 0 (đổi knob, không code):** nhả-khi-sập ≈ cùng CAGR, DD tốt hơn 2–4 điểm trong mẫu; phần DD đó không
  có ý nghĩa ngoài mẫu và ở 5 phiên/ngày.
- **B (duyệt qua Telegram): KHÔNG xây** — trễ tín hiệu 1 nến thì không khác 0 (−0,26 [−3,8; +3,5]), âm ở 5 phiên/ngày, ~500 yêu cầu/năm.
- **A: CHƯA đáng xây.** Muốn hành động thì hạ sàn bằng knob trên paper trước.
- **Vấn đề lớn hơn:** CAGR toàn kỳ ở bound bi quan đến từ năm 2021. Chạy từ 01/01/2024: floor20 trung vị **−25%/năm, DD 74%**;
  mọi chính sách đều lỗ. Bound lạc quan (CAGR 86%) không trả lời được vì sàn không bao giờ bị chạm. Nến ngày không phân xử
  được — cần đo lại trên nến giờ.

## 1. Thiết lập

$7k, cổng giữ chỗ coverage 1%, wave0 0,4% trần $40, 10 rung @7%, TP 5% +0,5%/rung, SL 0, hạn 60 ngày, deep-lock 4, dự phòng
24,8%, không quỹ ngoài. Nến 1d, 641 coin, 40 phiên mới/ngày trừ khi ghi khác. Chỉ bound bi quan (xem §4.4). Thống kê: chênh
cặp so với floor20 cùng seed, KTC95% bootstrap 10.000 lần trên 30 chênh lệch theo seed. Chạy lại lag 0 seed 0–29 khớp
`results.json` tuyệt đối (0 lệch trên 330 lượt).

## 2. Bảng chính (CAGR trung vị / DD tối đa trung vị, %; ô crash dùng trễ 1 nến trừ khi ghi lag0)

| chính sách | seed 0–29 | seed 30–59 | 2021-01..2023-12 | 2024-01..2026-07 | 5/ngày, seed 0–29 | 5/ngày, seed 30–59 |
|---|---|---|---|---|---|---|
| floor20 (hiện tại) | 25.0 / 56.0 | 21.5 / 56.8 | 51.4 / 56.0 | −25.3 / 74.1 | 12.6 / 44.2 | 10.2 / 48.1 |
| floor0 | 32.0 / 60.9 | 34.2 / 59.0 | 57.9 / 60.9 | −23.3 / 76.9 | 19.1 / 47.7 | 15.5 / 48.3 |
| A_crash_K1_R100_W7 | 33.8 / 56.2 | 31.7 / 56.8 | 62.4 / 56.2 | −18.5 / 69.7 | 20.6 / 46.6 | 14.7 / 46.9 |
| A_crash_K4_R100_W7 | 35.7 / 56.6 | 32.8 / 58.3 | 59.3 / 56.6 | −21.5 / 71.6 | 17.1 / 45.7 | 13.8 / 46.7 |
| A_crash_K4_R100_W3 | 34.3 / 57.7 | 32.4 / 56.1 | 58.7 / 57.7 | −21.7 / 72.2 | 14.2 / 46.8 | 15.3 / 47.9 |
| A_crash_K6_R50_W7 (ô bản nháp chọn) | 30.4 / 56.2 | 31.1 / 56.5 | 56.9 / 56.2 | −21.0 / 72.4 | 11.7 / 48.3 | 15.4 / 48.0 |
| A_crash_K6_R50_W7, lag0 (như bản nháp) | 33.4 / 55.8 | 28.6 / 57.2 | 58.5 / 55.8 | −24.3 / 75.6 | 16.8 / 46.4 | – |
| A_starved_K4_R100 | 34.8 / 59.2 | 35.7 / 58.5 | 60.8 / 59.2 | −29.4 / 79.7 | 18.1 / 47.1 | 18.4 / 46.9 |
| B_D2_K6_R50_crashonly | 26.2 / 55.6 | 27.8 / 57.4 | 54.2 / 55.3 | −21.0 / 70.8 | 9.4 / 50.2 | 12.7 / 47.2 |
| B_D2_K6_R50_crashonly, lag0 | 28.1 / 56.6 | 26.2 / 56.4 | 53.9 / 56.6 | −21.9 / 72.2 | 13.0 / 45.0 | – |
| B_D2_K1_R100_always | 29.7 / 60.6 | 30.0 / 60.3 | 55.5 / 60.6 | −24.9 / 75.5 | 17.2 / 45.9 | 12.0 / 50.1 |

## 3. Chênh cặp so với floor20 (trung bình [KTC95%]; CAGR + là tốt hơn / DD − là tốt hơn)

| chính sách | seed 0–29 | seed 30–59 | 2021–23 | 2024–26 | 5/ngày seed 0–29 | 5/ngày seed 30–59 |
|---|---|---|---|---|---|---|
| floor0 | +5.65 [+1.87,+9.41] / +3.46 [+1.72,+5.17] | +10.53 [+6.47,+14.59] / +1.01 [−1.31,+3.28] | +5.75 [+1.70,+9.75] / +3.75 [+2.21,+5.30] | +0.83 [−4.43,+6.48] / +1.95 [−2.22,+5.84] | +6.11 [+1.28,+10.94] / +0.44 [−4.53,+4.88] | +3.59 [−0.22,+7.35] / +1.97 [−1.97,+6.18] |
| A_crash_K1_R100_W7 | +9.22 [+5.50,+13.00] / −0.95 [−3.09,+1.05] | +10.41 [+7.60,+13.11] / −0.87 [−2.94,+1.18] | +9.41 [+5.48,+13.34] / −0.65 [−2.67,+1.27] | +8.76 [+3.55,+14.21] / −4.80 [−8.58,−0.99] | +5.62 [+1.62,+9.71] / +0.62 [−4.04,+4.90] | +2.30 [−2.14,+6.46] / +0.33 [−3.79,+4.55] |
| A_crash_K4_R100_W7 | +9.22 [+5.57,+12.89] / −0.26 [−2.16,+1.63] | +9.23 [+6.16,+12.21] / +0.33 [−1.69,+2.29] | +8.51 [+3.71,+13.28] / +0.04 [−1.80,+1.87] | +3.74 [−0.67,+8.33] / −2.59 [−6.01,+0.86] | +3.84 [−0.47,+8.09] / +0.72 [−3.34,+4.73] | +2.61 [−0.74,+6.02] / +0.45 [−2.67,+3.41] |
| A_crash_K4_R100_W3 | +7.82 [+4.28,+11.24] / +0.07 [−1.54,+1.55] | +9.45 [+6.52,+12.48] / −1.45 [−3.54,+0.63] | +6.73 [+2.70,+10.83] / +0.37 [−0.90,+1.65] | +4.12 [−0.70,+8.93] / −2.68 [−5.92,+0.49] | +2.74 [−0.93,+6.44] / +1.58 [−2.33,+5.43] | +3.27 [−0.43,+6.71] / −0.51 [−3.75,+2.57] |
| A_crash_K6_R50_W7 | +5.20 [+1.29,+9.30] / −0.71 [−3.00,+1.48] | +7.34 [+3.62,+10.92] / −1.33 [−3.26,+0.58] | +5.78 [+1.42,+10.27] / −0.60 [−2.54,+1.34] | +2.24 [−1.76,+6.13] / −0.65 [−3.27,+2.00] | +0.80 [−3.03,+5.00] / +2.72 [−2.16,+7.07] | +3.50 [+0.40,+6.53] / −0.03 [−3.76,+3.71] |
| A_crash_K6_R50_W7, lag0 | +3.87 [−0.29,+8.19] / −0.72 [−2.96,+1.41] | +5.82 [+2.09,+9.30] / +0.00 [−2.09,+2.23] | +5.88 [+1.24,+10.64] / −0.55 [−2.53,+1.39] | −0.45 [−4.06,+3.10] / −0.01 [−2.65,+2.51] | +3.25 [−0.86,+7.68] / +1.26 [−3.91,+6.11] | – |
| A_starved_K4_R100 | +6.91 [+0.97,+12.63] / +2.82 [+0.01,+5.71] | +8.74 [+4.77,+12.50] / +2.16 [+0.35,+3.92] | +9.97 [+4.42,+15.45] / +1.83 [−0.30,+4.00] | −2.36 [−8.01,+3.11] / +4.22 [+0.28,+8.18] | +5.79 [+1.78,+9.77] / +0.26 [−4.35,+4.63] | +5.90 [+2.62,+9.15] / −1.58 [−5.02,+1.75] |
| B_D2_K6_R50_crashonly | −0.26 [−3.83,+3.45] / −0.67 [−2.80,+1.35] | +2.97 [+0.20,+5.56] / −0.60 [−2.46,+1.20] | +2.68 [−1.14,+6.38] / −1.01 [−2.84,+0.75] | +1.58 [−2.24,+5.09] / −1.73 [−4.53,+1.19] | −2.77 [−6.06,+0.77] / +3.79 [+0.63,+7.19] | +1.36 [−1.84,+4.56] / −1.14 [−4.20,+1.85] |
| B_D2_K1_R100_always | +3.39 [−0.61,+7.39] / +3.35 [+0.93,+5.71] | +3.87 [−0.28,+7.87] / +2.72 [+0.86,+4.65] | +3.97 [−0.09,+8.05] / +3.45 [+1.26,+5.59] | +2.07 [−4.18,+8.54] / +0.22 [−4.21,+4.51] | +3.50 [−0.41,+7.49] / −0.67 [−4.96,+3.27] | +0.45 [−3.14,+4.00] / +1.58 [−2.33,+5.46] |

Nhả-khi-sập so với floor0 (cùng seed, trễ 1; ΔCAGR [KTC] / ΔDD [KTC]):

| thiết lập | A_crash_K1_R100_W7 | A_crash_K4_R100_W7 | A_crash_K6_R50_W7 |
|---|---|---|---|
| seed 0–29 | +3.57 [+0.08,+7.15] / −4.41 [−6.63,−2.21] | +3.57 [−0.31,+7.29] / −3.73 [−5.80,−1.63] | −0.46 [−3.85,+2.89] / −4.18 [−6.24,−2.09] |
| seed 30–59 | −0.11 [−3.35,+3.11] / −1.89 [−4.32,+0.46] | −1.30 [−4.79,+2.32] / −0.68 [−2.93,+1.63] | −3.19 [−7.67,+1.02] / −2.34 [−4.50,+0.23] |
| 2021–23 | +3.65 [−1.83,+9.42] / −4.40 [−6.61,−2.19] | +2.76 [−2.46,+8.01] / −3.71 [−5.79,−1.60] | +0.03 [−5.11,+5.01] / −4.34 [−6.43,−2.29] |
| 2024–26 | +7.93 [+1.39,+14.50] / −6.75 [−11.11,−2.44] | +2.91 [−2.56,+8.12] / −4.54 [−8.21,−0.52] | +1.41 [−4.82,+7.42] / −2.60 [−6.80,+1.79] |
| 5/ngày seed 0–29 | −0.50 [−4.99,+3.73] / +0.18 [−3.62,+4.31] | −2.28 [−6.52,+1.73] / +0.28 [−2.82,+3.46] | −5.32 [−9.93,−0.89] / +2.27 [−2.02,+6.68] |
| 5/ngày seed 30–59 | −1.29 [−6.20,+3.85] / −1.64 [−7.10,+3.73] | −0.98 [−4.30,+2.53] / −1.52 [−5.19,+1.97] | −0.10 [−3.76,+3.63] / −2.00 [−5.60,+1.39] |

## 4. Từng điểm kiểm

1. **Chọn ô may mắn — XÁC NHẬN.** Bản nháp xếp 44 ô theo CAGR trung vị; ô được chọn không có ý nghĩa trong mẫu và −0,45 ở
   2024–26. Ô có dấu nhất quán là A_crash R100 W7 (K1 hoặc K4); K1_R100_W7 thắng floor20 ở cả 4 tập 40/ngày và là ô duy nhất có
   ý nghĩa ở 2024–26. Lưu ý: K1/K4 cũng được chọn từ cùng lưới; seed 30–59 chỉ xáo lại coin trên CÙNG một lịch sử giá, không
   phải lịch sử độc lập — chia giai đoạn là bằng chứng gần độc lập nhất.
2. **Nhìn trộm tín hiệu sập — XÁC NHẬN, đã sửa** (`floor_release_crash_lag_bars`, mặc định 1). Trễ 1 nến KHÔNG làm A xấu đi
   (bằng hoặc tốt hơn — có thể vì nhả muộn một ngày dồn tiền vào rung sâu hơn, chưa kiểm); B crash_only mất lợi thế nhỏ.
   Cảnh báo live nhìn nến ngày đang chạy nên sự thật nằm giữa lag 0 và lag 1.
3. **Sổ sách — XÁC NHẬN lỗi.** `starved_usd` cộng mỗi lần thử lại; `floor_release_usd` cộng lặp thâm hụt (test: lệnh $53,76 ghi
   $73,76). Sau sửa (trung vị): K6_R50_W7 lag0 352 lệnh/$62k; K4_R100_W7 683/$114k; K1_R100_W7 1.111/$161k; B crash_only ~125/~$20k.
   Chạy có gắn kiểm tra (seed 3): 0 lần tiêu xuống dưới (1−R)×sàn, 0 lệnh dùng tiền sàn dưới rung K, 0 phiên mới mở khi tiền
   dưới sàn, lệnh thoát không bao giờ bị chặn (14.998 lệnh thoát, 240 lúc tiền dưới sàn). Sàn engine = pct × unit-NAV nến trước;
   production = pct × vốn neo chỉ dịch khi lệch 10% — khác nhỏ, chưa đo lại. A_starved_K1_R100 ≡ floor0 ở mọi seed: ở cấu hình này
   cổng dự phòng 24,8% chặn phiên mới trước, nên sàn chỉ còn là sàn cho RUNG.
4. **Bound lạc quan — THẬT, không phải lỗi.** Trên nến ngày, bound lạc quan xoay vòng tiền trong cùng ngày: 42% nến khớp rung cũng
   chốt TP cùng nến (bi quan 0,4%), tiền − sàn tối thiểu +$3.046, 0 rung đói ở mọi seed/ô → không trả lời được câu hỏi này.
5. **Độ nhạy — CÓ LÝ.** Ở 5/ngày floor20 có DD trung vị thấp nhất (44,2) nhưng KTC cặp đều chứa 0. CAGR toàn kỳ đến từ 2021
   (seed 0, floor20: 2021 +297%, 2022 −46%, 2023 +33%, 2024 +31%, 2025 −31%, 2026 −10%); từ 01/2024 mọi chính sách lỗ 18–29%/năm,
   DD 70–80% — câu hỏi nhả sàn là thứ yếu so với điều này. Tín hiệu sập đo trên 641 coin (live: top-100, chưa đo lại), bắn 29 lần/
   5,5 năm, W7 hoạt động ~14% số ngày. B_D2_K1_R100_always ~2.750 yêu cầu/1.260 lần duyệt mỗi lượt chạy; crash_watch đo cú sập
   diễn ra trong ~1 giờ nên duyệt sau ≥1 ngày là trễ về cấu trúc. File test có 41 test (không phải 43), nay 46.

## 5. Khuyến nghị

- **B: không xây.**
- **A: chưa đáng xây.** Gần hết lợi ích là "không giữ sàn 20%" — đổi knob (sàn 0 hoặc giữa 0–20) lấy được cùng CAGR, không code.
  Lợi thế DD thêm của A so với floor0 (2–4 điểm) có ý nghĩa trong mẫu và cả hai giai đoạn, nhưng không trên seed mới hay 5/ngày.
  Xây A còn biến `crash_watch` (thiết kế là không bao giờ đụng lệnh) thành đường đặt lệnh.
- Nếu xây: K1 (hoặc K4), R100, W7, trễ 1 ngày = "tắt sàn cho rung DCA trong 7 ngày kể từ ngày sau cảnh báo sập" — không phải K6_R50_W7.
- Trước khi xây bất cứ gì: đo lại trên top-100 và **nến giờ** (để không chỉ dựa vào bound bi quan), A/B sàn thấp hơn trên paper.

## 6. Thay đổi (chưa commit)

- `scripts/capital_portfolio_study.py`: `_record_release` = `min(spent, max(0, floor − post_cash))`; `floor_release_crash_lag_bars`
  (mặc định 1); `starved_rungs_distinct` / `starved_distinct_usd`; các option nhả sàn A/B (mặc định tắt, parity giữ nguyên).
- `scripts/cash_floor_release_grid.py`: stage `grid` / `sens` / `verify` (`--cells --seed-start --since --until --crash-lag --max-new --bounds --tag`).
- `tests/app/test_capital_portfolio.py`: 46 test (gồm parity), ruff sạch.
- Dữ liệu: `verify_{is,oos,p1,p2,n5}_lag{0,1}.json`, `verify_n5oos_lag1.json`, `run_verify.log`.

## Tái lập

```
.venv/Scripts/python.exe -m pytest tests/app/test_capital_portfolio.py -c tests/app/pytest.ini -q
.venv/Scripts/python.exe scripts/cash_floor_release_grid.py --stage verify --cells floor20 floor0 A_crash_K1_R100_W7 \
    --seed-start 30 --crash-lag 1 --tag oos_lag1 --out docs/cash-floor-release-2026-09-28
```
