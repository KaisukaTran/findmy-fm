"""Render holdout_report.md (Vietnamese) from results_holdout.json."""
from __future__ import annotations

import common  # noqa: F401
import json

from common import STUDY

r = json.load(open(STUDY / "results_holdout.json", encoding="utf-8"))
S1, S2 = r["seeds"]["20261011"], r["seeds"]["20261012"]
btc = r["btc"]


def f(x, d=2, sign=False):
    return "–" if x is None else (f"{x:+.{d}f}" if sign else f"{x:.{d}f}")


def ci(c):
    return f"[{c[0]:+.2f}; {c[1]:+.2f}]"


NAMES = {"a_all": "(a) tất cả", "b_resolved": "(b) đã thoát trước hết dữ liệu", "c_full60_horizon": "(c) đủ 60 ngày (vào ≤ 11/08)"}


def g_table(gate):
    out = ["| hạt | tập | n phiên (chặn) | lift lq | lift bq | CI khối-ngày14 lq | CI khối-ngày14 bq | p trong-ngày (lq/bq) | MDE 80% | luật |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for seed, so in r["seeds"].items():
        for name, d in so["subsets"].items():
            if gate not in d:
                continue
            o, p = d[gate]["o"], d[gate]["p"]
            out.append(f"| {seed[-1]} | {NAMES[name]} | {d['n']:,} ({o['n_vetoed']:,}) | {f(o['lift_pb'],3,True)} | {f(p['lift_pb'],3,True)} | "
                       f"{ci(o['ci_day14']['pb'])} | {ci(p['ci_day14']['pb'])} | {o['p_within_day']:.3f} / {p['p_within_day']:.3f} | "
                       f"{f(o['mde80_lift_pb'],2)} | **{d[gate]['decision_rule']}** |")
    return "\n".join(out)


def buckets(so, key):
    out = ["| RSI ngày | n | P(rung2≤48h) | ppdd lq / bq | $/phiên lq / bq |", "|---|---|---|---|---|"]
    for x in so[key]:
        out.append(f"| {x['bucket']} | {x['n']:,} | {f((x['o']['p_rung2_48'] or 0)*100,1)}% | {f(x['o']['ppdd'],2)} / {f(x['p']['ppdd'],2)} | "
                   f"{f(x['o']['mean_usd'],2)} / {f(x['p']['mean_usd'],2)} |")
    return "\n".join(out)


a1 = S1["subsets"]["a_all"]
a2 = S2["subsets"]["a_all"]
e1, e2 = S1["exploratory_ex_worst_coin"], S2["exploratory_ex_worst_coin"]
TXT = f"""# Holdout G4 (RSI14 ngày, kể cả nến đang hình thành, ≥ 60) — dữ liệu SAU cửa sổ gốc (2026-10-11)

*Luật quyết định đăng ký trước, không đổi: lift ppdd của G4 ≥ +0,15 ở CẢ HAI cận → **keep**; ≤ 0 ở MỘT cận bất kỳ → **drop**; còn lại → **inconclusive**.*

## 0. Kết quả

**Theo đúng luật: DROP** (cả hai hạt giờ-vào-lệnh, cả ba tập con; lift âm ở cả hai cận). Nhưng đọc kèm ba điều:

1. **Lift G4 trên mọi phiên (tập a, hạt 1): {f(a1['G4']['o']['lift_pb'],2,True)} (lạc quan) / {f(a1['G4']['p']['lift_pb'],2,True)} (bi quan)** ppdd; CI khối-ngày-14 {ci(a1['G4']['o']['ci_day14']['pb'])} / {ci(a1['G4']['p']['ci_day14']['pb'])}; p hoán vị trong-ngày {a1['G4']['o']['p_within_day']:.2f} / {a1['G4']['p']['p_within_day']:.2f}. Hạt 2: {f(a2['G4']['o']['lift_pb'],2,True)} / {f(a2['G4']['p']['lift_pb'],2,True)}, p {a2['G4']['o']['p_within_day']:.2f}. **Không có ý nghĩa thống kê theo hướng nào.**
2. **Mẫu gần như không có quyền lực.** 71 ngày; MDE 80% ≈ {f(a1['G4']['o']['mde80_lift_pb'],1)} ppdd (hạt 1), {f(a2['G4']['o']['mde80_lift_pb'],1)} (hạt 2) — gấp ~4–5 lần hiệu ứng cần xác nhận (+0,33 ở cửa sổ gốc) và ~25% ppdd nền của holdout ({f(a1['baseline']['o']['ppdd'],1)}). CI chứa cả +0,15 lẫn −1,2: dữ liệu này **không phân biệt được** "G4 vô dụng" với "G4 còn nguyên hiệu ứng".
3. **Kết quả "drop" do MỘT coin.** Loại KAITO (6–8 phiên chồng nhau, đủ 10 rung, timeout 60 ngày, tổng −$1,3k…−$1,8k; thuộc nhóm RSI 40–50 nên nằm bên "lọt") thì lift G4 thành {f(e1['G4']['o']['lift_pb'],2,True)}/{f(e1['G4']['p']['lift_pb'],2,True)} (hạt 1) và {f(e2['G4']['o']['lift_pb'],2,True)}/{f(e2['G4']['p']['lift_pb'],2,True)} (hạt 2) — sẽ là "keep" theo luật. **THĂM DÒ, chọn coin SAU khi thấy dữ liệu, không thay đổi quyết định.** Nhưng nó cho thấy kết luận 71 ngày này mong manh theo một cụm phiên.

**Tóm lại:** quyết định đăng ký trước = *drop*; bằng chứng hàm ý = *không kết luận được, nghiêng trung tính* (không phải bằng chứng mạnh chống G4). G4 không được xác nhận trên holdout.

## 1. Thiết kế (giống nghiên cứu chính)

* Dữ liệu `data/research/market_holdout.db` (chỉ-đọc): 1h từ 2026-07-01, 1d từ 2025-07-01. **Tháng 7/2026 chỉ làm lịch sử/khởi động, không có phiên nào vào lệnh trong đó.** Vào lệnh 2026-08-01 → 2026-10-10 (71 ngày). 53 mã chỉ có nến ngày (đã hủy niêm yết trước 07/2026) không có nến 1h nên không vào được vũ trụ.
* Pipeline y hệt: top-100 theo khối lượng 24h điểm-thời-gian (≥ $1M, bỏ stable/wrapped/token hoá), ≥ 30 nến ngày đóng, downtrend + falling-knife + consensus ≥ 45 trên 365 nến ngày đóng; cùng `simulate_kss`, cùng cấu hình (rung 7%, 10 sóng, TP 5,24% + 0,5%/rung, SL 0, 60 ngày, chi phí 0,30%, wave-0 $28 — in lại từ `runtime_config`, không đổi); cả hai cận trong-nến; giờ vào lệnh ngẫu nhiên mỗi ngày với hạt 20261011 (chính) và 20261012. Mã mới: `holdout.py` (vá hằng số của `entry_study.py`/`universe.py`, không sửa file đã commit). Look-ahead: 0 vi phạm.
* Thống kê: lift = ppdd(lọt) − ppdd(nền); CI bootstrap khối-ngày vòng. **Độ dài khối đổi từ 30 (nghiên cứu chính) xuống 14 vì mẫu chỉ 71 ngày**; CI với khối 7 và 30 trong JSON (kết luận không đổi). p = hoán vị trong-ngày (một cổng, không cần họ). MDE 80% ≈ 2,8 × SE (SE suy từ độ rộng CI khối-14) — xấp xỉ.

## 2. Mẫu

| | hạt 1 | hạt 2 |
|---|---|---|
| cơ hội trong vũ trụ | {S1['universe_entries']:,} | {S2['universe_entries']:,} |
| pool (qua downtrend/FK/consensus) | **{S1['pool']:,}** ({S1['pool_entries_per_day']:.1f}/ngày) | **{S2['pool']:,}** ({S2['pool_entries_per_day']:.1f}/ngày) |
| (a) tất cả (gồm MTM tại nến cuối) | {S1['pool']:,} | {S2['pool']:,} |
| trong đó chưa thoát hết dữ liệu (MTM, có cờ) | {S1['n_truncated']:,} ({S1['n_truncated']/S1['pool']*100:.1f}%) | {S2['n_truncated']:,} ({S2['n_truncated']/S2['pool']*100:.1f}%) |
| (b) đã thoát trước hết dữ liệu (TP hoặc timeout) | **{S1['n_resolved']:,}** | **{S2['n_resolved']:,}** |
| (c) đủ 60 ngày (vào ≤ 2026-08-11) | {S1['n_full60']:,} (12 ngày) | {S2['n_full60']:,} |
| mã đã chết cuối chuỗi | {S1['n_dead_end']} | {S2['n_dead_end']} |
| tỉ lệ G4 chặn | {S1['share_G4']*100:.1f}% | {S2['share_G4']*100:.1f}% |

**Cảnh báo tập (b):** chỉ giữ phiên đã thoát nghĩa là bỏ các phiên còn kẹt — đúng loại phiên thua nặng nhất — nên (b) thiên lệch lạc quan và ppdd nền tăng ({f(a1['baseline']['o']['ppdd'],1)} → {f(S1['subsets']['b_resolved']['baseline']['o']['ppdd'],1)}). Tập (c) chỉ ~12 ngày, ~500 phiên và ~70 phiên bị chặn: bootstrap suy biến, không dùng để kết luận.

## 3. G4 (quyết định) và G2 (hàng phụ, KHÔNG thuộc quyết định)

**G4 — RSI14 ngày ≥ 60** (lq/bq = cận lạc quan/bi quan; lift tính trên ppdd = $ ròng/$1000 vốn-ngày):
{g_table('G4')}

**G2 — pump24h ≥ 6% (phụ):**
{g_table('G2')}

G2 trong holdout **hại rõ ràng** (hạt 1: p = {a1['G2']['o']['p_within_day']:.4f}; loại KAITO vẫn {f(e1['G2']['o']['lift_pb'],2,True)}/{f(e1['G2']['p']['lift_pb'],2,True)}): coin pump ≥ 6% có ppdd {f(a1['G2']['o']['vetoed']['ppdd'],1)} so với {f(a1['G2']['o']['passed']['ppdd'],1)} của phần còn lại. Đây là chế độ tăng mạnh (xem §5), nơi động lượng thắng đảo chiều — ngược với 2024–2025 ở cửa sổ gốc. Cổng pump đổi dấu theo chế độ; G4 yếu hơn và không đổi dấu rõ ràng sau khi bỏ KAITO.

## 4. Bucket RSI ngày (holdout, nến đang hình thành)

Hạt 1, tất cả phiên:
{buckets(S1, 'buckets_rsiD')}

Hạt 1, chỉ phiên đã thoát (b):
{buckets(S1, 'buckets_rsiD_resolved')}

Hạt 2, tất cả phiên:
{buckets(S2, 'buckets_rsiD')}

* P(rung 2 trong 48h) tăng theo RSI từ nhóm 40–50 trở lên (hạt 1: 13% → 20% → 31% → 51%) — giống cửa sổ gốc; nhưng ở holdout rung 2 khớp KHÔNG kéo theo lỗ (thị trường đang lên, rung khớp rồi hồi) nên ppdd không giảm theo RSI.
* Nhóm 40–50 có ppdd bất thường thấp (0,58 hạt 1; 2,34 hạt 2) — do KAITO (phiên timeout −$420…−$460 mỗi phiên, xoá gần hết lãi của nhóm). Đây chính là điểm khiến lift G4 âm.

## 5. Bối cảnh thị trường

BTC trong cửa sổ vào lệnh (08-01 → 10-11): **{f(btc['pct'],1,True)}%** ({btc['start']:,.0f} → {btc['end']:,.0f}), theo tháng: 08 {f(btc['by_month_pct']['2026-08'],1,True)}%, 09 {f(btc['by_month_pct']['2026-09'],1,True)}%, 10 (đến 11/10) {f(btc['by_month_pct']['2026-10'],1,True)}%; sụt tối đa {f(btc['max_drawdown_pct'],1)}% — **một nhịp tăng gần như một chiều**, trái với cửa sổ gốc (BTC +112% / −7% / −29% theo năm, ppdd nền 1,7). Ppdd nền holdout {f(a1['baseline']['o']['ppdd'],1)} lạc quan / {f(a1['baseline']['p']['ppdd'],1)} bi quan — gấp ~4 lần — nên mọi nhóm đều lãi và hiệu ứng "tránh phiên xấu" ít có chỗ để hiện ra.

## 6. Quyền lực và giới hạn (rõ ràng)

* **71 ngày × ~54 phiên/ngày, nhưng phiên chồng nhau tới 60 ngày** → chỉ ~2–3 khối độc lập 30 ngày. SE lift G4 ≈ {f(a1['G4']['o']['se_lift_pb_day14'],2)} ppdd (hạt 1); MDE 80% ≈ {f(a1['G4']['o']['mde80_lift_pb'],1)}. Ngưỡng 95% của |lift lọt−chặn| dưới null hoán vị là {f(a1['G4']['o']['perm_min_detectable_lift_pv'],1)} ppdd. So với +0,15 (ngưỡng keep) và +0,33 (hiệu ứng gốc), mẫu **không thể xác nhận cũng không thể bác bỏ** hiệu ứng cỡ đó.
* ~10% phiên bị cắt ở cuối dữ liệu (MTM), tập trung ở tháng 9–10; tập (b) loại chúng nhưng thiên lệch.
* Hai hạt dùng chung một lịch sử giá và đều bị chi phối bởi cùng cụm KAITO.
* Chưa mô phỏng sàn tiền mặt, trần phiên, thứ hạng scanner; lift giả định chọn ngẫu nhiên trong pool.
* Phép loại KAITO và đổi khối bootstrap 30→14 là lựa chọn của người phân tích, ghi rõ; luật quyết định không bị đổi.
* Gợi ý bước sau (không thuộc luật): theo dõi G4 trên paper thật (`entry_rsi_max` ghi sẵn các phiên bị chặn) và đánh giá lại khi có ≥ 6 tháng dữ liệu ở nhiều chế độ; xét tách theo chế độ thị trường vì dấu của cổng pump đã đổi giữa hai cửa sổ.

Tệp: `holdout.py` (build/analyze), `build_holdout_report.py`, `results_holdout.json`, `cache_holdout_20261011.pkl`, `cache_holdout_20261012.pkl`, `run_holdout_*.log`.
"""
(STUDY / "holdout_report.md").write_text(TXT, encoding="utf-8")
print(len(TXT))
