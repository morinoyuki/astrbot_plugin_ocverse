"""LLM 封装层。

Brain 不直接 import astrbot —— 它接收一个注入的异步原语:

    call(system: str, user: str) -> str | None   # 返回原始补全文本,失败返回 None

main.py 用 AstrBot provider 实现它;测试里用 FakeBrain。所有产出均尽力返回
结构化 dict;LLM 不可用/解析失败时回退到内置模板(fallback),保证弱网/无网可玩。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import dataclass
from datetime import datetime

from .config import ATTRS, ATTR_KEYS, ATTR_NAMES

_WEEKDAYS = "一二三四五六日"


def now_stamp() -> str:
    """当前真实时间戳(中文),供所有 LLM 调用注入,让生成感知『今天/现在』。"""
    now = datetime.now()
    wd = _WEEKDAYS[now.weekday()]
    return (
        f"【当前时间】{now.strftime('%Y-%m-%d')}(星期{wd}) {now.strftime('%H:%M')}"
        f" ——这是此刻的真实时刻,可据此把握『今天/昨晚/季节/日夜』的氛围来生成内容;"
        f"但除非切题,叙事不要生硬报出现实年月。"
    )

STYLE_BASE = (
    "你是一个群聊文字游戏的叙事引擎,以轻小说的手法叙事:画面感强、有心理与感官细节、"
    "节奏明快、对话生动口语化、结尾留有余味或小小的转折;不水字数、不出戏、"
    "不提及任何现实平台或AI身份。所有输出必须是严格的 JSON,不要 markdown 代码围栏,不要解释。"
)


def _extract_json(text: str):
    """宽容 JSON 提取:去围栏 → 找首个平衡大括号块。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    t = t.strip()
    if t.startswith("{"):
        try:
            return json.loads(t)
        except Exception:
            pass
    # 平衡括号扫描
    start = t.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(t)):
            c = t[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start : i + 1])
                    except Exception:
                        break
        start = t.find("{", start + 1)
    return None


def _clamp(v, lo, hi, default=0):
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return default


def _clamp_effects(e: dict) -> dict:
    """效果表规范化到安全区间。"""
    if not isinstance(e, dict):
        return {}
    out = {}
    for k in ("stamina", "mood", "gold", "exp"):
        if k in e:
            out[k] = _clamp(e[k], -100, 100)
    attrs = e.get("attrs") if isinstance(e.get("attrs"), dict) else {}
    ka = {k: _clamp(attrs.get(k, 0), -10, 10) for k, _ in ATTRS}
    if any(v != 0 for v in ka.values()):
        out["attrs"] = ka
    return out


@dataclass
class BrainResult:
    ok: bool
    data: dict


class Brain:
    """LLM 高层封装。raw_call 为 None 时全部走 fallback(离线可玩)。"""

    def __init__(self, raw_call=None, style_extra: str = "", timeout: float = 120.0,
                 raw_call_tools=None):
        self.raw_call = raw_call
        # 联网增强通道(如 tool_loop_agent + 搜索工具);仅用于低频、可接受多步耗时的生成
        self.raw_call_tools = raw_call_tools
        self.style_extra = style_extra or ""
        self.timeout = timeout

    @property
    def style(self) -> str:
        s = STYLE_BASE
        if self.style_extra:
            s += f" 文风附加要求:{self.style_extra}"
        return s

    async def _ask(self, system: str, user: str, use_tools: bool = False):
        # 每次调用 LLM 都注入当前时间,保证所有生成都感知『今天/现在』
        system = f"{system}\n{now_stamp()}"
        if use_tools and self.raw_call_tools:
            res = self.raw_call_tools(system, user)
            if inspect.isawaitable(res):
                res = await asyncio.wait_for(res, timeout=self.timeout)
            if res:
                return res
            # 联网通道不可用 → 回退普通通道
        if self.raw_call is None:
            return None
        res = self.raw_call(system, user)
        if inspect.isawaitable(res):
            res = await asyncio.wait_for(res, timeout=self.timeout)
        return res

    async def _ask_json(self, system: str, user: str, retries: int = 1, use_tools: bool = False):
        for _ in range(retries + 1):
            try:
                text = await self._ask(system, user, use_tools=use_tools)
            except (asyncio.TimeoutError, Exception):
                text = None
            if text:
                data = _extract_json(text)
                if isinstance(data, dict):
                    return data
        return None

    # ════════════════ 世界生成 ════════════════
    _WORLD_SCHEMA = (
        '{"name":不超过8字,"genre":题材标签,"atmosphere":氛围一句话,'
        '"desc":世界描述80-160字,"rules":["规则1","规则2","规则3"],'
        '"features":["独特之处1","独特之处2","独特之处3"],'
        '"npcs":[{"name":"","role":"身份","persona":"性格一句话","hook":"可交互的钩子一句话","daily":"这名NPC每天一般会去做什么/在哪里","quirk":"一个鲜活的小怪癖/口头禅"}],'
        '"event_ideas":["该世界独有事件灵感",4-6条],'
        '"infra":[{"kind":"设施类型(店/馆/铺/工坊/祭坛/据点/地标…)","name":"设施名(2~6字)","desc":"功能/氛围一句话(≤20字)","work":"在这里能打工赚钱的职业(无则不填)"}],'
        '"mainline":[{"stage":"主线小节名(≤10字)","desc":"这一步要做什么/线索(≤30字)"}],'
        '"plots":[{"kind":"房|宅|小屋|公寓|铺面|庄园…","name":"可购住处名(2~6字)","desc":"一句话","price":整数金币价}]}'
    )

    @staticmethod
    def _norm_infra(d: dict) -> list:
        """规范化基础设施列表。"""
        out = []
        for it in (d.get("infra") or [])[:8]:
            if not isinstance(it, dict) or not (it.get("name") or ""):
                continue
            out.append({
                "kind": str(it.get("kind") or "设施")[:10],
                "name": str(it.get("name") or "")[:16],
                "desc": str(it.get("desc") or "")[:90],
                "work": str(it.get("work") or "")[:40],
            })
        return out

    @staticmethod
    def _norm_mainline(d: dict) -> list:
        out = []
        for i, m in enumerate((d.get("mainline") or [])[:8]):
            if not isinstance(m, dict) or not (m.get("stage") or ""):
                continue
            out.append({
                "stage": str(m.get("stage") or "")[:12],
                "desc": str(m.get("desc") or "")[:90],
                "done": bool(m.get("done")),
            })
        return out

    @staticmethod
    def _norm_plots(d: dict) -> list:
        out = []
        for p in (d.get("plots") or [])[:10]:
            if not isinstance(p, dict) or not (p.get("name") or ""):
                continue
            try:
                price = max(0, int(float(p.get("price") or 0)))
            except (TypeError, ValueError):
                price = 200
            out.append({
                "kind": str(p.get("kind") or "房")[:10],
                "name": str(p.get("name") or "")[:16],
                "desc": str(p.get("desc") or "")[:90],
                "price": price,
            })
        return out

    async def gen_world(self, desc: str | None = None, avoid_names: list[str] | None = None,
                        theme_hint: str = "",
                        material: str = "") -> BrainResult:
        sys = self.style
        if desc:
            ref = desc
        else:
            theme = (theme_hint or "").strip()
            ref = f"自由发挥。整体风格要求:{theme}" if theme else "自由发挥,题材新颖,避开烂大街的西幻冒险开局。"
        user = (
            "为群聊文字游戏生成一个新世界。世界观设定参考:" + ref
        )
        if avoid_names:
            user += "\n不要与这些已有世界重名:" + "、".join(avoid_names[:20])
        user += (
            "\nNPC 5~8个(名字不超过6字,融入世界,不要套模板名)。每个NPC都要有自己的"
            "日常行踪(daily:TA每天一般在哪/做什么)、鲜活小怪癖或口头禅(quirk),"
            "让这个世界住着活生生的人。\n"
            "然后为这个世界设计以下内容(必须全部给出,分别填入 infra / mainline / plots 三个数组):\n"
            "- infra: 3~5个贴合该世界题材与时代的基础设施(商店/饭馆/工坊/祭坛/据点/驿站等),\n"
            "  kind/name/desc/work 必填,其中至少 1 个能打工赚钱(填 work 职业);\n"
            "- mainline: 3~6 节世界主线(stage/desc),是一段能推动这个世界的故事;\n"
            "- plots: 3~5 处可供居民购置的住处(kind/name/desc/price),price 用整数金币。\n"
            "全部要贴合该世界的题材与时代,不要套用同一套现代模板。\n"
            f"严格输出 JSON,结构:{self._WORLD_SCHEMA}"
        )
        # 世界生成是低频操作,允许走联网增强通道(搜索工具)扩充知识
        user = self._with_material(user, material)
        d = await self._ask_json(sys, user, use_tools=True)
        if d and d.get("name"):
            # 校验:infra/mainline 必须至少各 1 条,否则带纠正提示重试一次
            if not (self._norm_infra(d) and self._norm_mainline(d)):
                d2 = await self._ask_json(sys, user + "\n\n【纠正】你漏掉了基础设施或主线,请补全:infra 至少 3 个可去的场所(含至少1个能打工的 work),mainline 至少 3 节主线,plots 至少 3 处可购住处。不要省略这些数组。", use_tools=True)
                if d2 and d2.get("name"):
                    d = {**d, "infra": d2.get("infra") or d.get("infra"),
                         "mainline": d2.get("mainline") or d.get("mainline"),
                         "plots": d2.get("plots") or d.get("plots")}
            return BrainResult(True, self._norm_world(d, source="llm", desc_hint=desc))
        return BrainResult(False, self._fallback_world(desc))

    async def enrich_user_world(self, name: str, desc: str, material: str = "") -> BrainResult:
        """用户自设世界落地时补全细节(失败也能用原始描述)。"""
        sys = self.style
        user = (
            f"玩家自设了一个世界。名称:{name}\n描述:{desc}\n"
            "请补全它的题材标签、氛围、规则、独特之处、4~5个NPC、独有事件灵感。"
            "并设计基础设施、一段世界主线、可供购置的住处——贴合该世界设定,勿套模板。\n"
            f"严格输出 JSON,结构:{self._WORLD_SCHEMA}"
        )
        user = self._with_material(user, material)
        d = await self._ask_json(sys, user, use_tools=True)
        if d and (d.get("name") or d.get("desc")):
            d["name"] = name or d.get("name")
            d["desc"] = d.get("desc") or desc
            return BrainResult(True, self._norm_world(d, source="user", desc_hint=desc))
        return BrainResult(False, self._fallback_world(desc, name=name, source="user"))

    def _norm_world(self, d: dict, source: str, desc_hint: str = "") -> dict:
        npcs = []
        for n in (d.get("npcs") or [])[:8]:
            if isinstance(n, dict) and n.get("name"):
                npcs.append(
                    {
                        "name": str(n["name"])[:10],
                        "role": str(n.get("role", ""))[:30],
                        "persona": str(n.get("persona", ""))[:80],
                        "hook": str(n.get("hook", ""))[:80],
                        "daily": str(n.get("daily", ""))[:60],
                        "quirk": str(n.get("quirk", ""))[:40],
                        "builtin": 1,   # 世界生成自带NPC,可随人口流动来去
                    }
                )
        return {
            "name": str(d.get("name", "无名世界"))[:16],
            "genre": str(d.get("genre", "未知"))[:20],
            "atmosphere": str(d.get("atmosphere", ""))[:60],
            "desc": str(d.get("desc", desc_hint or "一个尚未展开的世界。"))[:300],
            "rules": [str(x)[:40] for x in (d.get("rules") or [])][:4],
            "features": [str(x)[:50] for x in (d.get("features") or [])][:5],
            "npcs": npcs,
            "event_ideas": [str(x)[:40] for x in (d.get("event_ideas") or [])][:6],
            "infra": self._norm_infra(d),
            "mainline": self._norm_mainline(d),
            "source": source,
        }

    def _fallback_world(self, desc: str | None = None, name: str | None = None, source: str = "llm") -> dict:
        from .config import DEFAULT_WORLD

        w = dict(DEFAULT_WORLD)
        if name:
            w["name"] = name
        if desc:
            w["desc"] = desc[:300]
        w["source"] = source if source != "llm" else "default"
        # 离线/失败降级世界也要有一套默认基建/主线/地块,保证功能可用不是空壳
        w.setdefault("infra", [
            {"kind": "杂货铺", "name": "绫婆婆的杂货铺", "desc": "什么都有,也可以换些零用钱", "work": "杂货铺帮工"},
            {"kind": "面馆", "name": "雾边面馆", "desc": "热汤下肚,雾天也暖", "work": "面馆跑堂"},
            {"kind": "灯塔", "name": "旧灯塔", "desc": "灯叔守着的地方,夜里亮着", "work": ""},
        ])
        w.setdefault("mainline", [
            {"stage": "雾夜来客", "desc": "雾最浓的那夜,镇口的猫不停叫——好像有什么在靠近。"},
            {"stage": "旧物现形", "desc": "灯塔下捡到一件不该存在的东西,顺着它查下去。"},
            {"stage": "钟声十三", "desc": "钟楼的第十三声钟响,据说会把人领到雾散之后的地方。"},
        ])
        w.setdefault("plots", [
            {"kind": "小屋", "name": "转角小屋", "desc": "石板路尽头、旧灯塔能望见的那间", "price": 200},
            {"kind": "平房", "name": "街边平房", "desc": "紧挨着港口集市的一间", "price": 400},
            {"kind": "老宅", "name": "镇口老宅", "desc": "有院门的那座,据说镇子初建时就在了", "price": 600},
        ])
        return w

    # ════════════════ 事件生成 ════════════════
    async def make_event(self, *, world, char=None, kind="solo", npc=None,
                         memories: list[str] | None = None, ideas: list[str] | None = None,
                         state_note: str = "", material: str = "") -> BrainResult:
        """生成一次遭遇。char=None 时为全员群事件。state_note: 若角色处于特殊状态(囚禁等),强调脱困方向。"""
        role = (
            "这是全员都会被卷入的群事件,主角是『群里的众人』。"
            if char is None
            else f"主角是 {char.persona_line()},背景:{char.backstory[:120] or '未详'}。"
                 f"当前体力{char.stamina}/心情{char.mood}/金币{char.gold}。"
        )
        if state_note:
            role += (
                f"\n【处境】该角色此刻正被「{state_note}」缠身,处于无法自由行动的特殊状态。"
                "本次遭遇应围绕TA的处境展开,抉择要给出一线脱困/化解的生机(可以成功脱困,也可以失败吃鳖或半困半脱)。"
            )
        npc_line = ""
        if npc:
            npc_line = f"\n事件需围绕NPC「{npc['name']}」({npc.get('role','')},{npc.get('persona','')})展开。"
        mem = ("\n".join(memories[:6]) if memories else "")
        idea = ""
        if ideas:
            idea = "\n可参考该世界的事件灵感(选或化用,不要照抄原文):" + " | ".join(ideas[:6])
        sys = self.style
        user = (
            f"当前世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"氛围:{world.atmosphere}\n世界规则:{';'.join(world.rules or [])}\n"
            f"{role}{npc_line}{idea}\n"
            f"{mem}\n"
            "生成一次突发遭遇:结合世界设定与角色性格/状态,事件要具体、有钩子、能做出选择。\n"
            '严格输出 JSON:{"title":"标题≤10字","scene":"场景描述70-120字",'
            '"options":[{"label":"选项≤8字","hint":"后果暗示≤14字"},{"label":"","hint":""},{"label":"","hint":""}]}'
            "\n恰好3个选项,风格各异(稳健/冒险/离谱皆可)。"
        )
        user = self._with_material(user, material)
        d = await self._ask_json(sys, user)
        if d and d.get("scene"):
            opts = []
            for o in (d.get("options") or [])[:3]:
                if isinstance(o, dict) and o.get("label"):
                    opts.append({"label": str(o["label"])[:10], "hint": str(o.get("hint", ""))[:16]})
            if len(opts) < 3:  # 不足 3 个选项时用内置模板补齐
                opts = (opts + [dict(x) for x in FB_EVENT["options"]])[:3]
            ev = {
                "title": str(d.get("title", "突发状况"))[:12],
                "scene": str(d["scene"])[:180],
                "options": opts,
            }
            if npc:
                ev["npc"] = npc.get("name", "")
            return BrainResult(True, ev)
        return BrainResult(False, dict(FB_EVENT))

    async def make_life_event(self, *, world, chars, rels: str = "",
                              memories: list[str] | None = None,
                              material: str = "") -> BrainResult:
        """生成一次『群像生活事件』:2~N 名玩家角色在同一世界偶遇/结伴/共同度过一段日常。
        chars: 参与的 Char 列表(≥2);rels: 两两关系简述字符串(由 game 层拼接)。
        返回与 make_event 相同的 {title, scene, options} 结构。"""
        cast = []
        for i, c in enumerate(chars, 1):
            cast.append(
                f"角色{i}:{c.persona_line()},背景:{c.backstory[:100] or '未详'},"
                f"当前体力{c.stamina}/心情{c.mood}/金币{c.gold}"
            )
        cast_line = "\n".join(cast)
        mem = ("\n".join(memories[:5]) if memories else "")
        rel_line = ("\n" + rels) if rels else ""
        sys = self.style
        user = (
            f"当前世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"氛围:{world.atmosphere}\n世界规则:{';'.join(world.rules or [])}\n"
            f"{cast_line}{rel_line}\n{mem}\n"
            "这群人是生活在同一个世界的真实人物。请让其中几人产生交集,生成一幕自然的生活日常："
            "偶遇寒暄、结伴逛街/吃饭、一起去某个地方、碰上同一个小麻烦、或某个人的心事被旁人撞见。"
            "要有画面感与生活气息,不搞超展开;事件要具体、能做出选择。\n"
            '严格输出 JSON:{"title":"标题≤12字","scene":"场景描述70-120字,至少点出2名上述角色",'
            '"options":[{"label":"选项≤10字","hint":"后果暗示≤16字"},{"label":"","hint":""},{"label":"","hint":""}]}'
            "\n恰好3个选项,各角色可能会有不同想法(稳健/随性/热心/各走各的皆可)。"
        )
        user = self._with_material(user, material)
        d = await self._ask_json(sys, user)
        if d and d.get("scene"):
            opts = []
            for o in (d.get("options") or [])[:3]:
                if isinstance(o, dict) and o.get("label"):
                    opts.append({"label": str(o["label"])[:10], "hint": str(o.get("hint", ""))[:18]})
            if len(opts) < 3:
                opts = (opts + [dict(x) for x in FB_EVENT["options"]])[:3]
            return BrainResult(True, {
                "title": str(d.get("title", "街角的招呼"))[:14],
                "scene": str(d["scene"])[:220],
                "options": opts,
            })
        return BrainResult(False, dict(FB_EVENT))

    @staticmethod
    def _previous_block(previous: list[str] | None) -> str:
        """把最近同类互动的旧叙述拼进 prompt,要求这次明显不同。"""
        if not previous:
            return ""
        return ("\n此前同类互动的旧叙述(这次必须在场景、话题、对话上明显不同,禁止重复旧梗):\n"
                + "\n".join(f"- {t[:90]}" for t in previous[:3]))

    @staticmethod
    def _text_similar(a: str, b: str) -> bool:
        """字符 bigram 包含度:a 的内容大部分出现在 b 中(或反之)视为复读。"""
        def bigrams(s: str) -> set:
            s = re.sub(r"\s", "", s or "")
            return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else ({s} if s else set())
        ba, bb = bigrams(a), bigrams(b)
        if not ba or not bb:
            return False
        inter = len(ba & bb)
        return inter / max(1, min(len(ba), len(bb))) >= 0.6

    def _with_material(self, user: str, material: str) -> str:
        m = (material or "").strip()
        if not m:
            return user
        return user + "\n" + m + "\n"
    def _too_similar(self, text: str, previous: list[str] | None) -> bool:
        t = (text or "").strip()
        if not t or not previous:
            return False
        return any(self._text_similar(t, (old or "")[:400]) for old in previous)

    async def _ensure_fresh(self, system: str, user: str, d,
                            previous: list[str] | None = None,
                            counterpart: str = "", limit: int = 6,
                            max_fix: int = 2):
        """复读守卫:输出与最近同类互动过于相似时,带纠正提示重写(最多 max_fix 次)。"""
        if not previous:
            return d
        tries = 0
        while (isinstance(d, dict) and d.get("narration")
               and self._too_similar(str(d["narration"]), previous) and tries < max_fix):
            tries += 1
            user2 = user + ("\n\n【重要纠正】你这次的输出与之前发生过的情节几乎一模一样,这是敷衍的复读,不可接受。"
                            "完全重写:换新的场景、新话题、新对话,让情节向前推进。")
            d2 = await self._ask_fixed_dialogues(system, user2, counterpart=counterpart, limit=limit)
            if not (isinstance(d2, dict) and d2.get("narration")):
                break
            d = d2
        return d

    async def resolve_event(self, *, world, char=None, event: dict, choice_idx: int,
                            previous: list[str] | None = None,
                        state_note: str = "", material: str = "") -> BrainResult:
        """结算一次选择。char=None(群事件)时叙述群体结果。
        state_note: 若角色当前被困,提示本次抉择可脱困;输出 state 施加特殊状态 / state_lift 解除。"""
        who = char.persona_line() if char else "群里的众人"
        opts = event.get("options") or []
        pick = opts[choice_idx] if 0 <= choice_idx < len(opts) else {"label": "顺其自然", "hint": ""}
        attrs_names = "、".join(f"{k}={v}" for k, v in ATTR_NAMES.items())
        sys = self.style
        state_line = ""
        if state_note:
            state_line = (
                f"\n该角色正被「{state_note}」困住(无法自由行动)。本次抉择结果要明确交代处境:若这次成功挣脱,"
                "则输出 state_lift:true;若这次反而更被束缚或换一种束缚,则输出 state:{...}(type/reason自定);若只是推进未有果,则两者都不输出。"
            )
        user = (
            f"世界:《{world.name}》。{who}遭遇了:「{event.get('title')}」——{event.get('scene')}\n"
            f"TA选择了「{pick['label']}」({pick.get('hint','')})。\n"
            "请结算:叙述结果(轻小说式,120~220字:画面感+心理细节+余味或小转折),并给出数值变化。"
            "属性键:" + attrs_names + "。\n"
            '"dialogues":事件中人物的多轮对话(2~5轮,IM聊天体,每条"speaker"≤8字、"text"≤60字,可含(动作)小注)。'
            "禁止独角戏:至少 2 个不同说话人,事件人物必须开口回应,不能只有主角一人自说自话。\n"
            '严格输出 JSON:{"narration":"结果叙述","dialogues":[{"speaker":"","text":""}],"effects":{"stamina":±,"mood":±,"gold":±,"exp":0-25,'
            '"attrs":{"force":0}}, "memory":"第三人称一句话记忆存档", "state":{"type":"囚禁|束缚|被困...","reason":"一句原因"}, "state_lift":true}\n'
            "数值克制:大部分±5~15,exp 5~20;负反馈不要毁灭性。memory 一句话,30字内。"
            "state 与 state_lift 只在处境发生变化时才输出(见上),否则两字段都不要出现。"
        )
        user += state_line
        user += self._previous_block(previous)
        user = self._with_material(user, material)
        d = await self._ask_fixed_dialogues(
            sys, user,
            counterpart=str(event.get("npc") or ""),  # 事件涉及NPC时(payload里的名字),必须开口
            limit=5,
        )
        d = await self._ensure_fresh(sys, user, d, previous,
                                     counterpart=str(event.get("npc") or ""), limit=5)
        if d and d.get("narration"):
            return BrainResult(
                True,
                {
                    "narration": str(d["narration"])[:280],
                    "dialogues": self._norm_dialogues(d.get("dialogues"), 5),
                    "effects": _clamp_effects(d.get("effects") or {}),
                    "memory": str(d.get("memory", ""))[:120],
                    "state": d.get("state") if isinstance(d.get("state"), dict) else {},
                    "state_lift": bool(d.get("state_lift")),
                },
            )
        return BrainResult(False, dict(FB_RESOLVE))

    async def resolve_life_event(self, *, world, chars, event: dict, choice_idx: int,
                                 rels: str = "", material: str = "") -> BrainResult:
        """结算一次群像生活事件:叙述这次交集的结果 + 各角色效果 + 羁绊变化。"""
        opts = event.get("options") or []
        pick = opts[choice_idx] if 0 <= choice_idx < len(opts) else {"label": "顺其自然", "hint": ""}
        cast = "\n".join(
            f"角色{i}:{c.persona_line()},背景:{c.backstory[:90] or '未详'},体力{c.stamina}/心情{c.mood}/金币{c.gold}"
            for i, c in enumerate(chars, 1)
        )
        rel_line = ("\n" + rels) if rels else ""
        sys = self.style
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n{cast}{rel_line}\n"
            f"他们正在这场交集里:「{event.get('title')}」——{event.get('scene')}\n"
            f"大家共同选择了「{pick['label']}」({pick.get('hint','')})。\n"
            "请结算这段共同经历的结果:轻小说式叙述(110~200字:画面感+心理细节+余味),"
            "并分别给出各角色的效果与彼此羁绊的变化。\n"
            '"dialogues":这场交集里 2~5 轮的简短对话(IM聊天体,每条 speaker ≤8字、text ≤60字),需有至少 2 个不同说话人。\n'
            '"effects":每个参与角色的效果(体力/心情/金币/exp/attrs,克制:大部分±3~10),可逐个给不同角色不同起伏。\n'
            '"rel_delta":-10~15 整数(本次交集对彼此关系的整体影响)。\n'
            "memory:一句话存档(涉及哪几个人、发生了什么)。\n"
            '严格输出 JSON:{"narration":"叙述","dialogues":[{"speaker":"","text":""}],'
            '"effects":{[角色名或编号]: {"mood":±,"gold":±,"exp":0-12,"stamina":±,"attrs":{}}}, "rel_delta":0, "memory":"一句话"}'
            "(effects 的键用角色名即可,尽量给每人一个条目;数值克制,日常生活不必大起大落)"
        )
        user = self._with_material(user, material)
        d = await self._ask_fixed_dialogues(sys, user, limit=6)
        if d and d.get("narration"):
            eff = d.get("effects") if isinstance(d.get("effects"), dict) else {}
            # 规范:effects 键 → 每角色规范化
            per = {}
            for key, e in eff.items():
                if isinstance(e, dict):
                    per[str(key)] = _clamp_effects(e)
            return BrainResult(True, {
                "narration": str(d["narration"])[:300],
                "dialogues": self._norm_dialogues(d.get("dialogues"), 6),
                "effects_by": per,
                "rel_delta": _clamp(d.get("rel_delta", 0), -10, 15),
                "memory": str(d.get("memory", ""))[:120],
            })
        return BrainResult(False, dict(FB_RESOLVE))

    # ════════════════ 角色互动 ════════════════
    async def resolve_interaction(self, *, world, a, b=None, npc=None, mode: str,
                                  detail: str, rel_score: int, rel_stage: str = "",
                                  previous: list[str] | None = None,
                                  state_note: str = "", material: str = "") -> BrainResult:
        from .config import rel_label

        b_ps = ""
        if b:
            b_ps = f"\nB资料:{b.persona_line()},背景:{b.backstory[:100] or '未详'},体力{b.stamina}/心情{b.mood}"
        if npc:
            b_ps = f"\nB资料:{npc.get('name','?')}({npc.get('role','')}),{npc.get('persona','')}"
        rel_line = (f"两人当前关系:{rel_score}({rel_stage or rel_label(rel_score)})。" if not npc
                    else "对方是本世界的NPC。")
        state_line = ""
        if state_note:
            # A 在救被困的 B:是否救得成由 LLM 判断
            state_line = (
                f"\n【救援】B正被『{state_note}』困住,无法自由行动。A这次是来帮忙/营救/搭救B的。"
                "请在叙述里交代营救的经过与结果:若这次成功把B救出来(挣脱束缚),输出 state_lift:true;"
                "若救不动或反被卷入(换一种困局),输出 state:{...}(type/reason自定);若只是打照面没能救出,则两字段都不输出。"
            )
        sys = self.style
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"A:{a.persona_line()},背景:{a.backstory[:100] or '未详'},体力{a.stamina}/心情{a.mood}/金币{a.gold}"
            f"{b_ps}\n{rel_line}\n"
            f"互动:「{mode}」" + (f"({detail[:60]})" if detail else "") + state_line + "\n"
            "写出这段互动的走向与结果(轻小说式,120~220字:画面感+心理细节+余味)。\n"
            '"dialogues":A与B你来我往的多轮对话(3~6轮,IM聊天体,每条"speaker"用角色名,'
            '"text"≤60字,口语化,可含(动作/神态)小注),要能看出性格碰撞。'
            "禁止独角戏:A 与 B 都必须开口,不能只有一人说个不停。\n"
            "若是消费类互动(请客/送礼),务必扣 A 的金币并给 B 心情。\n"
            '严格输出 JSON:{"narration":"互动叙述","a_effects":{"mood":±,"gold":±,"exp":0-10,'
            '"stamina":±,"attrs":{}}, "b_effects":{"mood":±,"gold":±},'
            ' "rel_delta":-20~20整数, "memory":"一句话存档", "state":{...}, "state_lift":true}'
            "(state/state_lift 仅在救援场景、且B的处境发生变化时按上面的规则输出,否则不要出现)"
        )
        user += self._previous_block(previous)
        counterpart = b.name if b else (str(npc.get("name", "")) if npc else "")
        user = self._with_material(user, material)
        d = await self._ask_fixed_dialogues(
            sys, user, counterpart=counterpart, limit=6,
        )
        d = await self._ensure_fresh(sys, user, d, previous,
                                     counterpart=counterpart, limit=6)
        if d and d.get("narration"):
            return BrainResult(
                True,
                {
                    "narration": str(d["narration"])[:300],
                    "dialogues": self._norm_dialogues(d.get("dialogues"), 6),
                    "a_effects": _clamp_effects(d.get("a_effects") or {}),
                    "b_effects": _clamp_effects(d.get("b_effects") or {}),
                    "rel_delta": _clamp(d.get("rel_delta", 0), -20, 20),
                    "memory": str(d.get("memory", ""))[:120],
                    "state": d.get("state") if isinstance(d.get("state"), dict) else {},
                    "state_lift": bool(d.get("state_lift")),
                },
            )
        return BrainResult(False, dict(FB_INTERACT))

    # ════════════════ 主动行动(练习/健身/打怪/冒险)════════════════
    async def resolve_mainline(self, *, world, char, stage: dict,
                               material: str = "") -> BrainResult:
        """结算世界主线一小节:角色推进这一步,LLM 写出进展与结果。"""
        sys = self.style
        mem = f"\n当前主线小节:{stage.get('stage','')} —— {stage.get('desc','')}"
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"主角:{char.persona_line()}\n{mem}\n"
            "角色主动去推进这段世界主线。请写出这一步的经过与结果(轻小说式,90~180字):"
            "要扣住主线目标、有画面感和余味,并给出数值变化(克制:±3~10)。\n"
            '"dialogues":这段推进中的简短对话(1~3轮,IM聊天体,speaker≤8字、text≤50字,至少2个说话人)。\n'
            '严格输出 JSON:{"narration":"推进叙述","dialogues":[{"speaker":"","text":""}],'
            '"effects":{"mood":±,"gold":±,"exp":0-15,"attrs":{}}, "memory":"一句话存档"}'
        )
        user = self._with_material(user, material)
        d = await self._ask_fixed_dialogues(sys, user, limit=3)
        if d and d.get("narration"):
            return BrainResult(True, {
                "narration": str(d["narration"])[:280],
                "dialogues": self._norm_dialogues(d.get("dialogues"), 3),
                "effects": _clamp_effects(d.get("effects") or {}),
                "memory": str(d.get("memory", ""))[:120],
            })
        return BrainResult(False, {
            "narration": f"「{stage.get('stage','')}」有了进展——但路还长,先记下这一步。",
            "dialogues": [], "effects": {"exp": 5, "mood": 2},
        })

    async def resolve_action(self, *, world, char, action_name: str, detail: str,
                             kind: str = "safe", memories: list[str] | None = None,
                             state_note: str = "", material: str = "") -> BrainResult:
        """结算一次玩家主动行动。kind: safe | risk(风险型可失败/受伤)。
        state_note: 若角色被困,本次『冒险』即脱困尝试,由 LLM 判定是否成功脱困。"""
        attrs_names = "、".join(f"{k}={v}" for k, v in ATTR_NAMES.items())
        risk_line = (
            "【风险型】结果起伏大:可能大丰收,也可能受伤/掉属性/破财。数值范围可以放得更宽。"
            if kind == "risk"
            else "【日常型】大体都往好的方向走,只是奖励丰俭有别;不要给毁灭性打击。"
        )
        state_line = ""
        if state_note:
            risk_line += f"\n该角色正被「{state_note}」困住(无法自由行动)——这次行动是TA的脱困/求生尝试,成败由你判断并写进叙述。"
            state_line = (
                "\n处境说明:若这次行动成功挣脱束缚/脱困/破局,则输出 state_lift:true;"
                "若反而陷入新的束缚或换一种困局,则输出 state:{...};若只是挣扎推进未有果,则两者都不输出。"
            )
        mem = "\n".join(memories[:4]) if memories else ""
        sys = self.style
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"角色:{char.persona_line()},背景:{char.backstory[:100] or '未详'},"
            f"当前体力{char.stamina}/心情{char.mood}/金币{char.gold}\n"
            f"今日于《{world.name}》执行行动:「{action_name}」{detail[:80]}\n{risk_line}\n{mem}\n"
            "请写出这次行动的经过与结果(轻小说式,100~200字:画面感+心理细节+余味),"
            "结合世界设定与角色性格。\n"
            '"dialogues":行动中与场景人物的简短对话(2~4轮,IM聊天体,每条"speaker"≤8字、"text"≤60字)。'
            "禁止独角戏:至少 2 个不同说话人,场景人物必须回应,不能只有角色自说自话。\n"
            "属性键:" + attrs_names + "。日常型行动要消耗的体力由系统扣除,效果表里不要写体力。\n"
            '严格输出 JSON:{"narration":"行动叙述",'
            '"effects":{"mood":±,"gold":±,"exp":0-25,"stamina":±(仅风险型可写),"attrs":{"force":0}},"memory":"一句话存档", "state":{"type":"...","reason":"..."}, "state_lift":true}\n'
            "数值克制:日常型大部分±5~15、exp 5~18、金币±0~40;风险型可到 exp 5~30、金币 0~80,失败时给负反馈但不要毁灭性打击。"
            "state 与 state_lift 只在处境变化时输出(见规则说明),否则两字段都不要出现。"
        )
        user += state_line
        user = self._with_material(user, material)
        d = await self._ask_fixed_dialogues(sys, user, limit=4)
        if d and d.get("narration"):
            return BrainResult(
                True,
                {
                    "narration": str(d["narration"])[:300],
                    "dialogues": self._norm_dialogues(d.get("dialogues"), 4),
                    "effects": _clamp_effects(d.get("effects") or {}),
                    "memory": str(d.get("memory", ""))[:120],
                    "state": d.get("state") if isinstance(d.get("state"), dict) else {},
                    "state_lift": bool(d.get("state_lift")),
                },
            )
        return BrainResult(False, dict(FB_ACT))

    # ════════════════ NPC 对话 ════════════════
    async def npc_chat(self, *, world, npc: dict, char, action: str,
                       memories: list[str] | None = None,
                       previous: list[str] | None = None,
                       state_note: str = "", material: str = "") -> BrainResult:
        sys = self.style
        state_line = ""
        if state_note:
            # 被困玩家找NPC:是否『特殊NPC』、能否施以援手,由 LLM 判断
            state_line = (
                f"\n{char.name}正被『{state_note}』困住,无法自由行动。"
                "请判断当前这位NPC是否能算是能帮助到TA的『特殊NPC』:能的话,自然演一段TA帮上忙的情节,"
                "并在成功挣脱/获救时输出 state_lift:true,或换一种困局时输出 state;"
                "若这位NPC帮不上忙,就如实演一段TA爱莫能助/婉拒的对话,不要强行放人,也不要输出 state/state_lift。"
            )
        user = (
            f"世界:《{world.name}》。NPC「{npc['name']}」({npc.get('role','')},{npc.get('persona','')};"
            f"钩子:{npc.get('hook','')})"
            + (f"\nTA的日子:平时{npc.get('daily','')}" if npc.get("daily") else "")
            + (f";怪癖/口头禅:{npc.get('quirk','')}" if npc.get("quirk") else "")
            + f"\n角色:{char.persona_line()}\n角色行为:{action[:80]}\n"
            f"{chr(10).join(memories[:4]) if memories else ''}\n{state_line}\n"
            "与角色进行多轮对话(3~6轮,IM聊天体:dialogues 数组,每条 speaker ≤8字、text ≤60字,"
            "口语化,保留人设与神秘感,NPC与角色交替说话),再用旁白收尾(60~120字),可给微小奖励。\n"
            "让NPC像有自己生活的人:带上TA的行踪、习惯、语气与情绪,别念模板台词。\n"
            "禁止独角戏:NPC 与角色都必须开口,不能只有角色一人说个不停。\n"
            '严格输出 JSON:{"reply":"NPC最核心的一句台词","dialogues":[{"speaker":"","text":""}],'
            '"narration":"旁白收尾",'
            '"effects":{"mood":±,"gold":±,"exp":0-8}, "memory":"一句话存档", "state":{...}, "state_lift":true}'
        )
        user += self._previous_block(previous)
        counterpart = str(npc.get("name", ""))
        user = self._with_material(user, material)
        d = await self._ask_fixed_dialogues(sys, user, counterpart=counterpart, limit=6)
        d = await self._ensure_fresh(sys, user, d, previous,
                                     counterpart=counterpart, limit=6)
        if d and d.get("reply"):
            return BrainResult(
                True,
                {
                    "reply": str(d["reply"])[:160],
                    "dialogues": self._norm_dialogues(d.get("dialogues"), 6),
                    "narration": str(d.get("narration", ""))[:200],
                    "effects": _clamp_effects(d.get("effects") or {}),
                    "memory": str(d.get("memory", ""))[:120],
                    "state": d.get("state") if isinstance(d.get("state"), dict) else {},
                    "state_lift": bool(d.get("state_lift")),
                },
            )
        return BrainResult(False, dict(FB_NPC))

    # ════════════════ 抵达/晨报 ════════════════
    async def compose_arrival(self, *, world, prev_name: str, via: str,
                        material: str = "") -> BrainResult:
        via_line = {
            "shift": "世界在众人眼前剧烈扭曲、重组",
            "travel": "众人主动开启了一条穿越之门",
            "init": "群聊世界的帷幕第一次拉开",
        }.get(via, "时空泛起涟漪")
        sys = self.style
        user = (
            f"众人从《{prev_name or '虚无'}》来到新世界:《{world.name}》[{world.genre}]。\n"
            f"世界描述:{world.desc}\n{via_line}。写一段抵达播报(80-140字,渲染初印象,点出1-2个独特之处)。\n"
            '严格输出 JSON:{"narration":"抵达播报","tips":["给新来者的一句忠告","一句忠告"]}'
        )
        user = self._with_material(user, material)
        d = await self._ask_json(sys, user)
        if d and d.get("narration"):
            return BrainResult(
                True,
                {
                    "narration": str(d["narration"])[:300],
                    "tips": [str(x)[:40] for x in (d.get("tips") or [])][:3],
                },
            )
        return BrainResult(False, dict(FB_ARRIVE, name=world.name))

    async def morning_brief(self, *, world, chars: list, day_note: str,
                        material: str = "") -> BrainResult:
        who = "、".join(c.persona_line() for c in chars[:8]) or "暂无居民"
        sys = self.style
        user = (
            f"世界:《{world.name}》。今日({day_note})的晨报。居民:{who}\n"
            "写一段晨报(50-90字):天气/异象 + 今日氛围 + 点名一位居民该当心什么。\n"
            '严格输出 JSON:{"brief":"晨报正文","watch":"被点名者与原因(≤20字)"}'
        )
        user = self._with_material(user, material)
        d = await self._ask_json(sys, user)
        if d and d.get("brief"):
            return BrainResult(
                True, {"brief": str(d["brief"])[:220], "watch": str(d.get("watch", ""))[:40]}
            )
        return BrainResult(False, dict(FB_MORNING))

    async def summarize_core(self, uid_name: str, old_texts: list[str]) -> list[str]:
        sys = self.style
        user = (
            f"把角色「{uid_name}」的以下旧记忆压缩成 3-5 条稳定的『核心记忆』(第三人称,每条≤25字,"
            "只保留塑造性格/关系/重要经历的事实):\n- " + "\n- ".join(old_texts[:40])
        )
        d = await self._ask_json(sys, user + '\n严格输出 JSON:{"cores":["..."]}')
        if d and d.get("cores"):
            return [str(x)[:60] for x in d["cores"]][:5]
        return []


    # ════════════════ 自由文本 → 结构化人设(创角/改角/加NPC)════════════════
    async def parse_persona(self, text: str) -> BrainResult:
        """把一段口语化的「设定描述」整理成 {gender, tags, backstory, attrs}。
        attrs = 按设定分配的初始六维(如「大天才」的智力应最高)。
        失败返回 ok=False,由调用方朴素兑底(描述原文入背景,不丢信息)。"""
        attr_line = "、".join(f"{k}({_nm})" for k, _nm in ATTRS)
        user = (
            "群友在创建 OC 分身,给了一段口语化的设定描述。请整理成结构化人设,不要编造描述里没有的信息:\n"
            f"【设定描述】{text[:600]}\n"
            "1. gender:性别,没提就填「保密」;\n"
            "2. tags:性格标签数组,2~6个,每个2~6字(如:腹黑/重情义/独来独往/生人勿近),从性格与行事风格中提炼;\n"
            "3. backstory:第三人称背景设定一段话(60~150字),把外观、穿着、身份、能力、经历等信息全部合并进去,语句通顺;\n"
            f"4. attrs:按设定强弱给六维分配初始属性(数值 18~60),与设定强相关的 1~2 项给 55~60 且为最高(如「大天才」的 intellect 应最高),普通项 25~40,短板 18~25。键:{attr_line}\n"
            '严格输出 JSON:{"gender":"","tags":[""],"backstory":"","attrs":{"force":0,"agility":0,"intellect":0,"charm":0,"luck":0,"sanity":0}}'
        )
        d = await self._ask_json(self.style, user)
        if d:
            tags = [str(t).strip()[:8] for t in (d.get("tags") or []) if str(t).strip()][:6]
            gender = str(d.get("gender") or "").strip()[:8] or "保密"
            attrs_in = d.get("attrs") if isinstance(d.get("attrs"), dict) else {}
            attrs = {}
            for k in ATTR_KEYS:
                try:
                    attrs[k] = max(1, min(60, int(round(float(attrs_in.get(k))))))
                except (TypeError, ValueError):
                    pass
            return BrainResult(True, {
                "gender": gender,
                "tags": tags,
                "backstory": str(d.get("backstory") or "").strip()[:400],
                "attrs": attrs,
            })
        return BrainResult(False, {})

    async def parse_persona_update(self, *, cur_name: str, cur_gender: str,
                                   cur_tags: list, cur_backstory: str, text: str) -> BrainResult:
        """从一段自由描述中判断要修改哪些人设字段。
        只返回需要更新的字段;tags/backstory 给出合并旧设定后的完整新值。"""
        user = (
            f"角色「{cur_name}」当前人设:性别 {cur_gender};性格标签:{'、'.join(cur_tags) or '无'};"
            f"背景设定:{cur_backstory[:200] or '无'}\n"
            f"玩家发出一段修改描述:{text[:400]}\n"
            "请判断要更新哪些字段,只输出需要修改的字段:\n"
            "- gender:仅当明确提及性别时输出;\n"
            "- tags:输出更新后的完整标签列表(2~6个,每个2~6字,保留仍然成立的旧标签,融合新描述);\n"
            "- backstory:输出合并后的完整背景设定(保留未被推翻的旧设定,融入新描述);\n"
            '严格输出 JSON:{"gender":"","tags":[""],"backstory":""}(不改的字段不要出现)'
        )
        d = await self._ask_json(self.style, user)
        if not isinstance(d, dict) or not d:
            return BrainResult(False, {})
        out: dict = {}
        if str(d.get("gender") or "").strip():
            out["gender"] = str(d["gender"]).strip()[:8]
        tags = [str(t).strip()[:8] for t in (d.get("tags") or []) if str(t).strip()][:6]
        if tags:
            out["tags"] = tags
        if str(d.get("backstory") or "").strip():
            out["backstory"] = str(d["backstory"]).strip()[:400]
        return BrainResult(bool(out), out)

    async def parse_npc(self, name: str, text: str, world=None,
                        npc_names: list[str] | None = None) -> BrainResult:
        """把一段口语化描述整理成 NPC 档案 {role, persona, hook}。
        world: 所在世界(World),连同世界数据一起交给 LLM,确保档案贴合世界观;
        npc_names: 已有 NPC 名,提示避免重名/职业雷同。失败返回 ok=False。"""
        user = (
            f"群友要在世界里添加一位叫「{name}」的NPC,给了一段口语化描述。请整理成档案,不要编造描述里没有的信息:\n"
            f"【描述】{text[:400]}\n"
        )
        if world is not None:
            user += (
                f"【所在世界】《{world.name}》[{world.genre}] {world.desc}\n"
                f"氛围:{world.atmosphere};世界规则:{';'.join(world.rules or [])}\n"
                "档案(职业/性格/钩子)必须贴合该世界的题材、氛围与规则,不要出现与世界观冲突的设定。\n"
            )
        if npc_names:
            user += f"世界中已有NPC:{'、'.join(list(npc_names)[:10])}。不要与TA们重名,职业也不要雷同。\n"
        user += (
            "- role:职业/身份(2~10字,描述没提就结合世界背景推测一个最贴切的);\n"
            "- persona:性格一句话(≤30字);\n"
            "- hook:可交互的钩子/悬念一句话(≤30字,带一点神秘感或故事感);\n"
            '严格输出 JSON:{"role":"","persona":"","hook":""}'
        )
        d = await self._ask_json(self.style, user)
        if d and (d.get("persona") or d.get("role")):
            return BrainResult(True, {
                "role": (str(d.get("role") or "").strip() or "居民")[:20],
                "persona": (str(d.get("persona") or "").strip() or "性格未详")[:40],
                "hook": (str(d.get("hook") or "").strip() or "身上藏着一段待发掘的故事")[:40],
            })
        return BrainResult(False, {})


    # ════════════════ 每日小任务(简单、轻松、按世界生成)════════════════
    async def gen_quests(self, *, world, char, memories: list[str] | None = None,
                        material: str = "") -> BrainResult:
        """按世界观/角色/记忆生成 3 个日常小任务,目标不要太难。"""
        mem = "\n".join(memories[:4]) if memories else ""
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"氛围:{world.atmosphere};世界规则:{';'.join(world.rules or [])}\n"
            f"角色:{char.persona_line()},背景:{char.backstory[:100] or '未详'}\n"
            f"{mem}\n"
            "请给这个角色生成今天要做的 3 个简单小任务:日常小事、无危险、单人容易完成(如吃一顿当地早餐、"
            "向某位NPC打听一件小事、帮别人一个小忙、找一样有趣的小东西),结合世界设定,充满生活气息。\n"
            '严格输出 JSON:{"quests":[{"text":"任务描述≤16字","hint":"完成提示≤20字"}]}\n'
            "恰好 3 个。"
        )
        user = self._with_material(user, material)
        d = await self._ask_json(self.style, user)
        if d and d.get("quests"):
            quests = [{"text": str(q.get("text", "")).strip()[:24],
                       "hint": str(q.get("hint", "")).strip()[:30]}
                      for q in d["quests"][:3] if isinstance(q, dict) and str(q.get("text", "")).strip()]
            if quests:
                return BrainResult(True, {"quests": quests})
        return BrainResult(False, dict(FB_QUESTS))

    async def finish_quest(self, *, world, char, quest: str,
                           memories: list[str] | None = None,
                        material: str = "") -> BrainResult:
        """结算一个小任务:轻松日常的完成叙述 + 很小的奖励(数值克制)。"""
        mem = "\n".join(memories[:3]) if memories else ""
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"角色:{char.persona_line()},背景:{char.backstory[:80] or '未详'}\n"
            f"角色完成了今日小任务:「{quest[:30]}」\n"
            f"{mem}\n"
            "写一段简短的完成叙述(轻小说式,60~120字,轻松日常,有画面感,有余味),并给一点小奖励。\n"
            '"dialogues":完成过程中的一小段对话(1~3轮,IM聊天体,每条"speaker"≤8字、"text"≤40字)。'
            "禁止独角戏:至少 2 个不同说话人,不能只有角色一人说话。\n"
            '严格输出 JSON:{"narration":"完成叙述","effects":{"exp":5~12,"gold":0~20,"mood":0~3}}'
        )
        user = self._with_material(user, material)
        d = await self._ask_fixed_dialogues(self.style, user, limit=3)
        if d and d.get("narration"):
            eff_in = d.get("effects") if isinstance(d.get("effects"), dict) else {}
            eff = {}
            for k, lo, hi in (("exp", 0, 15), ("gold", 0, 25), ("mood", 0, 5)):
                try:
                    eff[k] = max(lo, min(hi, int(round(float(eff_in.get(k) or 0)))))
                except (TypeError, ValueError):
                    pass
            return BrainResult(True, {"narration": str(d["narration"])[:250],
                                     "dialogues": self._norm_dialogues(d.get("dialogues"), 3),
                                     "effects": eff})
        return BrainResult(False, dict(FB_QUEST_DONE))

    @staticmethod
    def _norm_dialogues(raw, limit: int = 6) -> list:
        """规范 IM 对话轮次:[{speaker, text}], speaker≤12字、text≤100字,最多 limit 条。"""
        if not isinstance(raw, list):
            return []
        out = []
        for d in raw[:limit]:
            if not isinstance(d, dict):
                continue
            sp = str(d.get("speaker") or "").strip()[:12]
            tx = str(d.get("text") or "").strip()[:100]
            if sp and tx:
                out.append({"speaker": sp, "text": tx})
        return out

    @staticmethod
    def _dialogue_ok(dialogues: list, counterpart: str = "") -> bool:
        """对话有效性(防独角戏):
        - 空 = 没有对话,不算独角戏(卡片回退纯叙述);
        - 说话人 < 2 = 独角戏;
        - counterpart 给定时,其名字必须出现过(双向包含容错,如"老铁(铁匠)")。"""
        if not dialogues:
            return True
        speakers = {str(d.get("speaker") or "").strip() for d in dialogues}
        speakers.discard("")
        if len(speakers) < 2:
            return False
        if counterpart and not any(counterpart in s or s in counterpart for s in speakers):
            return False
        return True

    async def _ask_fixed_dialogues(self, system: str, user: str, counterpart: str = "",
                                   limit: int = 6) -> dict | None:
        """带独角戏守卫的 JSON 请求:
        若返回的 dialogues 是独角戏(只有一个说话人/对方没开口),带着纠正提示重试一次;
        重试后仍是独角戏则丢弃对话(宁缺毋滥,由叙述承担表达)。返回解析后的 dict 或 None。"""
        d = await self._ask_json(system, user)
        dlg = self._norm_dialogues(d.get("dialogues"), limit) if isinstance(d, dict) else []
        if dlg and not self._dialogue_ok(dlg, counterpart=counterpart):
            user2 = (
                user
                + "\n\n【重要纠正】你刚才的对话是独角戏(只有一个说话人),这不合要求。"
                  "重写 dialogues:必须你来我往、至少 2 个不同的说话人"
                + (f",且「{counterpart}」必须开口回应" if counterpart else "")
                + ";每条 speaker≤8字、text≤60字。"
            )
            d2 = await self._ask_json(system, user2)
            if isinstance(d2, dict) and d2.get("narration"):
                dlg2 = self._norm_dialogues(d2.get("dialogues"), limit)
                if dlg2 and self._dialogue_ok(dlg2, counterpart=counterpart):
                    return d2  # 纠正成功
                d2["dialogues"] = []  # 仍独角戏 → 丢弃对话,不渲染独角戏
                return d2
        return d


    # ════════════════ 关系系统:告白 / 求婚(轻小说式场景)════════════════
    async def confess(self, *, world, a, b, score: int, outcome: str,
                        material: str = "") -> BrainResult:
        """告白场景。outcome: success(答应) | crush(婉拒留悬念) | reject(明确拒绝)。"""
        outcome_line = {
            "success": "告白成功,两人正式确立恋人关系(双向奔赴或水到渠成,写出动情与确定的一刻)",
            "crush": "告白被温柔地婉拒,但对方心动未泯、留下悬念(单相思的开始,克制而不绝情)",
            "reject": "告白被明确拒绝(写出局促、尴尬与体面收场,不要狗血)",
        }.get(outcome, "告白场景")
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"告白者:{a.persona_line()},背景:{a.backstory[:80] or '未详'}\n"
            f"被告白者:{b.persona_line()},背景:{b.backstory[:80] or '未详'}\n"
            f"两人当前好感:{score}。\n"
            f"本次走向:{outcome_line}。\n"
            "写一段告白场景:叙述(100~180字,轻小说式)+多轮对话(3~6轮,IM聊天体,"
            '每条"speaker"≤8字、"text"≤60字)。'
            "禁止独角戏:双方都必须开口。\n"
            '严格输出 JSON:{"narration":"告白场景叙述","dialogues":[{"speaker":"","text":""}]}'
        )
        user = self._with_material(user, material)
        d = await self._ask_fixed_dialogues(self.style, user, counterpart=b.name, limit=6)
        if d and d.get("narration"):
            return BrainResult(True, {"narration": str(d["narration"])[:300],
                                      "dialogues": self._norm_dialogues(d.get("dialogues"), 6)})
        return BrainResult(False, {"narration": "话到了嘴边,终究是说出口了。结果如何,彼此心里都清楚。",
                                   "dialogues": [{"speaker": a.name, "text": "那个……我喜欢你。"},
                                                 {"speaker": b.name, "text": "……让我想想。"}]})

    async def propose(self, *, world, a, b, score: int,
                        material: str = "") -> BrainResult:
        """求婚/缔结伴侣场景(条件已在游戏层校验,必定成功)。"""
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"求婚者:{a.persona_line()}\n"
            f"被求婚者:{b.persona_line()}\n"
            f"两人好感:{score},早已是彼此认定的恋人。\n"
            "写一段求婚场景:叙述(100~180字,轻小说式,仪式感与动情),"
            "+多轮对话(3~6轮,IM聊天体,每条\"speaker\"≤8字、\"text\"≤60字)。"
            "禁止独角戏:双方都必须开口。\n"
            '严格输出 JSON:{"narration":"求婚场景叙述","dialogues":[{"speaker":"","text":""}]}'
        )
        user = self._with_material(user, material)
        d = await self._ask_fixed_dialogues(self.style, user, counterpart=b.name, limit=6)
        if d and d.get("narration"):
            return BrainResult(True, {"narration": str(d["narration"])[:300],
                                      "dialogues": self._norm_dialogues(d.get("dialogues"), 6)})
        return BrainResult(False, {"narration": "在那盏灯下,戒指被稳稳戴上。世界仿佛安静了一瞬,然后是彼此的笑声。",
                                   "dialogues": [{"speaker": a.name, "text": "(单膝跪地)嫁给我,好不好?"},
                                                 {"speaker": b.name, "text": "(哽咽)……好。"}]})


# ════════════════ fallback 模板(离线可玩)════════════════
FB_EVENT = {
    "title": "街角的异动",
    "scene": "巷口的风突然停了。一个兜帽人擦肩而过,掉落了一只鼓鼓的钱袋。四周无人,远处的钟声恰好敲响。",
    "options": [
        {"label": "捡起归还", "hint": "或有意外的谢礼"},
        {"label": "收入囊中", "hint": "金币,但也有麻烦"},
        {"label": "原地观察", "hint": "看清来龙去脉"},
    ],
}

FB_RESOLVE = {
    "narration": "事情以一种说不上好也说不上坏的方式落幕了。远处钟声又敲了一遍,你把衣角掀了掀,继续往前走。"
                 "世界还在运转,只是从今往后,你记下了这一笔。",
    "dialogues": [
        {"speaker": "路人", "text": "(压低声音)喂,刚才那一幕你也看见了吧?"},
        {"speaker": "老人", "text": "别看啦,这地方奇怪的事,还多着呢。"},
    ],
    "effects": {"exp": 6, "mood": 0},
    "memory": "经历了一场无名的街头遭遇。",
}

FB_INTERACT = {
    "narration": "你们比划了几句,气氛微妙地平衡着。风从巷口掠过去,谁都没先开口,谁也没先走。"
                 "世界很大,相遇总是件小事,但小事攒多了,就成了故事。",
    "dialogues": [
        {"speaker": "对方", "text": "……你就这么看着我干嘛?"},
        {"speaker": "自己", "text": "(移开视线)没什么。下次请我吃饭,就原谅你刚才的眼神。"},
        {"speaker": "对方", "text": "(嗤笑)你还挺上道。"},
    ],
    "a_effects": {"exp": 4},
    "b_effects": {"mood": 2},
    "rel_delta": 2,
    "memory": "与同伴有过一次不起眼的交集。",
}

FB_NPC = {
    "reply": "……嗯?稀客。这镇子上的事,知道得越少,睡得越香。",
    "dialogues": [
        {"speaker": "NPC", "text": "……嗯?稀客。这镇子上的事,知道得越少,睡得越香。"},
        {"speaker": "自己", "text": "(笑了笑)可我偏偏是个爱打听的人。"},
        {"speaker": "NPC", "text": "(摆摆手)那你得先请我喝一杯。"},
    ],
    "narration": "对方摆了摆手,转身回了屋里,只留下一盏在风里晃的灯。",
    "effects": {"exp": 3},
    "memory": "与一位本地人打过照面。",
}

FB_ACT = {
    "narration": "你把这件事认真做了下来,汗顺着下巴滴在尘土里。收工时天色已经暗了,掌心多了道新茧。"
                 "出了不少汗,也攒下了一点东西——唯有自己清楚这份收获。",
    "dialogues": [
        {"speaker": "旁人", "text": "哟,又是你?今天这么拼?"},
        {"speaker": "自己", "text": "总得把今天的事做完。"},
    ],
    "effects": {"exp": 8, "mood": 2},
    "memory": "认真行动了一天,略有进境。",
}

FB_ARRIVE = {
    "narration": "眼前的景象成形了。陌生的街道、陌生的风、陌生的规则——但脚下的路是真实的。欢迎来到《{name}》。",
    "tips": ["先看看周围,再决定做什么", "认识这里的NPC或许有好处"],
}

FB_MORNING = {
    "brief": "薄雾如期而至。钟楼敲了七下,集市支起了摊子。今天的风里有一点说不清的骚动。",
    "watch": "所有人都该留意脚下的路",
}

FB_QUESTS = {
    "quests": [
        {"text": "在附近吃一顿当地早餐", "hint": "找个顺眼的小店坐下"},
        {"text": "向一位NPC打听一件小事", "hint": "聊上几句就算数"},
        {"text": "捡一件有趣的小东西", "hint": "路边的、海边的都行"},
    ],
}

FB_QUEST_DONE = {
    "narration": "你把这件小事认真做完了。热汤见了底,连风都变得温柔。日子就是这样一件件小事攒起来的。",
    "dialogues": [
        {"speaker": "摊主", "text": "(递来热汤)慢慢喝,别急。"},
        {"speaker": "自己", "text": "(捧着碗)嗯,今天也麻烦你了。"},
    ],
    "effects": {"exp": 8, "mood": 2},
}
