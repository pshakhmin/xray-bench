#!/usr/bin/env python3
"""Generate the static SVG charts used by the READMEs.

    python3 charts/make_charts.py

Reads charts/data.json and writes four SVGs next to it. Python standard library
only. Output is deterministic: no timestamps, no random data, fixed float
formatting.

Charts are transparent, mid-tone and self-contained so they read on both
GitHub light (#ffffff) and dark (#0d1117) pages. All copy is language-neutral
(English or symbols) because one set serves README.md and README.en.md.
"""

import json
import os
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "data.json")

FONT = "-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"

# Dual-theme text grey. A single fill cannot reach 4.5:1 on both #ffffff and
# #0d1117 (see the derivation below); this hex sits at the mathematical
# optimum, 4.35:1 on each, so every text role clears the same bar.
#
#   white ratio = 1.05 / (L + 0.05)        -> needs L <= 0.1833 for 4.5
#   dark  ratio = (L + 0.05) / 0.05548     -> needs L >= 0.1997 for 4.5
#
# The two windows do not overlap. Equal ratios at L = 0.1914 give 4.35 both.
TEXT = "#747987"
# Non-text strokes (box edges, arrows, whiskers' support lines): 3.6:1 on
# white, 5.2:1 on dark, i.e. above the 3:1 bar for meaningful graphics.
LINE = "#7f8999"
GRID = "#8a94a4"
GRID_OPACITY = 0.30

BAR_RADIUS = 3

# Engine -> colour for every chart. The two Unbihex builds share the amber
# family (dark = stock, bright = PR) so they read as one engine, two builds.
COLOR = {
    "xray": "#0e7490",
    "singbox": "#7c3aed",
    "unbihex_stock": "#b45309",
    "unbihex_pr": "#d97706",
}


def tint(hex_color, white=0.42):
    """Mix a colour towards white. Deterministic, no theme guessing."""
    r = int(round(int(hex_color[1:3], 16) * (1 - white) + 255 * white))
    g = int(round(int(hex_color[3:5], 16) * (1 - white) + 255 * white))
    b = int(round(int(hex_color[5:7], 16) * (1 - white) + 255 * white))
    return f"#{r:02x}{g:02x}{b:02x}"


# Secondary series fill: same hue, solid, ~42% towards white. Reads as a
# lighter solid on dark and as a pastel block with a crisp edge on light.
TINT = {key: tint(value) for key, value in COLOR.items()}
TINT_LEGEND = tint(LINE)

WIDTH = 880
FOOTER = "Medians · loopback, 16 cores · netem on the tunnel port only"


# --------------------------------------------------------------------------
# small formatting helpers


def num(value):
    """Fixed 2-decimal coordinate, trailing zeros trimmed, never empty."""
    text = f"{value:.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def f0(value):
    return f"{value:.0f}"


def f1(value):
    return f"{value:.1f}"


def f2(value):
    return f"{value:.2f}"


def esc(value):
    return escape(str(value))


def text(x, y, content, size=10, fill=TEXT, anchor="start", weight=400, transform=None):
    attrs = [
        f'x="{num(x)}"',
        f'y="{num(y)}"',
        f'font-size="{num(size)}"',
        f'fill="{fill}"',
    ]
    if anchor != "start":
        attrs.append(f'text-anchor="{anchor}"')
    if weight != 400:
        attrs.append(f'font-weight="{weight}"')
    if transform:
        attrs.append(f'transform="{transform}"')
    return f'<text {" ".join(attrs)}>{esc(content)}</text>'


def line(x1, y1, x2, y2, stroke, width=1, dash=None, opacity=None, cap="butt"):
    attrs = [
        f'x1="{num(x1)}"',
        f'y1="{num(y1)}"',
        f'x2="{num(x2)}"',
        f'y2="{num(y2)}"',
        f'stroke="{stroke}"',
        f'stroke-width="{num(width)}"',
    ]
    if dash:
        attrs.append(f'stroke-dasharray="{dash}"')
    if opacity is not None:
        attrs.append(f'stroke-opacity="{num(opacity)}"')
    if cap != "butt":
        attrs.append(f'stroke-linecap="{cap}"')
    return f'<line {" ".join(attrs)}/>'


def bar(x, y, w, h, fill, fill_opacity=None, stroke=None, stroke_width=1.5):
    if h <= 0.05:
        return ""
    radius = min(BAR_RADIUS, w / 2.0, h / 2.0)
    attrs = [
        f'x="{num(x)}"',
        f'y="{num(y)}"',
        f'width="{num(w)}"',
        f'height="{num(h)}"',
        f'rx="{num(radius)}"',
        f'fill="{fill}"',
    ]
    if fill_opacity is not None:
        attrs.append(f'fill-opacity="{num(fill_opacity)}"')
    if stroke:
        attrs.extend([f'stroke="{stroke}"', f'stroke-width="{num(stroke_width)}"'])
    return f'<rect {" ".join(attrs)}/>'


def arrow_head(x, y, direction=1, size=4.5, fill=LINE):
    """Small triangle pointing right (direction=1) or down (direction=-1)."""
    if direction == 1:
        points = [(x, y), (x - 2 * size, y - size), (x - 2 * size, y + size)]
    else:
        points = [(x, y), (x - size, y - 2 * size), (x + size, y - 2 * size)]
    pts = " ".join(f"{num(px)},{num(py)}" for px, py in points)
    return f'<polygon points="{pts}" fill="{fill}"/>'


def start_svg(height, title, desc):
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" '
        f'width="100%" role="img" aria-labelledby="chart-title chart-desc" '
        f'font-family="{FONT}">',
        f'<title id="chart-title">{esc(title)}</title>',
        f'<desc id="chart-desc">{esc(desc)}</desc>',
    ]


def finish_svg(lines):
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def header(lines, title, subtitle):
    lines.append(text(32, 38, title, 19, TEXT, weight=650))
    lines.append(text(32, 58, subtitle, 11.5, TEXT))


def y_axis(lines, x0, x1, top, base, vmax, step, unit):
    lines.append(text(x0 - 8, top - 9, unit, 10, TEXT, "end", 500))
    value = 0
    while value <= vmax + 1e-9:
        y = base - (value / vmax) * (base - top)
        if value == 0:
            lines.append(line(x0, y, x1, y, LINE, 1))
        else:
            lines.append(line(x0, y, x1, y, GRID, 1, opacity=GRID_OPACITY))
        lines.append(text(x0 - 8, y + 3.4, str(int(value)), 10, TEXT, "end"))
        value += step


# --------------------------------------------------------------------------
# 1. throughput at RTT ~ 0


def chart_throughput_rtt0(data):
    engines = data["engines"]
    series = [("down1", "↓1"), ("down8", "↓8"), ("up1", "↑1")]
    left, right, top, base = 64, 848, 104, 360
    vmax, step = 1400, 200
    group_w = (right - left) / len(engines)
    bar_w, gap = 34, 6
    cluster = len(series) * bar_w + (len(series) - 1) * gap
    offset = (group_w - cluster) / 2

    desc = (
        "Grouped bar chart of median throughput in MB/s at RTT close to zero for "
        "Xray 26.10.29, sing-box 1.14.4, Unbihex 0.2.2 (stock) and Unbihex 0.2.2 + "
        "PR #18. Each engine shows single-stream download, eight-stream download "
        "and single-stream upload. sing-box 1.14.4 leads all three metrics "
        "(5310, 1306.4 and 529.7 MB/s). Unbihex stock and PR are the same 0.2.2 "
        "build; the PR is ahead of stock on eight-stream download and close to it "
        "on single-stream download and upload."
    )
    lines = start_svg(440, "Throughput at RTT ≈ 0", desc)
    header(
        lines,
        "Throughput at RTT ≈ 0",
        "Median MB/s through SOCKS · higher is better",
    )
    y_axis(lines, left, right, top, base, vmax, step, "MB/s")

    for index, engine in enumerate(engines):
        gx = left + index * group_w
        color = COLOR[engine["id"]]
        for slot, (key, glyph) in enumerate(series):
            value = data["rtt0"][engine["id"]][key]
            height = (value / vmax) * (base - top)
            x = gx + offset + slot * (bar_w + gap)
            y = base - height
            lines.append(bar(x, y, bar_w, height, color))
            lines.append(text(x + bar_w / 2, y - 6, f1(value), 10, TEXT, "middle", 600))
            lines.append(text(x + bar_w / 2, base + 18, glyph, 10, TEXT, "middle", 500))
        lines.append(text(gx + group_w / 2, base + 40, engine["label"], 11.5, TEXT, "middle", 600))

    lines.append(text(32, 426, FOOTER, 10, TEXT))
    return finish_svg(lines)


# --------------------------------------------------------------------------
# 2. single-stream download, RTT ~ 0 vs 50 ms


def chart_throughput_rtt50(data):
    engines = data["engines"]
    left, right, top, base = 64, 848, 104, 360
    vmax, step = 600, 100
    group_w = (right - left) / len(engines)
    bar_w, gap = 52, 12
    cluster = 2 * bar_w + gap
    offset = (group_w - cluster) / 2

    desc = (
        "Single-stream download throughput in MB/s at RTT close to zero and at "
        "50 ms of emulated RTT. Xray falls from 360.6 to 88.6, sing-box from "
        "5310 to 103.6, Unbihex 0.2.2 + PR #18 from 430.3 to 85.8 MB/s. "
        "Unbihex 0.2.2 (stock) collapses from 437.5 to 5.8 MB/s, a 75 times drop, "
        "while the other three engines stay between 85.8 and 103.6 MB/s."
    )
    lines = start_svg(456, "Single-stream download: RTT ≈ 0 vs 50 ms", desc)
    header(
        lines,
        "Single-stream download: RTT ≈ 0 vs 50 ms",
        "↓1 median MB/s · higher is better",
    )
    y_axis(lines, left, right, top, base, vmax, step, "MB/s")

    # Legend: inline swatch + label on one baseline, right-aligned to the plot
    # edge. One neutral slate family, solid vs its 42% tint - the same fill
    # treatment as the bars, without implying any one engine.
    lines.append(f'<rect x="700" y="78.5" width="11" height="11" rx="2.5" fill="{LINE}"/>')
    lines.append(text(717, 88, "RTT ≈ 0", 10.5, TEXT))
    lines.append(
        f'<rect x="781" y="78.5" width="11" height="11" rx="2.5" fill="{TINT_LEGEND}" '
        f'stroke="{LINE}" stroke-width="1"/>'
    )
    lines.append(text(798, 88, "RTT 50 ms", 10.5, TEXT))

    stock_x = None
    stock_left = None
    stock_top = None
    drop_y = None
    for index, engine in enumerate(engines):
        gx = left + index * group_w
        color = COLOR[engine["id"]]
        zero = data["rtt0"][engine["id"]]["down1"]
        loaded = data["rtt50"][engine["id"]]["down1"]
        for slot, (value, filled) in enumerate(((zero, False), (loaded, True))):
            height = (value / vmax) * (base - top)
            x = gx + offset + slot * (bar_w + gap)
            y = base - height
            if filled:
                lines.append(
                    bar(x, y, bar_w, height, TINT[engine["id"]], stroke=color, stroke_width=1.2)
                )
            else:
                lines.append(bar(x, y, bar_w, height, color))
            lines.append(text(x + bar_w / 2, y - 6, f1(value), 10, TEXT, "middle", 600))
        lines.append(text(gx + group_w / 2, base + 40, engine["label"], 11.5, TEXT, "middle", 600))
        if engine["id"] == "unbihex_stock":
            stock_x = gx + offset
            stock_left = gx + offset + bar_w + gap
            stock_top = base - (zero / vmax) * (base - top)
            drop_y = base - (loaded / vmax) * (base - top)

    # Honest 0-based axis plus an explicit drop marker for the stock group.
    stock_zero = data["rtt0"]["unbihex_stock"]["down1"]
    stock_loaded = data["rtt50"]["unbihex_stock"]["down1"]
    factor = stock_zero / stock_loaded
    marker_x = stock_x - 12
    lines.append(line(stock_x, stock_top, marker_x, stock_top, LINE, 1.1, dash="3 3"))
    lines.append(line(marker_x, stock_top, marker_x, drop_y, LINE, 1.1, dash="3 3"))
    lines.append(line(marker_x, drop_y, stock_left - 9, drop_y, LINE, 1.1, dash="3 3"))
    lines.append(arrow_head(stock_left - 3, drop_y, direction=1, size=4, fill=LINE))
    # Horizontal callout, clear of gridlines, directly beside the tiny bar.
    lines.append(
        text(
            stock_left + bar_w + 8,
            drop_y - 5,
            f"{f0(factor)}× drop",
            10,
            TEXT,
            "start",
            600,
        )
    )

    lines.append(
        text(
            32,
            424,
            "At 8 streams and 50 ms, netem itself is the bottleneck, so only ↓1 is compared at 50 ms.",
            10,
            TEXT,
        )
    )
    lines.append(text(32, 440, "MB/s as reported by the bench · medians over the recorded runs.", 10, TEXT))
    return finish_svg(lines)


# --------------------------------------------------------------------------
# 3. TTFB and connection rate at RTT ~ 0


def chart_connections(data):
    engines = data["engines"]
    top, base = 136, 352
    panel_a, panel_b = 32, 452
    plot_a = (72, 424)
    plot_b = (492, 844)
    slot = 88
    bar_w = 40
    vmax_a, step_a = 12, 2
    vmax_b, step_b = 1000, 200

    desc = (
        "Two panels at RTT close to zero. Left: time to first byte in ms, median "
        "bar and p95 whisker, lower is better. Right: connections per second, "
        "higher is better. Unbihex 0.2.2 + PR #18 has the lowest TTFB at 1.98 ms "
        "with a p95 of 8.08 ms and the highest connection rate at 938 conn/s. "
        "Unbihex 0.2.2 (stock) has the highest p95 at 11.59 ms and the lowest "
        "connection rate at 276 conn/s. Xray: 3.95 ms median, 4.97 ms p95, "
        "448 conn/s. sing-box: 3.34 ms median, 4.03 ms p95, 563 conn/s."
    )
    lines = start_svg(428, "Latency and connection rate at RTT ≈ 0", desc)
    header(
        lines,
        "Latency and connection rate at RTT ≈ 0",
        "Medians over the recorded runs · TTFB lower is better · conn/s higher is better",
    )

    # panel A ---------------------------------------------------------------
    lines.append(text(panel_a, 92, "Time to first byte", 12.5, TEXT, weight=650))
    lines.append(text(panel_a, 110, "ms · bar = median, whisker = p95 · lower is better", 10, TEXT))
    y_axis(lines, plot_a[0], plot_a[1], top, base, vmax_a, step_a, "ms")

    for index, engine in enumerate(engines):
        values = data["rtt0"][engine["id"]]
        median, p95 = values["ttfb_median"], values["ttfb_p95"]
        cx = plot_a[0] + 44 + index * slot
        height = (median / vmax_a) * (base - top)
        y_top = base - height
        y_p95 = base - (p95 / vmax_a) * (base - top)
        color = COLOR[engine["id"]]
        lines.append(bar(cx - bar_w / 2, y_top, bar_w, height, color))
        # Median sits above the bar's left shoulder, clear of the shaft and the
        # p95 cap (cap spans cx +/- 10, label ends 6 px further left).
        lines.append(text(cx - 16, y_top - 7, f2(median), 10, TEXT, "end", 600))
        lines.append(line(cx, y_top, cx, y_p95, color, 1.4))
        lines.append(line(cx - 10, y_p95, cx + 10, y_p95, color, 1.4))
        lines.append(text(cx + 14, y_p95 + 3.2, f2(p95), 10, TEXT))

    # panel B ---------------------------------------------------------------
    lines.append(text(panel_b, 92, "Connection rate", 12.5, TEXT, weight=650))
    lines.append(text(panel_b, 110, "conn/s · bar = median · higher is better", 10, TEXT))
    y_axis(lines, plot_b[0], plot_b[1], top, base, vmax_b, step_b, "conn/s")

    for index, engine in enumerate(engines):
        value = data["rtt0"][engine["id"]]["conns_per_sec"]
        cx = plot_b[0] + 44 + index * slot
        height = (value / vmax_b) * (base - top)
        y_top = base - height
        lines.append(bar(cx - bar_w / 2, y_top, bar_w, height, COLOR[engine["id"]]))
        lines.append(text(cx, y_top - 6, f0(value), 10, TEXT, "middle", 600))

    # shared engine labels under both panels
    for index, engine in enumerate(engines):
        for plot in (plot_a, plot_b):
            cx = plot[0] + 44 + index * slot
            first, second = engine["label_lines"]
            lines.append(text(cx, 372, first, 10, TEXT, "middle", 600))
            lines.append(text(cx, 386, second, 10, TEXT, "middle", 400))

    lines.append(text(32, 414, FOOTER, 10, TEXT))
    return finish_svg(lines)


# --------------------------------------------------------------------------
# 4. topology


def chart_topology(data):
    desc = (
        "Diagram of the bench path. Bench client to engine client over SOCKS, "
        "engine client to engine server over REALITY/XHTTP with netem adding 25 ms "
        "each way on the tunnel port only, then engine server to the sink. The sink "
        "is a TEST-NET-3 address, 203.0.113.7 port 19000, on the loopback "
        "interface. The SOCKS handshake and the server to sink path carry no added "
        "delay, so TTFB is not inflated."
    )
    lines = start_svg(252, "Topology", desc)
    header(
        lines,
        "Topology",
        "Bench path and the only place where the emulated RTT is applied",
    )

    # Key first: the diagram then closes the artwork, so no dead band is left
    # under it. One neutral slate family, same as the rest of the set.
    lines.append(line(32, 78, 56, 78, LINE, 1.5, dash="4 3"))
    lines.append(
        text(
            66,
            82,
            "netem 25+25 ms · tunnel port only, both directions · RTT 50 ms (off at RTT ≈ 0)",
            10,
            TEXT,
        )
    )
    lines.append(line(32, 96, 56, 96, LINE, 1.5))
    lines.append(text(66, 100, "no added delay · SOCKS handshake, server → sink", 10, TEXT))
    lines.append(
        text(
            32,
            130,
            "Self-pair: same engine build on both ends · sink on TEST-NET-3 (203.0.113.7) on lo",
            10,
            TEXT,
        )
    )

    # loopback boundary, drawn tight around the path
    lines.append(text(848, 130, "lo · loopback · 16 cores", 10, TEXT, "end", 500))
    lines.append(
        f'<rect x="32" y="138" width="816" height="96" rx="12" fill="none" '
        f'stroke="{LINE}" stroke-opacity="0.55" stroke-width="1"/>'
    )

    boxes = [
        (58, 118, "bench client", "curl · 1–8 streams"),
        (240, 136, "engine client", "SOCKS in · tunnel out"),
        (486, 136, "engine server", "tunnel in · target out"),
        (692, 130, "sink", "203.0.113.7:19000"),
    ]
    box_top, box_h = 156, 60
    for x, width, title, subtitle in boxes:
        lines.append(
            f'<rect x="{num(x)}" y="{box_top}" width="{num(width)}" height="{box_h}" '
            f'rx="8" fill="none" stroke="{LINE}" stroke-opacity="0.95" stroke-width="1.2"/>'
        )
        lines.append(text(x + width / 2, 182, title, 11.5, TEXT, "middle", 650))
        lines.append(text(x + width / 2, 200, subtitle, 10, TEXT, "middle"))

    y = 186
    links = [
        (176, 240, "SOCKS", "no delay", False),
        (376, 486, "REALITY/XHTTP", "netem 25+25 ms", True),
        (622, 692, "TCP", "no delay", False),
    ]
    for x1, x2, above, below, delayed in links:
        dash = "4 3" if delayed else None
        lines.append(line(x1, y, x2 - 7, y, LINE, 1.5, dash=dash))
        lines.append(arrow_head(x2, y, fill=LINE))
        cx = (x1 + x2) / 2
        lines.append(text(cx, 172, above, 10, TEXT, "middle", 500))
        if delayed:
            lines.append(text(cx, 202, below, 10, TEXT, "middle", 600))
        else:
            lines.append(text(cx, 202, below, 10, TEXT, "middle"))

    return finish_svg(lines)


# --------------------------------------------------------------------------


def build_all(data):
    return {
        "throughput-rtt0.svg": chart_throughput_rtt0(data),
        "throughput-rtt50.svg": chart_throughput_rtt50(data),
        "connections.svg": chart_connections(data),
        "topology.svg": chart_topology(data),
    }


def main():
    with open(DATA_FILE, encoding="utf-8") as handle:
        data = json.load(handle)
    for name, content in build_all(data).items():
        path = os.path.join(HERE, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        print(f"wrote {os.path.relpath(path)}")


if __name__ == "__main__":
    main()
