"""
Server-rendered SVG charts — zero JavaScript, CSP-perfect (everything is `self`).

Pure functions that turn numbers into small inline SVG strings, embedded in
templates with `{{ svg|safe }}`. Kept deliberately simple (lines + bars + a
pyramid ladder) — no dependency, no canvas, no eval.
"""

from __future__ import annotations

import html

_BLUE, _GREEN, _RED, _YELLOW, _MUTED = "#2f81f7", "#2ea043", "#e5534b", "#d29922", "#8b98a5"
_LINE = "#2a333d"


def _points(values: list[float], w: float, h: float, pad: float = 4.0) -> str:
    """Map a value series to an SVG polyline 'x,y ...' string."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(values)
    step = (w - 2 * pad) / max(n - 1, 1)
    pts = []
    for i, v in enumerate(values):
        x = pad + i * step
        y = h - pad - (v - lo) / span * (h - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def sparkline_svg(values: list[float], w: int = 140, h: int = 34, stroke: str = _BLUE) -> str:
    if not values:
        return f'<svg width="{w}" height="{h}"></svg>'
    color = _GREEN if values[-1] >= values[0] else _RED
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img">'
        f'<polyline fill="none" stroke="{color}" stroke-width="1.5" '
        f'points="{_points(values, w, h)}"/></svg>'
    )


def _time_label(iso: str) -> str:
    """ISO timestamp -> short axis label in the display zone (HH:MM, or MM-DD if date-only)."""
    from app.timefmt import to_local

    dt = to_local(iso)
    if dt is None:
        return (iso or "")[:10]
    return dt.strftime("%H:%M") if "T" in iso else dt.strftime("%m-%d")


def equity_curve_svg(
    values: list[float], times: list[str] | None = None, w: int = 860, h: int = 300
) -> str:
    """Large equity curve: gridlines, right-edge value ticks, time axis, area fill,
    plus a running-peak line and a shaded *drawdown band* (the underwater region
    between peak and equity). Zero JS — pure inline SVG."""
    if not values:
        return '<p class="muted">No equity history yet.</p>'
    # running peak → the drawdown (underwater) band is the gap below it
    peaks: list[float] = []
    pk = values[0]
    for v in values:
        pk = max(pk, v)
        peaks.append(pk)
    lo, hi = min(values), max(peaks)
    span = (hi - lo) or 1.0
    x0, x1, y0, y1 = 8, w - 72, 12, h - 24  # plot rect (room for ticks + time axis)
    n = len(values)
    xstep = (x1 - x0) / max(n - 1, 1)

    def px(i):
        return x0 + i * xstep

    def py(v):
        return y1 - (v - lo) / span * (y1 - y0)

    pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(values))
    peak_pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(peaks))
    color = _GREEN if values[-1] >= values[0] else _RED

    parts = [f'<svg width="100%" height="{h}" viewBox="0 0 {w} {h}" role="img">']
    # horizontal gridlines + value ticks (top→bottom = hi→lo)
    for i in range(5):
        gy = y0 + i * (y1 - y0) / 4
        gval = hi - i * span / 4
        parts.append(f'<line x1="{x0}" y1="{gy:.1f}" x2="{x1}" y2="{gy:.1f}" '
                     f'stroke="{_LINE}" stroke-width="0.5"/>')
        parts.append(f'<text x="{x1+4}" y="{gy+3:.1f}" fill="{_MUTED}" font-size="10">'
                     f'${gval:,.2f}</text>')
    # drawdown band: peak line forward, equity line back → shaded red (underwater)
    dd_back = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in reversed(list(enumerate(values))))
    parts.append(f'<polygon fill="{_RED}" fill-opacity="0.07" points="{peak_pts} {dd_back}"/>')
    # area fill under the equity line
    parts.append(f'<polygon fill="{color}" fill-opacity="0.10" '
                 f'points="{px(0):.1f},{y1:.1f} {pts} {px(n-1):.1f},{y1:.1f}"/>')
    # running-peak reference (muted dashed)
    parts.append(f'<polyline fill="none" stroke="{_MUTED}" stroke-width="1" '
                 f'stroke-dasharray="4 3" points="{peak_pts}"/>')
    # the equity line
    parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}"/>')
    # time axis labels (start / middle / end)
    if times and len(times) == n:
        for i in (0, n // 2, n - 1):
            anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
            parts.append(f'<text x="{px(i):.1f}" y="{h-6}" fill="{_MUTED}" font-size="10" '
                         f'text-anchor="{anchor}">{_time_label(times[i])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def winloss_bars_svg(wins: int, losses: int, w: int = 300, h: int = 110) -> str:
    total = wins + losses
    if total == 0:
        return '<p class="muted">No closed trades yet.</p>'
    win_w = (w - 8) * wins / total
    rate = wins / total * 100
    return (
        f'<svg width="100%" height="{h}" viewBox="0 0 {w} {h}" role="img">'
        f'<rect x="4" y="20" width="{w-8:.1f}" height="22" fill="{_RED}" rx="3"/>'
        f'<rect x="4" y="20" width="{win_w:.1f}" height="22" fill="{_GREEN}" rx="3"/>'
        f'<text x="4" y="14" fill="{_MUTED}" font-size="11">win-rate {rate:.0f}%</text>'
        f'<text x="4" y="62" fill="{_GREEN}" font-size="12">{wins} win</text>'
        f'<text x="{w-4}" y="62" fill="{_RED}" font-size="12" text-anchor="end">{losses} loss</text>'
        f"</svg>"
    )


def opus_hourly_pnl_svg(labels: list[str], net_values: list[float], w: int = 520, h: int = 150) -> str:
    """Net P/L per auto-running hour: green/red bars around a zero baseline (requirement #2)."""
    if not net_values:
        return '<p class="muted">Chưa có dữ liệu giờ nào.</p>'
    hi = max(net_values + [0.0])
    lo = min(net_values + [0.0])
    span = (hi - lo) or 1.0
    x0, x1, y0, y1 = 8, w - 60, 12, h - 20
    n = len(net_values)
    bw = (x1 - x0) / max(n, 1)

    def py(v: float) -> float:
        return y1 - (v - lo) / span * (y1 - y0)

    zero_y = py(0.0)
    parts = [f'<svg width="100%" height="{h}" viewBox="0 0 {w} {h}" role="img">']
    # value ticks (top/zero/bottom)
    for gy, gval in ((y0, hi), (zero_y, 0.0), (y1, lo)):
        parts.append(f'<line x1="{x0}" y1="{gy:.1f}" x2="{x1}" y2="{gy:.1f}" stroke="{_LINE}" stroke-width="0.5"/>')
        parts.append(f'<text x="{x1+4}" y="{gy+3:.1f}" fill="{_MUTED}" font-size="10">${gval:,.2f}</text>')
    for i, v in enumerate(net_values):
        bx = x0 + i * bw
        top = py(max(v, 0.0))
        bot = py(min(v, 0.0))
        col = _GREEN if v >= 0 else _RED
        parts.append(f'<rect x="{bx+1:.1f}" y="{top:.1f}" width="{max(bw-2,1):.1f}" height="{max(bot-top,0.5):.1f}" fill="{col}" rx="1"/>')
    if labels and len(labels) == n:
        for i in (0, n // 2, n - 1):
            anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
            parts.append(f'<text x="{x0+i*bw+bw/2:.1f}" y="{h-5}" fill="{_MUTED}" font-size="10" text-anchor="{anchor}">{_time_label(labels[i])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def opus_cumulative_vs_target_svg(
    net_values: list[float], target_per_hour: float, w: int = 520, h: int = 150
) -> str:
    """Cumulative net profit (line) vs the linear KPI target (dashed) — pace at a glance."""
    if not net_values:
        return '<p class="muted">Chưa có dữ liệu lũy kế.</p>'
    cum, s = [], 0.0
    for v in net_values:
        s += v
        cum.append(s)
    n = len(cum)
    target = [(i + 1) * target_per_hour for i in range(n)]
    allv = cum + target + [0.0]
    lo, hi = min(allv), max(allv)
    span = (hi - lo) or 1.0
    x0, x1, y0, y1 = 8, w - 60, 12, h - 20
    xstep = (x1 - x0) / max(n - 1, 1)

    def px(i):
        return x0 + i * xstep

    def py(v):
        return y1 - (v - lo) / span * (y1 - y0)

    cum_pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(cum))
    tgt_pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(target))
    color = _GREEN if cum[-1] >= 0 else _RED
    parts = [f'<svg width="100%" height="{h}" viewBox="0 0 {w} {h}" role="img">']
    for gy, gval in ((y0, hi), (py(0.0), 0.0), (y1, lo)):
        parts.append(f'<line x1="{x0}" y1="{gy:.1f}" x2="{x1}" y2="{gy:.1f}" stroke="{_LINE}" stroke-width="0.5"/>')
        parts.append(f'<text x="{x1+4}" y="{gy+3:.1f}" fill="{_MUTED}" font-size="10">${gval:,.2f}</text>')
    parts.append(f'<polyline fill="none" stroke="{_MUTED}" stroke-width="1.2" stroke-dasharray="5 3" points="{tgt_pts}"/>')
    parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{cum_pts}"/>')
    parts.append(f'<text x="{x0+2}" y="{y0+10}" fill="{_MUTED}" font-size="10">— mục tiêu · </text>')
    parts.append(f'<text x="{x0+70}" y="{y0+10}" fill="{color}" font-size="10">lũy kế net</text>')
    parts.append("</svg>")
    return "".join(parts)


_ORANGE = "#f0883e"


def price_ladder_svg(status: dict, w: int = 460, h: int = 300) -> str:
    """
    Large, labelled price ladder for the click-to-view modal: horizontal reference lines
    for buy(avg), current, next DCA wave, take-profit and stop-loss, plus the wave rungs.
    """
    avg = status.get("avg_price") or status.get("entry_price") or 0.0
    markers = [
        ("Giá mua TB", status.get("avg_price") or status.get("entry_price") or 0.0, _YELLOW),
        ("Giá hiện tại", status.get("current_price") or 0.0, _BLUE),
        ("Sóng kế tiếp", status.get("next_wave_price") or 0.0, _ORANGE),
        ("Chốt lời (TP)", status.get("estimated_tp_price") or 0.0, _GREEN),
        ("Cắt lỗ (SL)", status.get("sl_price") or 0.0, _RED),
    ]
    markers = [(name, float(p), col) for name, p, col in markers if p and p > 0]
    if not markers:
        return '<p class="muted">Chưa có dữ liệu giá.</p>'
    waves = [wv for wv in (status.get("waves") or []) if wv.get("target_price")]
    allp = [p for _, p, _ in markers] + [wv["target_price"] for wv in waves]
    lo, hi = min(allp), max(allp)
    pad_v = (hi - lo) * 0.08 or hi * 0.02 or 1.0
    lo -= pad_v
    hi += pad_v
    span = (hi - lo) or 1.0
    x0, x1, y0, y1 = 10, w - 170, 14, h - 14

    def y(p: float) -> float:
        return y1 - (p - lo) / span * (y1 - y0)

    parts = [f'<svg width="100%" height="{h}" viewBox="0 0 {w} {h}" role="img">']
    # wave rungs (context): filled green, pending grey
    for wv in waves:
        col = _GREEN if wv.get("status") == "filled" else _MUTED
        yy = y(wv["target_price"])
        parts.append(f'<line x1="{x0}" y1="{yy:.1f}" x2="{x1}" y2="{yy:.1f}" stroke="{col}" '
                     f'stroke-width="1" stroke-opacity="0.45"/>')
    # the 5 key reference lines + labels (sorted high→low so labels stack cleanly)
    for name, price, col in sorted(markers, key=lambda m: m[1], reverse=True):
        yy = y(price)
        pct = (price - avg) / avg * 100 if avg > 0 else 0.0
        parts.append(f'<line x1="{x0}" y1="{yy:.1f}" x2="{x1}" y2="{yy:.1f}" stroke="{col}" stroke-width="2"/>')
        parts.append(f'<circle cx="{x1:.1f}" cy="{yy:.1f}" r="3.5" fill="{col}"/>')
        parts.append(
            f'<text x="{x1+8}" y="{yy-2:.1f}" fill="{col}" font-size="12" font-weight="600">'
            f'{html.escape(name)}</text>'
            f'<text x="{x1+8}" y="{yy+11:.1f}" fill="{_MUTED}" font-size="11">'
            f'{price:,.6g} ({pct:+.1f}%)</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def pyramid_ladder_svg(status: dict, w: int = 240, h: int = 120) -> str:
    """Horizontal lines: wave targets (red), avg (yellow), TP (green), current (blue)."""
    waves = status.get("waves") or []
    prices = [wv["target_price"] for wv in waves] or [status.get("entry_price", 0)]
    extra = [p for p in (status.get("avg_price"), status.get("estimated_tp_price"),
                         status.get("current_price")) if p]
    allp = [p for p in prices + extra if p]
    if not allp:
        return '<p class="muted">No waves.</p>'
    lo, hi = min(allp), max(allp)
    span = (hi - lo) or 1.0

    def y(p):
        return h - 6 - (p - lo) / span * (h - 12)

    parts = [f'<svg width="100%" height="{h}" viewBox="0 0 {w} {h}" role="img">']
    for wv in waves:
        col = _GREEN if wv["status"] == "filled" else _RED
        yy = y(wv["target_price"])
        parts.append(f'<line x1="4" y1="{yy:.1f}" x2="{w-60}" y2="{yy:.1f}" stroke="{col}" stroke-width="1"/>')
    for label, val, col in (("avg", status.get("avg_price"), _YELLOW),
                            ("tp", status.get("estimated_tp_price"), _GREEN),
                            ("now", status.get("current_price"), _BLUE)):
        if val:
            yy = y(val)
            parts.append(
                f'<line x1="4" y1="{yy:.1f}" x2="{w-60}" y2="{yy:.1f}" stroke="{col}" '
                f'stroke-width="1.5" stroke-dasharray="4 2"/>'
                f'<text x="{w-56}" y="{yy+3:.1f}" fill="{col}" font-size="10">{html.escape(label)} {val:,.2f}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)
