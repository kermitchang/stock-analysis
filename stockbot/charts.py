"""伺服器端產生 SVG 圖表（不依賴外部 CDN，區網離線也能看）。顏色用 CSS 變數，自動支援深淺色。"""
from __future__ import annotations

import math
import re
from html import escape
from typing import Optional, Sequence


def _lab(d: str) -> str:
    return d[5:] if re.match(r"\d{4}-\d{2}-\d{2}$", d) else d


def _anchor(i: int, n: int) -> str:
    return "start" if i == 0 else "end" if i == n - 1 else "middle"


def _scale(vmin: float, vmax: float, a: float, b: float):
    span = (vmax - vmin) or 1.0
    return lambda v: b - (v - vmin) / span * (b - a)


def _nice_ticks(vmin: float, vmax: float, n: int = 4) -> list[float]:
    span = vmax - vmin
    if span <= 0:
        return [vmin]
    step = 10 ** math.floor(math.log10(span / n))
    for m in (1, 2, 2.5, 5, 10):
        if span / (step * m) <= n:
            step *= m
            break
    start = math.ceil(vmin / step) * step
    ticks, v = [], start
    while v <= vmax + 1e-9:
        ticks.append(round(v, 10))
        v += step
    return ticks


def price_chart(dates: Sequence[str], o, h, l, c, bands: Sequence[dict] = (), width: int = 760,
                height: int = 300) -> str:
    """K 線 + 預測區間。bands: [{"i": 目標日索引, "low":, "high":, "hit": bool|None, "variant":}]"""
    n = len(dates)
    if n == 0:
        return '<div class="empty">尚無行情資料</div>'
    pad_l, pad_r, pad_t, pad_b = 8, 52, 12, 24
    lows = [v for v in l if v is not None] + [b["low"] for b in bands]
    highs = [v for v in h if v is not None] + [b["high"] for b in bands]
    vmin, vmax = min(lows), max(highs)
    m = (vmax - vmin) * 0.05
    vmin, vmax = vmin - m, vmax + m
    y = _scale(vmin, vmax, pad_t, height - pad_b)
    step = (width - pad_l - pad_r) / n
    x = lambda i: pad_l + step * (i + 0.5)
    bw = max(1.5, step * 0.6)
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="股價走勢與預測區間">']
    for t in _nice_ticks(vmin, vmax):
        out.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="grid"/>'
                   f'<text x="{width - pad_r + 6}" y="{y(t) + 4:.1f}" class="axis">{t:g}</text>')
    for b in bands:
        cls = "band-hit" if b.get("hit") else "band-miss" if b.get("hit") is False else "band-open"
        out.append(f'<rect x="{x(b["i"]) - step * 0.5:.1f}" y="{y(b["high"]):.1f}" width="{step:.1f}" '
                   f'height="{max(1, y(b["low"]) - y(b["high"])):.1f}" class="{cls}">'
                   f'<title>{escape(b.get("title", ""))}</title></rect>')
    for i in range(n):
        if c[i] is None:
            continue
        oo = o[i] if o[i] is not None else c[i]
        hh = h[i] if h[i] is not None else max(oo, c[i])
        ll = l[i] if l[i] is not None else min(oo, c[i])
        up = c[i] >= oo
        cls = "up" if up else "down"
        top, bot = y(max(oo, c[i])), y(min(oo, c[i]))
        out.append(f'<line x1="{x(i):.1f}" x2="{x(i):.1f}" y1="{y(hh):.1f}" y2="{y(ll):.1f}" class="wick {cls}"/>'
                   f'<rect x="{x(i) - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{max(1, bot - top):.1f}" '
                   f'class="body {cls}"><title>{dates[i]} 開{oo} 高{hh} 低{ll} 收{c[i]}</title></rect>')
    for i in sorted({0, n // 3, 2 * n // 3, n - 1}):
        out.append(f'<text x="{x(i):.1f}" y="{height - 6}" class="axis" text-anchor="{_anchor(i, n)}">{_lab(dates[i])}</text>')
    out.append("</svg>")
    return "".join(out)


def line_chart(x_labels: Sequence[str], series: dict, width: int = 760, height: int = 260,
               y_fmt=lambda v: f"{v:.0%}", ref: Optional[float] = None) -> str:
    """多條折線。series: {名稱: (css class, [值或 None])}"""
    n = len(x_labels)
    vals = [v for _, ys in series.values() for v in ys if v is not None]
    if n < 2 or not vals:
        return '<div class="empty">資料累積中，至少需要幾個交易日的對帳結果</div>'
    pad_l, pad_r, pad_t, pad_b = 8, 52, 12, 24
    vmin, vmax = min(vals + ([ref] if ref is not None else [])), max(vals + ([ref] if ref is not None else []))
    m = (vmax - vmin) * 0.1 or 0.05
    vmin, vmax = vmin - m, vmax + m
    y = _scale(vmin, vmax, pad_t, height - pad_b)
    x = lambda i: pad_l + (width - pad_l - pad_r) * i / (n - 1)
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    for t in _nice_ticks(vmin, vmax):
        out.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="grid"/>'
                   f'<text x="{width - pad_r + 6}" y="{y(t) + 4:.1f}" class="axis">{y_fmt(t)}</text>')
    if ref is not None:
        out.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y(ref):.1f}" y2="{y(ref):.1f}" class="ref"/>')
    for name, (cls, ys) in series.items():
        pts, segs = [], []
        for i, v in enumerate(ys):
            if v is None:
                if pts:
                    segs.append(pts)
                pts = []
            else:
                pts.append(f"{x(i):.1f},{y(v):.1f}")
        if pts:
            segs.append(pts)
        for s in segs:
            out.append(f'<polyline points="{" ".join(s)}" class="line {cls}"><title>{escape(name)}</title></polyline>')
        last = next(((i, v) for i, v in reversed(list(enumerate(ys))) if v is not None), None)
        if last:
            out.append(f'<circle cx="{x(last[0]):.1f}" cy="{y(last[1]):.1f}" r="3.5" class="dot {cls}"/>')
    for i in sorted({0, n // 2, n - 1}):
        out.append(f'<text x="{x(i):.1f}" y="{height - 6}" class="axis" text-anchor="{_anchor(i, n)}">{_lab(x_labels[i])}</text>')
    out.append("</svg>")
    return "".join(out)


def bar_chart(labels: Sequence[str], values: Sequence[float], width: int = 760, height: int = 160,
              y_fmt=lambda v: f"${v:.2f}") -> str:
    if not values:
        return '<div class="empty">尚無資料</div>'
    pad_l, pad_r, pad_t, pad_b = 8, 52, 10, 22
    vmax = max(values) or 1
    y = _scale(0, vmax * 1.1, pad_t, height - pad_b)
    n = len(values)
    step = (width - pad_l - pad_r) / n
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    for t in _nice_ticks(0, vmax * 1.1, 3):
        out.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="grid"/>'
                   f'<text x="{width - pad_r + 6}" y="{y(t) + 4:.1f}" class="axis">{y_fmt(t)}</text>')
    for i, v in enumerate(values):
        out.append(f'<rect x="{pad_l + step * i + step * 0.15:.1f}" y="{y(v):.1f}" width="{step * 0.7:.1f}" '
                   f'height="{y(0) - y(v):.1f}" class="bar" rx="2"><title>{labels[i]} {y_fmt(v)}</title></rect>')
    for i in sorted({0, n - 1}):
        out.append(f'<text x="{pad_l + step * (i + 0.5):.1f}" y="{height - 5}" class="axis" text-anchor="{_anchor(i, n)}">{_lab(labels[i])}</text>')
    out.append("</svg>")
    return "".join(out)
