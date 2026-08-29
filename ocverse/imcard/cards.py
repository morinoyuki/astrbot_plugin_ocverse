"""游戏卡片构建器:角色卡 / 事件卡 / 世界卡 / 日志卡 / 运势卡 / 晨报 / 互动卡。

所有 builder 返回 list[PIL.Image];行序列经 ChatRenderer.render_rows 绘制。
"""

from __future__ import annotations

import time

from PIL import Image

from . import markdown as md
from .engine import ChatRenderer
from .rows import (
    AvatarHeadRow,
    ChoiceRow,
    DialogueRow,
    EmptyRow,
    PanelRow,
    PillRow,
    RichTextRow,
    StatBarRow,
    TagRow,
)

# 属性条配色
ATTR_COLORS = {
    "force": "#E06C55",
    "agility": "#6FC9AE",
    "intellect": "#7FB2E8",
    "charm": "#E89AC0",
    "luck": "#E8C97F",
    "sanity": "#A89AE8",
    "stamina": "#E8A87F",
    "mood": "#8FD8A8",
}


def _mk(cfg: dict) -> ChatRenderer:
    return ChatRenderer(
        width=int(cfg.get("card_width", 1024)),
        font_size=int(cfg.get("card_font_size", 34)),
        theme=str(cfg.get("card_theme", "dark") or "dark"),
    )


def _avatar_img(path: str | None) -> Image.Image | None:
    if not path:
        return None
    try:
        return Image.open(path).convert("RGBA")
    except Exception:
        return None


def _para(r, text, color=None, size=None, margin=(0, 4, 0, 4), bold=False):
    return RichTextRow(r, [md.Span(text)], font_size=size, color=color, margin=margin, bold=bold)


def _dialogue_rows(r, dialogues, self_name: str = "", avatars: dict | None = None) -> list:
    """IM 聊天体多轮对话气泡(轻小说式你来我往)。
    self_name 为 POV 角色名:匹配到的发言者气泡靠右(自己),其余靠左(对方)。
    avatars: 名字→头像(路径或 PIL 图),按说话人挂头像;未设置者用名字首字占位。
    防独角戏:只有一个说话人的对话不渲染(宁缺毋滥,由叙述承担表达)。"""
    dlg = []
    for d in (dialogues or [])[:8]:
        sp = str(d.get("speaker") or "").strip()
        tx = str(d.get("text") or "").strip()
        if sp and tx:
            dlg.append((sp[:12], tx[:100]))
    if len({sp for sp, _ in dlg}) < 2:
        return []
    rows = []
    for sp, tx in dlg:
        av = (avatars or {}).get(sp)
        if not av:  # 宽松匹配:说话人名带括号/前缀时仍能挂上头像
            for k, v in (avatars or {}).items():
                if k and (k in sp or sp in k):
                    av = v
                    break
        rows.append(DialogueRow(
            r, speaker=sp, spans=[md.Span(tx)],
            is_self=bool(self_name and sp == self_name),
            avatar=av or None,
        ))
    return rows


def _hr(r, pad=(0, 2, 0, 2)):
    return EmptyRow(r, 6)


# ══════════════════════════ 角色卡 ══════════════════════════
def profile_card(ch, world, rels: list[tuple[str, int]], memories: list[str], cfg: dict,
                 rel_names: dict[str, str] | None = None, extra_badges: list[str] | None = None) -> list[Image.Image]:
    from ..config import ATTRS, exp_need
    from ..config import rel_label

    r = _mk(cfg)
    rows = []
    tags = list(ch.tags or [])
    badges = list(extra_badges or [])
    rows.append(AvatarHeadRow(
        r, avatar=_avatar_img(ch.avatar), name=ch.name,
        subtitle=f"Lv{ch.level} · {ch.title} · {ch.gender}",
        tags=tags,
        foot=f"入世于 {time.strftime('%Y-%m-%d', time.localtime(ch.created_at))}",
    ))
    # 特殊状态(囚禁/束缚等)横幅:优先展示,提醒无法自由行动
    _st = (ch.flags or {}).get("_state")
    if isinstance(_st, dict) and (_st.get("type") or _st.get("reason")):
        _styp = str(_st.get("type") or "特殊状态")
        _srs = str(_st.get("reason") or "").strip()
        _stext = f"⛓ {_styp}" + (f" ─ {_srs}" if _srs else "")
        rows.append(_para(r, _stext, size=int(r.font_size * 0.82),
                          color=r.t.warn if hasattr(r.t, "warn") else "#E0A04A"))
    # 六维
    stat_items = [(nm, ch.attrs.get(k, 0), 100, ATTR_COLORS.get(k, "#888888")) for k, nm in ATTRS]
    # 资源
    stat_items.append(("体力", ch.stamina, 100, ATTR_COLORS["stamina"]))
    stat_items.append(("心情", ch.mood, 100, ATTR_COLORS["mood"]))
    rows.append(PanelRow(r, "属性", lambda: [StatBarRow(r, stat_items)]))
    # 资源行
    need = exp_need(ch.level)
    res_line = f"💰 金币 {ch.gold}    ⭐ 经验 {ch.exp}/{need}    🌀 经历变动 {int((ch.flags or {}).get('shifts', 0))} 次"
    rows.append(_para(r, res_line, size=int(r.font_size * 0.82), color=r.t.text_secondary))
    # 背景设定
    if ch.backstory:
        story = ch.backstory if len(ch.backstory) <= 160 else ch.backstory[:157] + "…"
        rows.append(PanelRow(r, "背景设定", lambda: [
            _para(r, story, color=r.t.text_secondary, size=int(r.font_size * 0.8)),
        ]))
    # 称号徽章
    if badges:
        rows.append(TagRow(r, badges))
    # 羁绊
    if rels:
        rel_rows = []
        for uid, score in rels[:4]:
            nm = (rel_names or {}).get(uid, uid[:8])
            bar = "♥" * max(1, min(5, int(abs(score) / 20) + 1))
            rel_rows.append(_para(r, f"{nm}  {score:+d} 「{rel_label(score)}」 {bar if score > 0 else '〰'}",
                                  size=int(r.font_size * 0.78), color=r.t.text_secondary))
        rows.append(PanelRow(r, "羁绊", rel_rows))
    # 近期记忆
    if memories:
        rows.append(PanelRow(r, "近期记忆", lambda: [
            _para(r, f"· {m}", size=int(r.font_size * 0.78), color=r.t.text_muted) for m in memories[:3]
        ]))
    # 所在世界
    if world:
        rows.append(_para(r, f"📍 现居《{world.name}》[{world.genre}]",
                          size=int(r.font_size * 0.8), color=r.t.link))
    return r.render_rows(rows, title="分身界 · 角色卡")


# ══════════════════════════ 事件卡 ══════════════════════════
def event_card(view: dict, cfg: dict) -> list[Image.Image]:
    r = _mk(cfg)
    p = view.get("payload") or {}
    rows = []
    lead = view.get("char_name") or ""
    if lead:
        rows.append(PillRow(r, f"◈ {view.get('world_name', '')} · {lead} 的遭遇"))
    else:
        rows.append(PillRow(r, f"◈ {view.get('world_name', '')} · 全员共同遭遇"))
    rows.append(_para(r, str(p.get("scene", "")), color=r.t.text, margin=(6, 6, 0, 6)))
    npc = p.get("npc")
    if npc:
        rows.append(EmptyRow(r, 4))
        rows.append(DialogueRow(r, speaker=str(npc), spans=[md.Span("(就在旁边)")]))
    rows.append(EmptyRow(r, 6))
    for i, opt in enumerate(p.get("options") or [], 1):
        rows.append(ChoiceRow(r, i, str(opt.get("label", "?")), str(opt.get("hint", ""))))
    rows.append(EmptyRow(r, 4))
    rows.append(_para(r, f"回复本卡 +「/分身 选择 编号」做出抉择 · {view.get('expires_min', 45)} 分钟内有效",
                      size=int(r.font_size * 0.72), color=r.t.text_muted))
    return r.render_rows(rows, title=f"遭遇 · {p.get('title', '突发状况')}")


def result_card(view: dict, cfg: dict) -> list[Image.Image]:
    r = _mk(cfg)
    rows = []
    rows.append(PillRow(r, f"「{view.get('event_title', '')}」→ {view.get('chosen', '')}"))
    rows.append(_para(r, view.get("narration", ""), color=r.t.text, margin=(6, 8, 0, 6)))
    dlg = _dialogue_rows(r, view.get("dialogues"), view.get("char_name", ""), view.get("avatars"))
    if dlg:
        rows.append(EmptyRow(r, 4))
        rows.extend(dlg)
    changes = view.get("changes") or []
    if changes:
        rows.append(EmptyRow(r, 4))
        rows.append(TagRow(r, changes))
    return r.render_rows(rows, title=view.get("card_title") or "抉择 · 结算")


# ══════════════════════════ 世界卡 ══════════════════════════
def world_card(w, cfg: dict, is_current: bool = True, day: int = 1,
               world_mem: list[str] | None = None) -> list[Image.Image]:
    r = _mk(cfg)
    rows = []
    head = f"《{w.name}》" + (f" · 第{day}天" if is_current else "")
    rows.append(AvatarHeadRow(r, name=head, subtitle=w.genre, foot=w.atmosphere))
    rows.append(PanelRow(r, "世界", lambda: [_para(r, w.desc, color=r.t.text_secondary, size=int(r.font_size * 0.82))]))
    if w.rules:
        rows.append(PanelRow(r, "规则", lambda: [_para(r, f"{i}. {x}", color=r.t.text_secondary, size=int(r.font_size * 0.78)) for i, x in enumerate(w.rules, 1)]))
    if w.features:
        rows.append(PanelRow(r, "独特之处", lambda: [_para(r, f"✦ {x}", color=r.t.link, size=int(r.font_size * 0.78)) for x in w.features]))
    if w.npcs:
        def npc_rows():
            out = []
            for n in w.npcs:
                out.append(_para(
                    r, f"「{n.get('name', '?')}」{n.get('role', '')} — {n.get('persona', '')}",
                    color=r.t.text_secondary, size=int(r.font_size * 0.8)))
                if n.get("hook"):
                    out.append(_para(r, f"　↳ {n['hook']}", color=r.t.text_muted, size=int(r.font_size * 0.72)))
            return out
        rows.append(PanelRow(r, "NPC", npc_rows))
    if world_mem:
        # 世界记忆:全局事件 / 主线 / NPC·设施流转 的近况
        def mem_rows():
            out = []
            for t in world_mem[:5]:
                out.append(_para(r, f"· {t}", color=r.t.text_secondary,
                                 size=int(r.font_size * 0.74)))
            return out
        rows.append(PanelRow(r, "世界记忆(近况)", mem_rows))
    foot = "这是你们当前生活的世界" if is_current else "尚在沉眠,等待世界变动降临"
    rows.append(_para(r, foot, size=int(r.font_size * 0.72), color=r.t.text_muted))
    return r.render_rows(rows, title="世界档案")


def world_list_card(visited, pending, cur_id: int, cfg: dict) -> list[Image.Image]:
    r = _mk(cfg)
    rows = []
    if not visited:
        rows.append(_para(r, "还没有任何已到达的世界。", color=r.t.text_secondary))
    for i, w in enumerate(visited, 1):
        mark = "▶" if w.id == cur_id else f"{i}."
        rows.append(_para(r, f"{mark} 《{w.name}》[{w.genre}]" + (" ← 当前" if w.id == cur_id else ""),
                          color=r.t.text if w.id == cur_id else r.t.text_secondary,
                          size=int(r.font_size * 0.9), bold=(w.id == cur_id)))
        rows.append(_para(r, f"　{w.desc[:46]}…", size=int(r.font_size * 0.7), color=r.t.text_muted))
    if pending:
        rows.append(EmptyRow(r, 8))
        def pending_rows():
            return [
                _para(r, f"· 《{w.name}》{w.desc[:30]}…", color=r.t.text_muted, size=int(r.font_size * 0.75))
                for w in pending
            ]
        rows.append(PanelRow(r, "🔒 沉眠中的自设世界(需等待世界变动降临)", pending_rows))
    rows.append(EmptyRow(r, 4))
    rows.append(_para(r, "自由穿越:「/分身 穿越世界 编号/名称」(穿越过才能自由穿越)",
                      size=int(r.font_size * 0.72), color=r.t.text_muted))
    return r.render_rows(rows, title="世界列表")


# ══════════════════════════ 日志卡 ══════════════════════════
def log_card(entries: list[dict], page: int, total: int, scope: str, cfg: dict,
             name_map: dict[str, str] | None = None) -> list[Image.Image]:
    from ..config import LOG_KINDS

    r = _mk(cfg)
    rows = []
    if not entries:
        rows.append(_para(r, "这里还什么都没有发生。", color=r.t.text_muted))
    for e in entries:
        kind = LOG_KINDS.get(e.get("kind", "misc"), "·")
        ts = time.strftime("%m-%d %H:%M", time.localtime(e.get("ts", 0)))
        uid = e.get("uid") or ""
        who = (name_map or {}).get(uid, "")
        prefix = f"{who} · " if who else ""
        rows.append(_para(r, f"{kind} {ts}  {prefix}{e.get('text', '')}",
                          size=int(r.font_size * 0.78), color=r.t.text_secondary,
                          margin=(0, 3, 0, 3)))
    rows.append(EmptyRow(r, 4))
    rows.append(_para(r, f"第 {page}/{max(1, total)} 页 · {scope}",
                      size=int(r.font_size * 0.7), color=r.t.text_muted))
    return r.render_rows(rows, title="世界日志")


# ══════════════════════════ 运势 / 晨报 ══════════════════════════
def fortune_card(f: dict, cfg: dict) -> list[Image.Image]:
    r = _mk(cfg)
    rows = []
    rows.append(AvatarHeadRow(r, name=f"{f['name']} 的今日运势", subtitle=f"{f['day']}"))
    def sign_rows():
        return [
            _para(r, f"【{f['grade']}】幸运色:{f['color']} · 幸运数字:{f['number']}",
                  color=r.t.text, size=int(r.font_size * 0.95)),
            _para(r, f"『{f['line']}』", color=r.t.text_secondary, size=int(r.font_size * 0.85)),
        ]
    rows.append(PanelRow(r, "签文", sign_rows))
    return r.render_rows(rows, title="每日运势")


def morning_card(view: dict, cfg: dict) -> list[Image.Image]:
    r = _mk(cfg)
    rows = []
    rows.append(_para(r, view.get("brief", "今日无事发生。"), color=r.t.text, margin=(4, 8, 0, 4)))
    if view.get("watch"):
        rows.append(_para(r, f"⚠ {view['watch']}", color=r.t.link, size=int(r.font_size * 0.82)))
    return r.render_rows(rows, title=f"晨报 · {view.get('world_name', '')}")


def arrive_card(view: dict, cfg: dict) -> list[Image.Image]:
    """世界变动/穿越/初次建立的抵达播报。"""
    r = _mk(cfg)
    w = view.get("world")
    rows = []
    via = view.get("via", "")
    lead = {"shift": "🌀 世界变动!", "travel": "✈ 穿越完成", "init": "☁ 帷幕拉开"}.get(via, "☁ 抵达")
    rows.append(PillRow(r, f"{lead}  《{view.get('prev_name') or '虚空'}》 → 《{w.name}》"))
    rows.append(_para(r, view.get("narration", ""), color=r.t.text, margin=(6, 8, 0, 6)))
    tips = view.get("tips") or []
    if tips:
        rows.append(PanelRow(r, "新来者须知", lambda: [
            _para(r, f"· {t}", color=r.t.text_secondary, size=int(r.font_size * 0.8)) for t in tips
        ]))
    rows.append(_para(r, f"[{w.genre}] {w.atmosphere}", size=int(r.font_size * 0.72), color=r.t.text_muted))
    return r.render_rows(rows, title=f"抵达 · {w.name}")


# ══════════════════════════ 互动卡 ══════════════════════════
def interact_card(view: dict, cfg: dict) -> list[Image.Image]:
    r = _mk(cfg)
    rows = []
    rows.append(PillRow(r, f"{view.get('a_name', '?')} ⇄ {view.get('b_name', '?')} ·「{view.get('mode', '互动')}」"))
    rows.append(_para(r, view.get("narration", ""), color=r.t.text, margin=(6, 8, 0, 6)))
    dlg = _dialogue_rows(r, view.get("dialogues"), view.get("a_name", ""), view.get("avatars"))
    if dlg:
        rows.append(EmptyRow(r, 4))
        rows.extend(dlg)
    changes = view.get("changes") or []
    if changes:
        rows.append(EmptyRow(r, 4))
        rows.append(TagRow(r, changes))
    if "rel" in view:
        rows.append(_para(r, f"💞 羁绊 {view['rel']:+d} 「{view.get('rel_label', '')}」",
                          size=int(r.font_size * 0.82), color=r.t.link))
    return r.render_rows(rows, title="群友互动")


def npc_card(view: dict, cfg: dict) -> list[Image.Image]:
    r = _mk(cfg)
    npc = view.get("npc") or {}
    rows = []
    rows.append(PillRow(r, f"{view.get('char_name', '?')} ☂ {npc.get('name', 'NPC')}"))
    rows.append(_para(r, view.get("narration", ""), color=r.t.text_secondary, margin=(6, 6, 0, 6),
                      size=int(r.font_size * 0.85)))
    dlg = _dialogue_rows(r, view.get("dialogues"), view.get("char_name", ""), view.get("avatars"))
    if dlg:
        rows.append(EmptyRow(r, 4))
        rows.extend(dlg)
    elif view.get("reply"):
        rows.append(DialogueRow(r, speaker=str(npc.get("name", "NPC")), spans=[md.Span(view.get("reply"))]))
    changes = view.get("changes") or []
    if changes:
        rows.append(EmptyRow(r, 4))
        rows.append(TagRow(r, changes))
    return r.render_rows(rows, title=f"NPC · {npc.get('name', '')}")


def act_card(view: dict, cfg: dict) -> list[Image.Image]:
    r = _mk(cfg)
    rows = []
    rows.append(PillRow(r, view.get("action_pill", "行动")))
    rows.append(_para(r, view.get("narration", ""), color=r.t.text,
                      margin=(6, 8, 0, 6)))
    dlg = _dialogue_rows(r, view.get("dialogues"), view.get("char_name", ""), view.get("avatars"))
    if dlg:
        rows.append(EmptyRow(r, 4))
        rows.extend(dlg)
    changes = view.get("changes") or []
    if changes:
        rows.append(EmptyRow(r, 4))
        rows.append(TagRow(r, changes))
    wname = view.get("world_name", "")
    if wname:
        rows.append(EmptyRow(r, 2))
        rows.append(_para(r, f"行动发生地 · 《{wname}》", size=int(r.font_size * 0.72),
                          color=r.t.text_muted))
    return r.render_rows(rows, title=view.get("action_name", "行动"))


# ══════════════════════════ 兼职卡(上工 / 下班结算)══════════════════════════
def work_card(view: dict, cfg: dict) -> list[Image.Image]:
    """兼职时段卡:phase=start 上工 / phase=done 自动下班结算(含NPC同事道别)。"""
    r = _mk(cfg)
    rows = []
    phase = view.get("phase") or "start"
    spot = view.get("spot") or "某处"
    job = view.get("occupation") or "打零工"
    wn = view.get("world_name", "")
    if phase == "done":
        rows.append(PillRow(r, f"⚒ 下班收工 · {spot}"))
        rows.append(_para(r, view.get("narration", ""), color=r.t.text, margin=(6, 8, 0, 6)))
        dlg = _dialogue_rows(r, view.get("dialogues"), view.get("char_name", ""), {})
        if dlg:
            rows.append(EmptyRow(r, 4))
            rows.extend(dlg)
        changes = view.get("changes") or []
        earn = view.get("earn", 0)
        if earn:
            changes = list(changes) + [f"金币+{earn}"]
        if changes:
            rows.append(EmptyRow(r, 4))
            rows.append(TagRow(r, changes))
        hours = view.get("hours", 0)
        col = (view.get("colleague") or "").strip()
        foot = f"共约 {hours} 小时" + (f" · 同事:「{col}」" if col else "")
        if wn:
            foot += f" · 《{wn}》"
        rows.append(EmptyRow(r, 2))
        rows.append(_para(r, foot, size=int(r.font_size * 0.72), color=r.t.text_muted))
        return r.render_rows(rows, title="兼职·下班")
    # 上工
    rows.append(PillRow(r, f"⚒ 上工 · {spot}"))
    rows.append(_para(r, f"你在「{spot}」谋到一份{job}的差事。", color=r.t.text, margin=(6, 8, 0, 6)))
    col = (view.get("colleague") or "").strip()
    hm = view.get("until_min", 120)
    rows.append(TagRow(r, [f"职业:{job}", f"约{hm}分钟后下班", f"体力-{view.get('cost', 25)}"]))
    if col:
        rows.append(_para(r, f"同班同事:「{col}」——到点自动结算下班。", color=r.t.text_secondary, size=int(r.font_size * 0.78)))
    rows.append(_para(r, "上班期间没法自由行动/互动,到点自动下班结算(不用再敲指令)。",
                      size=int(r.font_size * 0.72), color=r.t.text_muted))
    if wn:
        rows.append(EmptyRow(r, 2))
        rows.append(_para(r, f"《{wn}》", size=int(r.font_size * 0.72), color=r.t.text_muted))
    return r.render_rows(rows, title="兼职·上工")


# ══════════════════════════ 回家卡 ══════════════════════════
def home_card(view: dict, cfg: dict) -> list[Image.Image]:
    r = _mk(cfg)
    rows = []
    plot = view.get("plot") or {}
    rows.append(PillRow(r, f"🏠 回家 · {plot.get('name') or '家'}"))
    rows.append(_para(r, str(plot.get("desc") or ""), color=r.t.text_secondary,
                      size=int(r.font_size * 0.78), margin=(4, 4, 0, 4)))
    if view.get("narration"):
        rows.append(_para(r, view["narration"], color=r.t.text, margin=(6, 8, 0, 6)))
        dlg = _dialogue_rows(r, view.get("dialogues"), view.get("char_name", ""), {})
        if dlg:
            rows.append(EmptyRow(r, 2))
            rows.extend(dlg)
    changes = view.get("changes") or []
    if changes:
        rows.append(EmptyRow(r, 4))
        rows.append(TagRow(r, changes))
    wn = view.get("world_name", "")
    foot = f"· 每日一次 · 《{wn}》" if wn else "· 每日一次"
    rows.append(EmptyRow(r, 2))
    rows.append(_para(r, foot, size=int(r.font_size * 0.72), color=r.t.text_muted))
    return r.render_rows(rows, title="回宅休整")


# ══════════════════════════ 设施光顾卡 ══════════════════════════
def facility_card(view: dict, cfg: dict) -> list[Image.Image]:
    r = _mk(cfg)
    rows = []
    rows.append(PillRow(r, view.get("title", "去光顾")))
    rows.append(_para(r, view.get("narration", ""), color=r.t.text, margin=(6, 8, 0, 6)))
    dlg = _dialogue_rows(r, view.get("dialogues"), view.get("char_name", ""), view.get("avatars"))
    if dlg:
        rows.append(EmptyRow(r, 4))
        rows.extend(dlg)
    changes = view.get("changes") or []
    if changes:
        rows.append(EmptyRow(r, 4))
        rows.append(TagRow(r, changes))
    wn = view.get("world_name", "")
    if wn:
        rows.append(EmptyRow(r, 2))
        rows.append(_para(r, f"《{wn}》 · 每日每家限1次", size=int(r.font_size * 0.72),
                          color=r.t.text_muted))
    return r.render_rows(rows, title="设施光顾")


# ══════════════════════════ 统一渲染入口 ══════════════════════════
def render_views(views: list[dict], cfg: dict) -> list[Image.Image]:
    """把 game 层产出的 view dict 批量渲染为图片。"""
    out: list[Image.Image] = []
    for v in views:
        t = v.get("type")
        if t == "event":
            out += event_card(v, cfg)
        elif t == "result":
            out += result_card(v, cfg)
        elif t == "morning":
            out += morning_card(v, cfg)
        elif t == "arrive":
            out += arrive_card(v, cfg)
        elif t == "interact":
            out += interact_card(v, cfg)
        elif t == "npc":
            out += npc_card(v, cfg)
        elif t == "act":
            out += act_card(v, cfg)
        elif t == "work":
            out += work_card(v, cfg)
        elif t == "home":
            out += home_card(v, cfg)
        elif t == "facility":
            out += facility_card(v, cfg)
    return out


# ══════════════════════════ 帮助卡 / 名册 ══════════════════════════
def help_card(cfg: dict, sub_prefix: str = "/分身") -> list[Image.Image]:
    r = _mk(cfg)
    fs = int(r.font_size * 0.78)

    def sec(title, lines):
        return PanelRow(r, title, lambda: [_para(r, x, color=r.t.text_secondary, size=fs) for x in lines])

    rows = [
        sec("🎪 从这里开始", [
            f"{sub_prefix} 初始化世界 [世界观描述…] — 管理员铺设群世界(不填则 LLM 自由发挥)",
            f"{sub_prefix} 创建 <名字> [设定描述…] — 一句话自由描述,AI 整理人设并按设定分配初始属性(兼容竖线速写);一人一个分身",
            f"{sub_prefix} 设置头像 [生活角色名] (随指令发一张图,或回复一张图) — 给分身或生活角色换头像",
        ]),
        sec("📜 每天的生活", [
            "每天会在活跃时段随机触发事件:主动事件到点推送,被动伏笔在有人说话时引爆(冲着说话的人来)",
            f"回复事件卡 + {sub_prefix} 选择 <编号> — 必须引用事件卡,按№编号精确定位并抉择(个人事件仅本人/多人事件仅当事人/全群人人可选)",
            f"{sub_prefix} 与 @群友 [互动方式/自由行动…] — 和别人的分身交朋友(或结仇),好感度随互动起落",
            f"🤝 {sub_prefix} 关系 @群友 <称谓> — 提议搞怪关系(想当TA的爸爸/主人/女仆…),AI 判定对方答不答应;恋人/情侣等亲密关系不可自定义,走告白/求婚",
            "💞 告白与求婚均由事件概率触发:好感≥85 互动中可能自然告白成恋人 / 65~79 单相思 / 恋人好感≥90 小概率求婚结为伴侣",
            f"{sub_prefix} 任务 / 交任务 <编号> — 每日委托(委托人+发布设施+多步骤):按步骤做完(冒险/互动/兼职/拿物品),再向委托人交付拿悬赏;步骤没做完交不了",
            f"{sub_prefix} npc <名字> <想做什么> — 找当前世界的NPC搭话",
            f"{sub_prefix} 背包 [丢弃 <物品>] — 查看随身物品(冒险/事件/委托/兼职都可能得到或失去)",
            f"{sub_prefix} 世界 — 世界档案含「世界记忆」:全局大事/主线进展/NPC·设施流转",
            "⛓ 特殊状态:陷入囚禁/束缚等处境时会受限,需靠冒险/特殊NPC/群友救援/世界变动脱困",
        ]),
        sec("⚡ 主动行动(能动起来)", [
            f"{sub_prefix} 练习 <练什么…> — 修习/训练,精进一门技艺或属性",
            f"{sub_prefix} 健身 — 锻炼体魄(力量/敏捷)",
            f"{sub_prefix} 兼职 — 在世界设施上一班(约2小时),到点自动下班结算:开工期间不能自由行动/互动/光顾,下班时会有NPC同事道别",
            f"{sub_prefix} 打怪 <目标…> — 去危险地带猎杀怪物(高风险高回报)",
            f"{sub_prefix} 冒险 <自由描述…> — 想做什么都不设限,由世界与性格决定走向",
            "行动会消耗体力(每天限次),属性/金币/心情随之起落,还小概率触发机缘彩蛋",
        ]),
        sec("🏙 世界的生活", [
            f"{sub_prefix} 设施 — 查看当前世界基础设施(20~28处,含社交娱乐约会;标注可光顾消遣者)",
            f"{sub_prefix} 去 <设施名> [想做什么] — 去社交/娱乐/约会设施消磨时光,触发小事件(每天每家1次)",
            f"{sub_prefix} 主线 / 主线 推进 — 查看世界主线 / 推进一步(AI结算并阶梯解锁)",
            f"{sub_prefix} 房产 / 房产 买 <编号> / 房产 回家 — 查看/购置当前世界房产;回家每天一次,按房价分档回复体力/心情,小概率触发家居事件",
            f"🎭 {sub_prefix} 定义角色 <名字> <描述…> — 创造持久『生活角色』;{sub_prefix} 找 <名字> [方式] 与TA互动,可发展关系/成婚;世界会不定时来人走人",
        ]),
        sec("🌀 世界的边界", [
            "世界有小概率发生变动——全员(包括生活角色)被卷进另一个世界!",
            f"{sub_prefix} 定义世界 <名称> <描述…> — 把你设定的世界写进世界书(等待降临)",
            f"{sub_prefix} 添加NPC <名> [描述…] — 在你自设的世界安插NPC(AI 结合世界整理档案)",
            f"{sub_prefix} NPC列表 / 删除NPC <名字> — 管理这些住民",
            f"{sub_prefix} 穿越世界 <编号/名称> — 只能去「穿越过」的世界",
            f"{sub_prefix} 世界 / 世界列表 — 查看当前世界与世界书",
        ]),
        sec("🧩 其他", [
            f"{sub_prefix} 我的卡片 / 看 <名字> / 名册 / 日志 [页] / 回忆 <关键词> / 运势",
            f"{sub_prefix} 编辑 性别|性格|背景 <内容> 或自由描述 · 删除角色 · 互动菜单",
            f"{sub_prefix} 事件频率 min max · 变动概率 p · 触发变动 / 重开世界 (管理员)",
            "🛠 Dashboard 插件页里有后台管理:查看/编辑数据、手动触发事件与变动(管理员)",
        ]),
    ]
    return r.render_rows(rows, title="分身界 · 帮助")


def roster_card(chars: list, cfg: dict, world_name: str = "") -> list[Image.Image]:
    r = _mk(cfg)
    rows = []
    if not chars:
        rows.append(_para(r, "这个世界还没有居民。用「/分身 创建 名字」成为第一个!", color=r.t.text_muted))
    for ch in chars:
        rows.append(AvatarHeadRow(
            r, avatar=_avatar_img(ch.avatar), name=ch.name,
            subtitle=f"Lv{ch.level} · {ch.title} · 心情{ch.mood} 体力{ch.stamina}",
            tags=ch.tags[:5] if ch.tags else [],
        ))
    rows.append(EmptyRow(r, 4))
    rows.append(_para(r, f"共 {len(chars)} 位居民" + (f" · 《{world_name}》" if world_name else ""),
                      size=int(r.font_size * 0.72), color=r.t.text_muted))
    return r.render_rows(rows, title="分身名册")


def memory_card(query: str, results: list[dict], cfg: dict) -> list[Image.Image]:
    r = _mk(cfg)
    rows = []
    if not results:
        rows.append(_para(r, f"关于「{query}」的记忆一片模糊。", color=r.t.text_muted))
    for i, item in enumerate(results, 1):
        scope_tag = {"char": " PERSONA", "world": " WORLD", "npc": " NPC", "core": " CORE"}.get(
            item.get("scope", "char"), "")
        rows.append(_para(r, f"{i}. {item.get('text', '')}",
                          color=r.t.text_secondary, size=int(r.font_size * 0.8), margin=(0, 3, 0, 3)))
        rows.append(_para(r, f"   └─ 相关度 {item.get('score', 0)}{scope_tag}",
                          color=r.t.text_muted, size=int(r.font_size * 0.62)))
    return r.render_rows(rows, title=f"回忆 · {query[:12]}")
