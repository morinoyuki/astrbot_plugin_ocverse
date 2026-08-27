"""排版与绘制引擎(行渲染 + 垂直拼接 + 渐变背景)。

(基于作者 astrbot_plugin_life_sim 的 im_render/engine.py 改造,新增 render_rows
公开入口,支持直接绘制预构建的行序列 —— 游戏卡片由此搭建。)
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Union

from PIL import Image, ImageDraw

from . import markdown as md
from .rows import (
    CodeRow,
    DialogueRow,
    EmptyRow,
    HrRow,
    ImageRow,
    PillRow,
    RichTextRow,
    Row,
    _draw_fallback,
    _measure_fallback,
)
from .style import THEMES, Theme, load_font, speaker_color

__all__ = ["AvatarSource", "ChatRenderer", "Row", "TooManyPages", "render_narrative"]

AvatarSource = Union[str, "os.PathLike", Image.Image]
SelfNamePred = Callable[[str], bool]


class TooManyPages(RuntimeError):
    """渲染结果分页数超过上限。"""


def _hex(c):
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(x * 2 for x in c)
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _rgba(c, a=255):
    r, g, b = _hex(c)
    return (r, g, b, a)


@dataclass
class ChatRenderer:
    """渲染配置。所有尺寸单位为像素。"""

    # 画布
    width: int = 1024
    font_size: int = 34
    name_font_size: int = 23
    time_font_size: int = 18
    title_font_size: int = 34
    avatar_size: int = 96
    h_pad: int = 32
    v_pad: int = 30
    line_height: float = 1.6
    bubble_radius: int = 18
    max_bubble_ratio: float = 0.68
    page_max_height: int = 24000
    max_pages: int = 6
    msg_gap: int = 26

    # 主题
    theme: str | Theme = "light"

    # 判定自己
    is_self: SelfNamePred | None = None

    # 标题
    title: str = ""
    show_title: bool = True

    # 块 → 行 的转换临时存储
    _rows_cache: list[Row] = field(default_factory=list)

    def __post_init__(self):
        self._t: Theme = (
            self.theme
            if isinstance(self.theme, Theme)
            else THEMES.get((self.theme or "light").lower(), THEMES["light"])
        )

    @property
    def t(self) -> Theme:
        return self._t

    def name_color_for(self, speaker: str) -> str:
        """说话人名字颜色:按角色名 hash 从主题色板取色,同一角色稳定同色。"""
        return speaker_color(self._t.name_palette, speaker, self._t.name_color)

    def get_max_bubble_width(self) -> int:
        return int((self.width - self.h_pad * 2) * self.max_bubble_ratio)

    def title_height(self) -> int:
        return (
            int(self.title_font_size * 0)
            if not (self.title and self.show_title)
            else int(self.title_font_size * 2.8)
        )

    def new_page(self, height: int) -> Image.Image:
        """创建带垂直渐变的画布(top → bottom 平滑线性过渡)。

        用 NumPy 一次性构造逐行渐变,避免逐行绘制导致高图过慢;
        过渡为完全连续的线性插值,无任何色带/阶梯感。
        """
        h = max(40, int(height))
        import numpy as _np

        top = _hex(self._t.bg_top)
        bottom = _hex(self._t.bg_bottom)
        # 每行一个比例,首尾渐变
        ys = _np.linspace(0, 1, h, dtype=_np.float32)
        # (h, 3) 颜色,再加 alpha=255 → (h, 4)
        rgb = (
            _np.array(top, dtype=_np.float32)[None, :] * (1 - ys)[:, None]
            + _np.array(bottom, dtype=_np.float32)[None, :] * ys[:, None]
        )
        arr = _np.zeros((h, 4), dtype=_np.uint8)
        arr[:, :3] = _np.clip(rgb, 0, 255).astype(_np.uint8)
        arr[:, 3] = 255
        # 每行重复到整宽
        arr = _np.repeat(arr[:, None, :], self.width, axis=1)  # (h, w, 4)
        img = Image.fromarray(arr, mode="RGBA")
        return img

    # ══════════════════════════════════════════════════════════
    # 主入口
    # ══════════════════════════════════════════════════════════
    def render(
        self,
        blocks: Sequence[md.Block],
        *,
        avatars: dict[str, AvatarSource] | None = None,
        title: str | None = None,
    ) -> list[Image.Image]:
        """渲染块序列为多张图。"""
        self.avatars = avatars or {}
        if title is not None:
            self.title = title

        # 1. 块 → 行
        self._rows: list[Row] = []
        for blk in blocks:
            self._layout_block(blk)
        rows = list(self._rows)
        self._rows = []

        return self.render_rows(rows, title=self.title if title is None else title)

    def render_rows(self, rows: list[Row], *, title: str | None = None) -> list[Image.Image]:
        """直接渲染预构建的行序列(跳过 markdown 布局),供游戏卡片使用。"""
        if title is not None:
            self.title = title

        # 2. 分页
        title_h = self.title_height()
        pages = self._paginate(rows, title_h)

        # 3. 每页绘制
        out = []
        for page_rows in pages:
            img = self._draw_page(
                title_h, page_rows, pages[0] if len(pages) > 1 else page_rows
            )
            out.append(img)
        return out

    # ── 头像匹配(容错:精确 → 前缀 → 包含 → 首字)──────────────
    def resolve_avatar(self, speaker: str) -> object | None:
        """按说话人查找头像,支持角色名变体的模糊匹配。

        模型可能输出简称(如「小亚」→ 头像存「汐见小亚」),这里依次尝试:
        1. 精确匹配
        2. 头像名以说话人开头(说话人是头像名的前缀省略,如「小亚」匹配「汐见小亚」不适用这里,见3)
        3. 说话人包含头像名 / 头像名包含说话人(去括号后)
        4. 说话人的每个字出现在头像名中(顺序匹配 ≥2 字)
        """
        if not speaker:
            return None
        av = self.avatars
        if not av:
            return None
        sp = speaker.strip()
        # 1. 精确(含空头像占位)
        if sp in av:
            return av[sp]
        # 归一化:去半角/全角空格、括号注释(如「汐见小亚(老板)」→「汐见小亚」)
        sp_norm = re.sub(r"[\s　]", "", sp).split("(")[0].split("（")[0].strip()
        # 2. 去空格/括号后全等
        for k, v in av.items():
            if not k:
                continue
            kn = re.sub(r"[\s　]", "", k).split("(")[0].split("（")[0].strip()
            if sp_norm == kn:
                return v
        # 3+. 模糊匹配:说话人 ≥2 字且头像名 ≥2 字才做包含/子序列,避免单字歧义
        if len(sp_norm) >= 2:
            best_key, best_v = None, None
            for k, v in av.items():
                if not k:
                    continue
                kn = re.sub(r"[\s　]", "", k).split("(")[0].split("（")[0].strip()
                if len(kn) < 2:
                    continue  # 头像名单字不参与模糊,防误配
                # 3. 包含:短者完全被子串包含(如「小亚」⊆「汐见小亚」)
                if sp_norm in kn or kn in sp_norm:
                    if best_key is None or len(kn) > len(best_key):
                        best_key, best_v = kn, v
                    continue

                # 4. 子序列:按顺序逐字出现在对方中(如「凯伊」⊆「天童凯伊」)
                def is_subseq(small: str, big: str) -> bool:
                    it = iter(big)
                    return all(any(c == ch for c in it) for ch in small)

                if (is_subseq(sp_norm, kn) or is_subseq(kn, sp_norm)) and (
                    best_key is None or len(kn) > len(best_key)
                ):
                    best_key, best_v = kn, v
            if best_v is not None:
                return best_v
        return None

    # ── 块 → 行 ──────────────────────────────────────────────
    def _layout_block(self, blk: md.Block) -> None:
        if blk.type == "heading":
            self._rows.append(EmptyRow(self, 0))  # 标题前留白由行内 margin 处理
            lv = min(getattr(blk, "level", 1), 3)
            size = max(20, self.font_size + max(0, 2 - lv) * 3)
            self._rows.append(
                RichTextRow(
                    self,
                    blk.spans,
                    font_size=size + 4 if lv == 1 else size,
                    bold=True,
                    color=self.t.text,
                    margin=(12, 8, 12, 0),
                    align="left",
                    use_title_font=True,
                )
            )
        elif blk.type == "paragraph":
            # 段落一律按正文渲染;灰胶囊只能由 <c> 标签产生,不再自动折行
            self._append_rich_paragraph(blk.spans)
        elif blk.type == "capsule":
            plain = md.plain_text(blk.spans).strip()
            if plain:
                self._rows.append(PillRow(self, plain))
        elif blk.type == "dialogue":
            # 主角判定:LLM 的 `*名字:` 标记优先;配置的 is_self 函数兜底
            is_self_row = bool(
                getattr(blk, "protagonist", False)
                or (self.is_self and self.is_self(blk.speaker))
            )
            self._rows.append(
                DialogueRow(
                    self,
                    speaker=blk.speaker,
                    spans=blk.spans,
                    is_self=is_self_row,
                    avatar=self.resolve_avatar(
                        getattr(blk, "avatar", None) or blk.speaker
                    ),
                )
            )
        elif blk.type == "list":
            for i, item in enumerate(blk.items):
                prefix = f"{i + 1}. " if blk.ordered else "•  "
                if not blk.ordered and len(blk.items) > 1 and i == len(blk.items) - 1:
                    pass
                span = [md.Span(prefix)] + item
                self._rows.append(
                    RichTextRow(
                        self,
                        span,
                        font_size=int(self.font_size * 0.95),
                        color=self.t.text_secondary,
                        margin=(0, 2, 0, 2),
                    )
                )
        elif blk.type == "quote":
            self._rows.append(
                RichTextRow(
                    self,
                    blk.spans,
                    font_size=int(self.font_size * 0.9),
                    color=self.t.text_secondary,
                    margin=(8, 6, 8, 6),
                    border_left=2,
                )
            )
        elif blk.type == "code":
            self._rows.append(CodeRow(self, blk.code, blk.lang))
        elif blk.type == "hr":
            self._rows.append(HrRow(self))
        elif blk.type == "image":
            self._rows.append(ImageRow(self, blk))
        elif blk.type == "table":
            for row in blk.rows:
                self._rows.append(
                    RichTextRow(
                        self,
                        [md.Span("  |  ".join(str(c) for c in row))],
                        font_size=int(self.font_size * 0.9),
                        color=self.t.text_secondary,
                        margin=(0, 1, 0, 1),
                    )
                )
        else:
            # 未知块,降级为段落
            if hasattr(blk, "spans"):
                self._append_rich_paragraph(blk.spans)

    def _append_rich_paragraph(self, spans, **_kw) -> None:
        self._rows.append(
            RichTextRow(
                self,
                spans,
                font_size=self.font_size,
                color=self.t.text_secondary,
                margin=(0, 4, 0, 4),
            )
        )

    # ── 分页 ─────────────────────────────────────────────────
    def _paginate(self, rows: list[Row], title_h: int) -> list[list[Row]]:
        if not rows:
            return [[]]
        pages: list[list[Row]] = []
        cur: list[Row] = []
        lead_gap = self.v_pad if (self.title and self.show_title) else self.v_pad // 2
        cur_h = title_h + lead_gap
        for row in rows:
            if cur and cur_h + row.height > self.page_max_height:
                pages.append(cur)
                cur = []
                cur_h = title_h + lead_gap
            cur.append(row)
            cur_h += row.height + self.msg_gap
        if cur:
            pages.append(cur)

        if len(pages) > self.max_pages:
            raise TooManyPages(
                f"聊天卡片共 {len(pages)} 页,超过上限 {self.max_pages} 页"
            )
        return pages

    # ── 绘制单页 ─────────────────────────────────────────────
    def _draw_page(self, title_h: int, rows: list[Row], _all: list[Row]) -> Image.Image:
        # 首行内容与标题之间留 v_pad 间隙(若无标题则顶部留 v_pad//2,与下方绘制起点一致)
        lead_gap = self.v_pad if (self.title and self.show_title) else self.v_pad // 2
        # 计算总高。各行高度 + 行间间距(与绘制循环一致)+ 标题 + 顶部间隙 + 底部留白。
        # 若忽略行间 msg_gap,多行时底边会被挤出画布、内容超长被裁。
        # 无标题时 lead_gap 必须计入 v_pad//2 —— 绘制起点就是 v_pad//2,
        # 否则顶部 padding 会吃掉底部留白,最后一个气泡贴底被裁。
        total_h = (
            sum(r.height for r in rows)
            + self.msg_gap * max(0, len(rows) - 1)
            + title_h
            + lead_gap
            + self.v_pad // 2
        )
        # 高宽比保护,避免过度拉伸
        img = self.new_page(max(80, min(40000, total_h)))
        y = 0
        # 绘制标题 + 间隙
        if self.title and self.show_title:
            y = self._draw_title(img, self.title) + self.v_pad
        else:
            y = self.v_pad // 2
        # 绘制各行(垂直堆叠)+ 行间加 msg_gap 间隙
        for idx, row in enumerate(rows):
            row.draw(img, y)
            y += row.height
            if idx < len(rows) - 1:
                y += self.msg_gap
        # 底部留白
        return img.convert("RGB")

    def _draw_title(self, img: Image.Image, title: str) -> int:
        """绘制顶部标题栏,返回下一个 y。"""
        th = self.title_height()
        draw = ImageDraw.Draw(img)
        # 半透明遮罩
        overlay = Image.new("RGBA", (self.width, th), _rgba(self.t.header_bg))
        img.alpha_composite(overlay, (0, 0))
        # 文字(emoji / 符号逐字符回退)
        font = load_font(self.title_font_size, bold=True)
        tw = _measure_fallback(draw, title, self.title_font_size, bold=True)
        _draw_fallback(
            img,
            (
                (self.width - tw) / 2,
                (th - font.getmetrics()[0] - font.getmetrics()[1]) / 2,
            ),
            title,
            self.title_font_size,
            _rgba(self.t.header_text),
            bold=True,
        )
        # 底部细线
        draw.line([(0, th - 1), (self.width, th - 1)], fill=_rgba(self.t.card_border))
        return int(th)


# ═════════════════════════════════════════════════════════════════════
# 便捷入口
# ═════════════════════════════════════════════════════════════════════


def render_narrative(
    text: str,
    *,
    avatars: dict[str, AvatarSource] | None = None,
    theme: str | Theme = "light",
    width: int = 1024,
    font_size: int = 34,
    title: str = "",
    is_self: SelfNamePred | None = None,
    max_pages: int = 5,
    **kw,
) -> list[Image.Image]:
    """渲染 markdown 剧情文本为一张或多张聊天截图。

    Args:
        text: 剧情 markdown 文本。
        avatars: 角色名 → 头像(路径 / URL / PIL Image)。
        theme: 'light' 或 'dark' 或 Theme。
        width: 画布宽度(px)。
        font_size: 正文字号(px)。
        title: 显示在顶部的标题(空则无)。
        is_self: 判断角色名是否为自己(靠右蓝泡)的函数。
        max_pages: 最大分页数。

    Returns:
        PIL Image 列表。

    Raises:
        TooManyPages: 分页超限。
    """
    blocks = md.parse_blocks(text)

    # 自动提取模型自带的标题:文本中第一个一级标题 `# xxx` → 顶部标题栏。
    # 模型输出的标题始终优先;没有一级标题时才 fallback 到传入的 title(chat_card_title 配置)。
    config_title = title or ""
    auto_title = ""  # 模型标题
    new_blocks: list[md.Block] = []
    for blk in blocks:
        if blk.type == "heading" and getattr(blk, "level", 1) == 1:
            # 只取第一个一级标题作为顶部标题,并从正文移除(不重复渲染)
            if not auto_title:
                auto_title = md.plain_text(blk.spans).strip()
            continue
        new_blocks.append(blk)
    auto_title = auto_title or config_title

    renderer = ChatRenderer(
        width=width,
        font_size=font_size,
        theme=theme,
        title=auto_title,
        is_self=is_self,
        max_pages=max_pages,
    )
    return renderer.render(new_blocks, avatars=avatars, title=auto_title or None)
