"""Markdown → 中间表示。

纯解析,无绘制逻辑。把 LLM 输出的 markdown 文本解析为块(Block)列表,
每个块内含富文本段(Span)。支持:

块级:标题 / 段落 / 对白 / 列表 / 引用 / 代码块 / 表格 / 水平线 / 图片
行内:粗体 ** ** / 斜体 * * / 删除线 ~~ ~~ / 行内代码 ` ` / 链接 [t](u)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "Block",
    "Span",
    "parse_blocks",
    "plain_text",
]

# ═════════════════════════════════════════════════════════════════════
# 数据结构
# ═════════════════════════════════════════════════════════════════════


@dataclass
class Span:
    """富文本片段。"""

    text: str
    bold: bool = False
    italic: bool = False
    strike: bool = False
    code: bool = False
    link: str = ""


@dataclass
class Block:
    """块基类。"""

    type: str = "paragraph"
    spans: list[Span] = field(default_factory=list)


@dataclass
class Heading(Block):
    def __init__(self, level: int, spans: list[Span]):
        super().__init__("heading", spans)
        self.level = level


@dataclass
class Dialogue(Block):
    """对白块。"""

    def __init__(
        self,
        speaker: str,
        content: str,
        protagonist: bool = False,
        avatar: str | None = None,
    ):
        super().__init__("dialogue")
        self.speaker = speaker.strip()
        self.protagonist = bool(protagonist)
        # LLM 可通过 av 属性显式指定该气泡使用名单里哪张已有头像(默认按角色名匹配)
        self.avatar = (avatar or "").strip() or None
        self.spans = parse_inline(content.strip())


@dataclass
class ListBlock(Block):
    def __init__(self, ordered: bool):
        super().__init__("list")
        self.ordered = ordered
        self.items: list[list[Span]] = []


@dataclass
class Quote(Block):
    def __init__(self, spans: list[Span]):
        super().__init__("quote", spans)


@dataclass
class CodeBlock(Block):
    def __init__(self, code: str, lang: str = ""):
        super().__init__("code")
        self.code = code
        self.lang = lang


@dataclass
class TableBlock(Block):
    def __init__(self):
        super().__init__("table")
        self.header: list[str] = []
        self.rows: list[list[str]] = []


@dataclass
class HR(Block):
    def __init__(self):
        super().__init__("hr")


@dataclass
class ImageBlock(Block):
    def __init__(self, alt: str, url: str):
        super().__init__("image")
        self.alt = alt
        self.url = url


def plain_text(spans: list[Span]) -> str:
    return "".join(s.text for s in spans)


# ═════════════════════════════════════════════════════════════════════
# 行内解析
# ═════════════════════════════════════════════════════════════════════

_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_CODE_RE = re.compile(r"`([^`\n]+)`")
_STYLE_RE = re.compile(
    r"(\*\*[^*\n]+\*\*"
    r"|~~[^~\n]+~~"
    r"|\*[^*\n]+\*"
    r")"
)


def parse_inline(text: str) -> list[Span]:
    """把纯文本解析为 span 列表。"""
    if not text:
        return []
    spans: list[Span] = []

    # 0. 兑底清理:残缺/未闭合的结构化标签不进画面
    text = _STRAY_TAG_RE.sub("", text)
    if not text:
        return []

    # 1. 提取行内代码
    tmp: list[Span] = []
    for idx, part in enumerate(_CODE_RE.split(text)):
        if not part:
            continue
        if idx % 2 == 1:
            tmp.append(Span(text=part, code=True))
        else:
            tmp.extend(_parse_style(part))

    # 2. 链接 优先于纯文本
    spans = parse_links(tmp)
    return _merge(spans)


def _parse_style(text: str) -> list[Span]:
    """解析单个片段内的粗体/斜体/删除线。"""
    if not text:
        return []
    out: list[Span] = []
    for seg in _STYLE_RE.split(text):
        if not seg:
            continue
        if seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
            out.append(Span(text=seg[2:-2], bold=True))
        elif seg.startswith("~~") and seg.endswith("~~") and len(seg) > 4:
            out.append(Span(text=seg[2:-2], strike=True))
        elif (
            seg.startswith("*")
            and seg.endswith("*")
            and len(seg) > 2
            and not seg.startswith("**")
        ):
            out.append(Span(text=seg[1:-1], italic=True))
        else:
            out.append(Span(text=seg))
    return out


def parse_links(spans: list[Span]) -> list[Span]:
    """从 spans 中提取链接。"""
    out: list[Span] = []
    for sp in spans:
        if sp.link or sp.code or not sp.text:
            out.append(sp)
            continue
        text = sp.text
        last = 0
        for m in _LINK_RE.finditer(text):
            if m.start() > last:
                out.append(Span(text=text[last : m.start()], bold=sp.bold))
            out.append(Span(text=m.group(1), link=m.group(2), bold=sp.bold))
            last = m.end()
        if last < len(text):
            out.append(Span(text=text[last:], bold=sp.bold))
        elif last == 0:
            out.append(sp)
    return out


def _merge(spans: list[Span]) -> list[Span]:
    """合并相同样式的相邻片段。"""
    if not spans:
        return []
    out: list[Span] = [spans[0]]
    for s in spans[1:]:
        last = out[-1]
        if (
            last.bold == s.bold
            and last.italic == s.italic
            and last.strike == s.strike
            and last.code == s.code
            and last.link == s.link
        ):
            last.text += s.text
        else:
            out.append(s)
    return out


# ═════════════════════════════════════════════════════════════════════
# 块级解析
# ═════════════════════════════════════════════════════════════════════

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*)$")
_FENCE_OPEN = re.compile(r"^ {0,3}(```|~~~)(\w*)\s*$")
_FENCE_CLOSE = re.compile(r"^ {0,3}(```|~~~)\s*$")
_HR_RE = re.compile(r"^ {0,3}([-*_]) ?(\1 ?){2,}$")
_UL_RE = re.compile(r"^ {0,3}[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^ {0,3}\d+[.)、]\s+(.*)$")
_QUOTE_RE = re.compile(r"^ {0,3}>\s?(.*)$")
_IMG_LINE_RE = re.compile(r"^!\[([^\]]*)\]\(([^)\s]+)\)\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")

# ════════════════════════════════════════════════════════════════════
# 结构化标签(聊天卡片模式)
# ════════════════════════════════════════════════════════════════════
# 对白气泡: <d name="角色名" av="头像名" me>台词</d>
#   - name 必填;av 可选(借用名单里另一张已有头像);me 可选(主角标记)
# 短旁白胶囊: <c>短旁白文字</c>
# 其余行一律按普通 markdown 解析 —— 不再做「冒号前缀」启发式猜测,
# 从根上杜绝把叙述句误识别成对白、短段落被自动折成胶囊。
_DLG_TAG_RE = re.compile(
    r"^<\s*d(?:lg)?\b([^>]*)>\s*(.*?)\s*</\s*(?:dlg|d)\s*>\s*$",
    re.IGNORECASE | re.DOTALL,
)
_CAP_TAG_RE = re.compile(
    r"^<\s*c\b[^>]*>\s*(.*?)\s*</\s*c\s*>\s*$", re.IGNORECASE | re.DOTALL
)
_ATTR_NAME_RE = re.compile(r'name\s*=\s*"([^"]*)"', re.IGNORECASE)
_ATTR_AV_RE = re.compile(r'av\s*=\s*"([^"]*)"', re.IGNORECASE)
# 主角标记:独立的 me 属性(me 或 me=true);\b 保证不会匹配到 name 里的 me
_ATTR_ME_RE = re.compile(r"\bme\b(?=\s|$|=)", re.IGNORECASE)

# 兑底清理:未闭合 / 残缺的标签不允许原样漏进画面
_STRAY_TAG_RE = re.compile(r"</?\s*(?:dlg|d|c)\b[^>]*>", re.IGNORECASE)


def _parse_dialogue_tag(line: str) -> Dialogue | None:
    """解析 <d name="角色名" av="头像名" me>台词</d>;不匹配返回 None。

    属性顺序任意;name 必填;av 可选(借用名单里另一张已有头像);
    me 可选(主角标记,气泡靠右显示)。
    """
    m = _DLG_TAG_RE.match(line.strip())
    if not m:
        return None
    attrs, content = m.group(1), m.group(2).strip()
    name_m = _ATTR_NAME_RE.search(attrs)
    if not name_m or not name_m.group(1).strip() or not content:
        return None
    av_m = _ATTR_AV_RE.search(attrs)
    protagonist = bool(_ATTR_ME_RE.search(attrs))
    return Dialogue(
        name_m.group(1).strip(),
        _strip_quotes(content),
        protagonist,
        avatar=(av_m.group(1) if av_m else None),
    )


def _parse_capsule_tag(line: str) -> Block | None:
    """解析 <c>短旁白文字</c>(居中灰胶囊);不匹配返回 None。"""
    m = _CAP_TAG_RE.match(line.strip())
    if not m or not m.group(1).strip():
        return None
    return Block("capsule", parse_inline(m.group(1).strip()))


def _strip_quotes(text: str) -> str:
    pairs = [("「", "」"), ("『", "』"), ("“", "”"), ('"', '"'), ("'", "'")]
    for op, cl in pairs:
        if len(text) >= 2 and text[0] == op and text[-1] == cl:
            return text[1:-1]
    return text


def parse_blocks(text: str) -> list[Block]:
    blocks: list[Block] = []
    lines = (text or "").replace("\r\n", "\n").split("\n")

    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 空白行
        if not stripped:
            i += 1
            continue

        # 标题
        m = _HEADING_RE.match(line)
        if m:
            # 计算 level 并去掉原有 # 的影响
            level = min(len(m.group(1)), 6)
            body = m.group(2).strip()
            blocks.append(Heading(level, parse_inline(body)))
            i += 1
            continue

        # 代码块
        m = _FENCE_OPEN.match(line)
        if m:
            lang = m.group(2) or ""
            i += 1
            code_lines = []
            while i < n and not _FENCE_CLOSE.match(lines[i]):
                code_lines.append(lines[i])
                i += 1
            if i < n:  # 跳过闭合
                i += 1
            else:  # 未闭合,保留后续所有行
                pass
            blocks.append(CodeBlock("\n".join(code_lines), lang))
            continue

        # 水平线
        if _HR_RE.match(line) and len(stripped) >= 3:
            blocks.append(HR())
            i += 1
            continue

        # 图片单行
        m = _IMG_LINE_RE.match(line)
        if m:
            blocks.append(ImageBlock(m.group(1), m.group(2)))
            i += 1
            continue

        # 结构化对白 / 胶囊标签(需在段落之前;不匹配则按普通文本处理)
        dlg = _parse_dialogue_tag(stripped)
        if dlg is not None:
            blocks.append(dlg)
            i += 1
            continue
        cap = _parse_capsule_tag(stripped)
        if cap is not None:
            blocks.append(cap)
            i += 1
            continue

        # 无序列表(连续收集)
        if _UL_RE.match(line):
            lb = ListBlock(ordered=False)
            while i < n:
                m_ul = _UL_RE.match(lines[i])
                if not m_ul:
                    break
                lb.items.append(parse_inline(m_ul.group(1).strip()))
                i += 1
            blocks.append(lb)
            continue

        # 有序列表
        if _OL_RE.match(line):
            lb = ListBlock(ordered=True)
            while i < n:
                m_ol = _OL_RE.match(lines[i])
                if not m_ol:
                    break
                lb.items.append(parse_inline(m_ol.group(1).strip()))
                i += 1
            blocks.append(lb)
            continue

        # 引用
        if _QUOTE_RE.match(line):
            quote_spans: list[Span] = []
            while i < n:
                mq = _QUOTE_RE.match(lines[i])
                if not mq:
                    break
                if quote_spans:
                    quote_spans.append(Span(text=" "))
                quote_spans.extend(parse_inline(mq.group(1)))
                i += 1
            blocks.append(Quote(quote_spans))
            continue

        # 表格
        if _TABLE_ROW_RE.match(line):
            try:
                header = _split_table_row(line)
                if header and len(header) >= 1 and i + 1 < n:
                    sep = lines[i + 1].strip()
                    if _TABLE_SEP_RE.match(sep):
                        tb = TableBlock()
                        tb.header = header
                        i += 2
                        while i < n and _TABLE_ROW_RE.match(lines[i]):
                            tb.rows.append(_split_table_row(lines[i]))
                            i += 1
                        blocks.append(tb)
                        continue
            except (ValueError, IndexError):
                pass  # 表格解析失败则按普通段落处理

        # 普通段落:收集到空行
        para_spans: list[Span] = []
        while i < n and lines[i].strip():
            if not para_spans:
                para_spans.extend(parse_inline(lines[i].strip()))
            else:
                para_spans.append(Span(text="\n"))
                para_spans.extend(parse_inline(lines[i].strip()))
            i += 1
        blocks.append(Block("paragraph", _merge(para_spans)))

    return blocks


def _split_table_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip("|").split("|")]
