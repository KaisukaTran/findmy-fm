# TP tính từ giá rung 1 thay vì giá trung bình — 2026-10-11

**Kết luận: KHÔNG đổi.** Neo TP vào giá rung 1 làm GIẢM lợi nhuận ở cả hai cận nến, ở cả mức từng phiên lẫn danh mục.
Cơ chế: mỗi lần TP lãi gấp ~1,9 lần nhưng vốn bị giam gấp ~2 lần lâu hơn → số vòng quay vốn giảm một nửa, số phiên hết hạn tăng 2–7 lần.

## Cấu hình (đọc từ `runtime_config` data/findmy.db + app/config.py)
Rung 7% (entry×0,93^k), 10 sóng, trọng số n+1 (`pyramid.py:233` = `backtest.py:145`); TP = 5% + 0,24% phí + 0,5%/rung đã khớp;
chi phí khứ hồi 0,30%; SL 0; hạn 60 ngày; không trail; wave-0 = 0,4% equity ($28 ở $7k). Danh mục: coverage 1% (paper) và 30%,
equity_backup 10, deep-lock 4, sàn tiền mặt 0, 150 phiên, 40 (và 5) phiên mới/ngày.
Dữ liệu: `data/research/market.db` nến 1h 2024-01-01 → 2026-07-31, 599 coin (gồm 131 coin chết); nến 1d 2021→2026 là phụ.

Biến thể: **A** = avg × (1+X) (hiện tại) · **B** = giá rung 1 × (1+X) (giả thuyết) · **C** = max(A, giá rung 1).

## Kiểm tra hợp lệ
A tái lập báo cáo 29/09 (cov1, sàn 0, 10 seed): −34,78/84,0 vs −34,8/84,0 (bi quan). 1 rung: A ≡ B (assert 27k phiên/cận).
Hàm bước copy + neo avg khớp bản gốc trên 1.774 phiên (0 sai lệch).

## Từng phiên (không giới hạn vốn; 48.762 phiên, 536 coin, 1 lượt/coin/168h; CI bootstrap cụm theo coin)

| | A | B | C |
|---|---|---|---|
| TP % (bq/lq) | 97,5/97,7 | 84,0/84,0 | 90,3/90,4 |
| lãi ròng TB $/phiên | 4,04/4,91 | 2,21/2,37 | 2,46/2,98 |
| trung vị $/phiên | 1,38 | 1,38 | 1,38 |
| ngày giữ TB | 6,9/6,7 | 15,0/15,0 | 10,9/10,8 |
| % hết hạn 60d | 2,4/2,2 | 15,5/15,5 | 9,4/9,3 |
| $ / $1000 vốn-ngày | 2,44/3,24 | 0,37/0,40 | 0,59/0,73 |

B−A $/phiên −1,84 [−3,10; −0,57] bq / −2,54 [−3,84; −1,27] lq; $/vốn-ngày −2,07 / −2,84 (CI không chứa 0).
56% phiên chỉ 1 rung → giống hệt nhau. Từ 2 rung trở lên A thắng $/vốn-ngày ở mọi nhóm (2 rung: B +$1,8/phiên nhưng giữ 17 ngày thay vì 5).

## Danh mục $7.000 (20 seed; trung vị CAGR % / max DD %)

| cấu hình | A | B | C | B−A [CI 95%] |
|---|---|---|---|---|
| cov30, 40/ngày, bq | +8,8 / 44 | −18,1 / 70 | −15,4 / 67 | −25,0 [−32,3; −17,9] |
| cov30, 40/ngày, lq | +29,6 / 34 | −17,1 / 71 | −17,0 / 66 | −45,5 [−50,6; −40,3] |
| cov1 (paper), bq | −39,9 / 86 | −43,0 / 88 | −43,6 / 89 | −4,4 [−7,9; −0,9] |
| cov1 (paper), lq | −28,6 / 83 | −42,0 / 88 | −44,1 / 89 | −14,0 [−17,2; −10,7] |

5 phiên/ngày (10 seed): B−A −31 (bq) / −44 (lq) ở cov30, ≈ −25 ở cov1; mọi CI loại 0.
Cơ chế (cov30 bq): phiên đóng 1.762 → 918; TP 1.713 → 794; hết hạn 52 → 122; giữ 6,6 → 12,7 ngày; lãi/TP $8,15 → $15,31.

## Giới hạn
- Một lịch sử giá duy nhất; seed chỉ đổi coin được chọn. Chênh lệch dồn vào 2025 (gấu + sập 10/10/2025); 2024 không có ý nghĩa ở cận bi quan.
  Nến 1d 2021/2023: B lãi/phiên nhiều hơn nhưng vẫn thua $/vốn-ngày → trong thị trường bò dài B có thể không thua, nhưng chưa bao giờ thắng về vòng quay vốn.
- Vào lệnh ngẫu nhiên, không cổng scanner; trần 150 phiên cố định (chưa mô hình trần động `f82ba54`).
- C không "mềm" hơn A: từ 3 rung, TP của C nằm ở giá rung 1, cao hơn hẳn TP theo avg.

Tệp: `tp_anchor_engine.py`, `per_session.py`, `portfolio.py`, `analyze_*.py`, `per_session_tables.md`, `portfolio_tables.md`, `results_portfolio.json`.
