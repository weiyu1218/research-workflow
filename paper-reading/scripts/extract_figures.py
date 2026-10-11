#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 paper.pdf 裁出每张图（和表），写 figures/index.md。

  python3 extract_figures.py <论文目录>              # 目录里要有 paper.pdf；有 paper.tex 会顺带对上 LaTeX label
  python3 extract_figures.py <论文目录> --dpi 250

怎么裁：
  图：找 "Figure N:" / "Fig. N." / 加粗的 "Figure N" 开头的 caption 行，从 caption 往上一块块收图形
      （位图 + 矢量路径），相邻两块隔得太远、或者中间隔着正文 / 小节标题就停；
      再把落在图形旁边的小字（坐标轴、图例）带上。上面没有图就往下找（caption 在图上方的排版）。
  表：caption 旁边那组横线（booktabs 的 top/mid/bottomrule）就是表的范围。
  页眉页脚、每页都出现的装饰图先排除。裁不出来的图整页渲染兜底，index.md 里标「整页」。
  最后拼一张 _contact.png，一次看完全部裁剪。

为什么从 PDF 裁，而不是直接拿 LaTeX 源码里的图片文件：PDF 里的样子才是读者看到的样子——
子图拼版、LaTeX 排的标签、TikZ 画的图（源码里根本没有图片文件）都在。
"""
import argparse
import difflib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.dont_write_bytecode = True

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("需要 PyMuPDF：pip install pymupdf")

NUM = r'([A-Z]?\d+(?:\.\d+)?|S\d+)'
FIG_STRICT = re.compile(r'^\s*(?:Figure|FIGURE|Fig\.|FIG\.|Fig)\s*' + NUM + r'\s*(?:[:.|：]|\s[-–—]\s)')
FIG_LOOSE = re.compile(r'^\s*(?:Figure|FIGURE|Fig\.|FIG\.)\s*' + NUM + r'\b')          # 要求标签加粗
TAB_STRICT = re.compile(r'^\s*(?:Table|TABLE|Tab\.)\s*([A-Z]?\d+(?:\.\d+)?|[IVXL]+|S\d+)\s*(?:[:.|：]|\s[-–—]\s|$)')
TAB_LOOSE = re.compile(r'^\s*(?:Table|TABLE)\s*([A-Z]?\d+(?:\.\d+)?|[IVXL]+|S\d+)\b')
HEAD_RE = re.compile(r'^(?:\d+(?:\.\d+)*\.?|[A-Z](?:\.\d+)*\.|[IVX]+\.)\s+[A-Z]')
SEED = 45   # 图的下沿离 caption 最多这么远（pt）
GAP = 36    # 同一张图里相邻两块图形最多隔这么远
MAX_PX = 2400  # 长边上限：再大对看图没帮助，只是更占上下文


def clean(t):
    return re.sub(r'[\ud800-\udfff]', '', t)


class Line:
    __slots__ = ("rect", "text", "size", "block", "idx", "bold", "gap")

    def __init__(self, rect, text, size, block, idx, bold, gap=0.0):
        self.rect, self.text, self.size, self.block, self.idx, self.bold = rect, text, size, block, idx, bold
        self.gap = gap  # 行内相邻两段文字之间最大的空隙：表格行的单元格之间会很开


def is_bold(span):
    return bool(span["flags"] & 16) or bool(re.search(r'Bold|Bd|Medi|Semi|Heavy|Black|\.B$', span["font"]))


def page_lines(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for i, l in enumerate(b["lines"]):
            spans = [s for s in l["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = clean("".join(s["text"] for s in l["spans"]).strip())
            size = max(s["size"] for s in spans)
            xs = sorted((s["bbox"][0], s["bbox"][2]) for s in spans)
            gap = max([b2[0] - a2[1] for a2, b2 in zip(xs, xs[1:])], default=0.0)
            out.append(Line(fitz.Rect(l["bbox"]), text, size, b["number"], i, is_bold(spans[0]), gap))
    return out


def norm_rect(r):
    r = fitz.Rect(min(r.x0, r.x1), min(r.y0, r.y1), max(r.x0, r.x1), max(r.y0, r.y1))
    # 细线（表格横线、坐标轴）的 bbox 高或宽是 0，PyMuPDF 当空矩形处理、求并集时会被吞掉；撑开半个点
    if r.height < 0.5:
        r.y1 = r.y0 + 0.5
    if r.width < 0.5:
        r.x1 = r.x0 + 0.5
    return r


def raw_graphics(page):
    """位图用 get_image_info，矢量路径用 get_drawings（get_bboxlog 给描边路径的框会外扩好几个点，不准）。"""
    rects = [("img", fitz.Rect(i["bbox"])) for i in page.get_image_info()]
    try:
        rects += [("path", fitz.Rect(d["rect"])) for d in page.get_drawings()]
    except Exception:
        pass
    try:
        rects += [("shade", fitz.Rect(bb)) for k, bb in page.get_bboxlog() if k == "fill-shade"]
    except Exception:
        pass
    return rects


class Doc:
    """整篇的版式：正文字号、单/双栏、正文区边界、页眉页脚范围、每页都有的装饰图。"""

    def __init__(self, doc):
        self.doc = doc
        self.lines = {}
        sizes, sample = Counter(), []
        for pno, page in enumerate(doc):
            self.lines[pno] = page_lines(page)
            if pno < 12:
                for ln in self.lines[pno]:
                    sizes[round(ln.size, 1)] += len(ln.text)
                    sample.append(ln)
        self.body = sizes.most_common(1)[0][0] if sizes else 10.0
        W = doc[0].rect.width
        body_lines = [l for l in sample if abs(l.size - self.body) < 0.6 and len(l.text) > 40]
        if body_lines:
            widths = sorted(l.rect.width for l in body_lines)
            self.two_col = widths[len(widths) // 2] < 0.5 * W
            xs0 = sorted(l.rect.x0 for l in body_lines)
            xs1 = sorted(l.rect.x1 for l in body_lines)
            self.left = xs0[max(0, len(xs0) // 20)]
            self.right = xs1[min(len(xs1) - 1, len(xs1) * 19 // 20)]
        else:
            self.two_col, self.left, self.right = False, 0.08 * W, 0.92 * W
        self.text_w = self.right - self.left
        self.col_w = self.text_w / 2 if self.two_col else self.text_w
        self.mid = (self.left + self.right) / 2
        self._margins()

    def _margins(self):
        """页眉页脚：在多数页同一位置反复出现的文字（数字归一化，页码也算）。"""
        n = len(self.doc)
        H = self.doc[0].rect.height
        top, bot = defaultdict(set), defaultdict(set)
        imgs = Counter()
        for pno, page in enumerate(self.doc):
            for ln in self.lines[pno]:
                key = re.sub(r'\d+', '#', ln.text)[:60]
                if ln.rect.y1 < 0.13 * H:
                    top[key].add(pno)
                elif ln.rect.y0 > 0.87 * H:
                    bot[key].add(pno)
            for info in page.get_image_info():
                imgs[tuple(round(v) for v in info["bbox"])] += 1
        need = max(3, 0.3 * n)
        key = lambda l: re.sub(r'\d+', '#', l.text)[:60]

        def usual_y(keys, top_side):
            """每个反复出现的文字，取它在各页的典型位置；位置对不上的那次（图里恰好也写着论文名）不算页眉。"""
            pos = defaultdict(list)
            for pno in range(n):
                for l in self.lines[pno]:
                    if key(l) in keys and ((l.rect.y1 < 0.13 * H) if top_side else (l.rect.y0 > 0.87 * H)):
                        pos[key(l)].append(round(l.rect.y1 if top_side else l.rect.y0))
            return {k: Counter(v).most_common(1)[0][0] for k, v in pos.items()}

        top_y = usual_y({k for k, p in top.items() if len(p) >= need}, True)
        bot_y = usual_y({k for k, p in bot.items() if len(p) >= need}, False)
        self.hb, self.ft = {}, {}
        for pno in range(n):
            ys = [l.rect.y1 for l in self.lines[pno] if key(l) in top_y and abs(l.rect.y1 - top_y[key(l)]) <= 3]
            yb = [l.rect.y0 for l in self.lines[pno] if key(l) in bot_y and abs(l.rect.y0 - bot_y[key(l)]) <= 3]
            self.hb[pno] = max(ys) + 8 if ys else 0
            self.ft[pno] = min(yb) - 4 if yb else H
        self.decor = {k for k, c in imgs.items() if c >= need}

    def graphics(self, pno):
        if not hasattr(self, "_gcache"):
            self._gcache = {}
        if pno not in self._gcache:
            self._gcache[pno] = self._graphics(pno)
        return self._gcache[pno]

    def _graphics(self, pno):
        page = self.doc[pno]
        W, H = page.rect.width, page.rect.height
        out = []
        for kind, r in raw_graphics(page):
            if abs(r.x1 - r.x0) < 0.5 and abs(r.y1 - r.y0) < 0.5:
                continue
            if kind == "img" and tuple(round(v) for v in r) in self.decor:
                continue
            r = norm_rect(r)
            if r.width > 0.95 * W and r.height > 0.95 * H:  # 整页底色
                continue
            if r.y1 <= self.hb[pno] or r.y0 >= self.ft[pno]:
                continue
            r = r & page.rect
            if not r.is_empty:
                out.append(r)
        return out

    def span_for(self, cap):
        """caption 所在那一栏（或通栏）的左右边界。双栏里跨过中缝的 caption 一定是通栏图。"""
        if not self.two_col:
            # 单栏里只贴一边、又明显比正文窄的 caption，是 wrapfigure / wraptable，图就在 caption 那一溜
            if cap.width < 0.7 * self.text_w and (abs(cap.x0 - self.left) < 12) != (abs(cap.x1 - self.right) < 12):
                return cap.x0 - 15, cap.x1 + 15
            return self.left - 4, self.right + 4
        if cap.width > 0.62 * self.text_w or (cap.x0 < self.mid - 10 and cap.x1 > self.mid + 10):
            return self.left - 4, self.right + 4
        if (cap.x0 + cap.x1) / 2 < self.mid:
            return self.left - 4, self.mid
        return self.mid, self.right + 4

    def obstacle(self, ln):
        """挡板：正文行、小节标题、大字号标题——图不会越过它们。"""
        t = ln.text
        if abs(ln.size - self.body) <= 1.0 and ln.rect.width >= 0.6 * self.col_w and len(t) >= 15:
            return True
        if HEAD_RE.match(t) and len(t) < 90 and ln.size >= self.body - 0.5:
            return True
        return ln.size > self.body + 1.5 and len(t.strip()) > 2


def hov(r, span):
    x0, x1 = span
    return min(r.x1, x1) - max(r.x0, x0) > min(10, 0.3 * max(r.width, 1))


def in_span(g, span):
    x0, x1 = span
    ov = min(g.x1, x1) - max(g.x0, x0)
    return ov > 0 and (ov >= 0.5 * g.width or ov >= 0.8 * (x1 - x0))


def union(rects):
    r = fitz.Rect(rects[0])
    for x in rects[1:]:
        r |= x
    return r


def caption_rect(lines, cap_line):
    """caption 往往跨好几行：同一 block 里从 caption 行往后的都算，再接上紧挨着的同字号行。"""
    group = [l for l in lines if l.block == cap_line.block and l.idx >= cap_line.idx]
    r = union([l.rect for l in group])
    while True:
        nxt = [l for l in lines if l not in group and 0 <= l.rect.y0 - r.y1 < 0.6 * cap_line.size
               and abs(l.size - cap_line.size) < 0.6 and min(l.rect.x1, r.x1) - max(l.rect.x0, r.x0) > 0]
        if not nxt:
            return r, group
        for l in nxt:
            group.append(l)
            r |= l.rect


def grow(G, anchor, up):
    """从挨着 caption 的那块图形开始，一块块往远处收；隔得太远就停。"""
    G = sorted(G, key=(lambda g: -g.y1) if up else (lambda g: g.y0))
    if not G:
        return None
    first = G[0]
    if (anchor - first.y1 if up else first.y0 - anchor) > SEED:
        return None
    sel, edge = [first], (first.y0 if up else first.y1)
    for g in G[1:]:
        if up and g.y1 >= edge - GAP:
            sel.append(g)
            edge = min(edge, g.y0)
        elif not up and g.y0 <= edge + GAP:
            sel.append(g)
            edge = max(edge, g.y1)
        else:
            break
    return union(sel)


def figure_region(D, pno, cap, cap_lines, up, blocked=(), other_caps=()):
    page = D.doc[pno]
    lines = D.lines[pno]
    span = D.span_for(cap)
    x0, x1 = span
    G = []
    for g in D.graphics(pno):
        if not in_span(g, span):
            continue
        g = fitz.Rect(max(g.x0, x0), g.y0, min(g.x1, x1), g.y1)  # 超出本栏的部分是被裁掉的位图，不算
        if any((g & b).get_area() > 0.3 * max(g.get_area(), 1) for b in blocked):  # 大半落在表里的是表的线
            continue
        if up and g.y0 < cap.y0 - 2 and g.y1 <= cap.y0 + 6:
            G.append(fitz.Rect(g.x0, g.y0, g.x1, min(g.y1, cap.y0 - 0.5)))
        elif not up and g.y1 > cap.y1 + 2 and g.y0 >= cap.y1 - 6:
            G.append(fitz.Rect(g.x0, max(g.y0, cap.y1 + 0.5), g.x1, g.y1))
    side = [l for l in lines if l not in cap_lines and hov(l.rect, span)
            and (l.rect.y1 <= cap.y0 + 1 if up else l.rect.y0 >= cap.y1 - 1)]
    obst = []
    for o in side:
        if not D.obstacle(o):
            continue
        # 被某块图形上下包住的字是图里的字（框里的标题、大号标签），不是挡板
        if any(g.y0 < o.rect.y0 - 1 and g.y1 > o.rect.y1 + 1 and hov(o.rect, (g.x0, g.x1)) for g in G):
            continue
        obst.append(o)
    tabs = [b for b in blocked if in_span(b, span)]
    oc = [c for c in other_caps if hov(c, span)]
    if up:
        limit = max([o.rect.y1 for o in obst] + [b.y1 for b in tabs if b.y1 <= cap.y0]
                    + [c.y1 for c in oc if c.y1 <= cap.y0 + 1], default=D.hb[pno])
        G = [g for g in G if g.y0 >= limit - 1]
        bb = grow(G, cap.y0, True)
    else:
        limit = min([o.rect.y0 for o in obst] + [b.y0 for b in tabs if b.y0 >= cap.y1]
                    + [c.y0 for c in oc if c.y0 >= cap.y1 - 1], default=D.ft[pno])
        G = [g for g in G if g.y1 <= limit + 1]
        bb = grow(G, cap.y1, False)
    if bb is None:
        return None
    ex = bb + (-8, -8, 8, 8)
    for l in side:  # 坐标轴标签、图例、子图标题这类贴着图形的小字
        if l in obst or not l.rect.intersects(ex) or any(l.rect.intersects(b) for b in tabs):
            continue
        # 正文字号、贴着栏左边、又不在图形框里的短行，是上一段的末行，不是图里的字
        if abs(l.size - D.body) < 0.6 and abs(l.rect.x0 - (x0 + 4 if x0 < D.mid else x0)) < 8 and not l.rect.intersects(bb):
            continue
        if (up and l.rect.y0 >= limit - 1) or (not up and l.rect.y1 <= limit + 1):
            bb |= l.rect
    if bb.height < 15 or bb.width < 30:
        return None
    bb = bb + (-3, -3, 3, 3)
    if up:
        bb.y1 = min(bb.y1, cap.y0 - 0.5)
    else:
        bb.y0 = max(bb.y0, cap.y1 + 0.5)
    return bb & page.rect


def table_region(D, pno, cap, cap_lines):
    page = D.doc[pno]
    lines = D.lines[pno]
    span = D.span_for(cap)
    H = page.rect.height
    rules = [g for g in D.graphics(pno) if g.height < 2.5 and g.width > 0.25 * D.col_w and in_span(g, span)]
    other_caps = [l for l in lines if l not in cap_lines and match_caption(l)[0]]

    def grab(seq, below):
        group = [seq[0]]
        for r in seq[1:]:
            prev = group[-1]
            lo, hi = (prev.y1, r.y0) if below else (r.y1, prev.y0)
            if hi - lo > 0.8 * H or any(lo <= c.rect.y0 <= hi for c in other_caps):
                break
            # 两条横线之间夹着正文段落 → 已经是下一张表了（单元格之间空隙大，正文行没有）
            if any(l not in cap_lines and lo - 1 <= l.rect.y0 and l.rect.y1 <= hi + 1 and hov(l.rect, (r.x0, r.x1))
                   and D.obstacle(l) and l.gap < 1.5 * l.size for l in lines):
                break
            group.append(r)
        if len(group) < 2:
            return None
        bb = union(group)
        cols = (bb.x0, bb.x1)

        def inside(l):  # 只认落在表宽里的行：wraptable 旁边同一基线的正文会被 PyMuPDF 并成一行
            return l not in cap_lines and l.rect.x0 >= cols[0] - 10 and l.rect.x1 <= cols[1] + 10

        for l in lines:
            if inside(l) and l.rect.y0 >= bb.y0 - 2 and l.rect.y1 <= bb.y1 + 2:
                bb |= l.rect
        # 横线组外侧（远离 caption 的一侧）紧挨着的表格行也收进来，碰到正文段落或大空隙就停
        rest = [l for l in lines if inside(l) and (l.rect.y0 >= bb.y1 - 1 if below else l.rect.y1 <= bb.y0 + 1)]
        rest.sort(key=(lambda l: l.rect.y0) if below else (lambda l: -l.rect.y1))
        edge = bb.y1 if below else bb.y0
        blobs = [g for g in D.graphics(pno) if g.height > 5 and hov(g, cols)]  # 图（不是表线）
        for l in rest:
            dist = (l.rect.y0 - edge) if below else (edge - l.rect.y1)
            if dist > 1.2 * l.size:
                break
            if any(g.intersects(l.rect) for g in blobs):  # 碰到图里的字了，表到头了
                break
            if (D.obstacle(l) and l.gap < 1.5 * l.size) or match_caption(l)[0]:
                break
            bb |= l.rect
            edge = bb.y1 if below else bb.y0
        return bb if bb.height > 12 else None

    below = sorted([g for g in rules if g.y0 >= cap.y1 - 2], key=lambda g: g.y0)
    above = sorted([g for g in rules if g.y1 <= cap.y0 + 2], key=lambda g: -g.y1)
    gb = below[0].y0 - cap.y1 if below else 1e9
    ga = cap.y0 - above[0].y1 if above else 1e9
    order = [(below, True, gb), (above, False, ga)]
    order.sort(key=lambda x: x[2])  # 先试离 caption 近的那一侧
    for seq, is_below, gap in order:
        if seq and gap <= 40:
            lo, hi = (cap.y1, seq[0].y0) if is_below else (seq[0].y1, cap.y0)
            if any(lo - 1 <= c.rect.y0 <= hi + 1 for c in other_caps if hov(c.rect, span)):
                continue
            bb = grab(seq, is_below)
            if bb:
                return (bb + (-3, -3, 3, 3)) & page.rect
    return None


def render(page, rect, out, dpi):
    rect = rect or page.rect
    zoom = dpi / 72
    longest = max(rect.width, rect.height) * zoom
    if longest > MAX_PX:
        zoom *= MAX_PX / longest
    page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect, alpha=False).save(str(out))


def fname(kind, num):
    stem = "fig" if kind == "figure" else "tab"
    return f"{stem}-{int(num):02d}.png" if num.isdigit() else f"{stem}-{num}.png"


def sort_key(num):
    if num.isdigit():
        return (0, int(num), "")
    m = re.match(r'([A-Z]+)\.?(\d+)', num)
    return (1, int(m.group(2)) if m else 0, m.group(1) if m else num)


def match_caption(ln):
    for kind, strict, loose in (("figure", FIG_STRICT, FIG_LOOSE), ("table", TAB_STRICT, TAB_LOOSE)):
        m = strict.match(ln.text)
        if not m and ln.bold:
            m = loose.match(ln.text)
        if m:
            return kind, m.group(1)
    return None, None


# ---------------- LaTeX 一侧：label、源文件、正文在哪引用 ----------------

def read_group(s, i):
    """s[i] == '{'，返回配对的 '}' 之后的位置和括号内文本。"""
    depth, j = 0, i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1, s[i + 1:j]
        j += 1
    return len(s), s[i + 1:]


def find_cmd_args(s, cmd):
    out = []
    for m in re.finditer(r'\\' + cmd + r'\*?\s*(?:\[[^\]]*\])?\s*\{', s):
        _, arg = read_group(s, m.end() - 1)
        out.append(arg)
    return out


def norm(s):
    s = re.sub(r'\$[^$]*\$', ' ', s)
    s = re.sub(r'\\[a-zA-Z]+\*?(\[[^\]]*\])?', ' ', s)
    s = re.sub(r'[^0-9a-zA-Z]+', ' ', s).lower()
    return " ".join(s.split())[:200]


FORMAT_CMDS = re.compile(r'\\(?:textbf|textit|emph|texttt|textsc|textrm|textsf|textnormal|mathrm|mathbf|mathit|bm|'
                         r'boldsymbol|xspace|protect|ensuremath|small|large|Large|footnotesize|normalsize|bf|it|rm|sc)\b\*?')
GREEK_CMDS = {"pi": "π", "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "lambda": "λ",
              "mu": "μ", "sigma": "σ", "tau": "τ", "phi": "φ", "omega": "ω", "theta": "θ", "rho": "ρ"}


def simple_macros(lines):
    """paper.tex 开头列出的无参数短宏（\\def\\ModelSymbol{$\\pi_0$} 这种），用来把标题里的宏还原成字。"""
    out = {}
    for line in lines[:400]:
        if line.strip() == "% ==== 正文 ====":
            break
        m = re.match(r'\\(?:(?:re)?newcommand|providecommand|def)\s*\{?(\\[A-Za-z@]+)\}?\s*\{(.*)\}\s*$', line)
        if m and len(m.group(2)) <= 40 and "#" not in m.group(2):
            out[m.group(1)] = m.group(2)
    return out


def tidy_tex(t, macros=None):
    for _ in range(2):
        for k, v in (macros or {}).items():
            t = re.sub(re.escape(k) + r'(?![A-Za-z@])(\s*\{\})?', lambda _m, v=v: v, t)
    t = re.sub(r'\\label\{[^}]*\}|\\footnote\{[^}]*\}', '', t)
    t = FORMAT_CMDS.sub(' ', t)
    t = re.sub(r'\\(' + '|'.join(GREEK_CMDS) + r')(?![A-Za-z])', lambda m: GREEK_CMDS[m.group(1)], t)
    t = re.sub(r'\\[ ,;!]|~', ' ', t)
    t = re.sub(r'[{}$]|_(?=\w)', '', t)
    return " ".join(t.split())


def latex_sections(lines):
    macros = simple_macros(lines)
    secs, counters, appendix = [], [0, 0, 0], False
    pat = re.compile(r'\\(section|subsection|subsubsection)(\*?)\s*(?:\[[^\]]*\])?\s*\{(.*)')
    for i, line in enumerate(lines, 1):
        if re.match(r'\s*\\appendix\b', line):
            appendix, counters = True, [0, 0, 0]
        m = pat.search(line)
        if not m:
            continue
        lvl = ["section", "subsection", "subsubsection"].index(m.group(1))
        _, title = read_group("{" + m.group(3), 0)
        title = tidy_tex(title, macros)
        num = ""
        if not m.group(2):
            counters[lvl] += 1
            for k in range(lvl + 1, 3):
                counters[k] = 0
            head = chr(64 + counters[0]) if appendix and counters[0] else str(counters[0])
            num = ".".join([head] + [str(c) for c in counters[1:lvl + 1]])
        secs.append((i, num, title))
    return secs


def section_of(secs, lineno):
    cur = None
    for s in secs:
        if s[0] <= lineno:
            cur = s
        else:
            break
    if not cur:
        return "正文开头"
    return (f"§{cur[1]} " if cur[1] else "") + cur[2][:40]


def latex_floats(tex):
    envs = []
    for m in re.finditer(r'\\begin\{(figure\*?|wrapfigure|SCfigure|table\*?|wraptable)\}(.*?)\\end\{\1\}', tex, re.S):
        body = m.group(2)
        caps = find_cmd_args(body, "caption")
        if not caps:
            continue
        envs.append({
            "kind": "table" if "table" in m.group(1) else "figure",
            "caption": caps[-1],
            "labels": re.findall(r'\\label\{([^}]+)\}', body),
            "files": find_cmd_args(body, "includegraphics"),
            "line": tex.count("\n", 0, m.start()) + 1,
        })
    for m in re.finditer(r'\\captionof\{(figure|table)\}\s*\{', tex):
        _, cap = read_group(tex, m.end() - 1)
        tail = tex[m.end():m.end() + 400]
        start = max(0, m.start() - 1500)
        envs.append({
            "kind": m.group(1), "caption": cap,
            "labels": re.findall(r'\\label\{([^}]+)\}', tail)[:1],
            "files": find_cmd_args(tex[start:m.start()], "includegraphics")[-4:],
            "line": tex.count("\n", 0, m.start()) + 1,
        })
    envs.sort(key=lambda e: e["line"])
    return envs


def match_latex(items, envs, tex_lines):
    secs = latex_sections(tex_lines)
    for kind in ("figure", "table"):
        pdf_items = sorted([it for it in items if it["kind"] == kind], key=lambda x: sort_key(x["num"]))
        tex_items = [e for e in envs if e["kind"] == kind]
        pairs = []
        for a, it in enumerate(pdf_items):
            cap = norm(re.sub(r'^\s*\S+\s*\S+?(?:[:.|]|\s)', '', it["caption"], count=1))
            for b, e in enumerate(tex_items):
                r = difflib.SequenceMatcher(None, cap[:160], norm(e["caption"])[:160]).ratio()
                pairs.append((r, a, b))
        pairs.sort(reverse=True)
        done, used = set(), set()
        for r, a, b in pairs:
            if r < 0.45 or a in done or b in used:
                continue
            done.add(a)
            used.add(b)
            pdf_items[a]["tex"] = tex_items[b]
        if len(pdf_items) == len(tex_items):  # 数量对得上时，剩下的按顺序配
            for a, it in enumerate(pdf_items):
                if "tex" not in it and a not in used:
                    it["tex"] = tex_items[a]
    for it in items:
        e = it.get("tex")
        if not e:
            continue
        refs = []
        for lab in e["labels"]:
            pat = re.compile(r'\\(?:[cC]ref|ref|autoref|[fF]igref|[tT]abref|pageref|subref)\*?\{[^}]*?' + re.escape(lab) + r'\s*[,}]')
            for i, line in enumerate(tex_lines, 1):
                if pat.search(line):
                    refs.append(f"{section_of(secs, i)} (L{i})")
        uniq = list(dict.fromkeys(refs))
        it["label"] = ", ".join(e["labels"])
        it["source_files"] = e["files"]
        it["tex_line"] = e["line"]
        it["refs"] = uniq[:6]


# ---------------- 主流程 ----------------

MARK = ".kelip-paper-reading"  # 标记文件：这个目录是脚本建的，里面的 fig-NN.png 可以重生成
OLD_MARK = ".paper-reading-agent"  # 改名前（2026-10-06 之前）建的目录用的标记，照样认


def figures_dir(paper_dir):
    """figures/ 不存在、或者是本脚本建的，就用它；里面已经有用户自己放的图，就另开 figures-auto/，不混、不覆盖。"""
    d = Path(paper_dir) / "figures"
    if not d.exists() or (d / MARK).exists() or (d / OLD_MARK).exists() or not any(not x.name.startswith(".") for x in d.iterdir()):
        return d
    return Path(paper_dir) / "figures-auto"


def extract(paper_dir, dpi=220, tables=True, quiet=False):
    paper_dir = Path(paper_dir)
    pdf = paper_dir / "paper.pdf"
    if not pdf.exists():
        sys.exit(f"找不到 {pdf}")
    out_dir = figures_dir(paper_dir)
    out_dir.mkdir(exist_ok=True)
    (out_dir / MARK).write_text("由 kelip-paper-reading/scripts/extract_figures.py 生成；重跑时只删 index.json 里列过的文件\n",
                                encoding="utf-8")
    doc = fitz.open(str(pdf))
    D = Doc(doc)

    found = {}  # (kind, num) -> item
    for pno in range(len(doc)):
        lines = D.lines[pno]
        caps = []
        for ln in lines:
            kind, num = match_caption(ln)
            if not kind or (kind == "table" and not tables):
                continue
            # 正文换行恰好落在 "...as shown in / Figure 3. Next" 的情况：上一行是没说完的正文、行距正常 → 不是 caption
            if ln.idx != 0:
                prev = [l for l in lines if l.block == ln.block and l.idx == ln.idx - 1]
                if prev:
                    p = prev[0]
                    tight = ln.rect.y0 - p.rect.y1 < 0.5 * ln.size
                    if D.obstacle(p) and tight and not p.text.rstrip().endswith((".", ":", "。", "：")):
                        continue
            cap, cap_lines = caption_rect(lines, ln)
            caps.append((kind, num, cap, cap_lines))
        regions = {}
        for kind, num, cap, cap_lines in caps:  # 先认表：表的横线和单元格不能被旁边的图吃进去
            if kind == "table":
                regions[(kind, num, id(cap_lines))] = table_region(D, pno, cap, cap_lines)
        blocked = [r for r in regions.values() if r is not None]
        all_caps = [c for _, _, c, _ in caps]
        for kind, num, cap, cap_lines in caps:
            if kind == "figure":
                others = [c for c in all_caps if c is not cap]
                r = figure_region(D, pno, cap, cap_lines, True, blocked, others)
                if r is None:
                    r = figure_region(D, pno, cap, cap_lines, False, blocked, others)
                regions[(kind, num, id(cap_lines))] = r
        # 同一块区域被两个 caption 认领（多半是隔壁表 / 图的）：只留给离它最近的那个 caption
        keys = list(regions)
        capmap = {(k, n, id(cl)): c for k, n, c, cl in caps}

        def dist(key):
            r, c = regions[key], capmap[key]
            return min(abs(r.y0 - c.y1), abs(c.y0 - r.y1))

        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                ra, rb = regions[a], regions[b]
                if ra is None or rb is None:
                    continue
                inter = (ra & rb).get_area()
                if inter > 0.7 * min(ra.get_area(), rb.get_area()):
                    regions[b if dist(b) > dist(a) else a] = None
        for kind, num, cap, cap_lines in caps:
            region = regions[(kind, num, id(cap_lines))]
            caption = " ".join(l.text for l in cap_lines)  # 按 PDF 里的行序，几何排序会把上下标搅乱
            score = region.get_area() if region else 0
            key = (kind, num)
            if key in found and found[key]["score"] >= score:
                continue
            found[key] = {"kind": kind, "num": num, "page": pno + 1, "region": region,
                          "caption": caption, "score": score}

    items = sorted(found.values(), key=lambda it: (it["kind"] != "figure", sort_key(it["num"])))
    prev = out_dir / "index.json"  # 重跑时只删上一轮自己生成的文件；crop.py 另存的放大图、用户的图都不动
    if prev.exists():
        try:
            for it in json.loads(prev.read_text(encoding="utf-8")):
                f = it.get("file")
                if f and (out_dir / Path(f).name).is_file():
                    (out_dir / Path(f).name).unlink()
        except (ValueError, OSError):
            pass
    for it in items:
        page = doc[it["page"] - 1]
        if it["region"] is None and it["kind"] == "table":
            it["file"], it["mode"] = None, "未裁出"
            continue
        name = fname(it["kind"], it["num"])
        render(page, it["region"], out_dir / name, dpi)
        it["file"] = f"{out_dir.name}/{name}"
        it["mode"] = "裁剪" if it["region"] is not None else "整页"
        if it["region"] is not None:
            r = it["region"]
            W, H = page.rect.width, page.rect.height
            it["bbox"] = [round(r.x0 / W, 3), round(r.y0 / H, 3), round(r.x1 / W, 3), round(r.y1 / H, 3)]

    tex_path = paper_dir / "paper.tex"
    if tex_path.exists():
        tex = tex_path.read_text(encoding="utf-8", errors="ignore")
        match_latex(items, latex_floats(tex), tex.split("\n"))

    write_index(out_dir, items, len(doc))
    sheet = contact_sheet(out_dir, items)
    nfig = sum(1 for it in items if it["kind"] == "figure")
    ntab = sum(1 for it in items if it["kind"] == "table" and it.get("file"))
    nfull = sum(1 for it in items if it.get("mode") == "整页")
    if not quiet:
        print(f"  ✓ 图 {nfig} 张、表 {ntab} 张 → {out_dir}/（整页兜底 {nfull} 张）")
        if out_dir.name != "figures":
            print("    figures/ 里已经有你自己的图，自动裁的放在 figures-auto/，没动原来的")
        if sheet:
            print(f"    全部裁剪拼在一张图里检查：{sheet}")
    return items


def write_index(out_dir, items, npages):
    nfig = sum(1 for it in items if it["kind"] == "figure")
    ntab = sum(1 for it in items if it["kind"] == "table")
    L = ["# 图表索引", "",
         f"来源 `paper.pdf`（{npages} 页）：图 {nfig} 张、表 {ntab} 张。全部裁剪拼在 `{out_dir.name}/_contact.png`，一次看完。", "",
         "> 讲到哪张图就把那个 PNG 发给用户；讲之前自己先看一眼——caption 不会告诉你图是怎么排的。",
         f"> 裁得不对：`crop.py paper.pdf --page P --grid` 看带刻度的整页，再 `crop.py paper.pdf --page P x0 y0 x1 y1 -o {out_dir.name}/fig-NN.png` 重裁（坐标是页面比例 0–1）。",
         ""]
    for it in items:
        head = ("图" if it["kind"] == "figure" else "表") + f" {it['num']} · 第 {it['page']} 页"
        if it.get("file"):
            head += f" · `{it['file']}`"
        if it.get("mode") in ("整页", "未裁出"):
            head += f"（{it['mode']}，需要时手动裁）"
        L.append(f"## {head}")
        L.append(f"**caption**：{it['caption']}")
        extra = []
        if it.get("label"):
            extra.append(f"label `{it['label']}`")
        if it.get("tex_line"):
            extra.append(f"paper.tex L{it['tex_line']}")
        if it.get("source_files"):
            extra.append("源文件 " + "、".join(f"`{x}`" for x in it["source_files"][:4]))
        if it.get("refs"):
            extra.append("正文引用：" + "；".join(it["refs"]))
        if extra:
            L.append("· " + " · ".join(extra))
        L.append("")
    (out_dir / "index.md").write_text("\n".join(L), encoding="utf-8")
    slim = [{k: v for k, v in it.items() if k not in ("region", "score", "tex")} for it in items]
    (out_dir / "index.json").write_text(json.dumps(slim, ensure_ascii=False, indent=1), encoding="utf-8")


def contact_sheet(out_dir, items, cols=4, cell_w=360, cell_h=250):
    files = [(it, out_dir / Path(it["file"]).name) for it in items if it.get("file")]
    if not files:
        return None
    rows = (len(files) + cols - 1) // cols
    doc = fitz.open()
    page = doc.new_page(width=cols * cell_w, height=rows * (cell_h + 22) + 6)
    for i, (it, path) in enumerate(files):
        r, c = divmod(i, cols)
        x0, y0 = c * cell_w, r * (cell_h + 22) + 4
        label = ("Fig " if it["kind"] == "figure" else "Tab ") + it["num"] + f"  p{it['page']}"
        if it.get("mode") == "整页":
            label += "  [full page]"
        page.insert_text((x0 + 6, y0 + 13), label, fontsize=11)
        box = fitz.Rect(x0 + 4, y0 + 18, x0 + cell_w - 4, y0 + 18 + cell_h)
        page.draw_rect(box, color=(0.75, 0.75, 0.75), width=0.6)
        try:
            page.insert_image(box + (2, 2, -2, -2), filename=str(path), keep_proportion=True)
        except Exception:
            pass
    out = out_dir / "_contact.png"
    page.get_pixmap(matrix=fitz.Matrix(1.4, 1.4), alpha=False).save(str(out))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="从 paper.pdf 裁出每张图和表")
    ap.add_argument("paper_dir")
    ap.add_argument("--dpi", type=int, default=220)
    ap.add_argument("--no-tables", action="store_true", help="只裁图，不裁表")
    a = ap.parse_args()
    extract(a.paper_dir, dpi=a.dpi, tables=not a.no_tables)
