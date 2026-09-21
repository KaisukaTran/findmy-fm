# Luật điều chỉnh quy mô theo vốn — bản thi hành

> **Đây là luật, không phải ghi chép.** Văn xuôi giải thích *vì sao*; phần thi hành ở
> `app/capital.py`, test ở `tests/app/test_capital_scaling.py` là hợp đồng. Khi mâu thuẫn,
> **code thắng** — sửa tài liệu, đừng sửa số cho khớp văn bản.
>
> Nền tảng: [`capital-scaling-2026-08-23.md`](capital-scaling-2026-08-23.md). **Lưu ý: file đó
> nằm trên nhánh `kss-capital-auto-sizing`, không có trên nhánh này** — nếu bạn không mở được
> nó, đó là lý do.

---

## ⚠ 0. Hai điều phải đọc trước khi dùng luật này

### 0.1 — Vốn KHÔNG đọc từ sàn. Nạp tiền vào Binance thì luật không thấy.

`risk.account_equity` → `portfolio.equity` → `risk.capital_anchor` → **`settings.account_equity`,
một hằng số trong `.env`** (`ACCOUNT_EQUITY=200000` tính tới 2026-09-21). Trên paper
(`app/risk.py:95-96`) nhánh này trả về hằng số đó **vô điều kiện**; `fetch_balance` chỉ chạy khi
live **và** `use_exchange_balance=true` (`app/risk.py:105-124`).

**Hệ quả:** nạp $500 vào sàn thì `equity` không đổi, mãi mãi. **Mỗi lần nạp vốn phải TỰ TAY sửa
`ACCOUNT_EQUITY` rồi restart.** Không có bước đó thì mọi thứ dưới đây là số học suông.

Ngược lại — và đây là phần hay bị quên — `equity()` = `mốc vốn + lãi đã chốt + lãi/lỗ chưa chốt`
(`app/portfolio.py:191-207`), nên **lợi nhuận gộp thì luật thấy ngay, không cần làm gì cả.** Chỉ
tiền NẠP MỚI là vô hình.

### 0.2 — Số suất phiên ĐÃ tự đi theo vốn; các núm ĐÔ-LA thì chưa.

*(Mục này viết lại 2026-09-21. Bản cũ kết luận "thêm vốn không mở thêm được gì" dựa trên
`max_session_deploy_usd = 240` và một cổng ngân sách đã bị thay. Cả hai đều không còn đúng.)*

Cổng mở phiên hiện tính theo **phần trăm vốn sống**: `equity_backup_pct` (24,8%) và
`ladder_coverage_pct` (30%) đều nhân với `risk.account_equity(db)` (`app/scanner.py:1276-1283`).
→ **thêm vốn thì số suất phiên tự tăng, không phải chỉnh gì.**

Cái chưa đi theo vốn là các núm ghi bằng **đô-la tuyệt đối**:

| núm | giá trị đang chạy | hỏng thế nào khi đổi vốn |
|---|---|---|
| `kss_first_wave_usd` | $28 | cỡ vị thế đứng yên → ở $5k thì quá lớn, ở $1M thì vô nghĩa |
| `cash_floor_usd` | $40.000 | **ở vốn dưới $40k, chặn MỌI lệnh mua** (`app/orders.py:145`) |
| `live_max_order_notional` | $500 | chỉ có trong `.env`, không có trong `runtime_config` |
| `autoapprove_max_notional` | $5.000 | |
| `max_session_deploy_usd` | 0 (tắt) | |

**Cách sửa (2026-09-21, `app/capital_scale.py`):** mỗi núm trên có một núm **%** song sinh, giải
ra đô-la **lúc đọc** chứ không ghi đè giá trị. Mặc định % được chọn sao cho ở vốn $200.000 mọi
giá trị bằng đúng con số hôm nay ($28 / $40.000 / $500 / $5.000) — bật công tắc
`capital_scale_enabled` ở mức vốn hiện tại **không đổi một con số nào**. Có test khoá tính chất đó.
Vùng chết `capital_scale_deadband_pct` (10%) ngăn giá thị trường làm cỡ sóng rung theo từng tick.

---

## 1. Điều tuyệt đối không được làm

**Luật này chỉ chỉnh KÍCH CỠ. Không bao giờ chỉnh HÌNH DẠNG.**

| Được suy từ vốn | Không bao giờ suy từ vốn |
|---|---|
| số session (`max_concurrent_sessions`) | take-profit (`scan_tp_pct`) |
| cỡ sóng đầu (`kss_first_wave_usd`) | stop-loss (`sl_pct`) |
| trần thanh khoản theo ADV | khoảng rung (`scan_distance_pct`), số sóng (`scan_max_waves`) |
| | mọi ngưỡng lọc, `deadline_days` |

Kích cỡ là số học kiểm toán được. Hình dạng khớp theo lịch sử là overfitting: với ~5 năm dữ liệu,
thử quá ~45 cấu hình gần như chắc chắn tạo Sharpe 1 trong mẫu và 0 ngoài mẫu. Cả 5 bot production
được khảo sát đều suy kích cỡ từ số dư và **cố định hình dạng**.

Test kiểm điều này trên **bề mặt API công khai** (tên hàm + tên tham số), không phải bằng cách
quét chuỗi trong mã nguồn — bản đầu dùng cách quét chuỗi và bị phá được ba kiểu: thiếu `sl_pct`
và `max_waves` trong danh sách cấm, chuỗi bị tách đôi, và chuyển logic sang file khác.

**Thêm một điều cấm mà bản đầu bỏ sót:** `sl_pct` của mọi session trong sổ đều là `0.0`, tức nó
được giải ra từ **giá trị toàn cục lúc chạy**. Đổi `settings.sl_pct` khi đang có session mở sẽ
**dịch mức dừng lỗ của toàn bộ sổ ngay lập tức**, và `stop_fraction` trong mọi phép tính dưới đây
sai theo. Không đổi `sl_pct` khi còn session mở.

### 1.1 — Sửa đổi hẹp 2026-09-21: `scan_max_waves` được phép bị GIỚI HẠN TRÊN theo khả năng chi trả

Kai duyệt 2026-09-21. Đây là **ngoại lệ duy nhất** với luật hình dạng, và nó hẹp có chủ đích.

**Được phép:** rút `scan_max_waves` xuống mức dài nhất mà tài khoản **nuôi nổi
`min_fundable_ladders` thang ĐẦY cùng lúc**. **Không bao giờ** được kéo dài, và **không bao giờ**
được chọn bằng cách dò backtest xem số rung nào cho lợi nhuận cao nhất.

**Vì sao đây không phải overfitting:** nó là số học khả năng chi trả, không phải khớp lịch sử.
Thang 30 rung ở wave $28 tốn **$6.186**; tài khoản $5.000 **không thể** trả. Đo được (20 hạt,
cổng reserve, biên bi quan, 427 coin thanh khoản, 2023-08→2026-07):

| vốn $5.000 | trung vị | đường tệ nhất | % đường mất vốn |
|---|---|---|---|
| 30 rung (thang $6.186) | $18.503 | $4.590 | **15%** |
| 10 rung (thang $1.212) | $19.067 | $11.264 | **0%** |

Ở $200.000 thì ngược lại (30 rung $396k so với 8 rung $242k), nên quy tắc **không đụng vào** vốn lớn.

**Thứ tự bắt buộc — rút wave TRƯỚC, rút thang SAU.** Thu nhỏ `kss_first_wave_usd` là chỉnh KÍCH
CỠ, hợp pháp sẵn, và đo ra **tốt hơn**: ở $7.000, wave $10 × 30 rung cho trung vị $30.339 / 0% mất
vốn, hơn hẳn wave $28 × 20 rung ($29.615 / 5%) và wave $28 × 8 rung ($21.814). Lý do: 47% lợi
nhuận nằm ở rung 13 trở xuống, wave nhỏ vẫn với tới được, rút thang thì cắt đứt.
→ Chỉ khi wave đã chạm sàn `scan_min_notional` mà thang vẫn quá đắt thì mới được rút thang.

Số liệu: `docs/brake-universe-2026-09-21.json`, và [[crash-brake-measured-2026-09-21]] trong bộ nhớ.

---

## 2. Ba cái phanh, theo đúng thứ tự bắn

Bản đầu của luật này hiệu chỉnh quanh cái phanh **thứ hai**. Cross-check bắt được.

**Phanh 1 — chuỗi thua liên tiếp.** `circuit.evaluate` đóng băng khi
`consecutive_losses >= max_consecutive_losses` (mặc định **4**). N session cùng dừng lỗ **chính
là** một chuỗi dài N, nên phanh này bắn ở N=4 bất kể mất bao nhiêu tiền.

```
sessions ≤ max_consecutive_losses − 1        # để một ngày đồng loạt không tự đóng băng app
```

**Phanh 2 — hạn mức lỗ ngày.** `daily_loss_hard_pct` (mặc định 5%).

```
ladder_usd = min(first_wave × ladder_ratio, max_session_deploy_usd)
worst_day  = sessions × ladder_usd × stop_fraction
ĐIỀU KIỆN:   worst_day ≤ safety_margin × daily_loss_limit × equity
```

Cẩn thận: cầu dao đo **lỗ thô đã thực hiện** chia cho **vốn mark-to-market hiện tại** — cả hai đều
làm nó thấy con số **lớn hơn** mô hình này tính. Xem nó là ước lượng dưới.

**Phanh 3 — sụt vốn tổng** `max_drawdown_pct = 15%`, tính cả lỗ chưa thực hiện. Chưa mô hình hoá.

### Kịch bản "dừng lỗ đồng loạt" là GIẢ ĐỊNH ÁP LỰC, không phải phép đo sổ này

Bản đầu ghi "13/16 mã dừng lỗ cùng ngày 2026-06-05, đo trên universe của ta" — **cách diễn đạt đó
sai**. Con số đó đến từ một **backtest walk-forward** trên 16 mã dữ liệu lịch sử: bằng chứng thật
rằng các lệnh dừng lỗ **có xu hướng cụm lại**, nhưng không phải bằng chứng sổ của ta từng như vậy.

Đo trên sổ thật (`findmy.db`, 40 ngày): ngày xấu nhất có **5 lệnh thoát lỗ**, lỗ thô **0,86%**, và
ngày đó vẫn **đóng dương +$4,75**. Tổng số lệnh thoát lỗ trong cả 40 ngày: 16.

Giả định "mọi ladder đầy khi dừng lỗ" cũng thận trọng quá mức về đô-la: fill trung bình đo được
là ~33% phần đặt chỗ, sổ live hiện tại mới 12,3%. Nhưng nó **thiếu** ba thứ: một ô trống được lấp
lại ngay trong ngày (N session là *một thế hệ*, không phải một ngày), các lệnh thoát lỗ **không
phải** dừng lỗ (hết hạn, trailing đỏ) cũng vào cùng bộ đếm, và trượt giá vượt mức dừng lỗ chỉ được
dung sai 1,25×.

---

## 3. Ba núm để nới, và cái giá của từng núm

| núm | hiện tại | nới ra được gì | cái giá |
|---|---:|---|---|
| `max_consecutive_losses` | 4 | mỗi +1 → thêm 1 session | chấp nhận chuỗi thua dài hơn trước khi app tự dừng |
| `max_session_deploy_usd` | 240 | ladder to hơn → sóng 0 to hơn | mỗi session ôm nhiều vốn hơn; phanh 2 tới sớm hơn |
| `equity_backup_pct` | 25% | thêm ngân sách | prior doc đo **đỉnh triển khai thực 97,4% vốn** — quỹ dự phòng này **trên thực tế không tồn tại**, nên nới nó gần như vô nghĩa |

Công thức cho từng chiều nằm ở `capital.recommend_sessions` (giải theo N) và
`capital.recommend_first_wave` (giải theo cỡ sóng, đã kẹp cả `min_notional` lẫn deploy cap).

Bản đầu của tài liệu này khuyên sóng 0 = $41,25 ở vốn $3.000 và $68,76 ở $5.000 — **cả hai đều
vượt trần deploy $240 và sẽ bị app cắt cụt ladder**, tức session không bình quân giá được và chết
ở mức dừng lỗ trên giá vốn xấu hơn. Đã sửa: hàm giờ kẹp theo trần và trả 0 nếu không có cỡ nào
thoả mãn.

---

## 4. Kiểm sổ đang mở: dùng `audit_book`, không dùng hệ số đồng nhất

Một `ladder_ratio` đồng nhất **không tồn tại**: `autotune_levels_enabled` cho mỗi mã một khoảng
rung riêng (live: 0,5% tới 10%), và session mở ở thời điểm khác nhau tính theo cỡ sóng khác nhau.

Đo thật 2026-08-30 với 6 session: năm ladder cũ ~$140 + một ladder WLD $223,86 = **$931,89 → ngày
xấu nhất 3,86%**. Giả định đồng nhất ở cỡ $40 cho ra **5,81%** — **báo động giả**.

```python
capital.audit_book(equity, ladder_usds=[...], stop_fraction=..., daily_loss_limit=...,
                   max_consecutive_losses=...)
```

Truyền vào `min(isolated_fund, max_session_deploy_usd)` cho từng session. **`isolated_fund` không
phải chi phí ladder** — code gọi nó là "planning cap": DCA+ thủ công **thu nhỏ** nó về
`đã tiêu + một rung`, nhận orphan **thổi phồng** nó thêm `scan_fund` (mặc định 1000), merge thì
cộng dồn hai ladder. Session nào đã đi qua ba đường đó thì con số không đáng tin.

`audit_book` trả về `freezes_on_loss_streak` — vì một sổ **có thể nằm trong hạn mức tiền mà vẫn
đóng băng app**. Đó chính là sổ hiện tại: 6 session = 3,86% (trong hạn mức) nhưng 6 lệnh thua liên
tiếp ≥ 4 (đóng băng).

---

## 5. Quy trình áp dụng (người, Claude API, hoặc xAI API)

1. **Đọc `equity`.** Trên nhánh này nó là hằng số `.env` — xem §0.1. Nếu vừa nạp tiền mà chưa sửa
   `.env`, **dừng lại**, mọi kết quả sau đó sai.
2. Đọc `ladder_ratio` từ toán KSS đã khoá cho `distance_pct`/`max_waves` **đang chạy**. Không
   hardcode 5.841 — live trải từ 5,23 tới 5,96 tuỳ mã.
3. Gọi `capital.recommend_sessions(...)` với **đủ mọi ràng buộc** (hàm sẽ báo lỗi nếu thiếu, cố ý:
   một ràng buộc bị bỏ quên sẽ trả về con số **lớn hơn** và không an toàn).
4. Gọi `capital.audit_book(...)` với ladder **thật** của sổ. Kiểm **cả hai** cờ: `within_limit`
   *và* `freezes_on_loss_streak`.
5. **Không tự áp dụng.** Trả khuyến nghị kèm `binding`, để người quyết. Module này không import
   bất cứ thứ gì từ `app/` — có test khoá — nên nó **không thể** tự ghi settings.
6. Trước khi áp dụng bất cứ thay đổi nào: kiểm `runtime.is_frozen(db)` (nới hạn mức khi đang đóng
   băng chỉ dồn thêm phơi nhiễm cho lúc mở lại), và nhớ `kss_first_wave_usd` **chỉ áp cho session
   MỚI** — sổ sẽ thành hỗn hợp nhiều cỡ, đúng tình huống làm phép kiểm đồng nhất sai ở §4.

---

## 6. Ghi chép thay đổi

| ngày | thay đổi | lý do |
|---|---|---|
| 2026-08-30 | Lập luật. | Kai nạp vốn hằng tháng, cần quy mô đi theo tất định và máy đọc được. |
| 2026-08-30 | Universe 20 → **100** mã. | Nới nguồn cung tín hiệu, vốn là thứ prior doc đo là trần thật của lợi nhuận. |
| 2026-08-30 | `max_concurrent_sessions` = **6** theo yêu cầu thử nghiệm của Kai. | Thí nghiệm có chủ ý trên testnet. Lưới an toàn đã diễn tập: khi đóng băng, lệnh mua bị chặn, lệnh bán vẫn qua. |
| 2026-08-30 | **Tự đính chính 1:** "6 session = 5,81%, vượt hạn mức" **sai**. Sổ thật: **3,86%**. | Công thức giả định mọi session ở cỡ $40; năm session cũ mở khi sóng 0 còn ~$15. Thêm `audit_book()`. |
| 2026-08-30 | **Tự đính chính 2 (cross-check):** luật hiệu chỉnh quanh **sai cái phanh**. `max_consecutive_losses = 4` bắn TRƯỚC hạn mức 5%. N tối đa là **3**, không phải 4. | 6 session hiện tại: một ngày dừng lỗ đồng loạt **chắc chắn đóng băng app**, dù chỉ mất 3,86%. |
| 2026-08-30 | **Tự đính chính 3:** "thêm $484,80 được thêm 1 session" **sai trong thực tế**. | Đúng dưới phanh 2, nhưng phanh 1 và trần deploy khoá vốn triển khai ở **~$701 bất kể vốn bao nhiêu**. Xem §0.2. |
| 2026-08-30 | **Tự đính chính 4:** bỏ khẳng định "rủi ro phẳng 3,88% ở mọi mức vốn" và cái bẫy "nếu cột đó trôi là luật hỏng". | Bất biến thật là **chặn trên 4,00%**, chỉ chạm đúng 3,88% khi vốn rơi đúng bội số của bước. Ở $2.002,21 (chính điểm hiệu chỉnh) là 3,874%; ở $20.000 là 3,975%. Cái bẫy đó sẽ báo hỏng mỗi tháng dù luật vẫn đúng. |
| 2026-08-30 | **Tự đính chính 5:** nguồn "13/16 mã cùng ngày" là **backtest**, không phải sổ của ta. Sổ thật: ngày xấu nhất 5 lệnh, net **dương**. | Xem §2. |
| 2026-09-21 | **Viết lại §0.1 và §0.2.** Mọi con số trong bản cũ đã lệch: vốn $2.000→**$200.000**, wave $40→**$28**, 6 suất→**80**, `max_session_deploy_usd` 240→**0**, `equity_backup_pct` 25→**24,8**; `ladder_coverage_pct` (ra đời 16/09) không hề được nhắc. | Tài liệu tự nhận là "luật, không phải ghi chép" mà lệch 3 tuần thì tệ hơn không có tài liệu. |
| 2026-09-21 | **Rút lại kết luận "thêm vốn KHÔNG mở thêm được gì".** Cổng mở phiên nay tính theo % vốn sống, nên số suất ĐÃ tự đi theo vốn. Cái chưa đi theo là 5 núm ghi bằng đô-la tuyệt đối. | Cổng ngân sách cũ (`scan_fund` phẳng so với `account_equity` tĩnh) đã bị thay từ lâu; kết luận cũ chết theo nó. |
| 2026-09-21 | **Thêm `app/capital_scale.py`:** mỗi núm đô-la có một núm % song sinh, giải lúc ĐỌC, không ghi đè. Mặc định % chọn sao cho ở $200.000 mọi giá trị bằng đúng hôm nay. Vùng chết 10%. | Dự án đã 7 lần dính lỗi "điều khiển báo CÀI ĐẶT thay vì TÁC DỤNG". Ghi đè giá trị thì không phân biệt được người sửa hay máy sửa; giải lúc đọc thì phân biệt được. |
| 2026-09-21 | **§1.1 — sửa đổi hẹp:** `scan_max_waves` được phép bị GIỚI HẠN TRÊN theo khả năng chi trả (không bao giờ kéo dài, không bao giờ dò backtest). Rút wave TRƯỚC, rút thang SAU. | Thang $6.186 là thứ tài khoản $5.000 không trả nổi — số học, không phải khớp lịch sử. Đo: 30 rung ở $5k = 15% đường mất vốn; 10 rung = 0%. Kai duyệt cùng ngày. |
| 2026-09-21 | **Bác bỏ "phanh sập hàng loạt".** Kai yêu cầu dừng mua rung khi thị trường sập; đo 4 phiên bản × 3 mức vốn: **tất cả đều làm sụt sâu TỆ HƠN** ($5k: 31%→39–49%) và tốn 27–70% trung vị. | Trong DCA, mua khi giá rơi CHÍNH LÀ cơ chế gỡ lỗ; chặn nó là đóng băng lỗ ở mức cao nhất. Giữ lại phần cảnh báo, bỏ phần dừng mua. |
