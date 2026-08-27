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

from .config import ATTRS, ATTR_NAMES

STYLE_BASE = (
    "你是一个群聊文字游戏的叙事引擎,文风简洁生动、有画面感,不水字数、不出戏、"
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

    def __init__(self, raw_call=None, style_extra: str = "", timeout: float = 120.0):
        self.raw_call = raw_call
        self.style_extra = style_extra or ""
        self.timeout = timeout

    @property
    def style(self) -> str:
        s = STYLE_BASE
        if self.style_extra:
            s += f" 文风附加要求:{self.style_extra}"
        return s

    async def _ask(self, system: str, user: str):
        if self.raw_call is None:
            return None
        res = self.raw_call(system, user)
        if inspect.isawaitable(res):
            res = await asyncio.wait_for(res, timeout=self.timeout)
        return res

    async def _ask_json(self, system: str, user: str, retries: int = 1):
        for _ in range(retries + 1):
            try:
                text = await self._ask(system, user)
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
        '"npcs":[{"name":"","role":"身份","persona":"性格一句话","hook":"可交互的钩子一句话"}],'
        '"event_ideas":["该世界独有事件灵感",4-6条]}'
    )

    async def gen_world(self, desc: str | None = None, avoid_names: list[str] | None = None) -> BrainResult:
        sys = self.style
        user = (
            "为群聊文字游戏生成一个新世界。世界观设定参考:" + (desc or "自由发挥,题材新颖,避开烂大街的西幻冒险开局。")
        )
        if avoid_names:
            user += "\n不要与这些已有世界重名:" + "、".join(avoid_names[:20])
        user += (
            "\nNPC 4~5个(名字不超过6字,融入世界,不要套模板名)。\n"
            f"严格输出 JSON,结构:{self._WORLD_SCHEMA}"
        )
        d = await self._ask_json(sys, user)
        if d and d.get("name"):
            return BrainResult(True, self._norm_world(d, source="llm", desc_hint=desc))
        return BrainResult(False, self._fallback_world(desc))

    async def enrich_user_world(self, name: str, desc: str) -> BrainResult:
        """用户自设世界落地时补全细节(失败也能用原始描述)。"""
        sys = self.style
        user = (
            f"玩家自设了一个世界。名称:{name}\n描述:{desc}\n"
            "请补全它的题材标签、氛围、规则、独特之处、4~5个NPC、独有事件灵感。尊重玩家设定,不推翻。\n"
            f"严格输出 JSON,结构:{self._WORLD_SCHEMA}"
        )
        d = await self._ask_json(sys, user)
        if d and (d.get("name") or d.get("desc")):
            d["name"] = name or d.get("name")
            d["desc"] = d.get("desc") or desc
            return BrainResult(True, self._norm_world(d, source="user", desc_hint=desc))
        return BrainResult(False, self._fallback_world(desc, name=name, source="user"))

    def _norm_world(self, d: dict, source: str, desc_hint: str = "") -> dict:
        npcs = []
        for n in (d.get("npcs") or [])[:6]:
            if isinstance(n, dict) and n.get("name"):
                npcs.append(
                    {
                        "name": str(n["name"])[:10],
                        "role": str(n.get("role", ""))[:30],
                        "persona": str(n.get("persona", ""))[:80],
                        "hook": str(n.get("hook", ""))[:80],
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
        return w

    # ════════════════ 事件生成 ════════════════
    async def make_event(self, *, world, char=None, kind="solo", npc=None,
                         memories: list[str] | None = None, ideas: list[str] | None = None) -> BrainResult:
        """生成一次遭遇。char=None 时为全员群事件。"""
        role = (
            "这是全员都会被卷入的群事件,主角是『群里的众人』。"
            if char is None
            else f"主角是 {char.persona_line()},背景:{char.backstory[:120] or '未详'}。"
                 f"当前体力{char.stamina}/心情{char.mood}/金币{char.gold}。"
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
        d = await self._ask_json(sys, user)
        if d and d.get("scene"):
            opts = []
            for o in (d.get("options") or [])[:3]:
                if isinstance(o, dict) and o.get("label"):
                    opts.append({"label": str(o["label"])[:10], "hint": str(o.get("hint", ""))[:16]})
            while len(opts) < 3:
                opts = list(FB_EVENT["options"]) if opts == [] else opts
                break
            if len(opts) < 3:
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

    async def resolve_event(self, *, world, char=None, event: dict, choice_idx: int) -> BrainResult:
        """结算一次选择。char=None(群事件)时叙述群体结果。"""
        who = char.persona_line() if char else "群里的众人"
        opts = event.get("options") or []
        pick = opts[choice_idx] if 0 <= choice_idx < len(opts) else {"label": "顺其自然", "hint": ""}
        attrs_names = "、".join(f"{k}={v}" for k, v in ATTR_NAMES.items())
        sys = self.style
        user = (
            f"世界:《{world.name}》。{who}遭遇了:「{event.get('title')}」——{event.get('scene')}\n"
            f"TA选择了「{pick['label']}」({pick.get('hint','')})。\n"
            "请结算:叙述结果(60-110字,有反转或余味),并给出数值变化。属性键:" + attrs_names + "。\n"
            '严格输出 JSON:{"narration":"结果叙述","effects":{"stamina":±,"mood":±,"gold":±,"exp":0-25,'
            '"attrs":{"force":0}}, "memory":"第三人称一句话记忆存档"}\n'
            "数值克制:大部分±5~15,exp 5~20;负反馈不要毁灭性。memory 一句话,30字内。"
        )
        d = await self._ask_json(sys, user)
        if d and d.get("narration"):
            return BrainResult(
                True,
                {
                    "narration": str(d["narration"])[:250],
                    "effects": _clamp_effects(d.get("effects") or {}),
                    "memory": str(d.get("memory", ""))[:120],
                },
            )
        return BrainResult(False, dict(FB_RESOLVE))

    # ════════════════ 角色互动 ════════════════
    async def resolve_interaction(self, *, world, a, b=None, npc=None, mode: str,
                                  detail: str, rel_score: int) -> BrainResult:
        from .config import rel_label

        b_ps = ""
        if b:
            b_ps = f"\nB资料:{b.persona_line()},背景:{b.backstory[:100] or '未详'},体力{b.stamina}/心情{b.mood}"
        if npc:
            b_ps = f"\nB资料:{npc.get('name','?')}({npc.get('role','')}),{npc.get('persona','')}"
        rel_line = f"两人当前关系:{rel_score}({rel_label(rel_score)})。" if not npc else "对方是本世界的NPC。"
        sys = self.style
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"A:{a.persona_line()},背景:{a.backstory[:100] or '未详'},体力{a.stamina}/心情{a.mood}/金币{a.gold}"
            f"{b_ps}\n{rel_line}\n"
            f"互动:「{mode}」" + (f"({detail[:60]})" if detail else "") + "\n"
            "写出这段互动的走向与结果(70-130字,让性格碰撞出戏)。"
            "若是消费类互动(请客/送礼),务必扣 A 的金币并给 B 心情。\n"
            '严格输出 JSON:{"narration":"互动叙述","a_effects":{"mood":±,"gold":±,"exp":0-10,'
            '"stamina":±,"attrs":{}}, "b_effects":{"mood":±,"gold":±},'
            ' "rel_delta":-20~20整数, "memory":"一句话存档"}'
        )
        d = await self._ask_json(sys, user)
        if d and d.get("narration"):
            return BrainResult(
                True,
                {
                    "narration": str(d["narration"])[:280],
                    "a_effects": _clamp_effects(d.get("a_effects") or {}),
                    "b_effects": _clamp_effects(d.get("b_effects") or {}),
                    "rel_delta": _clamp(d.get("rel_delta", 0), -20, 20),
                    "memory": str(d.get("memory", ""))[:120],
                },
            )
        return BrainResult(False, dict(FB_INTERACT))

    # ════════════════ 主动行动(练习/健身/打工/打怪/冒险)════════════════
    async def resolve_action(self, *, world, char, action_name: str, detail: str,
                             kind: str = "safe", memories: list[str] | None = None) -> BrainResult:
        """结算一次玩家主动行动。kind: safe | risk(风险型可失败/受伤)。"""
        attrs_names = "、".join(f"{k}={v}" for k, v in ATTR_NAMES.items())
        risk_line = (
            "【风险型】结果起伏大:可能大丰收,也可能受伤/掉属性/破财。数值范围可以放得更宽。"
            if kind == "risk"
            else "【日常型】大体都往好的方向走,只是奖励丰俭有别;不要给毁灭性打击。"
        )
        mem = "\n".join(memories[:4]) if memories else ""
        sys = self.style
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"角色:{char.persona_line()},背景:{char.backstory[:100] or '未详'},"
            f"当前体力{char.stamina}/心情{char.mood}/金币{char.gold}\n"
            f"今日于《{world.name}》执行行动:「{action_name}」{detail[:80]}\n{risk_line}\n{mem}\n"
            "请写出这次行动的经过与结果(60-120字,结合世界设定与角色性格,有画面感与余味)。\n"
            "属性键:" + attrs_names + "。日常型行动要消耗的体力由系统扣除,效果表里不要写体力。\n"
            '严格输出 JSON:{"narration":"行动叙述",'
            '"effects":{"mood":±,"gold":±,"exp":0-25,"stamina":±(仅风险型可写),"attrs":{"force":0}},"memory":"一句话存档"}\n'
            "数值克制:日常型大部分±5~15、exp 5~18、金币±0~40;风险型可到 exp 5~30、金币 0~80,失败时给负反馈但不要毁灭性打击。"
        )
        d = await self._ask_json(sys, user)
        if d and d.get("narration"):
            return BrainResult(
                True,
                {
                    "narration": str(d["narration"])[:280],
                    "effects": _clamp_effects(d.get("effects") or {}),
                    "memory": str(d.get("memory", ""))[:120],
                },
            )
        return BrainResult(False, dict(FB_ACT))

    # ════════════════ NPC 对话 ════════════════
    async def npc_chat(self, *, world, npc: dict, char, action: str,
                       memories: list[str] | None = None) -> BrainResult:
        sys = self.style
        user = (
            f"世界:《{world.name}》。NPC「{npc['name']}」({npc.get('role','')},{npc.get('persona','')};"
            f"钩子:{npc.get('hook','')})\n"
            f"角色:{char.persona_line()}\n角色行为:{action[:80]}\n"
            f"{chr(10).join(memories[:4]) if memories else ''}\n"
            "以NPC的口吻回应1-3句(保留人设与神秘感),旁白一句收尾,可给微小奖励。\n"
            '严格输出 JSON:{"reply":"NPC台词","narration":"旁白",'
            '"effects":{"mood":±,"gold":±,"exp":0-8}, "memory":"一句话存档"}'
        )
        d = await self._ask_json(sys, user)
        if d and d.get("reply"):
            return BrainResult(
                True,
                {
                    "reply": str(d["reply"])[:160],
                    "narration": str(d.get("narration", ""))[:160],
                    "effects": _clamp_effects(d.get("effects") or {}),
                    "memory": str(d.get("memory", ""))[:120],
                },
            )
        return BrainResult(False, dict(FB_NPC))

    # ════════════════ 抵达/晨报 ════════════════
    async def compose_arrival(self, *, world, prev_name: str, via: str) -> BrainResult:
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

    async def morning_brief(self, *, world, chars: list, day_note: str) -> BrainResult:
        who = "、".join(c.persona_line() for c in chars[:8]) or "暂无居民"
        sys = self.style
        user = (
            f"世界:《{world.name}》。今日({day_note})的晨报。居民:{who}\n"
            "写一段晨报(50-90字):天气/异象 + 今日氛围 + 点名一位居民该当心什么。\n"
            '严格输出 JSON:{"brief":"晨报正文","watch":"被点名者与原因(≤20字)"}'
        )
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
    "narration": "事情以一种说不上好也说不上坏的方式落幕了。世界继续运转,而你记下了这一笔。",
    "effects": {"exp": 6, "mood": 0},
    "memory": "经历了一场无名的街头遭遇。",
}

FB_INTERACT = {
    "narration": "你们比划了几句,气氛微妙地平衡着。世界很大,相遇总是件小事,但小事攒多了就成了故事。",
    "a_effects": {"exp": 4},
    "b_effects": {"mood": 2},
    "rel_delta": 2,
    "memory": "与同伴有过一次不起眼的交集。",
}

FB_NPC = {
    "reply": "……嗯?稀客。这镇子上的事,知道得越少,睡得越香。",
    "narration": "对方摆了摆手,没再多说。",
    "effects": {"exp": 3},
    "memory": "与一位本地人打过照面。",
}

FB_ACT = {
    "narration": "你把这件事认真做下来,出了不少汗,也攒下了一点东西。唯有自己清楚这份收获。",
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
