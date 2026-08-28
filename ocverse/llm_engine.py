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

from .config import ATTRS, ATTR_KEYS, ATTR_NAMES

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
        '"npcs":[{"name":"","role":"身份","persona":"性格一句话","hook":"可交互的钩子一句话"}],'
        '"event_ideas":["该世界独有事件灵感",4-6条]}'
    )

    async def gen_world(self, desc: str | None = None, avoid_names: list[str] | None = None,
                        theme_hint: str = "") -> BrainResult:
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
            "\nNPC 4~5个(名字不超过6字,融入世界,不要套模板名)。\n"
            f"严格输出 JSON,结构:{self._WORLD_SCHEMA}"
        )
        # 世界生成是低频操作,允许走联网增强通道(搜索工具)扩充知识
        d = await self._ask_json(sys, user, use_tools=True)
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
        d = await self._ask_json(sys, user, use_tools=True)
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
    async def gen_quests(self, *, world, char, memories: list[str] | None = None) -> BrainResult:
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
        d = await self._ask_json(self.style, user)
        if d and d.get("quests"):
            quests = [{"text": str(q.get("text", "")).strip()[:24],
                       "hint": str(q.get("hint", "")).strip()[:30]}
                      for q in d["quests"][:3] if isinstance(q, dict) and str(q.get("text", "")).strip()]
            if quests:
                return BrainResult(True, {"quests": quests})
        return BrainResult(False, dict(FB_QUESTS))

    async def finish_quest(self, *, world, char, quest: str,
                           memories: list[str] | None = None) -> BrainResult:
        """结算一个小任务:轻松日常的完成叙述 + 很小的奖励(数值克制)。"""
        mem = "\n".join(memories[:3]) if memories else ""
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"角色:{char.persona_line()},背景:{char.backstory[:80] or '未详'}\n"
            f"角色完成了今日小任务:「{quest[:30]}」\n"
            f"{mem}\n"
            "写一段简短的完成叙述(40~70字,轻松日常,有画面感,有余味),并给一点小奖励。\n"
            '严格输出 JSON:{"narration":"完成叙述","effects":{"exp":5~12,"gold":0~20,"mood":0~3}}'
        )
        d = await self._ask_json(self.style, user)
        if d and d.get("narration"):
            eff_in = d.get("effects") if isinstance(d.get("effects"), dict) else {}
            eff = {}
            for k, lo, hi in (("exp", 0, 15), ("gold", 0, 25), ("mood", 0, 5)):
                try:
                    eff[k] = max(lo, min(hi, int(round(float(eff_in.get(k) or 0)))))
                except (TypeError, ValueError):
                    pass
            return BrainResult(True, {"narration": str(d["narration"])[:200], "effects": eff})
        return BrainResult(False, dict(FB_QUEST_DONE))


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

FB_QUESTS = {
    "quests": [
        {"text": "在附近吃一顿当地早餐", "hint": "找个顺眼的小店坐下"},
        {"text": "向一位NPC打听一件小事", "hint": "聊上几句就算数"},
        {"text": "捡一件有趣的小东西", "hint": "路边的、海边的都行"},
    ],
}

FB_QUEST_DONE = {
    "narration": "你把这件小事认真做完了。日子就是这样一件件小事攒起来的。",
    "effects": {"exp": 8, "mood": 2},
}
