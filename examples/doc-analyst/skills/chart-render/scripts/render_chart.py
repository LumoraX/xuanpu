"""断网沙盒内的数据可视化图表生成脚本（输出高清矢量 SVG + 可选 PNG 图片供视觉自检）。"""

from __future__ import annotations

import json
import sys
from html import escape
from pathlib import Path
from typing import Any


def render_svg_chart(spec: dict[str, Any]) -> str:
    """根据 JSON 数据规范确定性生成出版级柱状图/折线图 SVG（零外部依赖）。"""
    title = str(spec.get("title") or "数据图表")
    chart_type = str(spec.get("type") or "bar").lower()
    labels = [str(x) for x in (spec.get("labels") or [])]
    values = [float(v) for v in (spec.get("values") or [])]
    unit = str(spec.get("unit") or "")
    width, height = 680, 360
    pad_l, pad_r, pad_t, pad_b = 64, 28, 52, 56
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    max_val = max(values, default=1.0)
    if max_val <= 0:
        max_val = 1.0

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{pad_l}" y="30" font-family="system-ui,sans-serif" font-size="16" font-weight="700" fill="#0f172a">{escape(title)}</text>',
    ]
    # 水平网格线与纵轴刻度
    for step in range(5):
        y = pad_t + plot_h - (plot_h * step / 4)
        val_lbl = f"{max_val * step / 4:.1f}{unit}"
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="#e2e8f0" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" font-family="system-ui,sans-serif" font-size="11" fill="#64748b">{escape(val_lbl)}</text>')

    count = max(1, len(values))
    slot_w = plot_w / count
    points: list[tuple[float, float]] = []
    for idx, val in enumerate(values):
        lbl = labels[idx] if idx < len(labels) else f"#{idx + 1}"
        cx = pad_l + slot_w * idx + slot_w / 2
        bar_h = (max(0.0, val) / max_val) * plot_h
        y = pad_t + plot_h - bar_h
        points.append((cx, y))
        if chart_type != "line":
            bw = min(56.0, slot_w * 0.56)
            parts.append(f'<rect x="{cx - bw / 2:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bar_h:.1f}" rx="4" fill="#2563eb"/>')
        parts.append(f'<text x="{cx:.1f}" y="{y - 6:.1f}" text-anchor="middle" font-family="system-ui,sans-serif" font-size="11" fill="#1e293b">{val:g}{escape(unit)}</text>')
        parts.append(f'<text x="{cx:.1f}" y="{pad_t + plot_h + 20:.1f}" text-anchor="middle" font-family="system-ui,sans-serif" font-size="11.5" fill="#334155">{escape(lbl[:12])}</text>')

    if chart_type == "line" and len(points) >= 2:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        parts.append(f'<polyline fill="none" stroke="#2563eb" stroke-width="2.5" points="{pts}"/>')
        for x, y in points:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#2563eb"/>')

    parts.append("</svg>")
    return "\n".join(parts)


def render_png_chart(spec: dict[str, Any], dst: Path) -> bool:
    """使用 PIL 在断网环境下渲染 PNG 柱状图供管家视觉直读自检；不可用返回 False。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False

    values = [float(v) for v in (spec.get("values") or [])]
    width, height = 640, 320
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    max_val = max(values, default=1.0) or 1.0
    pad_l, pad_r, pad_t, pad_b = 50, 30, 40, 40
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    draw.line([(pad_l, pad_t + plot_h), (width - pad_r, pad_t + plot_h)], fill=(148, 163, 184), width=2)
    slot_w = plot_w / max(1, len(values))
    for idx, val in enumerate(values):
        bh = int((max(0.0, val) / max_val) * plot_h)
        cx = int(pad_l + slot_w * idx + slot_w / 2)
        bw = max(12, int(min(48, slot_w * 0.55)))
        draw.rectangle([cx - bw // 2, pad_t + plot_h - bh, cx + bw // 2, pad_t + plot_h], fill=(37, 99, 235))
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst, format="PNG")
    return True


def main(argv: list[str]) -> int:
    """命令行入口：``render_chart.py <spec.json> <out.svg> [out.png]``。"""
    if len(argv) < 3:
        print(json.dumps({"error": "请传入 spec.json 与输出 SVG 路径"}, ensure_ascii=False))
        return 1
    spec = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    svg_dst = Path(argv[2])
    svg_text = render_svg_chart(spec)
    svg_dst.parent.mkdir(parents=True, exist_ok=True)
    svg_dst.write_text(svg_text, encoding="utf-8")
    png_created = False
    if len(argv) >= 4:
        png_created = render_png_chart(spec, Path(argv[3]))
    print(json.dumps({"svg": str(svg_dst), "svg_bytes": len(svg_text.encode("utf-8")), "png_created": png_created}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
