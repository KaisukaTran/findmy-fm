import json, sys
def fmt(x, d=2):
    return "-" if x is None else f"{x:,.{d}f}"
out = []
for lab, title in (("1h_s168", "1h (chính)"), ("1d_s168", "1d (phụ — không xác định được thứ tự high/low trong ngày)")):
    r = json.load(open(f"per_session_{lab}.json"))
    for b, bn in (("pess", "bi quan"), ("opt", "lạc quan")):
        out.append(f"**{title} — {bn}** (n = {r['stats']['avg|'+b]['n']:,} phiên, {r['n_coins']} coin)\n")
        out.append("| chỉ số | A avg | B entry | C max(avg,entry) |")
        out.append("|---|---|---|---|")
        rows = [("TP rate %", "tp_rate_pct", 1), ("mean net $/phiên", "mean_pnl_usd", 2), ("median net $/phiên", "median_pnl_usd", 2),
                ("mean giờ giữ → ngày giữ", "mean_days_all", 1), ("capital-days / phiên (mean)", None, 0),
                ("$ / $1000-capital-day", "usd_per_1000_capday", 3), ("còn mở ở timeout 60d (% phiên)", "deadline_share_pct", 2),
                ("  lỗ MTM TB phiên timeout (%)", "deadline_mean_pnl_pct", 1), ("  lỗ MTM TB phiên timeout ($)", "deadline_mean_pnl_usd", 1),
                ("hết dữ liệu coin (% phiên)", "data_end_share_pct", 2), ("  MTM TB (%)", "data_end_mean_pnl_pct", 1),
                ("$ vốn triển khai TB/phiên", "mean_deployed", 0)]
        for name, key, d in rows:
            cells = []
            for v in ("avg", "entry", "max"):
                s = r["stats"][f"{v}|{b}"]
                if key is None:
                    cells.append(fmt(s["capdays_total"] / s["n"], 0))
                else:
                    cells.append(fmt(s[key], d))
            out.append(f"| {name} | " + " | ".join(cells) + " |")
        out.append("")
        out.append("Chênh cặp (cluster-bootstrap theo coin, 95% CI):\n")
        out.append("| chỉ số | B−A | C−A |")
        out.append("|---|---|---|")
        for name, key, d in (("net $/phiên", "mean_pnl_usd", 2), ("$ / $1000-capday", "usd_per_1000_capday", 2), ("TP rate (điểm %)", "tp_rate_pct", 2),
                             ("tỉ lệ timeout (điểm %)", "deadline_share_pct", 2), ("ngày giữ TB", "mean_days_held", 2), ("capital-days/phiên", "mean_capdays_per_session", 0)):
            cells = []
            for v in ("entry", "max"):
                x = r["diff"][f"{v}-avg|{b}"][key]
                cells.append(f"{x['diff']:+,.{d}f} [{x['ci'][0]:+,.{d}f}; {x['ci'][1]:+,.{d}f}]")
            out.append(f"| {name} | " + " | ".join(cells) + " |")
        out.append("")
open("per_session_tables.md", "w", encoding="utf-8").write("\n".join(out))
print("\n".join(out))
