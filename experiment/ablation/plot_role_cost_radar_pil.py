import argparse
import json
import math
import os
from typing import Dict, List, Tuple

from PIL import Image, ImageDraw, ImageFont


def _load_font(size: int):
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _point(cx: float, cy: float, r: float, angle: float) -> Tuple[float, float]:
    # angle in radians, 0 at top, clockwise
    return cx + r * math.cos(angle), cy + r * math.sin(angle)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="experiment/role_costs.json")
    ap.add_argument("--output", default="experiment/role_costs_radar.png")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    overall = (data or {}).get("overall") or {}
    scenarios: List[str] = overall.get("scenarios") or []
    roles: List[str] = overall.get("role_order") or []
    matrix: Dict[str, Dict[str, int]] = overall.get("scenario_role_matrix") or {}

    if not scenarios or not roles or not matrix:
        raise SystemExit("Missing scenario-role matrix in role_costs.json. Run analyze_role_costs.py first.")

    # Canvas setup
    W, H = 1100, 900
    cx, cy = W * 0.43, H * 0.52
    radius = min(W, H) * 0.30

    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    font_title = _load_font(30)
    font_axis = _load_font(18)
    font_tick = _load_font(14)
    font_legend = _load_font(16)

    axis_color = (70, 70, 70)
    grid_color = (220, 220, 220)
    colors = {
        "simple": (52, 152, 219),
        "medium": (46, 204, 113),
        "complex": (231, 76, 60),
    }

    n = len(roles)
    if n < 3:
        raise SystemExit("Need at least 3 roles to draw radar chart")

    # angles: start at top (-90°), clockwise
    angles = [(-math.pi / 2) + 2 * math.pi * i / n for i in range(n)]

    # global max for consistent scale
    vmax = 0
    for sc in scenarios:
        for role in roles:
            vmax = max(vmax, int((matrix.get(sc) or {}).get(role, 0)))
    if vmax <= 0:
        vmax = 1

    # grid rings
    levels = 5
    for lv in range(1, levels + 1):
        rr = radius * lv / levels
        pts = [_point(cx, cy, rr, a) for a in angles]
        d.polygon(pts, outline=grid_color)
        # tick label near top axis
        tick_val = int(vmax * lv / levels)
        tx, ty = _point(cx, cy, rr, -math.pi / 2)
        d.text((tx + 6, ty - 8), str(tick_val), fill=(120, 120, 120), font=font_tick)

    # axis lines + role labels
    for role, a in zip(roles, angles):
        x, y = _point(cx, cy, radius, a)
        d.line([(cx, cy), (x, y)], fill=axis_color, width=2)

        lx, ly = _point(cx, cy, radius + 42, a)
        label = role
        tw = d.textlength(label, font=font_axis)
        d.text((lx - tw / 2, ly - 10), label, fill=axis_color, font=font_axis)

    # plot scenario polygons
    for sc in scenarios:
        color = colors.get(sc, (120, 120, 120))
        vals = [int((matrix.get(sc) or {}).get(role, 0)) for role in roles]
        pts = []
        for v, a in zip(vals, angles):
            rr = radius * (v / vmax)
            pts.append(_point(cx, cy, rr, a))

        # fill with transparency on RGBA overlay
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.polygon(pts, fill=(color[0], color[1], color[2], 70), outline=(color[0], color[1], color[2], 255), width=3)
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        d = ImageDraw.Draw(img)

    # title
    title = "Role Token Cost Radar (Simple vs Medium vs Complex)"
    tw = d.textlength(title, font=font_title)
    d.text(((W - tw) / 2, 28), title, fill=(40, 40, 40), font=font_title)

    # legend
    lgx, lgy = int(W * 0.78), int(H * 0.24)
    d.text((lgx, lgy - 28), "Task", fill=(40, 40, 40), font=font_legend)
    yoff = 0
    for sc in scenarios:
        c = colors.get(sc, (120, 120, 120))
        d.rectangle([lgx, lgy + yoff, lgx + 20, lgy + yoff + 20], fill=c)
        d.text((lgx + 28, lgy + yoff + 1), sc, fill=(40, 40, 40), font=font_legend)
        yoff += 34

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    img.save(args.output)
    print(f"[*] Saved radar chart: {args.output}")


if __name__ == "__main__":
    main()
