# Nghiên cứu: thang 30 rung mất bao lâu để chạm TP, theo độ sâu

Cấu hình: cách đều 4%, tối đa 30 rung, TP 5% + 0,5%/rung khớp, KHÔNG stop-loss, KHÔNG timeout (deadline 3650 ngày = tắt), wave0 $17. Mỗi coin đủ lịch sử được vào lệnh định kỳ (7 nến ngày / 168 nến giờ), hai biên intrabar (OPTIMISTIC/PESSIMISTIC) đều chạy.

Đây là kiểm tra chéo độc lập cho câu hỏi hẹp: trong số các thang RỒI CŨNG chạm TP, bao nhiêu % cần hơn 30/45/60/90/180 ngày — tức một timeout 30 ngày sẽ CẮT bao nhiêu thang lẽ ra có lãi, và cắt ở độ sâu nào.

## Panel 1d

388 coin đủ điều kiện (>= 2 năm lịch sử), lấy mẫu 388 (seed 7), vào lệnh mỗi 7 nến, cost round-trip 0.300%. Runtime 2.1s.

### OPTIMISTIC

- Tổng số lượt vào lệnh: 86,404; chạm TP: 85,769 (99.3%); tổng đô-la TP: +976,674$
- Vẫn còn MỞ khi hết dữ liệu: 635 (0.73% tổng số lượt), MAE trung bình -16.1%, P&L chưa thực hiện trung bình -12.4% — những thang này KHÔNG timeout nào cứu được, vì tới cuối dữ liệu vẫn chưa hồi.

**Số ngày tới TP (chỉ các lượt CÓ chạm TP), toàn bộ:**
- n=85,769  p50=2  p75=4  p90=9  p95=16  p99=51  max=979

**Theo độ sâu (số rung đã khớp khi chạm TP):**

| rung | n | p50 | p75 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|
| 1 | 34,113 | 1 | 2 | 4 | 6 | 12 | 152 |
| 2-3 | 34,488 | 2 | 4 | 7 | 11 | 20 | 272 |
| 4-6 | 11,516 | 3 | 9 | 16 | 23 | 44 | 310 |
| 7-10 | 3,437 | 9 | 19 | 33 | 45 | 84 | 189 |
| 11-15 | 1,401 | 21 | 39 | 61 | 88 | 170 | 250 |
| 16-30 | 814 | 36 | 81 | 176 | 255 | 703 | 979 |

**Timeout ở mốc T ngày sẽ cắt bao nhiêu thang lẽ ra có lãi (days > T):**

| T (ngày) | số thang bị cắt | % số lượt TP | % tổng đô-la TP | rung TB lúc bị cắt |
|---|---|---|---|---|
| 14 | 4,959 | 5.78% | 41.49% | 8.46 |
| 30 | 1,825 | 2.13% | 27.25% | 11.76 |
| 45 | 1,012 | 1.18% | 20.40% | 13.75 |
| 60 | 633 | 0.74% | 15.49% | 15.12 |
| 90 | 340 | 0.40% | 10.51% | 17.05 |
| 120 | 186 | 0.22% | 7.46% | 19.85 |
| 180 | 112 | 0.13% | 4.79% | 20.36 |
| 365 | 24 | 0.03% | 1.77% | 30 |

**Đường chờ: % TRÊN TỔNG SỐ LƯỢT (kể cả chưa/không TP) đã chạm TP trong vòng T ngày:**

| T (ngày) | % tổng số lượt đã chạm TP |
|---|---|
| 14 | 93.53% |
| 30 | 97.15% |
| 45 | 98.09% |
| 60 | 98.53% |
| 90 | 98.87% |
| 120 | 99.05% |
| 180 | 99.14% |
| 365 | 99.24% |

### PESSIMISTIC

- Tổng số lượt vào lệnh: 86,404; chạm TP: 85,497 (99.0%); tổng đô-la TP: +1,628,926$
- Vẫn còn MỞ khi hết dữ liệu: 907 (1.05% tổng số lượt), MAE trung bình -29.5%, P&L chưa thực hiện trung bình -23.4% — những thang này KHÔNG timeout nào cứu được, vì tới cuối dữ liệu vẫn chưa hồi.

**Số ngày tới TP (chỉ các lượt CÓ chạm TP), toàn bộ:**
- n=85,497  p50=2  p75=6  p90=14  p95=25  p99=78  max=1072

**Theo độ sâu (số rung đã khớp khi chạm TP):**

| rung | n | p50 | p75 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|
| 1 | 41,866 | 1 | 2 | 4 | 5 | 11 | 152 |
| 2-3 | 23,440 | 3 | 6 | 9 | 13 | 24 | 272 |
| 4-6 | 10,521 | 7 | 11 | 19 | 26 | 50 | 310 |
| 7-10 | 5,005 | 13 | 22 | 35 | 48 | 80 | 189 |
| 11-15 | 2,734 | 21 | 38 | 59 | 77 | 134 | 250 |
| 16-30 | 1,931 | 45 | 90 | 180 | 244 | 688 | 1072 |

**Timeout ở mốc T ngày sẽ cắt bao nhiêu thang lẽ ra có lãi (days > T):**

| T (ngày) | số thang bị cắt | % số lượt TP | % tổng đô-la TP | rung TB lúc bị cắt |
|---|---|---|---|---|
| 14 | 8,483 | 9.92% | 61.68% | 10.45 |
| 30 | 3,313 | 3.87% | 40.94% | 14.16 |
| 45 | 2,007 | 2.35% | 32.01% | 16.33 |
| 60 | 1,300 | 1.52% | 25.22% | 18.1 |
| 90 | 668 | 0.78% | 16.81% | 20.93 |
| 120 | 402 | 0.47% | 12.09% | 23.42 |
| 180 | 230 | 0.27% | 7.39% | 24.14 |
| 365 | 56 | 0.07% | 2.48% | 30 |

**Đường chờ: % TRÊN TỔNG SỐ LƯỢT (kể cả chưa/không TP) đã chạm TP trong vòng T ngày:**

| T (ngày) | % tổng số lượt đã chạm TP |
|---|---|
| 14 | 89.13% |
| 30 | 95.12% |
| 45 | 96.63% |
| 60 | 97.45% |
| 90 | 98.18% |
| 120 | 98.49% |
| 180 | 98.68% |
| 365 | 98.89% |

## Panel 1h

98 coin đủ điều kiện (>= 1 năm lịch sử), lấy mẫu 98 (seed 7), vào lệnh mỗi 168 nến, cost round-trip 0.300%. Runtime 3.4s.

### OPTIMISTIC

- Tổng số lượt vào lệnh: 10,997; chạm TP: 10,728 (97.5%); tổng đô-la TP: +209,508$
- Vẫn còn MỞ khi hết dữ liệu: 269 (2.45% tổng số lượt), MAE trung bình -15.7%, P&L chưa thực hiện trung bình -11.6% — những thang này KHÔNG timeout nào cứu được, vì tới cuối dữ liệu vẫn chưa hồi.

**Số ngày tới TP (chỉ các lượt CÓ chạm TP), toàn bộ:**
- n=10,728  p50=2.04  p75=6.46  p90=14.58  p95=24.75  p99=64.42  max=238.12

**Theo độ sâu (số rung đã khớp khi chạm TP):**

| rung | n | p50 | p75 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|
| 1 | 4,435 | 0.79 | 1.79 | 3.62 | 7.25 | 22.29 | 94.88 |
| 2-3 | 3,460 | 2.42 | 4.88 | 8.75 | 12.67 | 27.04 | 110.67 |
| 4-6 | 1,560 | 6.67 | 10.67 | 17.5 | 23.67 | 39.62 | 116.96 |
| 7-10 | 701 | 11.17 | 18.71 | 29.88 | 42.5 | 67.71 | 187.67 |
| 11-15 | 340 | 25 | 36.71 | 53.67 | 69.29 | 98.33 | 124.08 |
| 16-30 | 232 | 27.25 | 71.46 | 111.46 | 172.83 | 228.54 | 238.12 |

**Timeout ở mốc T ngày sẽ cắt bao nhiêu thang lẽ ra có lãi (days > T):**

| T (ngày) | số thang bị cắt | % số lượt TP | % tổng đô-la TP | rung TB lúc bị cắt |
|---|---|---|---|---|
| 14 | 1,130 | 10.53% | 49.25% | 9.08 |
| 30 | 386 | 3.60% | 27.97% | 12.13 |
| 45 | 225 | 2.10% | 21.21% | 14 |
| 60 | 125 | 1.17% | 14.44% | 15.62 |
| 90 | 53 | 0.49% | 8.89% | 19.49 |
| 120 | 21 | 0.20% | 4.91% | 24.05 |
| 180 | 8 | 0.07% | 2.05% | 24.88 |
| 365 | 0 | 0.00% | 0.00% | 0 |

**Đường chờ: % TRÊN TỔNG SỐ LƯỢT (kể cả chưa/không TP) đã chạm TP trong vòng T ngày:**

| T (ngày) | % tổng số lượt đã chạm TP |
|---|---|
| 14 | 87.28% |
| 30 | 94.04% |
| 45 | 95.51% |
| 60 | 96.42% |
| 90 | 97.07% |
| 120 | 97.36% |
| 180 | 97.48% |
| 365 | 97.55% |

### PESSIMISTIC

- Tổng số lượt vào lệnh: 10,997; chạm TP: 10,726 (97.5%); tổng đô-la TP: +204,939$
- Vẫn còn MỞ khi hết dữ liệu: 271 (2.46% tổng số lượt), MAE trung bình -16.6%, P&L chưa thực hiện trung bình -12.0% — những thang này KHÔNG timeout nào cứu được, vì tới cuối dữ liệu vẫn chưa hồi.

**Số ngày tới TP (chỉ các lượt CÓ chạm TP), toàn bộ:**
- n=10,726  p50=2.08  p75=6.62  p90=14.92  p95=25.46  p99=65.5  max=238.12

**Theo độ sâu (số rung đã khớp khi chạm TP):**

| rung | n | p50 | p75 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|
| 1 | 4,447 | 0.79 | 1.79 | 3.62 | 7.21 | 22.29 | 94.88 |
| 2-3 | 3,435 | 2.46 | 4.92 | 8.83 | 12.79 | 27.04 | 110.67 |
| 4-6 | 1,542 | 6.79 | 10.75 | 17.62 | 23.71 | 40.21 | 116.96 |
| 7-10 | 727 | 11.21 | 18.79 | 29.88 | 41.79 | 67.71 | 187.67 |
| 11-15 | 347 | 25.17 | 38.75 | 53.67 | 68.25 | 98.33 | 124.08 |
| 16-30 | 228 | 31.29 | 78.58 | 113.83 | 175.33 | 228.54 | 238.12 |

**Timeout ở mốc T ngày sẽ cắt bao nhiêu thang lẽ ra có lãi (days > T):**

| T (ngày) | số thang bị cắt | % số lượt TP | % tổng đô-la TP | rung TB lúc bị cắt |
|---|---|---|---|---|
| 14 | 1,156 | 10.78% | 50.85% | 9.09 |
| 30 | 401 | 3.74% | 30.34% | 12.29 |
| 45 | 235 | 2.19% | 23.02% | 14.17 |
| 60 | 129 | 1.20% | 15.85% | 15.95 |
| 90 | 55 | 0.51% | 9.62% | 19.73 |
| 120 | 23 | 0.21% | 5.56% | 24.22 |
| 180 | 9 | 0.08% | 2.44% | 25.44 |
| 365 | 0 | 0.00% | 0.00% | 0 |

**Đường chờ: % TRÊN TỔNG SỐ LƯỢT (kể cả chưa/không TP) đã chạm TP trong vòng T ngày:**

| T (ngày) | % tổng số lượt đã chạm TP |
|---|---|
| 14 | 87.02% |
| 30 | 93.89% |
| 45 | 95.40% |
| 60 | 96.36% |
| 90 | 97.04% |
| 120 | 97.33% |
| 180 | 97.45% |
| 365 | 97.54% |

## Đọc nhanh

- Timeout 30 ngày cắt ~2-4% số lượt TP (1d: 2,1/3,9% opt/pess; 1h: 3,6/3,7%), nhưng vì mỗi thang bị cắt là thang đã đi SÂU và giữ vốn LÂU, nó cắt tới ~27-41% TỔNG ĐÔ-LA lẽ ra thắng — chênh lệch số lệnh vs số đô-la là điểm chính của cả nghiên cứu này.
- Số lệnh bị cắt ở mốc 30 ngày tập trung ở rung sâu: rung trung bình lúc bị cắt là ~12-17 rung (trong 30), tức đây đúng là nhóm "đã DCA nhiều lần rồi mới hồi", không phải nhiễu ở rung 1-2.
- Nới sang 60 ngày giảm đáng kể: số thang bị cắt còn ~0,7-1,5% lượt TP nhưng vẫn ~15-25% tổng đô-la TP. Nới tiếp sang 90 ngày giảm thêm nhưng đô-la bị cắt vẫn còn 2 chữ số phần trăm (9-17%) — 90 ngày vẫn cắt một phần lãi đáng kể, không phải mốc an toàn.
- Đường chờ cho thấy phần lớn giá trị đến sớm: ~87-97% tổng số lượt (kể cả không TP) đã chạm TP trong 14 ngày, ~94-98% trong 30 ngày — timeout ngắn ảnh hưởng số LƯỢT rất ít, chỉ ảnh hưởng nặng phần ĐÔ-LA vì các lượt còn lại là các lượt lớn, sâu, chậm.
- Nhóm không cứu được (`data_end`, chưa chạm TP khi hết dữ liệu) chiếm 0,7-2,5% tổng số lượt, MAE trung bình -16% đến -30%, P&L chưa thực hiện trung bình -12% đến -23% — timeout không giúp gì nhóm này vì chúng đơn giản là chưa quay đầu.

## Lưu ý bắt buộc

Một thang bị timeout cắt ở ngày T KHÔNG biến mất — nó bị bán ở giá thị trường ngày đó. Script này không đo được giá đó: các lượt "TP" ở đây chạy KHÔNG timeout, nên một lượt mất 61 ngày để chạm TP chưa từng bị ép bán ở ngày 30 hay ngày 60 để biết lúc đó nó đáng bao nhiêu (lãi, hòa, hay đang lỗ giữa chừng theo MAE). "% thang bị cắt" ở đây chỉ nói: bao nhiêu thang RỒI CŨNG có lãi nếu để yên, nhưng có lãi SAU mốc T ngày — không nói cắt ở mốc T thì thực lỗ hay thực lãi bao nhiêu.
