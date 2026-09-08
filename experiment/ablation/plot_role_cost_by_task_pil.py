import argparse
import json
import os
from typing import Dict, List

from PIL import Image, ImageDraw, ImageFont


def _load_font(size: int):
    for name in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf"]:
        if os.path.exists(name):
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="experiment/role_costs.json")
    ap.add_argument("--output", default="experiment/role_costs_by_task.png")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    overall = (data or {}).get("overall") or {}
    scenarios: List[str] = overall.get("scenarios") or []
    roles: List[str] = overall.get("role_order") or []
    matrix: Dict[str, Dict[str, int]] = overall.get("scenario_role_matrix") or {}

    if not scenarios or not roles or not matrix:
        raise SystemExit("Missing scenario-role matrix in role_costs.json")

    # canvas
    W, H = 1400, 780
    M_LEFT, M_RIGHT, M_TOP, M_BOTTOM = 110, 40, 80, 150
    chart_w = W - M_LEFT - M_RIGHT
    chart_h = H - M_TOP - M_BOTTOM

    bg = (255, 255, 255)
    axis = (40, 40, 40)
    grid = (220, 220, 220)
    colors = {
        "simple": (52, 152, 219),
        "medium": (46, 204, 113),
        "complex": (231, 76, 60),
    }

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    font_title = _load_font(28)
    font_axis = _load_font(18)
    font_tick = _load_font(14)
    font_legend = _load_font(15)

    # y scale
    max_v = 0
    for sc in scenarios:
        for role in roles:
            max_v = max(max_v, int((matrix.get(sc) or {}).get(role, 0)))
    if max_v <= 0:
        max_v = 1

    # axes
    x0, y0 = M_LEFT, M_TOP + chart_h
    x1, y1 = M_LEFT + chart_w, M_TOP
    d.line([(x0, y0), (x1, y0)], fill=axis, width=2)
    d.line([(x0, y0), (x0, y1)], fill=axis, width=2)

    # grid + y ticks
    n_ticks = 6
    for i in range(n_ticks + 1):
        y = M_TOP + chart_h - int(chart_h * i / n_ticks)
        d.line([(x0, y), (x1, y)], fill=grid, width=1)
        val = int(max_v * i / n_ticks)
        txt = f"{val}"
        tw = d.textlength(txt, font=font_tick)
        d.text((x0 - tw - 8, y - 8), txt, fill=axis, font=font_tick)

    # grouped bars
    n_roles = len(roles)
    n_sc = len(scenarios)
    group_w = chart_w / max(1, n_roles)
    bar_w = max(8, int(group_w * 0.7 / max(1, n_sc)))
    group_pad = (group_w - bar_w * n_sc) / 2

    for ri, role in enumerate(roles):
        gx = M_LEFT + ri * group_w
        for si, sc in enumerate(scenarios):
            v = int((matrix.get(sc) or {}).get(role, 0))
            bh = int((v / max_v) * chart_h)
            bx0 = int(gx + group_pad + si * bar_w)
            bx1 = bx0 + bar_w - 1
            by0 = y0 - bh
            by1 = y0
            d.rectangle([bx0, by0, bx1, by1], fill=colors.get(sc, (120, 120, 120)), outline=None)

        # x label
        lbl = role
        tw = d.textlength(lbl, font=font_tick)
        lx = int(gx + group_w / 2 - tw / 2)
        d.text((lx, y0 + 12), lbl, fill=axis, font=font_tick)

    # title
    title = "Role Token Cost by Task Complexity"
    tw = d.textlength(title, font=font_title)
    d.text(((W - tw) / 2, 24), title, fill=axis, font=font_title)

    # legend
    lg_x = W - 360
    lg_y = 24
    d.text((lg_x, lg_y), "Task", fill=axis, font=font_legend)
    off = 55
    for sc in scenarios:
        c = colors.get(sc, (120, 120, 120))
        d.rectangle([lg_x + off, lg_y + 2, lg_x + off + 18, lg_y + 20], fill=c)
        d.text((lg_x + off + 24, lg_y), sc, fill=axis, font=font_legend)
        off += 110

    # axis labels
    ylab = "Total Tokens"
    d.text((20, M_TOP - 8), ylab, fill=axis, font=font_axis)

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    img.save(args.output)
    print(f"[*] Saved chart: {args.output}")


if __name__ == "__main__":
    main()
