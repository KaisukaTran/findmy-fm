# Thang 10 rung + SL 50%: "khó thua một lần lớn so với nhiều lần thắng nhỏ"?

Đo ngày 2026-09-12 bằng `scripts/ladder_depth_study.py` (mô phỏng sản xuất `app.backtest.simulate_kss`,
đúng code cổng win-rate của scanner đang gọi), trên bộ dữ liệu nghiên cứu không sống sót
`data/research/market.db`. Tính bằng **đô la** ở wave đầu **$75** (lựa chọn của Kai), phí khứ hồi
0,30%. Hai bảng thô: `ladder-depth-2026-09-12-1h.txt` và `-1d.txt` (kèm JSON).

Hai tấm dữ liệu, vì không tấm nào đủ một mình:

| tấm | phủ | coin | vào lệnh | ghi chú |
|---|---|---|---|---|
| nến **1h** | 2024-01 → 2026-08 | 63 (đủ 2 năm) | mỗi tuần, 8.300 lượt/cấu hình | độ phân giải tốt, **thiếu năm gấu 2022** |
| nến **1d** | 2021-01 → 2026-08 | 200 mẫu | mỗi 7 ngày, 43.000 lượt/cấu hình | có 2022, nhưng SL và TP hay rơi cùng một nến nên hai biên lệch nhau nhiều |

Cấu hình so sánh (mọi cấu hình đều tắt cổng chọn coin: vào lệnh mù theo lịch, nên đây là "cái thang"
chứ không phải "cái thang + scanner"):

| | rung | bước | TP | SL | chân trời | bậc TP/rung |
|---|---|---|---|---|---|---|
| A hiện tại | 3 | 2% | 3% | 8% | 7 ngày | 0 |
| B sâu, TP phẳng | 10 | 2% | 5% | 50% | 365 ngày | 0 |
| **C đề xuất của Kai** | 10 | 2% | 5% | 50% | 365 ngày | **+0,5%** |
| D = C nhưng còn timeout | 10 | 2% | 5% | 50% | 7 ngày | +0,5% |
| E = C nhưng rung thưa | 10 | **4%** | 5% | 50% | 365 ngày | +0,5% |

"Tắt timeout" được mô phỏng bằng chân trời 365 ngày; phiên còn mở khi hết dữ liệu **không** tính là
thắng hay thua mà báo riêng kèm P&L đang gánh.

## Kết quả chính (nến 1h, hai biên gần như trùng nhau nên chỉ ghi biên bi quan)

| | TP | SL | mean $/lượt | thắng TB | thua TB | thua lớn nhất = N lần thắng TB | Σthắng / Σthua | còn mở khi hết dữ liệu | %/$-ngày |
|---|---|---|---|---|---|---|---|---|---|
| A hiện tại | 78,6% | 16,8% | **−1,69** | +5,96 | −31,9 | −36 = 6 | 39,6k / −53,7k | 22 | −0,39 |
| B sâu phẳng | 97,9% | 2,1% | +5,86 | +45,7 | −1.840 | −1.840 = **40** | 364k / −317k | 205 (gánh −98k) | +0,040 |
| **C Kai** | **96,2%** | **3,9%** | **+9,86** | **+84** | **−1.840** | **−1.840 = 22** | **650k / −570k** | **297 (gánh −149k, 181 phiên đã đầy 10 rung)** | **+0,038** |
| D còn timeout 7d | 70,7% | 0,4% | −4,36 | +42 | −205 | −1.840 = 44 | 284k / −320k | 49 | −0,12 |
| E rung 4% | 98,5% | 1,5% | **+16,6** | +41 | −1.632 | −1.632 = 40 | 329k / −194k | 225 (gánh −65k) | **+0,150** |

Theo năm, cấu hình C (1h): 2024 **+36,8 $/lượt**, 2025 **−23,1 $/lượt**, 2026 +23,5 $/lượt.

## Đọc kết quả

**Về tần suất, luận điểm đúng.** Với 10 rung và SL 50%, chỉ **3,9%** lượt vào lệnh chạm SL; 96% chốt
lời. Trên nến ngày 2021–2026 tỉ lệ SL là 2,8–6,0% tuỳ năm, cao nhất ở 2022 và 2025.

**Về tiền, luận điểm không đứng vững.** Một lần chạm SL mất **−$1.840** (một nửa thang $3.680 đã lấp
đầy), bằng **22 lần thắng trung bình**. Tổng thắng $650k so với tổng thua $570k: **cái thua hiếm ăn
gần hết cái thắng thường**, biên còn lại 12%. Năm 2025 âm. Trên nến ngày, biên bi quan của C chỉ còn
**+$3,8/lượt** và hai năm 2022, 2025 âm sâu (−20 và −35 $/lượt).

**Cái thua lớn còn chưa hiện ra hết.** 297 phiên (3,6%) vẫn mở khi hết dữ liệu, gánh trung bình
**−13,8%**, trong đó **181 phiên đã lấp đầy 10 rung** và không có gì để bình quân thêm. Nếu số này
chạm SL sau đó, riêng chúng mất tới ~$333k, xoá sạch net +$79k của cả tấm. Không biết chúng sẽ hồi
hay không; chỉ biết đó là chỗ cái thua kế tiếp đang nằm.

**TP bậc thang làm gì?** So B với C: mean $/lượt tăng $5,9 → $9,9 và thua lớn nhất "chỉ còn" bằng 22
lần thắng thay vì 40, vì mỗi lần thắng to hơn. Nhưng nó cũng đẩy tỉ lệ SL từ 2,1% lên 3,9% và số phiên
mắc kẹt từ 205 lên 297: đích xa hơn thì thang sâu khó thoát hơn. Lợi ròng dương nhưng mua bằng đuôi
rủi ro dày hơn.

**Tắt timeout là đúng nếu đã chọn SL 50%.** D (giữ timeout 7 ngày) âm ở cả hai tấm: timeout cắt thang
sâu ở −2,5% trung bình trên 29% số lượt, đúng lúc thang đang làm việc của nó.

**Rung thưa hơn tốt hơn ở mọi cột.** E (bước 4% thay vì 2%) có SL 1,5%, mean **$16,6/lượt**, thua lớn
nhất nhỏ hơn, ít phiên kẹt hơn, và **hiệu suất trên đô-la-ngày gấp 4 lần** C. Autotune live đang đặt
bước 1,4–6% theo ATR từng coin, nên E gần với thực tế live hơn là C.

**Hiệu suất vốn là con số cần nhìn thẳng.** C giữ trung bình 26.000 đô-la-ngày mỗi lượt và sinh
0,038%/đô-la-ngày. Với ngân sách $146k dùng hết, đó là khoảng **$55/ngày ≈ 1,1%/tháng**, trước khi trừ
phần đuôi chưa hiện. Cấu hình A hiện tại thậm chí **âm** khi vào lệnh mù (16,8% chạm SL 8%): sổ live
dương hai tuần qua là nhờ gì đó ngoài cái thang, hoặc nhờ may.

## Cảnh báo về phép đo

- Vào lệnh mù theo lịch, không có cổng chọn coin, không có Grok. Đo cái thang, không đo hệ thống.
- Mọi rung được giả định khớp đúng giá limit; live hiện tại rung TB chỉ 1,17/3 (`ab-trail-off`).
- Nến 1h chỉ có từ 2024; 2022 chỉ thấy qua nến ngày, nơi hai biên lệch nhau và biên bi quan đáng
  tin hơn.
- Danh sách coin là những coin còn dữ liệu trên Binance tới 2026-08: bộ dữ liệu được dựng để không
  sống sót, nhưng coin bị huỷ niêm yết giữa chừng vẫn có thể thiếu.
- Chưa mô phỏng Ride & Trail, phí maker, và trần ngân sách `ladder_budget_exceeded`.

## Kết luận một dòng

Thang 10 rung / SL 50% **thua hiếm nhưng thua bằng 22–40 lần thắng**, tổng thua ăn 85–90% tổng thắng,
âm ở năm xấu, và một đuôi 3,6% phiên đang kẹt sâu chưa tính; nếu vẫn làm, **bước rung 4% (E) tốt hơn
2% ở mọi thước đo**, và **phải tắt timeout**.
