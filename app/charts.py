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
    """ISO timestamp -> short axis label (HH:MM, or MM-DD if no time part)."""
    if "T" in iso:
        return iso[11:16]
    return iso[5:10] or iso[:10]


def equity_curve_svg(
    values: list[float], times: list[str] | None = None, w: int = 520, h: int = 180
) -> str:
    """Professional equity curve: gridlines, right-edge value ticks, time axis, area fill."""
    if not values:
        return '<p class="muted">No equity history yet.</p>'
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    x0, x1, y0, y1 = 8, w - 66, 12, h - 22  # plot rect (room for ticks + time axis)
    n = len(values)
    xstep = (x1 - x0) / max(n - 1, 1)

    def px(i):
        return x0 + i * xstep

    def py(v):
        return y1 - (v - lo) / span * (y1 - y0)

    pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(values))
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
    # area fill under the line
    parts.append(f'<polygon fill="{color}" fill-opacity="0.08" '
                 f'points="{px(0):.1f},{y1:.1f} {pts} {px(n-1):.1f},{y1:.1f}"/>')
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


def winloss_bars_svg(wins: int, losses: int, w: int = 220, h: int = 90) -> str:
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
