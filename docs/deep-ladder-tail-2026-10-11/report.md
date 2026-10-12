# Kiểm soát đuôi thang sâu không hồi — 2026-10-11

**Kết luận:** chỉ **D3** (≥5 lần khớp VÀ phiên ≥30 ngày → bán market) cải thiện danh mục nhất quán, chủ yếu qua **giảm sụt tối đa**;
toàn bộ phần lợi CAGR nằm ở **2025** (gấu + sập 10/10/2025) → D3 là **bảo hiểm thị trường gấu**, không phải lợi thế đã chứng minh.
D1, D2, D5 null/chưa quyết; D4 tệ hơn. Không biến thể nào làm cấu hình paper (cov 1%) có lãi trên lịch sử này.

## Cấu hình (data/findmy.db runtime_config, giống docs/tp-anchor-2026-10-11)
10 sóng @7%, trọng số n+1, TP = avg×(1+5%+0,24% phí+0,5%/rung), chi phí khứ hồi 0,30%, SL 0, hạn 60 ngày, không trail; wave-0 0,4% equity
($28 ở $7k), equity_backup 10, deep-lock 4, sàn tiền mặt 0, trần 150 phiên, 40 phiên mới/ngày (dòng phụ: 5/ngày).
Dữ liệu: market.db 1h 2024-01 → 2026-07, 599 coin (gồm 131 coin chết); holdout market_holdout.db, vào lệnh 01/08 → 10/10/2026.
k = số lần khớp kể cả wave 0; "≥5 lần khớp" = chỉ số sóng ≥4.

| biến thể | luật |
|---|---|
| A0 | paper hiện tại |
| D1 | bỏ +0,5%/rung của TP |
| D2 | k≥5 → TP = avg×1,0124 (ròng +0,94%; assert K-2 không bao giờ kích) |
| D3 | k≥5 VÀ tuổi ≥30 ngày → bán ở giá đóng nến (cùng quy ước với hạn 60 ngày) |
| D4 | thang tối đa 7 sóng (rung sâu nhất −35%) |
| D5 | chỉ vào coin có ≥180 nến ngày lịch sử |

Kiểm tra hợp lệ: A0 tái lập đúng tp-anchor A (seed 0 cov1 bq CAGR −35,73, DD 82,82, 5.628 đóng); bước copy khi tắt hook = bản gốc trên 6.018 phiên;
D2/D3 = A0 trên mọi phiên <5 lần khớp (12.036 lượt chạy, 0 sai khác + assert trên 48.762 phiên); không TP nào ròng ≤ 0.

## Từng phiên (không giới hạn tiền; CI bootstrap cụm theo coin; bq/lq)

| | A0 | D1 | D2 | D3 | D4 | D5 |
|---|---|---|---|---|---|---|
| TP % | 97,5/97,7 | 98,4/98,5 | 98,8/98,9 | 95,3/95,7 | 96,5/96,9 | 97,7/97,9 |
| $/phiên | 4,04/4,91 | 3,13/3,78 | 2,02/2,50 | 2,96/3,97 | 1,77/2,62 | 4,15/4,99 |
| ngày giữ | 6,9/6,7 | 6,1/5,9 | 6,0/5,8 | 6,1/6,0 | 7,3/7,0 | 6,9/6,7 |
| $/1000 vốn-ngày | 2,44/3,24 | 2,54/3,43 | 2,00/2,72 | **2,87/4,11** | 1,10/1,77 | 2,53/3,31 |
| Δ$/1000 vốn-ngày (bq) | – | +0,10 [−0,07;+0,29] | −0,44 [−0,66;−0,21] | **+0,42 [+0,21;+0,63]** | −1,34 | +0,08 [−0,03;+0,21] |

- Đuôi có thật: phiên kết thúc ở 10 lần khớp = 2,8% lượt, lỗ −$101k (bq); phiên ≥5 lần khớp = 12% lượt nhưng 83% vốn-ngày.
- **D2 cắt đúng cú hồi đang trả tiền:** bỏ lỡ $214,8k ở thang sâu mà A0 sau đó TP bình thường, cứu được $116,0k → ròng −$98,8k.
- D3: bỏ lỡ $128,7k, cứu $75,8k (ròng −$52,9k) nhưng giải phóng 31% vốn-ngày của thang sâu → $/vốn-ngày tăng.
- D5: coin trẻ có 12,6% phiên sâu vs 12,0% còn lại → giả thuyết "đuôi nằm ở coin mới list" BỊ BÁC.

## Danh mục $7.000, 20 seed — ΔCAGR điểm % [CI 95%] / ΔDD điểm % so với A0 (ΔDD âm = tốt)

| ô | A0 CAGR/DD | D1 | D2 | D3 | D4 | D5 |
|---|---|---|---|---|---|---|
| cov1 bq | −39,9/86 | −1,4 [−5,1;2,5] / +0,6 | −1,0 [−5,5;3,3] / +0,2 | **+10,1 [5,6;14,0] / −6,4** | −1,7 / +1,3 | −1,0 / +0,5 |
| cov1 lq | −28,6/83 | +2,8 [−2,1;7,9] / −1,7 | +2,9 [−1,5;7,5] / −2,6 | **+14,1 [8,2;21,0] / −9,9** | −2,1 / +1,0 | +4,0 / −2,1 |
| cov30 bq | 8,8/44 | +11,0 [4,6;17,3] / −9,5 | +7,2 [−0,6;15,0] / −13,0 | **+9,3 [3,9;14,7] / −13,7** | −7,8 [−14,1;−1,8] / +5,1 | +3,8 / −3,3 |
| cov30 lq | 29,6/34 | +5,3 [−0,9;11,4] / −4,5 | −6,9 [−12,3;−1,8] / −8,8 | +6,9 [−0,1;13,8] / −6,8 | −21,6 [−27,7;−15,1] / +14,1 | −4,8 / +4,6 |

- D3 theo năm (ΔReturn, bq): cov1 +0,5 / **+16,2** / −1,4; cov30 +4,8 / **+20,2** / −2,4; cov30 lq −6,6 / +23,9 / −3,7.
- D3 giảm DD ở 18/20, 16/20, 18/20, 15/20 seed. CI 99% toàn họ vẫn loại 0 ở cov1 (cả hai cận) và cov30 bq.
- D3 KHÔNG tránh được lỗ (cov30: −$11,5k lỗ thoát sâu vs −$10,9k lỗ hết hạn 60d ở A0) — nó quay vòng vốn sớm ~30 ngày và giảm DD.
- 5 phiên/ngày (10 seed): D3 cov30 null về CAGR (−3,0/−1,4) nhưng ΔDD −7,7/−9,4; cov1 +5,8 [−7,1;17,1] / +18,8 [10,0;27,4].

## Holdout (01/08 → 10/10/2026, ΔTổng lợi nhuận điểm %)
D3 cov1 +1,1 [−0,6;2,8] bq / +2,7 [0,8;4,7] lq; cov30 +0,4 / −0,1. Chỉ 1,2–1,3% phiên đạt ≥5 lần khớp (vs 12% kỳ chính), ~11 phiên sâu
đóng/lượt, không có cú sập → MDE D3 2,4–2,7 điểm (cov1). Holdout không xác nhận cũng không bác bỏ biến thể nào.

## Phán quyết
| | |
|---|---|
| D1 bỏ bước TP | chưa xác lập (cov30 bq +11, còn lại CI chứa 0) |
| D2 hoà vốn từ 5 khớp | KHÔNG dùng — mất $99–118k cú hồi, cov30 hai cận trái dấu |
| **D3 timeout 30d cho thang ≥5 khớp** | ứng viên tốt nhất: DD giảm nhất quán, CAGR tốt hơn 3/4 ô; lợi dồn vào 2025 → bảo hiểm gấu |
| D4 7 sóng | tệ hơn ở cov30 — không phải cách kiểm soát đuôi |
| D5 tuổi coin ≥180d | null mọi nơi |

Thăm dò (ngoài họ, không hiệu chỉnh): D3 với đồng hồ 20 ngày, D1+D3 — không cái nào thắng D3 ở cả 4 ô. Không chỉnh sau khi nhìn.

## Giới hạn
Một lịch sử giá; CI đo nhiễu chọn coin, không đo nhiễu thị trường. Vào lệnh ngẫu nhiên không cổng scanner; trần 150 phiên cố định (chưa mô hình
trần động f82ba54). cov1 lỗ ở mọi biến thể trên lịch sử này (−40%/năm, DD 86%); D3 chỉ thu hẹp còn ~−29%. Không chạy nến 1m.
Báo cáo do Opus viết từ `summary_primary.txt`, `summary_holdout.txt`, `ps_*_tables.md`, `results_*_tables.md` (agent bị harness chặn ghi .md).
