# Top-N liquidity filter cho thang DCA sâu — quét N ∈ {10, 20, 30, 50, 100, 200, all}

Câu hỏi của Kai: giới hạn coin được vào lệnh ở top-N theo thanh khoản (median quote volume
30 ngày trước đó, point-in-time), N nào cho kết quả tốt nhất cho một thang DCA sâu (60 rung)?

Công cụ: `scripts/ladder_grid_study.py`, không sửa code, không chạy live app. 14 lần chạy
(7 giá trị N × 2 panel `1d`/`1h`), mỗi lần dùng cùng 5 cấu hình `--only
"10:50:3650,20:0:14,30:0:30,60:0:30,60:0:3650"` để N nào cũng so trên đúng một bộ luật vào/ra.

Quy ước cột: `mtm` = tỷ lệ thắng tính bằng đô-la, coi thang còn mở ở cuối dữ liệu là thua (đây
là số chính, không dùng `share` thô). `opt/pess` = biên lạc quan/bi quan của cách chấm điểm
lệnh còn mở. Cột `mean_usd`/các số infinite dùng biên **pess** (bảo thủ hơn, giống cách chọn
`mean_usd (pess)` được yêu cầu).

## Bảng 1 — panel `1d` (2021–2026, kể cả gấu 2022), `--every 7`, `--min-years 2`

| N | coins (đã lấy mẫu / từng đủ điều kiện) | n (60:0:30) | infinite open%(pess) | infinite mae_min(pess) | infinite waves_p99(pess) | infinite days_to_TP_p99(pess) | 10:50:3650 mtm opt/pess | 20:0:14 mtm opt/pess | 30:0:30 mtm opt/pess | 60:0:30 mtm opt/pess | 60:0:3650 mtm opt/pess | $/trial(pess) 10:50:3650 / 20:0:14 / 30:0:30 / 60:0:30 / 60:0:3650 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 10  | 77 / 93   | 2,786  | 1.96% | -100.0% | 26 | 122 | 64.7/52.5 | 86.5/65.6 | 83.4/64.1 | 84.4/61.6 | 90.6/64.7 | 10.12 / 14.51 / 20.80 / 18.80 / 102.63 |
| 20  | 149 / 194 | 5,451  | 1.66% | -100.0% | 27 | 103 | 67.5/52.5 | 86.3/67.8 | 85.4/69.2 | 86.4/68.4 | 82.0/67.1 | 8.46 / 16.40 / 27.84 / 28.93 / 105.52 |
| 30  | 207 / 278 | 8,056  | 1.50% | -100.0% | 27 | 98  | 68.2/52.9 | 87.5/68.7 | 86.9/70.4 | 88.2/70.6 | 85.3/68.1 | 8.01 / 17.08 / 29.23 / 32.00 / 105.80 |
| 50  | 273 / 394 | 13,164 | 1.22% | -100.0% | 27 | 98  | 69.3/52.7 | 86.7/65.1 | 86.1/68.8 | 87.6/69.1 | 87.2/68.8 | 7.50 / 14.62 / 28.00 / 30.90 / 107.74 |
| 100 | 350 / 533 | 25,655 | 0.99% | -100.0% | 27 | 99  | 69.4/52.6 | 87.4/65.2 | 86.4/69.3 | 87.9/69.9 | 90.5/73.2 | 6.61 / 14.94 / 29.55 / 33.65 / 113.09 |
| 200 | 386 / 588 | 50,276 | 0.86% | -100.0% | 27 | 93  | 71.2/53.7 | 87.1/65.1 | 86.5/70.4 | 88.0/72.3 | 84.5/71.0 | 7.53 / 14.53 / 30.30 / 35.88 / 108.38 |
| all | 388       | 85,949 | 0.97% | -100.0% | 26 | 81  | 68.8/53.5 | 87.0/66.1 | 86.2/71.3 | 87.7/73.0 | 84.9/73.4 | 7.62 / 15.16 / 32.31 / 39.11 / 103.67 |

## Bảng 2 — panel `1h` (2024–2026), `--every 168`, `--min-years 1`

| N | coins (đã lấy mẫu / từng đủ điều kiện) | n (60:0:30) | infinite open%(pess) | infinite mae_min(pess) | infinite waves_p99(pess) | infinite days_to_TP_p99(pess) | 10:50:3650 mtm opt/pess | 20:0:14 mtm opt/pess | 30:0:30 mtm opt/pess | 60:0:30 mtm opt/pess | 60:0:3650 mtm opt/pess | $/trial(pess) 10:50:3650 / 20:0:14 / 30:0:30 / 60:0:30 / 60:0:3650 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 10  | 41 / 48  | 1,252  | 4.46% | -82.8%  | 22 | 80.4 | 50.4/50.7 | 82.0/81.8 | 80.0/79.8 | 81.1/81.0 | 79.1/79.2 | 15.96 / 24.96 / 36.18 / 39.75 / 86.12 |
| 20  | 70 / 91  | 2,468  | 3.59% | -82.8%  | 24 | 74.8 | 50.8/50.7 | 79.1/79.0 | 78.5/78.3 | 79.6/79.4 | 76.3/76.6 | 12.79 / 25.18 / 38.71 / 42.34 / 97.82 |
| 30  | 87 / 117 | 3,645  | 3.06% | -96.5%  | 29 | 69.8 | 50.2/50.6 | 76.8/76.2 | 79.8/78.8 | 82.1/81.0 | 70.5/70.0 | 9.57 / 25.24 / 43.69 / 52.60 / 106.17 |
| 50  | 94 / 137 | 6,069  | 2.51% | -97.6%  | 26 | 71.8 | 51.6/51.9 | 78.2/76.6 | 79.9/76.4 | 82.2/78.9 | 72.4/69.4 | 10.17 / 25.71 / 40.68 / 49.75 / 103.45 |
| 100 | 98 / 145 | 9,950  | 2.38% | -100.0% | 26 | 71.5 | 50.8/51.8 | 76.9/75.1 | 80.5/77.2 | 84.2/81.0 | 75.8/73.3 | 10.25 / 23.76 / 42.69 / 58.07 / 109.62 |
| 200 | 98 / 145 | 10,362 | 2.53% | -100.0% | 26 | 69.8 | 51.3/52.4 | 76.5/74.7 | 80.2/77.0 | 83.9/80.7 | 76.0/73.4 | 11.15 / 23.28 / 42.14 / 56.91 / 107.30 |
| all | 98       | 10,852 | 2.42% | -100.0% | 25 | 67.6 | 52.4/53.5 | 77.5/75.8 | 80.8/77.7 | 84.3/81.3 | 76.4/74.0 | 12.46 / 24.43 / 42.78 / 57.13 / 105.31 |

Ghi chú: universe `1h` bão hòa ở N≥100 (chỉ có 98 coin đủ 1 năm lịch sử trong panel này), nên
top100/top200/all trên bảng 2 gần như là cùng một tập coin.

## Đọc số liệu

- **1d**: MTM (pess) của `30:0:30` và `60:0:30` gần như phẳng từ N=30 trở lên (~69–73%) và
  đỉnh nằm ở **"all" (không lọc)** — 71.3%/73.0% — chỉ nhỉnh hơn N=30 hoặc N=200 vài điểm
  phần trăm. Không có N nào rõ ràng vượt trội; lọc thanh khoản không mua thêm gì đáng kể trên
  panel này.
- **1h**: `60:0:30` cũng gần như phẳng (80–81% pess) suốt từ N=10 đến "all" — top-10 đã đạt
  81.0%, ngang với "all" (81.3%). `30:0:30` thì đỉnh nhẹ ở N=10 (79.8% pess), giảm dần rồi
  hồi lại ở N cao.
- **Đuôi -100% (mất trắng)**: trên `1d` đuôi này **không biến mất ở bất kỳ N nào** (luôn
  -100.0% từ N=10 đến "all"). Trên `1h` thì có: N=10 và N=20 chỉ tệ đến -82.8%, nhưng đuôi
  quay lại -96.5%/-97.6%/-100.0% ngay từ N=30 trở lên. Muốn giữ N=10/20 để né đuôi đó trên
  `1h`, cái giá là mẫu nhỏ nhất (n=1,252–2,468 so với 10,852 ở "all") và tỷ lệ còn kẹt cuối kỳ
  cao nhất (4.46%/3.59% vs 2.42%).
- **Cấu hình paper hiện tại `10:50:3650`** không bao giờ chạm 80% ở bất kỳ N nào trên cả hai
  panel — dao động 52.5–53.7% (pess) trên `1d` và 50.6–53.5% (pess) trên `1h`, tệ hơn hẳn mọi
  cấu hình 60-rung.
- **Cảnh báo**: N càng nhỏ, số lệnh chấm điểm càng ít (1d: 2,786 ở N=10 vs 85,949 ở "all"; 1h:
  1,252 ở N=10 vs 10,852 ở "all") → khoảng tin cậy rộng hơn nhiều lần, các con số MTM ở N thấp
  kém tin cậy hơn con số ở N cao/"all".

## File kết quả

- JSON thô: `docs/ladder-topn-2026-09-13-{1d,1h}-top{10,20,30,50,100,200,all}.json`
- Log stdout: cùng tên với đuôi `.txt`
- Tóm tắt đã trích: `docs/ladder-topn-2026-09-13-summary.json`
