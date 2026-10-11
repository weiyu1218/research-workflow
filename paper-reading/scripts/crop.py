#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
放大图里的一块，或者从 PDF 页面上裁一块（公式、表、自动裁坏了的图）。坐标一律是比例 0–1，左上角是 (0, 0)。

  python3 crop.py figures/fig-03.png --grid                          # 先看带刻度的图，找要放大的那块在哪
  python3 crop.py figures/fig-03.png 0 0 0.45 1 -o figures/fig-03-左半.png   # 讲到左边那个模块时只发这一块
  python3 crop.py paper.pdf --page 5 --grid                          # 带刻度的整页
  python3 crop.py paper.pdf --page 5 0.08 0.12 0.92 0.31 -o figures/eq-loss.png   # 页面上的公式 / 表 / 重裁的图

--grid 每 0.1 画一条线并标数字（--step 0.05 更细），出来的图只用来定坐标，不要发给用户。
要放大的是图里很小的一块时，从 paper.pdf 那一页裁比从 figures/ 里的 PNG 裁更清楚。
"""
import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # 别在 skill 目录里留 __pycache__

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("需要 PyMuPDF：pip install pymupdf")


def draw_grid_on_page(page, step=0.1):
    W, H = page.rect.width, page.rect.height
    n = round(1 / step)
    for k in range(1, n):
        f = k * step
        x, y = W * f, H * f
        major = abs(f * 10 - round(f * 10)) < 1e-6
        width, alpha = (0.5, 0.6) if major else (0.3, 0.35)
        page.draw_line((x, 0), (x, H), color=(1, 0, 0), width=width, stroke_opacity=alpha)
        page.draw_line((0, y), (W, y), color=(1, 0, 0), width=width, stroke_opacity=alpha)
        page.insert_text((x + 2, 9), f"{f:.2f}".rstrip("0").rstrip("."), fontsize=6 if not major else 7, color=(1, 0, 0))
        page.insert_text((2, y - 2), f"{f:.2f}".rstrip("0").rstrip("."), fontsize=6 if not major else 7, color=(1, 0, 0))


def main():
    ap = argparse.ArgumentParser(description="按比例坐标裁图 / 裁 PDF 页面")
    ap.add_argument("src", help="PNG/JPG 图，或 paper.pdf")
    ap.add_argument("box", nargs="*", type=float, help="x0 y0 x1 y1（0–1）；不给就是整张")
    ap.add_argument("--page", type=int, help="PDF 页码（从 1 开始）")
    ap.add_argument("--grid", action="store_true", help="叠加刻度，用来定坐标")
    ap.add_argument("--step", type=float, default=0.1, help="刻度间隔，小元素用 0.05")
    ap.add_argument("--dpi", type=int, help="PDF 渲染分辨率，默认 220（--grid 时 110）")
    ap.add_argument("-o", "--out", help="输出文件")
    a = ap.parse_intermixed_args()  # 坐标写在 --page 前后都行

    src = Path(a.src)
    if a.box and len(a.box) != 4:
        sys.exit("坐标要四个数：x0 y0 x1 y1")
    x0, y0, x1, y1 = a.box if a.box else (0, 0, 1, 1)
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        sys.exit("坐标要满足 0 ≤ x0 < x1 ≤ 1、0 ≤ y0 < y1 ≤ 1")

    if src.suffix.lower() == ".pdf":
        if not a.page:
            sys.exit("PDF 要给 --page")
        doc = fitz.open(str(src))
        page = doc[a.page - 1]
        if a.grid:
            draw_grid_on_page(page, a.step)
        W, H = page.rect.width, page.rect.height
        clip = fitz.Rect(x0 * W, y0 * H, x1 * W, y1 * H)
        dpi = a.dpi or (110 if a.grid else 220)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from extract_figures import figures_dir
        fd = figures_dir(src.parent)
        default = (fd if fd.is_dir() else src.parent) / f"page-{a.page:02d}{'-grid' if a.grid else ''}.png"
        out = Path(a.out) if a.out else default
        page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), clip=clip, alpha=False).save(str(out))
    else:
        doc = fitz.open(str(src))  # PyMuPDF 能把图片当成单页文档打开
        page = doc[0]
        W, H = page.rect.width, page.rect.height
        if a.grid:
            pdf = fitz.open()
            p2 = pdf.new_page(width=W, height=H)
            p2.insert_image(p2.rect, filename=str(src))
            draw_grid_on_page(p2, a.step)
            page = p2
        clip = fitz.Rect(x0 * W, y0 * H, x1 * W, y1 * H)
        tag = "-grid" if a.grid else "-crop"
        out = Path(a.out) if a.out else src.with_name(src.stem + tag + ".png")
        # 图片文档的页面尺寸就是像素数，按 1:1 渲染不损失清晰度；太小的块放大一点
        zoom = max(1.0, min(3.0, 900 / max(clip.width, 1)))
        page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False).save(str(out))
    print(out)


if __name__ == "__main__":
    main()
