"""可复用的行类型。每个 Row 是一个 "宽度已知、高度固定、渲染到 RGBA 图层" 的单元。

VerticalStack 负责把多行垂直拼接到画布;每行内部可以自由布局而不影响其他行,
从而从机制上避免错位 / 重叠。
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager

from PIL import Image, ImageDraw, ImageFont

from . import markdown as md
from .style import (
    _is_emoji_control,
    char_renderable,
    emoji_font_for,
    font_for_char,
    is_bitmap_font,
    load_font,
    load_title_font,
    main_font_supports,
)

try:  # noqa: E129
    from .engine import AvatarSource  # noqa: F401
except ImportError:  # pragma: no cover
    pass


def _hex(c):
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(x * 2 for x in c)
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _rgba(c, a=255):
    r, g, b = _hex(c)
    return (r, g, b, a)


def _iter_runs(
    text: str, size: int, *, bold: bool = False
) -> list[tuple[ImageFont.FreeTypeFont | None, str]]:
    """按字体把文本切段(性能优化核心)。

    主字体覆盖的连续字符合并为一段(font=None 表示主字体,由调用方解析),
    回退字体的字符逐个产出(彩色 emoji 位图字体不宜整串绘制)。
    中文正文几乎全部落在主字体段 → 绘制/测量从「每字符 2 次 PIL 调用」
    变成「整段各 1 次」,长文本渲染提速数倍。

    所有字体都覆盖不了的字符直接丢弃(避免渲染成豆腐块);
    字体覆盖信息不可得(cmap 加载失败)时保守照常绘制。
    """
    runs: list[tuple[ImageFont.FreeTypeFont | None, str]] = []
    batch: list[str] = []
    for ch in text:
        if _is_emoji_control(ch):
            continue
        ef = emoji_font_for(ch, size)
        if ef is None:
            # 主字体覆盖 → 正常;确认无任何字体覆盖 → 丢弃
            if main_font_supports(ch) or char_renderable(ch):
                batch.append(ch)
            continue
        if batch:
            runs.append((None, "".join(batch)))
            batch = []
        runs.append((ef, ch))
    if batch:
        runs.append((None, "".join(batch)))
    return runs


def _resolve_main(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    return load_font(size, bold=bold)


def _measure_fallback(draw, text: str, size: int, *, bold: bool = False) -> float:
    """测量含 emoji 的字符串宽度(主字体段批量测量,回退字符逐个测)。

    位图 emoji 字体(内置尺寸与目标不符)按比例换算宽度。
    """
    total = 0.0
    main: ImageFont.FreeTypeFont | None = None
    for f, run in _iter_runs(text, size, bold=bold):
        if f is None:
            if main is None:
                main = _resolve_main(size, bold)
            f = main
        w = f.getlength(run)
        if is_bitmap_font(f):
            w *= size / f.size
        total += w
    return total


def _draw_bitmap_emoji(
    canvas: Image.Image, xy, ch: str, font: ImageFont.FreeTypeFont, size: int
) -> float:
    """绘制固定尺寸位图 emoji(如 NotoColorEmoji)。

    先在内置尺寸下渲染到临时画布(embedded_color=True 才能出彩色),
    再等比缩放贴回目标画布,底部与文字行高对齐。返回该字符的推进宽度。

    注意:NotoColorEmoji 的字形位图会超出 em box(实测右/下越界可达
    ~0.5em),临时画布四周必须留足 padding,否则字形边缘被裁。
    """
    x, y = xy
    ratio = size / font.size
    pad = font.size // 2  # 四周留 0.5em,容纳越界字形
    tmp = Image.new("RGBA", (font.size + pad * 2, font.size + pad * 2), (0, 0, 0, 0))
    td = ImageDraw.Draw(tmp)
    td.text((pad, pad), ch, font=font, embedded_color=True)
    bbox = tmp.getbbox()
    if bbox:
        tmp = tmp.crop(bbox)
        w = max(1, int(tmp.width * ratio))
        h = max(1, int(tmp.height * ratio))
        tmp = tmp.resize((w, h), Image.LANCZOS)
        # 底部对齐到行高底边,与文字基线视觉一致
        canvas.alpha_composite(tmp, (int(x), int(y + max(0, size - h))))
    return font.getlength(ch) * ratio


def _draw_fallback(
    canvas: Image.Image, xy, text: str, size: int, fill, *, bold: bool = False
) -> float:
    """绘制文本,支持 emoji / 符号字体回退与位图 emoji 缩放。返回末端 x 坐标。"""
    x, y = xy
    draw = ImageDraw.Draw(canvas)
    main: ImageFont.FreeTypeFont | None = None
    for f, run in _iter_runs(text, size, bold=bold):
        if f is None:
            if main is None:
                main = _resolve_main(size, bold)
            f = main
        if is_bitmap_font(f):
            for ch in run:
                x += _draw_bitmap_emoji(canvas, (x, y), ch, f, size)
        else:
            draw.text((x, y), run, font=f, fill=fill)
            x += f.getlength(run)
    return x


def _measure_spans_fallback(draw, spans, size: int) -> float:
    """测量 span 序列总宽度。"""
    total = 0.0
    for sp in spans:
        total += _measure_fallback(draw, sp.text, size, bold=sp.bold)
    return total


def font_metrics(font: ImageFont.FreeTypeFont) -> int:
    """返回字体行高(含行距)。"""
    top, bottom = font.getmetrics()
    return top + bottom


def make_canvas(width: int, height: int, rgb: tuple = (0, 0, 0, 0)):
    """创建 RGBA 画布 + 绘制对象。"""
    img = Image.new("RGBA", (max(1, width), max(1, height)), rgb)
    return img, ImageDraw.Draw(img)


class Row:
    """行基类。所有子类必须实现 :meth:`_render_impl`,在 (0,0) 处绘制。"""

    def __init__(self, r: object):
        from .engine import ChatRenderer

        self.r: ChatRenderer = r
        self.height: int = 10

    def measure(self) -> Row:
        """计算高度(子类可覆盖)。返回 self。"""
        return self

    def draw(self, canvas: Image.Image, y: int) -> None:
        """把本行内容绘制到 canvas 的 y 处(左上角)。"""
        # 直接绘制在 y 偏移
        self._paint_on(canvas, y)

    def _paint_on(self, canvas: Image.Image, y: int) -> None:
        raise NotImplementedError

    # 便捷工具
    def _draw_spans(
        self,
        canvas: Image.Image,
        x: int,
        y: int,
        spans: Sequence[md.Span],
        default_color,
        font_size: int | None = None,
    ) -> int:
        """绘制富文本 span 序列,返回结束的 x 坐标。不换行。主字体段批量绘制。"""
        draw = ImageDraw.Draw(canvas)
        cx = x
        fs = font_size or self.r.font_size
        for sp in spans:
            color = _rgba(self.r.t.link) if sp.link else _rgba(default_color)
            main: ImageFont.FreeTypeFont | None = None
            for f, run in _iter_runs(sp.text, fs, bold=sp.bold):
                if f is None:
                    if main is None:
                        main = load_font(fs, bold=sp.bold)
                    f = main
                if is_bitmap_font(f):
                    for ch in run:
                        cx += _draw_bitmap_emoji(canvas, (cx, y), ch, f, fs)
                    continue
                draw.text((cx, y), run, font=f, fill=color)
                cw = f.getlength(run)
                if sp.strike:
                    mid = y + f.getmetrics()[0] // 2
                    draw.line(
                        [(cx, mid), (cx + cw, mid)], fill=color, width=max(1, fs // 20)
                    )
                cx += cw
        return cx

    def _measure_spans(self, spans: Sequence[md.Span], font_size: int) -> float:
        """测量 span 序列的总宽度(emoji/符号逐字符回退)。"""
        from PIL import ImageDraw

        draw = ImageDraw.Draw(Image.new("RGB", (4, 4)))
        return _measure_spans_fallback(draw, spans, font_size)

    def _wrap_spans(
        self,
        spans: Sequence[md.Span],
        max_width: int,
        font_size: int,
    ) -> list[list[md.Span]]:
        """把 spans 按最大宽度换行,返回多行。emoji 宽度按回退字体计算。"""
        if not spans:
            return [[]]
        lines: list[list[md.Span]] = []
        cur: list[md.Span] = []
        cur_w = 0.0

        for sp in spans:
            for ch in sp.text:
                if _is_emoji_control(ch):
                    continue
                f = emoji_font_for(ch, font_size)
                if f is None:
                    # 主字体覆盖或保守绘制 → 主字体;无字体覆盖 → 跳过不占宽
                    if not (main_font_supports(ch) or char_renderable(ch)):
                        continue
                    f = load_font(font_size, bold=sp.bold)
                cw = f.getlength(ch)
                if is_bitmap_font(f):
                    cw *= font_size / f.size  # 位图 emoji 按目标尺寸换算宽度
                if cur and cur_w + cw > max_width:
                    lines.append(cur)
                    cur = []
                    cur_w = 0.0
                cur.append(
                    md.Span(
                        ch,
                        bold=sp.bold,
                        italic=sp.italic,
                        strike=sp.strike,
                        code=sp.code,
                        link=sp.link,
                    )
                )
                cur_w += cw
        if cur:
            lines.append(cur)
        return lines


class RichTextRow(Row):
    """富文本段落行(单行,由上层保证不超宽)。"""

    def __init__(
        self,
        r,
        spans: Sequence[md.Span],
        *,
        font_size: int | None = None,
        bold: bool = False,
        color: str | None = None,
        align: str = "left",
        margin=(0, 0, 0, 0),
        border_left: int = 0,
        dedent: bool = True,
        use_title_font: bool = False,
    ):
        super().__init__(r)
        self.bold = bold
        self.use_title_font = use_title_font
        self.color = color or r.t.text_secondary
        self.font_size = font_size or r.font_size
        self.align = align
        self.margin = margin
        self.border_left = border_left

        # 可用内容宽度(全宽 - 左右 margin - border)
        max_w = r.width - r.h_pad * 2 - margin[2] - margin[3] - (border_left > 0) * 10
        if self.border_left:
            max_w -= 8

        # 先按显式换行符拆成子段(span.text 可能含 \n),再分别换行
        para_lines: list[list[md.Span]] = []
        for sp in spans:
            parts = sp.text.split("\n")
            for i, pt in enumerate(parts):
                if i == 0:
                    if para_lines:
                        para_lines[-1].append(
                            md.Span(
                                pt,
                                bold=sp.bold,
                                italic=sp.italic,
                                strike=sp.strike,
                                code=sp.code,
                                link=sp.link,
                            )
                        )
                    else:
                        para_lines.append(
                            [
                                md.Span(
                                    pt,
                                    bold=sp.bold,
                                    italic=sp.italic,
                                    strike=sp.strike,
                                    code=sp.code,
                                    link=sp.link,
                                )
                            ]
                        )
                else:
                    para_lines.append(
                        [
                            md.Span(
                                pt if pt else " ",
                                bold=sp.bold,
                                italic=sp.italic,
                                strike=sp.strike,
                                code=sp.code,
                                link=sp.link,
                            )
                        ]
                    )

        # 每段按像素换行
        wrapped: list[list[md.Span]] = []
        for pl in para_lines:
            ws = self._wrap_spans(pl, max_w, self.font_size)
            wrapped.extend(ws)
        self.lines = wrapped

        # 计算高度
        font = self._measure_font(bold)
        self.text_h = font_metrics(font)
        line_h = int(self.text_h * r.line_height) if self.text_h else self.font_size
        pad_y = margin[0] + margin[1]
        self.height = max(self.font_size, len(self.lines) * line_h + pad_y)

    def _measure_font(self, bold: bool) -> ImageFont.FreeTypeFont:
        if self.use_title_font:
            return load_title_font(self.font_size, bold=bold)
        return load_font(self.font_size, bold=bold)

    def _paint_on(self, canvas: Image.Image, y: int) -> None:
        draw = ImageDraw.Draw(canvas)
        x0 = self.r.h_pad + self.margin[2]
        ty = y + self.margin[0]

        if self.border_left:
            draw.rectangle(
                [self.r.h_pad, y, self.r.h_pad + 4, y + self.height - self.margin[1]],
                fill=_rgba(self.r.t.link),
            )
            x0 += 8

        color = _rgba(self.color)
        line_h = (
            int(self.text_h * self.r.line_height) if self.text_h else self.font_size
        )
        for line in self.lines:
            x = x0
            for sp in line:
                col = _rgba(self.r.t.link) if sp.link else color
                if self.use_title_font:
                    # 标题模式:逐字符走专用字体 + 回退(字量小,无需批量)
                    for ch in sp.text:
                        if _is_emoji_control(ch):
                            continue
                        f = self._paint_font(ch, sp)
                        draw.text((x, ty), ch, font=f, fill=col)
                        x += draw.textlength(ch, font=f)
                    continue
                main: ImageFont.FreeTypeFont | None = None
                for f, run in _iter_runs(sp.text, self.font_size, bold=sp.bold):
                    if f is None:
                        if main is None:
                            main = load_font(self.font_size, bold=sp.bold or self.bold)
                        f = main
                    if is_bitmap_font(f):
                        for ch in run:
                            x += _draw_bitmap_emoji(
                                canvas, (x, ty), ch, f, self.font_size
                            )
                        continue
                    draw.text((x, ty), run, font=f, fill=col)
                    x += f.getlength(run)
            ty += line_h

    def _paint_font(self, ch: str, sp) -> ImageFont.FreeTypeFont:
        """标题模式:标题专用字体(仓耳小丸子)优先;emoji/符号仍回退。"""
        if self.use_title_font and sp is not None and not getattr(sp, "code", False):
            f = load_title_font(self.font_size)
            # 标题字体缺该字符时回退常规路径
            try:
                if f and f.getmask(ch).getbbox():
                    return f
            except Exception:
                pass
        return font_for_char(ch, self.font_size, bold=sp.bold or self.bold)


class CodeRow(Row):
    """代码块行。"""

    def __init__(self, r, code: str, lang: str = ""):
        super().__init__(r)
        self.code = code
        self.lang = lang
        self.fs = max(11, int(r.font_size * 0.85))
        self.font = load_font(self.fs)
        self.line_h = font_metrics(self.font) + 4

        lines = code.split("\n") or [""]
        self.lines = lines
        self.pad = 10
        self.height = len(lines) * self.line_h + self.pad * 2

    def _paint_on(self, canvas: Image.Image, y: int) -> None:
        draw = ImageDraw.Draw(canvas)
        x0 = self.r.h_pad
        x1 = self.r.width - self.r.h_pad
        draw.rectangle(
            [x0, y, x1, y + self.height],
            fill=_rgba(self.r.t.code_bg),
        )
        ty = y + self.pad
        for line in self.lines:
            draw.text(
                (x0 + self.pad, ty),
                line,
                font=self.font,
                fill=_rgba(self.r.t.text_secondary),
            )
            ty += self.line_h


class EmptyRow(Row):
    """占位空白行。"""

    def __init__(self, r, height: int):
        super().__init__(r)
        self.height = max(0, int(height))

    def _paint_on(self, canvas: Image.Image, y: int) -> None:
        pass


class PillRow(Row):
    """居中灰色胶囊行(MomoTalk 剧情事件 / 系统提示样式)。

    用于穿插在对话间的短旁白(如“我微微停顿,没有立刻回答。”),
    视觉上与对话气泡区分,避免割裂。
    """

    def __init__(
        self, r, text: str, *, font_size: int | None = None, color: str | None = None
    ):
        super().__init__(r)
        self.text = text.strip()
        self.font_size = font_size or int(r.font_size * 0.92)
        self.color = color or r.t.pill_text

        # 换行(先一次性算出每个字符宽度,再贪心折行,避免 O(n²) 重测)
        font = load_font(self.font_size)
        max_w = int(r.width * 0.72)
        widths: list[float] = []
        chars: list[str] = []
        for f, run in _iter_runs(self.text, self.font_size):
            if f is None:
                f = font
            scale = self.font_size / f.size if is_bitmap_font(f) else 1.0
            for ch in run:
                chars.append(ch)
                widths.append(f.getlength(ch) * scale)
        total_w = sum(widths)
        if total_w > max_w:
            # 贪心按字换行
            lines = []
            cur = []
            cur_w = 0.0
            for ch, cw in zip(chars, widths):
                if cur and cur_w + cw > max_w:
                    lines.append("".join(cur))
                    cur = []
                    cur_w = 0.0
                cur.append(ch)
                cur_w += cw
            if cur:
                lines.append("".join(cur))
            self.lines = lines
        else:
            self.lines = [self.text]

        # 高度:行数 * 行高 + 上下内边距
        line_h = int(font.size * 1.7)
        self.line_h = line_h
        self.pad_v = max(5, int(font.size * 0.35))
        self.height = len(self.lines) * line_h + self.pad_v * 2

    def _paint_on(self, canvas: Image.Image, y: int) -> None:
        draw = ImageDraw.Draw(canvas)
        font = load_font(self.font_size)
        line_h = self.line_h
        pad_x = int(self.font_size * 0.9)
        color = _rgba(self.color)

        # 逐行绘制居中胶囊(emoji 逐字符回退)
        ty = y + self.pad_v
        pill_color = _rgba(self.r.t.pill_bg)
        for line in self.lines:
            tw = _measure_fallback(draw, line, self.font_size)
            pw = tw + pad_x * 2
            px = (self.r.width - pw) / 2
            draw.rounded_rectangle(
                [px, ty, px + pw, ty + line_h],
                radius=line_h / 2,
                fill=pill_color,
            )
            _draw_fallback(
                canvas,
                (px + pad_x, ty + (line_h - font.size) / 2),
                line,
                self.font_size,
                color,
            )
            ty += line_h


class HrRow(Row):
    def __init__(self, r):
        super().__init__(r)
        self.height = max(6, r.v_pad // 2)

    def _paint_on(self, canvas: Image.Image, y: int) -> None:
        draw = ImageDraw.Draw(canvas)
        mid = y + self.height // 2
        draw.line(
            [(self.r.h_pad, mid), (self.r.width - self.r.h_pad, mid)],
            fill=_rgba(self.r.t.card_border),
            width=2,
        )


class ImageRow(Row):
    """图片行(支持本地路径 / URL / data URI)。"""

    def __init__(self, r, blk: md.ImageBlock):
        super().__init__(r)
        self.blk = blk
        self._img: Image.Image | None = None
        self._load()

    def _load(self) -> None:
        import io
        import os
        import urllib.request

        url = self.blk.url
        data = None
        try:
            if url.startswith("data:"):
                import base64

                b64 = url.split(",", 1)[1]
                data = base64.b64decode(b64)
            elif url.startswith(("http://", "https://")):
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = resp.read()
            elif os.path.isfile(url):
                with open(url, "rb") as f:
                    data = f.read()
        except Exception:
            data = None

        self._img = None
        if data:
            try:
                self._img = Image.open(io.BytesIO(data)).convert("RGBA")
            except Exception:
                self._img = None

        max_w = self.r.width - self.r.h_pad * 2
        max_h = max(60, self.r.page_max_height // 5)
        if self._img is not None:
            self._img.thumbnail((max_w, max_h), Image.LANCZOS)
            self.height = self._img.height
        else:
            # 占位
            self.height = 48
            self._img = None

    def _paint_on(self, canvas: Image.Image, y: int) -> None:
        if self._img is None:
            draw = ImageDraw.Draw(canvas)
            x0 = self.r.h_pad
            draw.rectangle(
                [x0, y, self.r.width - self.r.h_pad, y + self.height],
                fill=_rgba(self.r.t.code_bg),
            )
            msg = f"🖼️ {self.blk.alt or '(未加载)'}"
            _draw_fallback(
                canvas,
                (x0 + 8, y + (self.height - self.font_size) // 2),
                msg,
                int(self.r.font_size * 0.85),
                _rgba(self.r.t.text_muted),
            )
            return
        x = (self.r.width - self._img.width) // 2
        canvas.alpha_composite(self._img, (x, y))


class DialogueRow(Row):
    """聊天气泡行:momotalk 风格。

    布局(对方为例)::

        ┌────┐ 名字
        │头像│ ┌──────────┐
        │    │ │  气泡     │
        │    │ └──────────┘
        └────┘

    - 头像固定在左(对方)/ 右(自己),顶部与名字第一行对齐
    - 名字在头像右侧、气泡上方(自己无名字)
    - 头像始终保持正方形,尺寸 = avatar_size
    """

    def __init__(
        self,
        r,
        *,
        speaker: str,
        spans: Sequence[md.Span],
        is_self: bool = False,
        avatar=None,
    ):
        super().__init__(r)
        self.speaker = speaker
        self.spans = spans
        self.is_self = is_self
        self.avatar = avatar

        self.fs = r.font_size
        # 间距配置
        self.gap = max(10, int(r.font_size * 0.4))  # 头像与内容间距
        self.margin_lr = 14  # 气泡水平内边距
        self.margin_v = 10  # 气泡垂直内边距
        # 名字行高(即名字占用的高度)
        self.name_h = r.name_font_size + 4
        # 名字与气泡间距
        self.name_bubble_gap = 4
        self.av = r.avatar_size
        self._compute_layout()

    def _content_x(self) -> int:
        """内容(名字/气泡)左边界(对方)。"""
        return self.r.h_pad + self.av + self.gap

    def _compute_layout(self):
        r = self.r
        # 可用内容总宽
        content_w = r.width - r.h_pad * 2
        max_text = int(content_w * r.max_bubble_ratio)
        self.line_h = int(self.fs * r.line_height)

        # 换行
        self.wrapped_lines = self._wrap_spans(self.spans, max_text, self.fs)
        if not self.wrapped_lines:
            self.wrapped_lines = [[md.Span(" ")]]

        # 最大行宽
        max_line_w = self._max_line_width(self.wrapped_lines)

        # 气泡尺寸
        self.bubble_w = int(min(max_line_w, max_text) + self.margin_lr * 2)
        self.bubble_w = max(int(r.font_size * 1.2), self.bubble_w)
        self.bubble_h = len(self.wrapped_lines) * self.line_h + self.margin_v * 2

        # 右侧内容高度 = 名字 + 间距 + 气泡(自己/对方都显示名字,像微信)
        if self.speaker:
            right_h = self.name_h + self.name_bubble_gap + self.bubble_h
        else:
            right_h = self.bubble_h

        # 行高 = max(头像, 右侧内容)
        self.height = int(max(self.av, right_h) + 2)

    def _measure_spans_px(self, spans, font_size):
        from PIL import ImageDraw

        draw = ImageDraw.Draw(Image.new("RGB", (4, 4)))
        return _measure_spans_fallback(draw, spans, font_size)

    def _max_line_width(self, lines) -> float:
        max_w = 0.0
        for line in lines:
            max_w = max(max_w, self._measure_spans_px(line, self.fs))
        return max_w

    def _paint_on(self, canvas: Image.Image, y: int) -> None:
        r = self.r
        y0 = y

        # ---- 自己/对方区分布局 ----
        if self.is_self:
            # 头像固定在右侧边缘
            av_x = r.width - r.h_pad - self.av
            # 气泡右边界 = 头像左边界 - gap
            bubble_right = av_x - self.gap
            bubble_x = bubble_right - self.bubble_w
            bubble_x = max(r.h_pad, bubble_x)  # 防越界
        else:
            av_x = r.h_pad
            bubble_x = self._content_x()

        # 名字 / 气泡 的起始 y(顶部对齐)
        outer_top = y0 + 1

        # 画头像(固定左侧或右侧,高度方向顶部对齐)
        self._draw_avatar(canvas, av_x, outer_top, self.av)

        # 气泡位置:名字下方(自己/对方都显示名字)
        name_h = self.name_h + self.name_bubble_gap if self.speaker else 0
        if self.speaker:
            bubble_y = outer_top + name_h
        else:
            bubble_y = outer_top

        # 绘制气泡背景
        bubble_bg = r.t.bubble_self if self.is_self else r.t.bubble_other
        self._draw_bubble(
            canvas,
            bubble_x,
            bubble_y,
            self.bubble_w,
            self.bubble_h,
            _rgba(bubble_bg),
        )

        # 名字(在气泡上方;自己的名字右对齐,与头像同侧)
        if self.speaker:
            draw = ImageDraw.Draw(canvas)
            name_w = _measure_fallback(draw, self.speaker, r.name_font_size)
            if not self.is_self:
                name_x = self._content_x()
            else:
                # 自己的名字右对齐到头像左边界前
                avatar_left = r.width - r.h_pad - self.av
                name_x = avatar_left - self.gap - int(name_w)
                name_x = max(r.h_pad, name_x)
            # 自己用默认灰,其他角色按名字 hash 上色(同一角色稳定同色)
            nc = r.t.name_color if self.is_self else r.name_color_for(self.speaker)
            _draw_fallback(
                canvas,
                (name_x, outer_top),
                self.speaker,
                r.name_font_size,
                _rgba(nc),
            )

        # 文本(在气泡内垂直居中)
        # bubble_h = 行数*line_h + margin_v*2,每个 line_h 槽位内文字应居中
        font0 = load_font(self.fs)
        _asc, _desc = font0.getmetrics()
        font_line_h = _asc + _desc  # 文字实际像素高度
        slot_offset = max(0, (self.line_h - font_line_h) / 2)

        tx = bubble_x + self.margin_lr
        ty = bubble_y + self.margin_v + slot_offset
        bubble_fg = r.t.bubble_self_text if self.is_self else r.t.bubble_other_text
        for line in self.wrapped_lines:
            self._draw_spans_at(canvas, tx, ty, line, bubble_fg)
            ty += self.line_h

    def _draw_spans_at(self, canvas, x, y, spans, color):
        draw = ImageDraw.Draw(canvas)
        cx = x
        for sp in spans:
            col = _rgba(self.r.t.link) if sp.link else _hex(color) + (255,)
            main: ImageFont.FreeTypeFont | None = None
            for f, run in _iter_runs(sp.text, self.fs, bold=sp.bold):
                if f is None:
                    if main is None:
                        main = load_font(self.fs, bold=sp.bold)
                    f = main
                if is_bitmap_font(f):
                    for ch in run:
                        cx += _draw_bitmap_emoji(canvas, (cx, y), ch, f, self.fs)
                    continue
                draw.text((cx, y), run, font=f, fill=col)
                cx += f.getlength(run)

    def _draw_bubble(self, canvas, x, y, w, h, fill):
        draw = ImageDraw.Draw(canvas)
        radius = min(self.r.bubble_radius, h // 2)
        draw.rounded_rectangle(
            [x, y, x + w, y + h],
            radius=radius,
            fill=fill,
        )
        # 小尾巴:指向头像一侧(微信/MomoTalk 风)。尾巴顶部略低于气泡上沿。
        tail = max(7, int(self.r.font_size * 0.26))
        tail_h = int(tail * 1.5)
        ty0 = y + min(self.margin_v + 2, h // 3)
        if self.is_self:
            bx = x + w - 1
            pts = [(bx, ty0), (bx + tail, ty0 + tail_h // 2), (bx, ty0 + tail_h)]
        else:
            bx = x + 1
            pts = [(bx, ty0), (bx - tail, ty0 + tail_h // 2), (bx, ty0 + tail_h)]
        draw.polygon(pts, fill=fill)

    def _draw_avatar(self, canvas, x, y, size):
        img: Image.Image | None = None
        src = self.avatar
        if src is not None:
            try:
                if hasattr(src, "convert"):
                    img = src.convert("RGBA")
                else:
                    img = Image.open(src).convert("RGBA")
                img = img.resize((size, size), Image.LANCZOS)
            except Exception:
                img = None

        if img is None:
            # 占位:名字首字符
            img = Image.new("RGBA", (size, size), (203, 213, 224, 255))
            d = ImageDraw.Draw(img)
            ch = (self.speaker or "?")[:1]
            f = font_for_char(ch, int(size * 0.5))
            bbox = d.textbbox((0, 0), ch, font=f)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            d.text(
                ((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]),
                ch,
                font=f,
                fill=(255, 255, 255, 255),
            )

        # 圆角蒙版
        mask = Image.new("L", (size, size), 0)
        md = ImageDraw.Draw(mask)
        radius = int(size * 0.24)
        md.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
        canvas.paste(img, (x, y), mask)

        # 细描边(ring):让头像与浅色背景分离,更接近 IM 截图质感
        ring_w = max(2, size // 48)
        ring_color = self.r.t.card_border
        ImageDraw.Draw(canvas).rounded_rectangle(
            [
                x + ring_w // 2,
                y + ring_w // 2,
                x + size - 1 - ring_w // 2,
                y + size - 1 - ring_w // 2,
            ],
            radius=radius,
            outline=_rgba(ring_color),
            width=ring_w,
        )


# ═════════════════════════════════════════════════════════════════════
# 游戏卡片行(ocverse 新增):资料头 / 属性条 / 选项卡 / 标签胶囊 / 面板
# ═════════════════════════════════════════════════════════════════════


def _draw_rounded_avatar(canvas, x, y, size, src, fallback_char, radius_ratio=0.24, ring=None):
    """圆角方头像;src 为路径/PIL Image/None(画首字占位)。"""
    img = None
    if src is not None:
        try:
            img = src.convert("RGBA") if hasattr(src, "convert") else Image.open(src).convert("RGBA")
            img = img.resize((size, size), Image.LANCZOS)
        except Exception:
            img = None
    if img is None:
        img = Image.new("RGBA", (size, size), (203, 213, 224, 255))
        d = ImageDraw.Draw(img)
        ch = (fallback_char or "?")[:1]
        f = font_for_char(ch, int(size * 0.5))
        bbox = d.textbbox((0, 0), ch, font=f)
        d.text(((size - (bbox[2] - bbox[0])) / 2 - bbox[0], (size - (bbox[3] - bbox[1])) / 2 - bbox[1]),
               ch, font=f, fill=(255, 255, 255, 255))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * radius_ratio), fill=255)
    canvas.paste(img, (x, y), mask)
    if ring:
        rw = max(2, size // 48)
        ImageDraw.Draw(canvas).rounded_rectangle(
            [x + rw // 2, y + rw // 2, x + size - 1 - rw // 2, y + size - 1 - rw // 2],
            radius=int(size * radius_ratio), outline=_rgba(ring), width=rw,
        )


@contextmanager
def _inset(r, inset: int):
    """临时右移左边界,让子行在面板内宽下测量/绘制(右边界不变,由 h_pad 推出)。"""
    op = r.h_pad
    r.h_pad = op + inset
    try:
        yield
    finally:
        r.h_pad = op


class AvatarHeadRow(Row):
    """资料卡头部:大头像 + 名字/副标题/标签/脚注。"""

    def __init__(self, r, avatar=None, name="?", subtitle="", tags=None, foot=""):
        super().__init__(r)
        self.avatar_src = avatar
        self.name = name
        self.subtitle = subtitle
        self.tags = [str(t) for t in (tags or []) if str(t).strip()][:8]
        self.foot = foot
        fs = r.font_size
        self.av = int(fs * 2.6)
        self.fs = fs
        self.name_f = load_font(int(fs * 1.25), bold=True)
        self.sub_f = load_font(int(fs * 0.78))
        self.tag_f = load_font(int(fs * 0.7))
        self.foot_f = load_font(int(fs * 0.72))
        content_x = r.h_pad + self.av + int(fs * 0.5)
        self.avail_w = max(60, r.width - content_x - r.h_pad)
        h = font_metrics(self.name_f) + 4
        if self.subtitle:
            h += font_metrics(self.sub_f) + 4
        self.tag_lines = self._wrap_tags(self.avail_w)
        if self.tag_lines and self.tag_lines[0]:
            h += len(self.tag_lines) * (font_metrics(self.tag_f) + 12) + 4
        if self.foot:
            h += font_metrics(self.foot_f) + 6
        self.height = max(self.av, h) + int(fs * 0.4)

    def _wrap_tags(self, max_w):
        lines = [[]]
        cur_w = 0
        gap = 6
        for t in self.tags:
            w = self.tag_f.getlength(t) + 14
            if lines[-1] and cur_w + gap + w > max_w:
                lines.append([])
                cur_w = 0
            if lines[-1]:
                cur_w += gap
            lines[-1].append((t, w))
            cur_w += w
        return lines

    def _paint_on(self, canvas, y):
        r = self.r
        draw = ImageDraw.Draw(canvas)
        ax = r.h_pad
        _draw_rounded_avatar(canvas, ax, y, self.av, self.avatar_src, self.name, ring=r.t.card_border)
        x0 = ax + self.av + int(self.fs * 0.5)
        ty = y
        _draw_fallback(canvas, (x0, ty), self.name, int(self.fs * 1.25), _rgba(r.t.text), bold=True)
        ty += font_metrics(self.name_f) + 4
        if self.subtitle:
            _draw_fallback(canvas, (x0, ty), self.subtitle, int(self.fs * 0.78), _rgba(r.t.text_secondary))
            ty += font_metrics(self.sub_f) + 4
        line_h = font_metrics(self.tag_f) + 12
        for line in self.tag_lines:
            x = x0
            for t, w in line:
                draw.rounded_rectangle([x, ty, x + w, ty + line_h], radius=line_h // 2, fill=_rgba(r.t.pill_bg))
                _draw_fallback(canvas, (x + 7, ty + 6), t, int(self.fs * 0.7), _rgba(r.t.pill_text))
                x += w + 6
            ty += line_h
        if self.foot:
            _draw_fallback(canvas, (x0, ty + 2), self.foot, int(self.fs * 0.72), _rgba(r.t.text_muted))


class StatBarRow(Row):
    """两列属性条。items: [(label, value, max, color_hex), ...]"""

    BAR_H = 18

    def __init__(self, r, items):
        super().__init__(r)
        self.items = list(items)
        fs = int(r.font_size * 0.72)
        self.font = load_font(fs)
        self.label_w = int(self.font.size * 3.1)
        self.val_w = int(self.font.size * 2.6)
        self.gap = int(self.font.size * 0.8)
        content_w = r.width - r.h_pad * 2
        self.cell_w = max(80, (content_w - self.gap) // 2)
        self.bar_w = max(30, self.cell_w - self.label_w - self.val_w - 8)
        self.line_h = max(self.BAR_H, self.font.size) + 10
        self.height = ((len(self.items) + 1) // 2) * self.line_h + 4

    def _paint_on(self, canvas, y):
        r = self.r
        draw = ImageDraw.Draw(canvas)
        ty_off = (self.line_h - 2 - self.font.size) // 2
        for i, item in enumerate(self.items):
            label, val, mx, color = item
            row, col = divmod(i, 2)
            x = r.h_pad + col * (self.cell_w + self.gap)
            ty = y + 2 + row * self.line_h
            _draw_fallback(canvas, (x, ty + ty_off), str(label), self.font.size, _rgba(r.t.text_secondary))
            bx = x + self.label_w
            by = ty + (self.line_h - 2 - self.BAR_H) // 2
            draw.rounded_rectangle([bx, by, bx + self.bar_w, by + self.BAR_H],
                                   radius=self.BAR_H // 2, fill=_rgba(r.t.pill_bg))
            try:
                ratio = max(0.0, min(1.0, float(val) / max(1, float(mx))))
            except (TypeError, ValueError):
                ratio = 0.0
            fw = int(self.bar_w * ratio)
            if fw > self.BAR_H:
                draw.rounded_rectangle([bx, by, bx + fw, by + self.BAR_H],
                                       radius=self.BAR_H // 2, fill=_rgba(color))
            _draw_fallback(canvas, (bx + self.bar_w + 8, ty + ty_off), f"{val}", self.font.size, _rgba(r.t.text))


class ChoiceRow(Row):
    """事件选项卡:序号圆徽 + 选项 + 后果暗示。"""

    def __init__(self, r, idx: int, label: str, hint: str = ""):
        super().__init__(r)
        fs = r.font_size
        self.fs = fs
        self.idx = idx
        self.label = label
        self.hint = hint
        self.f = load_font(fs)
        self.hint_f = load_font(int(fs * 0.72))
        self.badge_d = int(fs * 1.05)
        self.pad_v = max(8, int(fs * 0.28))
        self.pad_h = int(fs * 0.55)
        avail = r.width - r.h_pad * 2 - self.badge_d - 14 - self.pad_h * 2
        if hint:
            avail -= int(self.hint_f.getlength(hint)) + 12
        # 截断过长选项
        while self.f.getlength(label) > avail and len(label) > 2:
            label = label[:-1]
        if label != self.label:
            self.label = label + "…"
        self.height = max(self.badge_d, font_metrics(self.f)) + self.pad_v * 2

    def _paint_on(self, canvas, y):
        r = self.r
        draw = ImageDraw.Draw(canvas)
        x0 = r.h_pad
        x1 = r.width - r.h_pad
        draw.rounded_rectangle([x0, y, x1, y + self.height], radius=min(14, self.height // 2),
                               fill=_rgba(r.t.card_bg), outline=_rgba(r.t.card_border), width=2)
        # 序号徽章
        bd = self.badge_d
        bx = x0 + self.pad_h + 4
        by = y + (self.height - bd) // 2
        draw.ellipse([bx, by, bx + bd, by + bd], fill=_rgba(r.t.bubble_self))
        badge_f = load_font(int(bd * 0.55), bold=True)
        num = str(self.idx)
        tw = badge_f.getlength(num)
        draw.text((bx + (bd - tw) / 2, by + (bd - (badge_f.getmetrics()[0] + badge_f.getmetrics()[1])) / 2),
                  num, font=badge_f, fill=_rgba(r.t.bubble_self_text))
        # 选项文本
        tx = bx + bd + 14
        ty = y + (self.height - font_metrics(self.f)) // 2
        _draw_fallback(canvas, (tx, ty), self.label, self.fs, _rgba(r.t.bubble_other_text), bold=True)
        # 右侧暗示
        if self.hint:
            hw = self.hint_f.getlength(self.hint)
            hx = x1 - self.pad_h - hw
            hty = y + (self.height - font_metrics(self.hint_f)) // 2
            _draw_fallback(canvas, (hx, hty), self.hint, int(self.fs * 0.72), _rgba(r.t.text_muted))


class TagRow(Row):
    """横向换行的标签胶囊。"""

    def __init__(self, r, tags, bg=None, fg=None):
        super().__init__(r)
        fs = int(r.font_size * 0.72)
        self.fs = fs
        self.f = load_font(fs)
        self.line_h = font_metrics(self.f) + 10
        self.pad_h = 10
        self.gap = 8
        self.bg = bg
        self.fg = fg
        max_w = r.width - r.h_pad * 2
        lines = [[]]
        cur_w = 0
        for t in tags:
            t = str(t).strip()
            if not t:
                continue
            w = self.f.getlength(t) + self.pad_h * 2
            if lines[-1] and cur_w + self.gap + w > max_w:
                lines.append([])
                cur_w = 0
            if lines[-1]:
                cur_w += self.gap
            lines[-1].append((t, w))
            cur_w += w
        self.lines = lines
        self.height = len(lines) * (self.line_h + 6)

    def _paint_on(self, canvas, y):
        r = self.r
        draw = ImageDraw.Draw(canvas)
        ty = y
        bg = _rgba(self.bg or r.t.pill_bg)
        fg = _rgba(self.fg or r.t.pill_text)
        for line in self.lines:
            x = r.h_pad
            for t, w in line:
                draw.rounded_rectangle([x, ty, x + w, ty + self.line_h], radius=self.line_h // 2, fill=bg)
                _draw_fallback(canvas, (x + self.pad_h, ty + 5), t, self.fs, fg)
                x += w + self.gap
            ty += self.line_h + 6


class PanelRow(Row):
    """圆角面板容器:可选标题 + 嵌套子行。

    rows 可传:
    - 可调用对象(推荐):在面板内宽上下文中调用并构建子行,
      文本会按内宽正确换行,不会超出面板边界;
    - 已构建的行列表:原样使用(注意:此时子行是按面板外全宽测量的,
      长文本可能溢出,请优先使用可调用形式)。
    """

    def __init__(self, r, title: str = "", rows=None, accent: str | None = None):
        super().__init__(r)
        self.title = title
        self.accent = accent or r.t.card_border
        inset = max(6, int(r.font_size * 0.4))
        self.inset = inset
        with _inset(r, inset):
            self.children = [c for c in (rows() if callable(rows) else rows) or [] if c is not None]
        pad = max(8, inset // 2)
        self.pad = pad
        if title:
            tf = load_font(int(r.font_size * 0.95), bold=True)
            self.title_f = tf
            self.title_h = font_metrics(tf) + 10
        else:
            self.title_f = None
            self.title_h = 4
        gaps = int(r.msg_gap * 0.6) * max(0, len(self.children) - 1)
        self.height = pad * 2 + self.title_h + sum(c.height for c in self.children) + gaps

    def _paint_on(self, canvas, y):
        r = self.r
        draw = ImageDraw.Draw(canvas)
        x0, x1 = r.h_pad, r.width - r.h_pad
        draw.rounded_rectangle([x0, y, x1, y + self.height], radius=max(10, int(r.font_size * 0.35)),
                               fill=_rgba(r.t.card_bg), outline=_rgba(self.accent), width=2)
        with _inset(r, self.inset):
            cy = y + self.pad
            if self.title_f:
                _draw_fallback(canvas, (r.h_pad + 4, cy), self.title, int(r.font_size * 0.95),
                               _rgba(r.t.text), bold=True)
                cy += self.title_h
            gap = int(r.msg_gap * 0.6)
            for i, c in enumerate(self.children):
                c.draw(canvas, cy)
                cy += c.height + (gap if i < len(self.children) - 1 else 0)
