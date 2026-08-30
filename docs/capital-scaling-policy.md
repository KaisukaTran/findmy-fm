# Luật điều chỉnh quy mô theo vốn — bản thi hành

> **Đây là luật, không phải ghi chép.** Bản văn xuôi này giải thích *vì sao*; phần thi hành nằm ở
> `app/capital.py`, và test ở `tests/app/test_capital_scaling.py` là hợp đồng. Khi hai bên mâu
> thuẫn, **code thắng** — sửa tài liệu, đừng sửa số liệu cho khớp văn bản.
>
> Mục đích: Kai nạp thêm vốn hằng tháng và cần quy mô tự đi theo một cách tất định, đủ rõ để một
> lệnh gọi Claude API hoặc xAI API sau này áp dụng được mà không phải suy luận lại.
>
> Nền tảng: [`capital-scaling-2026-08-23.md`](capital-scaling-2026-08-23.md) (khảo sát 18 nguồn,
> mô phỏng phản-thực trên 72/82 lệnh thật). Bản này bổ sung ràng buộc mà bản đó thiếu — **ngày dừng
> lỗ đồng loạt** — và biến kết luận thành số học chạy được.

---

## 0. Điều tuyệt đối không được làm

**Luật này chỉ chỉnh KÍCH CỠ. Không bao giờ chỉnh HÌNH DẠNG.**

| Được suy từ vốn | Không bao giờ suy từ vốn |
|---|---|
| số session đồng thời (`max_concurrent_sessions`) | take-profit (`scan_tp_pct`) |
| cỡ sóng đầu (`kss_first_wave_usd`) | stop-loss (`sl_pct`) |
| trần thanh khoản theo ADV | khoảng rung DCA (`scan_distance_pct`) |
| | số sóng (`scan_max_waves`) |
| | mọi ngưỡng lọc (`min_expectancy_pct`, `min_win_rate`, …) |

Lý do không phải sở thích. Kích cỡ là số học kiểm toán được, không khớp dữ liệu. Hình dạng khớp
theo lịch sử là overfitting: với ~5 năm dữ liệu, thử quá ~45 cấu hình gần như chắc chắn tạo ra
Sharpe 1 trong mẫu và 0 ngoài mẫu. Cả 5 bot production được khảo sát (Freqtrade, 3Commas,
Hummingbot, OctoBot, Jesse) đều suy kích cỡ từ số dư và **cố định hình dạng**.

`tests/app/test_capital_scaling.py` có một test quét mã nguồn `app/capital.py` và **fail nếu**
module này nhắc tới bất kỳ tham số hình dạng nào. Ràng buộc được thi hành, không phải lời hứa.

---

## 1. Ràng buộc quyết định: ngày dừng lỗ đồng loạt

Không phải ngân sách triển khai. Crypto dừng lỗ **cùng lúc**: đo trên chính universe của bot,
ngày **2026-06-05 có 13/16 mã dừng lỗ trong cùng một ngày**. Nên tình huống phải sống sót không
phải "một vị thế hỏng" mà là "mọi vị thế đang mở hỏng cùng lúc".

```
ladder_usd  = first_wave_usd × ladder_ratio
worst_day   = sessions × ladder_usd × stop_fraction
ĐIỀU KIỆN:    worst_day ≤ safety_margin × daily_loss_limit × equity
```

| ký hiệu | nghĩa | nguồn |
|---|---|---|
| `equity` | vốn thật, đọc từ số dư sàn | `risk.account_equity(db)` |
| `first_wave_usd` | USD của sóng 0 | `settings.kss_first_wave_usd` |
| `ladder_ratio` | tổng ladder ÷ sóng 0 | `kss.projected_ladder_cost ÷ projected_first_wave_cost` (toán KSS đã khoá) |
| `stop_fraction` | `(sl_pct + phí khứ hồi)/100` | `settings.sl_pct`, `costengine.round_trip_cost_pct()` |
| `daily_loss_limit` | `daily_loss_hard_pct/100` — mức app tự đóng băng | `settings.daily_loss_hard_pct` |
| `safety_margin` | phần hạn mức được phép tiêu, mặc định **0.8** | `capital.DEFAULT_SAFETY_MARGIN` |

Biên 0,8 giữ lại một phần năm hạn mức cho trượt giá vượt qua mức dừng lỗ và cho nến nhảy gap.
`audit_current` **cố ý** báo con số thô so với hạn mức thô, vì cầu dao không biết gì về biên này.

Ràng buộc thứ hai (ngân sách) hầu như không bao giờ chạm tới, nhưng vẫn kiểm:

```
sessions × ladder_usd ≤ (1 − backup_fraction) × equity
```

Đo ngày 2026-08-30 với cấu hình live: trần rủi ro cho 4,13 session còn trần ngân sách cho 6,43 —
**trần rủi ro luôn chạm trước**, và nó chính là con số 4 đang chạy.

> ### ⚠ Một `ladder_ratio` đồng nhất KHÔNG tồn tại trong hệ đang chạy
>
> `autotune_levels_enabled` cấp cho mỗi mã một khoảng rung riêng, và các session mở ở thời điểm
> khác nhau được tính theo cỡ sóng 0 khác nhau. Đo thật ngày 2026-08-30 với 6 session: năm
> ladder cũ quanh $140 (mở khi sóng 0 còn ~$15) và một ladder WLD $223,86 (mở ở $40, rung
> 5,15%). Tổng **$931,89 → ngày xấu nhất 3,86%**, trong khi giả định đồng nhất 5,841× ở cỡ $40
> hiện tại cho ra **5,81%** — tức **báo động giả**.
>
> **Hệ số đồng nhất dùng để CẤP CỠ cho session chưa tồn tại. Để KIỂM TRA session đang mở, phải
> đọc ladder thật:** `capital.audit_book(equity, ladder_usds=[...])`. Hai câu hỏi khác nhau,
> đừng dùng lẫn. Hệ quả thực tế: một sổ đang an toàn có thể **trôi dần** tới mức vượt hạn mức khi
> các session cũ đóng và được thay bằng session cỡ mới — phải kiểm lại theo sổ, không kiểm một
> lần rồi thôi.

---

## 2. Luật nạp vốn hằng tháng

Giữ nguyên `first_wave_usd`, giải theo `sessions`. Do vế phải tuyến tính theo `equity`, luật rút
gọn thành **một con số duy nhất**:

```
equity_per_extra_session = ladder_usd × stop_fraction ÷ (safety_margin × daily_loss_limit)
```

Với cấu hình live 2026-08-30 (`first_wave_usd=40`, `ladder_ratio=5.841`, `stop_fraction=0.083`,
`daily_loss_limit=0.05`, `safety_margin=0.8`):

> ### Cứ thêm **$484,80** vốn thì được thêm **một** session. Không đổi gì khác.

| vốn | session | sóng 0 | vốn cam kết | ngày xấu nhất | ràng buộc |
|---:|---:|---:|---:|---:|---|
| $1.000 | 2 | $40 | $467 | 3,88% | ngày dừng lỗ đồng loạt |
| $1.500 | 3 | $40 | $701 | 3,88% | ngày dừng lỗ đồng loạt |
| **$2.000** | **4** | **$40** | **$935** | **3,88%** | ngày dừng lỗ đồng loạt |
| $2.500 | 5 | $40 | $1.168 | 3,88% | ngày dừng lỗ đồng loạt |
| $3.000 | 6 | $40 | $1.402 | 3,88% | ngày dừng lỗ đồng loạt |
| $5.000 | 10 | $40 | $2.336 | 3,88% | ngày dừng lỗ đồng loạt |
| $10.000 | 20 | $40 | $4.673 | 3,88% | ngày dừng lỗ đồng loạt |
| $50.000 | 100 | $40 | $23.364 | 3,88% | **universe (100 mã)** |

**Tính chất quan trọng nhất của bảng này: cột "ngày xấu nhất" là hằng số 3,88% ở mọi mức vốn.**
Quy mô lớn lên, rủi ro tính theo phần trăm đứng yên. Đó là điều làm luật này an toàn khi nạp thêm
tiền — và cũng là thứ để kiểm tra nếu ai đó sửa luật: nếu cột đó bắt đầu trôi, luật đã hỏng.

Ở khoảng **$50.000** ràng buộc đổi sang **số mã trong universe** (1 session/mã). Từ đó trở đi câu
trả lời là **sóng to hơn**, không phải nhiều session hơn.

---

## 3. Cần giữ nguyên số session? Đổi cỡ sóng thay vì đổi số lượng

Cùng một phương trình, giải theo `first_wave_usd`:

```
first_wave_usd = safety_margin × daily_loss_limit × equity ÷ (sessions × ladder_ratio × stop_fraction)
```

| vốn | muốn 6 session | sóng 0 cần đặt | ngày xấu nhất |
|---:|---:|---:|---:|
| $2.000 | 6 | **$27,50** | 4,00% ✅ |
| $3.000 | 6 | $41,25 | 4,00% ✅ |
| $5.000 | 6 | $68,76 | 4,00% ✅ |

So sánh: 6 session ở **sóng $40** trên vốn $2.000 cho ngày xấu nhất **5,81%**, tức **vượt** hạn mức
5% và tự kích cầu dao. Cùng 6 session ở **sóng $27,50** cho **4,00%**, nằm trong hạn mức. Cùng mức
phân tán, khác nhau ở chỗ có sập cầu dao hay không.

`recommend_sessions` và `recommend_first_wave` giải cùng một phương trình theo hai chiều và **phải
cho kết quả nhất quán** — có test khoá điều đó.

---

## 4. Sàn và trần

| chặn | giá trị | hệ quả |
|---|---|---|
| sóng 0 phải ≥ `min_notional` | $10 (`scan_min_notional`), sàn Binance $5 | Dưới ngưỡng, wave 0 bị `-1013 NOTIONAL` và session giữ chỗ mà không bao giờ khớp. Trả về 0 session. |
| vốn quá nhỏ | < ~$485 → 0 session | Dưới ~$870 thì "tính theo %" không còn là tính theo %: min-notional chi phối. Nên chạy chế độ cỡ cố định tối thiểu. |
| universe | 1 session/mã | Ở 100 mã, trần là 100 session (~$48.500 vốn). |
| thanh khoản | 0,5% ADV mỗi mã | Universe hiện có thanh khoản trung vị $2,81 triệu/24h → lợi nhuận **giảm một nửa ở $81k–$101k**. Trần năng lực thực của chiến lược ~$100k, giỏi lắm ~$250k. **Chưa thi hành trong code** — phải thêm trước khi vượt $10k. |

---

## 5. Quy trình áp dụng (dành cho người, Claude API hoặc xAI API)

Thực hiện **đúng thứ tự này**, không bỏ bước:

1. Đọc `equity` từ **số dư sàn thật** (`risk.account_equity`), không dùng hằng số trong `.env`.
2. Đọc `ladder_ratio` từ toán KSS đã khoá cho `distance_pct`/`max_waves` **đang chạy** — không
   hardcode 5.841, nó đổi theo hình dạng.
3. Gọi `capital.recommend_sessions(...)`. Ghi lại cả `sessions` **và** `binding`.
4. Gọi `capital.audit_current(...)` với cấu hình **đang chạy**. Nếu `within_limit is False`,
   nêu rõ mức vượt — đừng lặng lẽ làm tròn.
5. **Không tự áp dụng.** Trả về khuyến nghị kèm ràng buộc đã chạm, để người quyết. Module này
   không ghi vào settings, và không được phép ghi.
6. Nếu `binding` là:
   - `correlated_stop_day` → cần thêm vốn, hoặc giảm cỡ sóng.
   - `deployable_budget` → giảm `equity_backup_pct` **chỉ khi** người chủ động quyết.
   - `universe_size` → nới universe, hoặc chuyển sang tăng cỡ sóng.
   - `first_wave_below_min_notional` → sóng 0 quá nhỏ để giao dịch; tăng nó trước mọi thứ khác.

**Cấm tuyệt đối với mọi tác nhân tự động:** không đổi `tp_pct`, `sl_pct`, `distance_pct`,
`max_waves` hay bất kỳ ngưỡng lọc nào dựa trên luật này. Đó là hình dạng, không phải kích cỡ.

---

## 6. Ghi chép thay đổi

| ngày | thay đổi | lý do |
|---|---|---|
| 2026-08-30 | Lập luật. Ràng buộc quyết định = ngày dừng lỗ đồng loạt (13/16 mã cùng ngày, 2026-06-05), không phải ngân sách. `equity_per_extra_session = $484,80` ở cấu hình live. | Kai nạp vốn hằng tháng, cần quy mô đi theo tất định và máy đọc được. |
| 2026-08-30 | Universe mở từ 20 → **100** mã (`scan_max_symbols=100`, `min_quote_volume=$1M`). | Trần universe lùi từ ~$9.700 lên ~$48.500 vốn. |
| 2026-08-30 | `max_concurrent_sessions` đặt **6** trên vốn $2.002 theo yêu cầu thử nghiệm của Kai. | Thí nghiệm có chủ ý trên testnet, đã cảnh báo trước. Lưới an toàn đã diễn tập: khi đóng băng, lệnh mua bị chặn, lệnh bán vẫn qua. |
| 2026-08-30 | **Tự đính chính:** cảnh báo "6 session = 5,81%, vượt hạn mức" là **sai với sổ thật**. Đo theo ladder thật: **3,86%, nằm trong hạn mức**. | Công thức giả định mọi session ở cỡ sóng $40 hiện tại; năm session cũ mở khi sóng 0 còn ~$15 nên ladder chỉ ~$140. Thêm `capital.audit_book()` để kiểm theo sổ thật. **Rủi ro sẽ trôi lên ~5,81% khi sổ quay vòng hết sang cỡ mới** — phải kiểm lại định kỳ, không kiểm một lần. |
