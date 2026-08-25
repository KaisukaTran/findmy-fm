# Tham số động theo quy mô vốn — 2026-08-23

> Nguồn: đo ràng buộc theo quy mô (S1), nghiên cứu ngoài 18 nguồn (S2), mô phỏng phản-thực trên
> **72/82 lệnh thật** với 2 mô hình intrabar (S3), cộng kiểm chứng code do Opus tự làm.
> Nhãn **[V]** = đã tự kiểm chứng bằng code/SQL/số học · **[S]** = kết quả mô phỏng (kèm cả 2 mô hình)
> · **[R]** = nguồn ngoài · **[M]** = trích memory (dữ liệu gốc đã bị xoá, không đo lại được).
> Không có dòng code nào bị sửa trong lần phân tích này.

---

## 0. Tóm tắt điều hành

1. **Câu hỏi "app tự chỉnh mọi tham số để tối ưu lợi nhuận" tách làm hai nửa, và chỉ một nửa an toàn.**
   Nửa **kích cỡ** (bao nhiêu tiền mỗi lệnh, bao nhiêu lệnh) → tự suy ra từ vốn được, thuần số học,
   nên làm. Nửa **hình dạng** (khoảng rung %, TP %, SL %, ngưỡng lọc) → **không nên** để máy tự tối ưu.
2. **Cả 5 hệ thống production đều làm đúng như vậy** [R]: Freqtrade, 3Commas, Hummingbot, OctoBot,
   Jesse đều suy "bao nhiêu tiền" từ số dư và **cố định hình dạng**. Không hệ nào tự tối ưu cả hai.
3. **Phát hiện cấu trúc quan trọng nhất** [V][S]: với `cap = ladder × 1.05`, lợi nhuận **tuyến tính
   tuyệt đối** theo `first_wave` (tỉ lệ đo được 2.000000), và `N_max = floor(0.75 / (w_pct × 10.086))`
   — **equity triệt tiêu**. Nghĩa là luật tự-scale hiện tại chỉ làm **lệnh to ra**, không bao giờ làm
   **nhiều lệnh hơn**. `w_pct` là **cần gạt đòn bẩy**, không phải tham số chiến lược.
4. **Trần năng lực của chiến lược này là ~$100k, giỏi lắm ~$250k** [S] — không phải $1M. Universe hiện
   tại có thanh khoản trung vị **$2,81 triệu/24h**; ở giới hạn tham gia 0,5% ADV thì lợi nhuận **giảm
   một nửa ở $81k–$101k**. Nhánh còn lại (giữ nguyên cỡ sóng, tăng số lệnh) đụng **giới hạn nguồn cung
   tín hiệu**: chỉ 72 lệnh trong 33 ngày → lãi tính bằng đô-la **đứng yên $93** ở mọi quy mô vốn.
5. **Sổ $1k hiện tại đang mua lợi nhuận bằng chính quỹ dự phòng.** Đỉnh triển khai thực đo được
   **97,4% equity** [V] — mô hình lạc quan tái tạo đúng con số này. "Giữ 25% dự phòng" trên thực tế
   **không tồn tại**.
6. **Hai điều kiện tiên quyết phải sửa trước khi bật bất kỳ luật tự-scale nào** [V]:
   (a) không dòng code nào đọc số dư thật từ sàn — mỏ neo vốn duy nhất là hằng số `.env ACCOUNT_EQUITY`,
   và **rút tiền không bị trừ**; (b) lỗi `stepSize` làm sai cỡ **37/361 mã**, đúng nhóm coin đắt mà một
   sổ lớn buộc phải dùng.
7. **Sổ $1k hiệu quả vốn hơn hẳn thời $1M** [V][M]: sử dụng vốn trung bình **70,6%** so với **~12%**,
   và lợi nhuận trên mỗi đô-la triển khai cao hơn **1,3–2,4×**. Bài học: vấn đề thời $1M không phải
   chiến lược yếu, mà là **đặt chỗ quá nhiều rồi không dùng đến**.
8. **Cộng dồn (compounding) có lợi nhưng nhỏ và phải chặn trần** [S]: +2,0%/+3,9% trong 33 ngày.
   Kelly từ mẫu này ra **29× cỡ hiện tại** — con số vô nghĩa của 33 ngày thắng 87%, đừng dùng.
9. **Sàn dưới**: dưới **~$870** thì "tính theo %" không còn là tính theo % (min-notional chi phối);
   dưới **$135** thì `N_max = 0`. Dưới $1.000 nên chạy chế độ **cỡ cố định tối thiểu**, đừng giả vờ %.
10. Kế hoạch: **Pha 0** sửa 2 điều kiện tiên quyết → **Pha 1** lớp kích cỡ suy từ vốn (default OFF)
    → **Pha 2** bất biến tiền mặt cứng thay cho mô hình đặt chỗ → **Pha 3** trần năng lực theo thanh
    khoản. Không có pha nào "tự tối ưu hình dạng".

---

## 1. Trả lời thẳng câu hỏi

| Bạn muốn | Có làm được không | Vì sao |
|---|---|---|
| App tự chỉnh **cỡ lệnh** theo vốn | **Có** — nên làm | Thuần số học, kiểm toán được, không khớp dữ liệu (no fitting). Mọi bot production đều làm [R] |
| App tự chỉnh **số lệnh đồng thời** theo vốn | **Có, nhưng phải đổi cách tính** | Luật hiện tại làm equity triệt tiêu → N không đổi theo vốn [V]. Phải neo N vào **tiền mặt thật**, không vào đặt chỗ |
| App tự chỉnh **giới hạn thanh khoản** theo vốn | **Có, và bắt buộc** khi lên trên $10k | Đây mới là thứ quyết định trần năng lực [S] |
| App tự tối ưu **TP/SL/khoảng rung/ngưỡng lọc** | **Không nên** | Với ~5 năm dữ liệu, quá ~45 cấu hình thử là gần như chắc chắn tạo Sharpe 1 trong mẫu = 0 ngoài mẫu (MinBTL) [R]. Hồ sơ 81% thắng / payoff 0,47 của bot này là dạng bị Deflated Sharpe phạt nặng nhất [R]. Chính repo này đã **xoá `hyperopt.py`** vì hàm mục tiêu bỏ qua SL và phí |
| App tự tối ưu **để "đảm bảo tối ưu lợi nhuận"** | **Không tồn tại** | Không có cấu hình nào tối ưu ở mọi chế độ thị trường. Cái làm được là: đúng cỡ theo vốn + không vượt năng lực + không tự lừa mình bằng backtest |

---

## 2. Hai điều kiện tiên quyết (phải xong trước Pha 1)

### 2.1 Mỏ neo vốn không phản ánh vốn thật [V]

`risk.account_equity()` = mark-to-market thật, nhưng đáy của nó là hằng số:

```
cash   = settings.account_equity - đã_đầu_tư + đã_chốt_lời     (portfolio.py:197)
equity = cash + giá_trị_thị_trường
```

`settings.account_equity` đến từ `.env ACCOUNT_EQUITY=1000`. **Không một dòng nào gọi `fetch_balance()`**
[V] — grep toàn bộ `app/` chỉ ra 12 chỗ dùng `settings.account_equity`, 0 chỗ đọc sàn. Và bảng
`withdrawals` **không hề bị trừ** khỏi equity [V] — nó chỉ phục vụ tab Chi phí.

Hệ quả: equity *có* tự lớn theo lãi đã chốt (nên compounding hoạt động), nhưng nếu bạn **nạp/rút tiền
thật, hoặc có lệnh ngoài bot, hoặc trả phí bằng BNB**, mỏ neo lệch khỏi thực tế — và **mọi knob kích
cỡ tự-scale sẽ lệch theo cùng một hệ số**. Trên paper thì vô hại; trên live đây là lỗi làm sai cỡ toàn
hệ thống.

**Phải làm:** hoặc đọc số dư thật qua ccxt `fetch_balance()` khi `live_trading=true` (đường sạch), hoặc
tối thiểu trừ `withdrawals` + cảnh báo khi lệch quá X%. Không có cái này thì "tự chỉnh theo vốn" là
tự chỉnh theo **một con số bạn gõ tay**.

### 2.2 Lỗi `stepSize` chặn đúng nhóm coin mà sổ lớn cần [V]

`ccxt.binance().precisionMode == 2` (`DECIMAL_PLACES`) → `market['precision']['amount']` là **số chữ số
thập phân**, nhưng `providers.py:188` dùng thẳng nó làm `stepSize`:

```python
"stepSize": market.get("precision", {}).get("amount") or 0.00001   # BNB -> 3 (đồng!), BTC -> 5
```

Qua công thức thật `pyramid.py:224-227` (`steps = round(raw_qty/step)` → `max(steps*step, minQty)`),
với sóng đầu $15 mọi coin đắt cho `steps = 0` → rơi về `minQty`:

| Mã | Sóng đầu thực tế | Đúng ra | Sai số |
|---|---:|---:|---:|
| MUB | $0,96 | $15,40 | 16× |
| ZEC | $0,81 | $14,64 | 18× |
| WBTC | $0,77 | $15,31 | 20× |
| BNB | $0,69 | $15,17 | 22× |
| PAXG / XAUT | $0,46 | ~$15,15 | 33× |
| BCH | $0,27 | $14,93 | 55× |

**37/361 mã (10,2%) sai cỡ** [V] — đo bằng giá và filter `LOT_SIZE` thật lấy từ ccxt.
Bằng chứng trên sổ: **DASH #1 là coin duy nhất giá >$17 từng mở phiên trong 82 phiên**, giải ngân
**$101,87 thay vì $15 (thừa 6,8×)** [V]. Commit `7a7a264` chỉ thêm guard chống dust, **không sửa gốc**.

**Trạng thái: tiềm ẩn, chưa bùng** — không phiên dust nào, và lý do ZEC/WBTC/BNB (đạt "all gates passed"
59/64/10 lần, win_lb 85–93%) chưa mở phiên là **trần 2 lệnh/lần quét + xếp hạng**, chứ chưa chứng minh
được là do lỗi này. Nhưng nó nổ **ngay khi** một coin đắt được chọn — và sổ càng lớn càng buộc phải
dùng coin thanh khoản cao, tức coin đắt.

**Sửa gốc:** đọc `market['info']['filters']` → `LOT_SIZE.stepSize` (đã verify có sẵn và đúng cho mọi mã),
chỉ fallback `10**-precision` khi thiếu. Kèm test hồi quy so stepSize suy ra với LOT_SIZE thật.

---

## 3. Tham số nào theo vốn, tham số nào bất biến

Phân tách này khớp với cả 5 hệ thống production [R] và với cấu trúc code hiện tại.

| Nhóm | Tham số | Luật |
|---|---|---|
| **Theo vốn** (suy ra, không gõ tay) | `kss_first_wave_usd` · `max_session_deploy_usd` · `scan_fund` · `autoapprove_max_notional` · `cash_floor_usd` · `live_max_order_notional` | tỉ lệ với equity, có sàn/trần tuyệt đối |
| **Theo vốn, gián tiếp** | `max_concurrent_sessions` · `min_quote_volume` | suy từ tiền mặt thật và từ trần tham gia thanh khoản (§6) |
| **BẤT BIẾN** (không bao giờ tự chỉnh) | `scan_distance_pct` · `scan_tp_pct` · `sl_pct` · `scan_max_waves` · `kss_trail_arm/lock/gap/atr_mult/min` · `deadline_days` · `min_win_rate` · `min_expectancy_pct` · `min_confidence` · `block_downtrend_adx` · `rel_strength_*` · `overextension_*` | Đây là **niềm tin về phân phối giá**, không phải về cỡ tài khoản. Đổi theo vốn = vô nghĩa; đổi theo backtest gần nhất = overfit |

---

## 4. Lớp kích cỡ đề xuất

```
w_pct           = 1,15%                       # cần gạt đòn bẩy, DUY NHẤT ở đây
first_wave      = clamp( equity × w_pct , min_wave_floor , max_wave_ceiling )
ladder_cost     = first_wave × Σ (n+1)(1−d)ⁿ  # = first_wave × 9,606 ở d=2%, W=4
session_cap     = ladder_cost × 1,05
autoapprove_max = rung_sâu_nhất × 1,25        # = first_wave × W × (1−d)^(W−1) × 1,25
N_max           = min(
                    floor( tiền_mặt_thật × (1 − backup_pct) / kỳ_vọng_dùng_thực ),
                    trần_cứng
                  )
min_quote_volume ≥ rung_sâu_nhất / X_participation      # X = 0,5%
```

**Ba khác biệt so với hiện tại, và vì sao:**

1. **`N_max` neo vào *kỳ vọng dùng thực*, không vào *đặt chỗ đầy đủ*.** Đo được: đặt chỗ $144,09 nhưng
   **mức dùng thực trung bình chỉ $47,78 (33,2%)** — trung bình 1,85 rung, chỉ **18,1% số phiên** từng
   khớp đủ 4 rung [S]. Mô hình đặt-chỗ-đầy-đủ **under-deploy khoảng 3×**. Tính N theo kỳ vọng dùng ra
   **14**, đúng bằng mức concurrency đỉnh quan sát được (14).
2. **Phải có bất biến tiền mặt CỨNG đi kèm.** Đây là điều kiện an toàn, không phải tuỳ chọn: ở
   `w_pct = 3%`, luật "cho mượn đặt chỗ nhàn rỗi" hiện tại **vượt qua được cổng ngân sách trong khi
   triển khai thực chạm 119% equity** [S] — không có tường chặn nào. Sổ thật đã tới **97,4%**, tức
   cách bức tường đó **2,6%**.
3. **`autoapprove_max_notional` phải suy ra, không gõ tay.** Hiện là **$120.000** trong khi rung sâu
   nhất là **$56,47** — cao gấp **2.125×**, tức cổng "người duyệt lệnh quá cỡ" đang **tắt trên thực tế** [V].

---

## 5. Bằng chứng mô phỏng

**Cách đọc:** mọi con số có 2 mô hình intrabar (nến 15m không nói được đỉnh hay đáy đến trước) —
*primary* bi quan (SL trước) / *secondary* lạc quan (TP trước). **Chỉ tin khi cả hai cùng dấu.**
Engine đã tái tạo **chính xác** baseline của lần đánh giá trước: **$93,21 / $116,32**, vốn đỉnh $831 [S].

### 5.1 `w_pct` — tuyến tính, không có điểm tối ưu

Lợi nhuận **tuyến tính tuyệt đối** theo `first_wave` (tỉ lệ 2.000000 khi nhân đôi). Win-rate, profit
factor, cơ cấu thoát, thời gian giữ và **lãi trên mỗi đô-la-ngày triển khai đều bất biến tới 5 chữ số**
(0,00614 P / 0,00507 S) [S]. Nghĩa là **không có `w_pct` "tối ưu"** — chỉ có `w_pct` lớn nhất mà sổ chịu được.

| `w_pct` | sóng đầu @ $1k | `N_max` (luật hiện tại) | đỉnh triển khai P / S |
|---:|---:|---:|---|
| 1,00% | $10,00 | 7 | 55,4% / 64,9% |
| **1,15%** | **$11,50** | **6** | **63,7% / 74,6%** ← lớn nhất còn nằm trong dự phòng ở **cả hai** mô hình |
| 1,40% (đang chạy) | $15,00 | 4–5 | **83,1% / 97,4%** ← đã vượt |
| ≥1,75% | ≥$17,50 | 3 | vượt quá tiền sổ có |
| 3,00% | $30,00 | 2 | **119% equity** — không có tường chặn [S] |

**Đối chiếu thực tế:** đỉnh triển khai đo từ `fills` là **97,4%** [V] — mô hình lạc quan trúng y hệt.

### 5.2 Concurrency — overcommit đang có lợi, cả hai mô hình đồng ý

`max_concurrent` 5 → 12 cho **+$45 (+99%) primary / +$86 (+305%) secondary**, vốn đỉnh vẫn $831/$1.000 [S].
Nguyên nhân đúng như §4.1: đặt chỗ gấp 3× mức thực dùng. **Ràng buộc đang bind là số lượng, không phải
tiền** ở mọi N ≤ 12 (0 lần bị chặn vì thiếu tiền).

⚠ Nhưng điều này **chỉ đúng trong chế độ thị trường này**: `expected_fill_fraction = 0,33` phụ thuộc
chế độ và **tiến về 1,0 trong một đợt giảm** — khi đó 12 phiên cùng khớp đủ 4 rung sẽ cần $1.729 trên
một sổ $1.074. Đó chính là lý do §4.2 (bất biến tiền mặt cứng) là bắt buộc chứ không phải tuỳ chọn.

### 5.2b ĐÍNH CHÍNH sau khi áp dụng (đo trực tiếp 2026-08-23 17:47–18:02) [V]

`max_concurrent_sessions` đã được nâng **12 → 14**. Kết quả đo trên sổ thật **bác bỏ hai luận điểm ở
§4.1 và §5.2 khi áp vào ảnh chụp hiện tại** — ghi lại đầy đủ vì đây là bằng chứng ngược:

1. **Trần concurrency KHÔNG còn là ràng buộc bind.** Sau khi nâng lên 14, còn dư **3 slot** nhưng
   **0 phiên mở thêm** trong 15 phút quan sát: ngân sách còn **$101,11**, một phiên mới cần **$144,09**.
   Cái bind bây giờ là **cổng ngân sách**, không phải số lượng.
2. **`committed_pct` phẳng ở 84,7%** qua 4 mẫu (84,76 → 84,70), `locked` đứng yên $709,54.
3. **Mục tiêu >95% không đạt được bằng knob.** Kể cả `equity_backup_pct = 0` cũng chỉ mở thêm 2 phiên
   → **~93,0%**. Bảng đầy đủ: backup 25%→0 phiên (84,8%) · 15%→1 (88,9%) · 5%→2 (93,0%) · 0%→2 (93,0%).
4. **Đề xuất I-3 ở §4.1 ("`_session_lock` = dùng + rung kế tiếp") SẼ LÀM TỆ HƠN ở trạng thái này.**
   Tính lại từng phiên: nó chỉ giảm khoá ~$8 cho 4 phiên sâu (rung kế tiếp là rung TO nhất nên
   `dùng + rung kế` ≈ trọn reserve) nhưng **tăng khoá $29–43 cho mỗi phiên nông** (7 phiên) → tổng khoá
   ~$920 > ngân sách $810. Con số "phantom $88–108" trong §1.2/§5 là **trung bình theo thời gian của cả
   cửa sổ 33 ngày**, KHÔNG phải ảnh chụp hôm nay ($177,43 bị khoá chưa tiêu, nhưng phân bố lệch về
   4 phiên sâu). ⇒ **I-3 phải được mô phỏng lại trên phân bố ladder-depth thực tế trước khi ship.**

**Đòn bẩy duy nhất còn lại để lên >95%: nhiều phiên hơn nhưng NHỎ hơn.** `kss_first_wave_usd` $15 → $10
hạ mỗi ladder từ $144,09 xuống **$96,06** → cùng ngân sách nuôi được ~50% số ladder. Mô phỏng đã chứng
minh tổng vốn đặt chỗ **bất biến theo `w_pct`** (luôn = 75% equity), nên không mất mức triển khai — chỉ
trải ra nhiều coin hơn và bắt được nhiều tín hiệu hơn (funnel đang chặn rất nhiều ứng viên vì hết slot).
⚠ **Chưa đo:** lãi mỗi phiên giảm tuyến tính theo cỡ sóng; vế "nhiều lệnh hơn bù lại được" là **giả
thuyết**, vì §5.1 chỉ chứng minh tuyến tính trên **cùng một tập lệnh**. Cần chạy lại `S3_sweep` với số
phiên thay đổi trước khi đổi.

### 5.3 Trần năng lực — con số quyết định

Ràng buộc: rung sâu nhất ≤ X% thanh khoản 24h của mã đó. Universe hiện tại là small-cap, thanh khoản
**trung vị $2,81 triệu/24h** [S].

| Vốn | X=0,1% | X=0,5% | X=1,0% |
|---|---|---|---|
| $1k–$10k | 68–72/72 mã sống · 9,3%/11,6% | 72/72 · 9,3%/11,6% | 72/72 · 9,3%/11,6% |
| $100k | 24/72 · 0,7%/1,8% | **49/72 · 2,5%/5,9%** | 68/72 · 8,4%/10,6% |
| $1M | 7/72 · 1,1%/1,1% | 15/72 · **chưa chứng minh** (2 mô hình ngược dấu) | 24/72 · 0,7%/1,8% |

**Lợi nhuận giảm một nửa ở:** $16k–$20k (X=0,1%) · **$81k–$101k (X=0,5%)** · $163k–$201k (X=1,0%) ·
$185k–$277k nếu cho phép thu nhỏ cỡ theo từng mã.

⇒ **Trần thực tế ≈ $100k ở giới hạn tham gia 0,5%; ≈$250k nếu chấp nhận thu nhỏ theo mã.** Kiểm chứng
độc lập bằng số học: mã trung vị chịu được sóng $3.731 → tương đương equity $248.728 ở `w_pct` 1,5%.

**Nhánh còn lại cũng bịt:** nếu giữ nguyên sóng $15 và để `N` tăng theo vốn, năng lực không bao giờ
bị chạm — nhưng **lãi tính bằng đô-la đứng yên ở $93,21 tại mọi quy mô** (= 0,009% của sổ $1M), vì
ràng buộc chuyển thành **nguồn cung tín hiệu: chỉ 72 lệnh trong 33 ngày** [S]. Chiến lược bị ép từ
cả hai phía.

### 5.4 Cộng dồn

+$1,89 (+2,0%) P / +$4,56 (+3,9%) S trong 33 ngày [S] — nhỏ vì equity chỉ nhảy khi phiên đóng, nên chỉ
các phiên muộn được hưởng (hiệu ứng bậc hai ≈ r/2). Kéo dài 1 năm: tuyến tính +97%/+126% so với cộng
dồn ngây thơ +164%/+252%; **vol drag đo được chỉ −4,4/−8,1 điểm/năm** (~2,7% phần lãi cộng dồn).

Lý do thật để **chặn trần** không phải vol drag, mà là: (a) drawdown tỉ lệ **chính xác** với `w_pct` và
**chưa bao giờ được thử qua một downtrend**; (b) cộng dồn làm bạn **chạm trần năng lực §5.3 nhanh hơn**;
(c) Kelly từ mẫu này ra `w ≈ 43%` = **29× cỡ hiện tại** — hiện vật của 33 ngày thắng 87%, tuyệt đối
không dùng.

### 5.5 Sàn dưới

- **$870**: dưới mức này `min_notional / w_pct` chi phối — "tính theo %" không còn là tính theo %.
- **$135**: `N_max = 0` — một ladder đã clamp ($96,06) vượt ngân sách 75%.
- Ở $200 equity, luật clamp **tái tạo đúng lỗi GIGGLE**: một phiên chiếm **48% sổ**.
- Phí **không phải** sàn (mọi chi phí đều theo %; đo thực: phí ăn **8,64% lãi gộp**, và tỉ lệ này
  **không đổi** ở sóng $5/$10/$15/$25/$50) [S1]. Cái tạo sàn là **lượng tử hoá khối lượng**.
- ⇒ Dưới **$1.000**: chạy chế độ **cỡ cố định tối thiểu** ($10/sóng, giảm `max_waves`), đừng giả vờ %.

---

## 6. Bài học từ hai thời kỳ

| | Thời $10M/$1M [M] | Sổ $1k hiện tại [V] |
|---|---|---|
| Sử dụng vốn trung bình | ~12% | **70,6%** (trung vị 75,5%, đỉnh 97,4%) |
| Lợi nhuận trên equity | ~0,63%/tháng | **+8,40% / 33,9 ngày** |
| Lợi nhuận trên vốn triển khai | ~5–8%/tháng | **+11,9% / 33,9 ngày** |
| Cấu hình | concurrent 300, sóng đầu $1.500 | concurrent 12, sóng đầu $15 |

**Kết luận:** thời $1M không thất bại vì chiến lược yếu, mà vì **đặt chỗ khổng lồ rồi không dùng đến** —
đúng cùng một lỗi mô hình mà §4.1 chỉ ra, chỉ khác quy mô. Sổ nhỏ hiện tại hiệu quả hơn **~6× về mức
sử dụng vốn** và **1,3–2,4× trên mỗi đô-la triển khai**.

---

## 7. Kế hoạch triển khai

Mọi pha: knob runtime nhìn thấy được, **mặc định TẮT**, TDD, kill-metric đăng ký trước.
`app/kss/pyramid.py` giữ FROZEN.

### Pha 0 — Điều kiện tiên quyết (không đổi hành vi giao dịch)
- Sửa `stepSize` đọc từ `LOT_SIZE` (§2.2) + test hồi quy trên mẫu universe.
- Mỏ neo vốn: đọc số dư thật khi `live_trading`, hoặc tối thiểu trừ `withdrawals` + cảnh báo lệch (§2.1).
- Sửa 4 knob sót thời $1M: `autoapprove_max_notional` $120.000 → suy ra (~$70) · `min_quote_volume`
  500k → 1M · `scan_max_symbols` 320 → xem lại · `min_confidence` 40 → thống nhất với `.env`.
- Đồng bộ `.env` với runtime (hiện `.env` là fallback **độc**: `min_expectancy 3.0` + `scan_tp 3.0` =
  deadlock, `scan_fund=1000` = lỗi GIGGLE).
- **Nghiệm thu:** 0 thay đổi PnL · stepSize suy ra khớp `LOT_SIZE` cho 100% universe · một reset
  runtime_config không còn dẫn tới deadlock.

### Pha 1 — Lớp kích cỡ suy từ vốn (`capital_autosize_enabled`, mặc định TẮT)
- Suy `first_wave` / `session_cap` / `autoapprove_max` từ equity theo §4, tính lại khi mở phiên mới.
- Sàn/trần tuyệt đối; dưới $1.000 chuyển chế độ cỡ cố định.
- **Nghiệm thu:** bật/tắt cho kết quả **giống hệt** ở đúng equity hiện tại · số rung khớp trung bình
  giữ ≈1,85 (tụt = ladder bị cắt) · không phiên nào vượt `session_cap`.

### Pha 2 — Bất biến tiền mặt cứng thay cho mô hình đặt chỗ (**quan trọng nhất về an toàn**)
- `N_max` theo kỳ vọng dùng thực + **tường tiền mặt cứng** không bao giờ cho triển khai thực vượt
  `(1 − backup_pct)` — kể cả khi luật cho-mượn-đặt-chỗ nói được.
- **Nghiệm thu:** mô phỏng lại `w_pct = 3%` **không còn** chạm 119% · đỉnh triển khai thực ≤ mốc đã
  chọn · số phiên mở/ngày không giảm trong chế độ thị trường hiện tại.

### Pha 3 — Trần năng lực theo thanh khoản
- `min_quote_volume` suy từ `rung_sâu_nhất / X` với `X = 0,5%`; ghi audit khi một mã bị loại vì năng lực.
- **Nghiệm thu:** ở equity giả lập $100k, số mã sống khớp bảng §5.3 (49/72 ở X=0,5%).

### KHÔNG làm
Tự tối ưu TP/SL/khoảng rung/ngưỡng lọc theo lịch sử gần nhất. Bằng chứng: MinBTL/DSR [R], `hyperopt.py`
đã bị xoá khỏi chính repo này vì hàm mục tiêu bỏ qua SL và phí, và 4 phân tích measure-first hồi tháng 7
đã bác 12 ý tưởng chỉnh knob nghe rất hợp lý.

---

## 8. Quyết định của Kai (chốt 2026-08-23) — và hệ quả

| # | Quyết định | Hệ quả kỹ thuật |
|---|---|---|
| 1 | **Giữ mức triển khai > 95%** | Đo theo **"đã cam kết"** (đã mua + rung DCA đang treo), KHÔNG theo "đã mua". Xem §8.1 — ép 95% theo nghĩa "đã mua" sẽ làm chết ladder |
| 2 | **Đích vốn hiện tại < $100k**, sau tính tiếp | Trần năng lực §5.3 (~$100k @ 0,5% ADV) **vừa đủ** — Pha 3 vẫn cần vì $100k đúng là biên |
| 3 | **Có đọc số dư thật từ sàn**, nhưng chạy paper để tìm công thức | Xây `capital_anchor` + `fetch_balance`, knob **mặc định TẮT**, paper giữ nguyên hành vi |
| 4 | **Không chặn trần cộng dồn** | `w_pct` cố định, vốn tự lớn → cỡ lệnh tự lớn. Rủi ro đã ghi: drawdown tỉ lệ **chính xác** với `w_pct` và chưa qua downtrend thật; và cộng dồn làm chạm trần §5.3 nhanh hơn |

### 8.1 "Trên 95%" đo bằng thước nào — mâu thuẫn số học phải giải

Đo trên sổ thật 2026-08-23 [V]:

| Thước đo | Giá trị | Ý nghĩa |
|---|---:|---|
| Đã mua (`Σ total_cost`) | 48,7% | tiền đã bỏ ra |
| **Đã cam kết** = đã mua + notional 10 lệnh BUY đang treo ($383,08) | **84,2%** | tiền đã có nhiệm vụ |
| Tự do thật | 15,7% | tiền thực sự nằm không |

Tổng chi phí **rung kế tiếp** của cả 12 phiên = **$445,50 = 41,4% equity**. Nếu ép tiền mặt xuống ≤5%
thì $445 rung DCA **không còn tiền để khớp** → `orders._apply_cash_cap` sẽ thu nhỏ lệnh hoặc ném
`InsufficientCashError` (lệnh nằm PENDING) → phiên rơi tới hard-SL **mà không được trung bình giá**.
Đó đúng là cơ chế đã giết cụm INJ/UNI/ALICE/BABY ngày 22/08, nhưng ở quy mô toàn sổ.

⇒ **Cách đạt >95% mà không phá ladder: tăng `max_concurrent_sessions` 12 → 14–15, giữ `w_pct`.**
Con số 14 trùng khớp độc lập với mô phỏng (N theo *kỳ vọng dùng thực* = 14 = đúng đỉnh concurrency đã
quan sát) và funnel xác nhận cap đang bind (**66 chu kỳ quét bị bỏ vì "max concurrent" trong 7 ngày**).
Phải đi kèm **chỉ số "đã cam kết"** hiển thị trên UI thì mới quản được mục tiêu 95%.

⚠ Rủi ro còn lại của quyết định #1, ghi rõ để không quên: `expected_fill_fraction = 0,33` phụ thuộc
chế độ thị trường. Trong một đợt giảm nó tiến về 1,0 — khi đó 14 phiên cùng muốn khớp rung sẽ cần
**~$2.000 trên một sổ $1.074**. Chạy >95% nghĩa là **chấp nhận rằng trong một đợt giảm mạnh, phần lớn
rung DCA sẽ không khớp được**. Đây là đánh đổi đã được chọn có ý thức, không phải sơ suất.

## 9. Quyết định còn treo

Bốn câu hỏi lớn đã được chốt ở §8. Còn lại **một** câu chưa trả lời:

1. **4 knob sót thời $1M — sửa ngay hay chờ?** (§7 Pha 0)
   - `autoapprove_max_notional` **$120.000** trong khi rung sâu nhất là **$56,47** → cao gấp **2.125×**,
     tức cổng "người duyệt lệnh quá cỡ" **đang tắt trên thực tế**. *Khuyến nghị: sửa ngay, và cho nó
     **suy ra** từ rung sâu nhất (§4) thay vì gõ tay.*
   - `min_quote_volume` 500k → 1M · `scan_max_symbols` 320 → xem lại (tốn 41,6s Grok/lần quét,
     $11,95 AI Grok trong tháng 8) · `min_confidence` 40 → thống nhất với `.env`.

Và **hai việc bắt buộc** phát sinh từ quyết định #1 (>95% triển khai), không phải câu hỏi mà là hệ quả:

2. **`max_concurrent_sessions` 12 → 14–15** — đây là đòn bẩy đúng để đạt >95% "đã cam kết" mà không
   phá ladder (§8.1).
3. **Chỉ số "đã cam kết" phải lên UI** — không có nó thì mục tiêu 95% không quản được, vì con số
   "Cash %" hiện tại trên dashboard **không phân biệt** tiền nằm không với tiền đã dành cho rung DCA
   đang treo.

---

## Phụ lục: giới hạn của phân tích này

- **n = 72 phiên / 33 ngày, MỘT chế độ thị trường**, và cửa sổ đó **kết thúc bằng hồi phục**. Không có
  downtrend thật trong mẫu. Đây là giả thuyết, không phải dự báo.
- Sai số tuyệt đối của mô hình **+13% đến +42%**; chỉ tin thứ tự xếp hạng và các kết quả **cả hai mô
  hình cùng dấu**. Các con số có **cổng năng lực** khuếch đại bất đồng giữa hai mô hình (25% → 100%+)
  nên chỉ mang tính định hướng.
- Ràng buộc năng lực là **phép thử tỉ lệ tham gia bậc nhất**, không phải mô hình tác động giá đầy đủ.
  Luật căn bậc hai `I(Q) = Yσ√(Q/V)` [R] gợi ý suy giảm mượt hơn là ngưỡng cứng.
- `expected_fill_fraction = 0,33` **phụ thuộc chế độ thị trường** và sẽ tiến về 1,0 trong một đợt giảm.
- Dữ liệu thời $10M/$1M **đã bị xoá** (reset paper 2026-07-20) — mọi so sánh với thời kỳ đó là trích
  memory, không đo lại được.

## Phụ lục: nguồn

Nội bộ (scratchpad phiên 2026-08-23, read-only): `S1_scale_constraints.md` · `S2_research_dynamic_sizing.md`
· `S3_capital_scaling.md` + `S3_results.json` · engine tái dùng `D_engine.py` từ phiên đánh giá chiến lược.
Ngoài: Freqtrade / 3Commas / Hummingbot / OctoBot / Jesse docs · Bailey–Borwein–López de Prado–Zhu
(MinBTL, Deflated Sharpe Ratio) · nghiên cứu tác động giá căn bậc hai (TSE 8 năm; nhân rộng trên BTC
futures δ≈0,59) — URL đầy đủ trong `S2_research_dynamic_sizing.md`.
