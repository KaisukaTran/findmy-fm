# Ride & Trail v2 — "chạm TP rồi trail, sàn khoá bằng giá TP": đo trước khi build

Đo ngày 2026-09-14 bằng `scripts/tp_then_trail_study.py` trên `simulate_kss(trail_after_tp_pct=…)`
(cùng cấu hình paper đang chạy: 30 rung, bước 4%, TP 5% + 0,5%/rung, không SL, timeout 60 ngày, wave đầu
$17, phí khứ hồi 0,30%). Cơ chế: khi high của nến chạm đích TP thì **không bán**, vũ trang một trailing
stop có **sàn = giá TP**; stop = max(sàn, đỉnh × (1 − trail)); thoát ở stop khi low chạm. Kết cục chỉ có
thể là giá TP hoặc cao hơn. Đã vũ trang thì không khớp thêm rung (stop nằm trên giá vốn, giá tới rung
nghĩa là đã thoát). Bản chạy đầu của agent còn cho rung khớp sau khi vũ trang — mua đáy bán đỉnh trong
cùng một nến — và thổi lợi ích lên gấp rưỡi; bảng dưới là sau khi sửa.

Số thô: `tp-then-trail-2026-09-14-1d.json`, `-1h.json`.

## Nến 1h 2024–26 (98 coin, 10.8k lượt) — tấm đáng tin hơn vì nến mịn

| trail | tiền thắng MTM opt/pess | $/lượt opt/pess | Δ so TP cố định (pess) | % lượt TP thoát trên sàn | vượt sàn TB / p90 | vốn-ngày/lượt |
|---|---|---|---|---|---|---|
| 0 (TP cố định) | 80,5 / 78,1 | +12,95 / +11,91 | — | — | — | 2.495 |
| **1%** | 89,2 / 83,3 | +29,55 / **+18,32** | **+6,41 (+54%)** | 38% | +2,7 / +5,9 điểm | 2.503 |
| 2% | 88,8 / 83,0 | +28,45 / +17,85 | +5,94 | 23% | +4,0 / +8,8 | 2.505 |
| 3% | 88,6 / 82,9 | +27,67 / +17,65 | +5,74 | 17% | +5,2 / +12,0 | 2.508 |
| 5% | 88,2 / 82,6 | +26,46 / +17,15 | +5,24 | 11% | +7,4 / +18,1 | 2.514 |
| 8% | 87,6 / 82,3 | +24,78 / +16,81 | +4,90 | 7% | +10,4 / +24,2 | 2.530 |

## Nến ngày 2021–26 (388 coin, 86k lượt) — nến thô, lợi ích bị phóng đại

| trail | tiền thắng MTM opt/pess | $/lượt opt/pess | Δ so TP cố định (pess) | % lượt TP thoát trên sàn | vượt sàn TB / p90 |
|---|---|---|---|---|---|
| 0 | 82,5 / 67,2 | +7,71 / +8,15 | — | — | — |
| 1% | 92,8 / 79,2 | +24,14 / +20,42 | +12,27 | 75% | +6,7 / +14,4 |
| 3% | 92,1 / 77,9 | +21,92 / +18,47 | +10,32 | 46% | +8,9 / +19,2 |
| 8% | 90,8 / 76,3 | +18,24 / +16,26 | +8,11 | 21% | +14,3 / +30,4 |

## Đọc kết quả

- **Có lợi ở mọi độ rộng trail, cả hai biên, cả hai tấm**, và không tốn thêm vốn-ngày (thời gian giữ
  thêm không đáng kể). Đúng như thiết kế: sàn khoá bằng TP nên không bao giờ tệ hơn TP cố định.
- Con số nên tin là **nến 1h, biên bi quan: +$6,4/lượt (+54%)**, tiền thắng MTM 78 → 83%. Nến ngày cho
  +150% vì một nến ngày gói cả cú vượt đỉnh lẫn cú lùi; đó là trần trên, không phải kỳ vọng.
- Trail hẹp (1%) ăn nhiều nhất vì hầu hết cú vượt TP là nhỏ và ngắn. Nhưng app chỉ kiểm giá mỗi 90 giây
  (guard) và stop trên sàn thật phải đặt/đổi bằng lệnh; trail 1% dễ bị nhiễu và trễ. **Khuyến nghị
  2–3%**: mất ~10% lợi ích so với 1% (+$5,7–5,9 thay vì +$6,4) đổi lấy độ bền với nhịp 90 giây.
- Hai biên lệch nhau trên 1h (opt +29,6 vs pess +18,3) vì thứ tự high/low trong nến quyết định stop có
  bị chạm ngay nến vũ trang hay không; live thật nằm giữa hai số.
- Chưa mô phỏng: trượt giá khi stop khớp (stop market), độ trễ 90 giây, khớp một phần, lệnh stop-limit bị
  gap qua. Cần kiểm chứng trên paper (mô hình chạm nến 1m) trước khi bật live.
