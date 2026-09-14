# Bỏ SL, vốn vô hạn, rung không giới hạn: thang thật sự đi tới đâu, và SL / vốn / rung / timeout nào giữ 80% tiền ở phía thắng?

Đo ngày 2026-09-13 bằng `scripts/ladder_grid_study.py` (mô phỏng sản xuất `simulate_kss`, vào lệnh mù theo
lịch, bước rung **4%**, TP **5% + 0,5%/rung**, wave đầu **$75**, phí khứ hồi 0,30%). Hai tấm dữ liệu:
nến **1h** 2024→2026 (63 coin, hai biên trong-nến gần trùng nhau) và nến **ngày** 2021→2026 (200 coin, có
năm gấu 2022, hai biên lệch nhau nhiều). Số thô: `ladder-grid-2026-09-13.json` (lưới 280 cấu hình, nến
ngày) và `ladder-grid-2026-09-13-1h.json` (12 ứng viên, nến 1h).

**Định nghĩa "tổng giá trị tiền thắng 80%"**: Σ đô-la thắng ÷ (Σ đô-la thắng + Σ đô-la thua + **lỗ đang gánh
của các phiên còn mở khi hết dữ liệu**). Không tính phần cuối thì cấu hình "không SL, không timeout" luôn ra
100%, vì lỗ của nó không bao giờ hiện thành số, chỉ nằm im trong các phiên chưa đóng.

## 1. Trường hợp giả định: không SL, không giới hạn rung (trần 60 = giá còn 9%), không timeout

| | nến 1h 2024–26 | nến ngày 2021–26 (bi quan) |
|---|---|---|
| lượt chạm TP | 100% số lượt đã đóng | 100% số lượt đã đóng |
| **còn mở khi hết dữ liệu** | **2,6%**, gánh −8,4%, giam **$826k** | **1,0%**, gánh −17,5%, giam **$3,1 triệu** |
| rung khớp p50 / p90 / p99 / max | 2 / 8 / **22** / **60** | 2 / 7 / 26 / 60 |
| ngày tới TP p50 / p90 / p99 / max | 2,2 / 16 / **70** / 299 | 2 / 15 / 85 / **1.115** |
| $ nạp mỗi phiên p50 / p90 / p99 / max | 219 / 2.239 / **10.977** / **33.113** | 219 / 1.788 / 13.791 / 33.113 |
| MAE (rơi sâu nhất) p50 / p99 / tệ nhất | −3,8% / −0,0% / **−100%** | −5,2% / −0,0% / **−100%** |
| tiền thắng theo giá thị trường | **77%** | **58%** |

Đọc: "vốn vô hạn" nghĩa là chấp nhận 1–2,6% phiên có thể không bao giờ về (có coin về 0), mỗi phiên như
vậy giam tới $33k cho một wave đầu $75, và 1% phiên chờ hơn 70 ngày. Toàn bộ lỗ của chiến lược nằm ở cái
đuôi này; nó không hiện ra trong tỉ lệ thắng, chỉ hiện ra trong vốn bị giam.

## 2. Lưới SL × rung × timeout, nến ngày 2021–26 (280 cấu hình)

Không cấu hình nào đạt 80% ở cả hai biên. Tốt nhất mỗi mức rung (biên lạc quan / bi quan):

| rung | SL | timeout | thang/phiên | tiền thắng | $/lượt | thua lớn nhất |
|---|---|---|---|---|---|---|
| 5 | 0 | ∞ | $1.010 | 71 / 51% | +19,8 | 0 (lỗ nằm trong phiên mở) |
| 10 | 0 | ∞ | $3.245 | 77 / 58% | +39,8 | 0 (như trên) |
| 15 | 0 | 14 ngày | $6.219 | 86 / 64% | +11,9 | −4.787 |
| 20 | 0 | 14 ngày | $9.581 | 87 / 66% | +15,2 | −7.072 |
| 30 | 0 | 30 ngày | $16.571 | 86 / 71% | +32,3 | −8.112 |
| 60 | 0 | 30 ngày | $33.113 | 87 / **74%** | +40,6 | −8.442 |

Tác dụng riêng từng knob ở 60 rung (biên bi quan): **thêm SL luôn làm giảm tiền thắng** (SL 20% → 44%,
SL 50% → 64%, SL 70% → 74%, không SL → 100% danh nghĩa / 58% theo giá thị trường), vì SL chốt lỗ đúng ở
đáy các cú rơi mà thang vốn sẽ bình quân được. **Timeout ngắn lại làm tăng** tiền thắng theo giá thị
trường: 7 ngày 69,5%, 30 ngày 77%, 365 ngày 80%, ∞ 100% danh nghĩa. Timeout cắt lỗ nhỏ (trung bình −2,5%)
thay vì giam vốn.

## 3. Kiểm lại 12 ứng viên trên nến 1h 2024–26 (hai biên trùng nhau)

| rung | SL | timeout | thang/phiên | tiền thắng | $/lượt | thua lớn nhất | %/$-ngày |
|---|---|---|---|---|---|---|---|
| **10 (paper hiện tại)** | **50** | **∞** | $3.245 | **55 / 56%** | +15,6 | −1.632 | 0,15 |
| 10 | 0 | 14 ngày | $3.245 | 65 / 64% | +10,5 | −1.352 | 0,34 |
| 15 | 0 | 14 ngày | $6.219 | 77 / 75% | +20,6 | −2.085 | 0,65 |
| 20 | 0 | 14 ngày | $9.581 | **80 / 79%** | +26,2 | −1.517 | **0,84** |
| 20 | 70 | 30 ngày | $9.581 | 65 / 66% | +23,4 | −6.735 | 0,36 |
| 30 | 0 | 30 ngày | $16.571 | **81 / 80%** | +41,5 | −3.736 | 0,63 |
| 60 | 0 | 30 ngày | $33.113 | **84 / 82%** | +51,9 | −3.736 | 0,79 |
| 60 | 0 | ∞ | $33.113 | 77 / 77% | +93,0 | 0 (214 phiên mở, −$826k) | 0,62 |

## 4. Trả lời câu hỏi

Để tổng tiền thắng đạt 80% (theo giá thị trường, kể cả phiên chưa đóng), trên dữ liệu 2024–26:

- **SL: 0 — bỏ hẳn.** Mọi mức SL đã thử (20/35/50/70%) đều kéo tiền thắng xuống, vì nó bán đúng chỗ thang
  sắp bình quân được. Bộ đang chạy trên paper (10 rung, SL 50%) chỉ đạt 55%.
- **Rung: ≥ 30 rung ở bước 4%** (chịu rơi tới −70%); 20 rung (−56%) chỉ chạm 79–80%, tức ở ngưỡng.
  60 rung (−91%) cho 82–84%.
- **Timeout: 30 ngày kể từ lúc vào lệnh**, chốt thị trường khi hết hạn. Đây mới là cái cắt lỗ, không phải
  SL: nó thay một cú thua −50% hiếm bằng nhiều cú thua −2,5% thường.
- **Vốn:** thang 30 rung ở wave đầu $75 = **$16.571/phiên** nếu lấp đầy; 60 rung = $33.113. Thực tế
  p90 phiên chỉ nạp $2,2k và p99 $11k, nhưng vốn phải có sẵn cho tình huống tệ nhất vì đó chính là lúc
  thang cần tiền. Với 40 suất: **$663k (30 rung) hoặc $1,32 triệu (60 rung)** dự phòng đầy thang; với
  $150k ngân sách hiện tại chỉ đủ 9 suất 30 rung, hoặc giữ 40 suất với wave đầu **$17**.
- **Cảnh báo bắt buộc:** trên nến ngày có năm 2022, cùng bộ 60 rung / 30 ngày chỉ đạt 74% ở biên bi quan.
  80% là con số của một thị trường 2024–26; qua một năm gấu thật, chưa cấu hình nào giữ được 80%.

Cảnh báo phép đo như `ladder-depth-2026-09-12.md`: vào lệnh mù, mọi rung khớp đúng limit, chưa mô phỏng
Ride&Trail, cổng ngân sách và khớp một phần.

## 5. Chỉ top 100 coin (câu hỏi tiếp của Kai): đuôi −100% KHÔNG biến mất

Top 100 xếp **tại thời điểm vào lệnh** theo trung vị khối lượng quote 30 ngày trước đó (`--top 100`,
không nhìn tương lai). Nến ngày: 533 coin từng lọt top 100, 350 coin trong mẫu; nến 1h: 145 coin từng
lọt, 98 trong mẫu.

| | toàn vũ trụ 1h | **top-100 1h** | toàn vũ trụ ngày (bi quan) | **top-100 ngày (bi quan)** |
|---|---|---|---|---|
| vô hạn: phiên còn mở | 2,6%, −$826k | **2,4%, −$1,1M** | 1,0%, −$3,1M | 1,0%, −$1,8M |
| vô hạn: MAE tệ nhất | −100% | **−100%** | −100% | −100% |
| vô hạn: rung p99 | 22 | **26–29** | 26 | 27 |
| 10 rung / SL 50 / ∞ (paper) | 55–56% | **51–52%** | 53% | 53% |
| 20 rung / 0 / 14 ngày | 79–80% | 75–77% | 66% | 65% |
| 30 rung / 0 / 30 ngày | 80–81% | 77–80% | 71% | 69% |
| 60 rung / 0 / 30 ngày | 82–84% | **81–84%** | 74% | 70% |
| 30 rung / SL 50 / 30 ngày | — | **50–52%** | — | 60% |

Vì sao không giúp: khối lượng đạt đỉnh đúng lúc coin ở đỉnh, nên "top 100 theo thanh khoản" chọn đúng
những coin sắp sập. Từ một ngày nằm trong top 100, **385/531 coin về sau rơi hơn 90%**, 472 rơi hơn 70%.
Các cú −100% đích danh trong top 100: LUNA (04/2022), FTT (09/2021), GMT, GALA, GLMR, FLOW, C98, SLP,
BNX, DYM, NFP (2024)... Phần lớn là 2021 → gấu 2022, và huỷ niêm yết.

Kết luận không đổi: SL không cứu được (SL 50 ở 30 rung rơi xuống 50–52% tiền thắng), timeout 30 ngày là
thứ cắt lỗ đúng, và cần ≥30–60 rung để đạt 80% trong 2024–26; qua 2022 không bộ nào giữ 80% dù chỉ chọn
top 100. Lọc thanh khoản là chuyện khác với lọc "coin không chết"; chuyện đó cần một cổng khác (tuổi
coin, tỉ lệ rơi từ đỉnh, hoặc chỉ top 20) và phải đo riêng.
