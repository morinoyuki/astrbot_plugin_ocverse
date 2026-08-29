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
    "节奏明快、对话生动口语化、结尾留有余味或小小的转折;"
    "严格贴合指令给定的行动/互动主题展开,不跑题、不擅改场景、不凭空插入无关事件或陌生人物;"
    "文风克制不堆砌,不水字数、不出戏、不提及任何现实平台或AI身份。"
    "所有输出必须是严格的 JSON,不要 markdown 代码围栏,不要解释。"
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
        '"infra":[{"kind":"设施类型(店/馆/铺/坊/堂/楼/坛/站/所/市集/乐园/酒吧/剧场/温泉/观景等,尽量不重复)","name":"设施名(2~6字)","desc":"功能/氛围一句话(≤20字)","work":"在这里能打工赚钱的职业(无则不填)"}],'
        '"mainline":[{"stage":"主线小节名(≤10字)","desc":"这一步要做什么/线索(≤30字)"}],'
        '"plots":[{"kind":"房|宅|小屋|公寓|铺面|庄园…","name":"可购住处名(2~6字)","desc":"一句话","price":按物价设定的金币价,600~8000区间}]}'
    )

    @staticmethod
    def _norm_infra(d: dict) -> list:
        """规范化基础设施列表(上限 INFRA_MAX)。"""
        from . import config as C
        out = []
        for it in (d.get("infra") or [])[:C.INFRA_MAX]:
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
                price = max(30, int(float(p.get("price") or 0)))
            except (TypeError, ValueError):
                price = 800
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
            "\nNPC 8~14个(名字不超过6字,融入世界,不要套模板名;身份/职业尽量多样,"
            "覆盖衣食住行玩各方面)。每个NPC都要有自己的"
            "日常行踪(daily:TA每天一般在哪/做什么)、鲜活小怪癖或口头禅(quirk),"
            "让这个世界住着活生生的人。\n"
            "然后为这个世界设计以下内容(必须全部给出,分别填入 infra / mainline / plots 三个数组):\n"
            "- infra: 20~28个贴合该世界题材与时代的基础设施,种类尽量丰富不重复,"
            "必须覆盖生存必要(补给/住宿/餐饮/医疗/据点),还要有不少社交娱乐约会场所"
            "(茶馆酒馆/戏台剧场/温泉澡堂/公园花园/观景看台/夜市市集/约会胜地等,越丰富越热闹),"
            "(茶馆酒馆/戏台剧场/温泉澡堂/公园花园/观景看台/夜市市集/约会胜地等,越丰富越热闹),"
            "其中至少 2 个能打工赚钱(填 work 职业,符合世界观);\n"
            "  可从这些类型里选或自创:商店/集市/饭馆/小吃摊/茶馆/酒馆/咖啡馆/旅店/澡堂/书店/当铺/"
            "  花店/药铺/诊所/铁匠铺/工坊/裁缝铺/戏院/道场/学园/祭坛/神社/码头/驿站/车马行/据点/地标等,\n"
            "  kind/name/desc/work 必填;\n"
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
                d2 = await self._ask_json(sys, user + "\n\n【纠正】你漏掉了基础设施或主线,请补全:infra 至少 20 个可去的场所(种类尽量丰富:含生存必要与社交娱乐约会场所、至少2个能打工的 work),mainline 至少 3 节主线,plots 至少 3 处可购住处。不要省略这些数组。", use_tools=True)
                if d2 and d2.get("name"):
                    d = {**d, "infra": d2.get("infra") or d.get("infra"),
                         "mainline": d2.get("mainline") or d.get("mainline"),
                         "plots": d2.get("plots") or d.get("plots")}
            return BrainResult(True, self._norm_world(d, source="llm", desc_hint=desc))
        return BrainResult(False, self._fallback_world(desc))

    async def regen_infra(self, *, world, material: str = "") -> BrainResult:
        """管理员:重新规划世界基础设施。必须贴合世界观与时代,
        20~28 个、覆盖生存必要类型(补给/住宿/餐饮/医疗/据点)、包含社交娱乐约会场所、至少 2 个可打工。"""
        sys = self.style
        existing = "、".join(
            str(i.get("name", "")) for i in (world.infra or []) if isinstance(i, dict) and i.get("name"))
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"氛围:{world.atmosphere}\n世界规则:{';'.join(world.rules or [])}\n"
            f"现有设施(重新规划,不要照抄这些名字):{existing or '无'}\n\n"
            "请以世界规划者的身份,重新设计这个世界的基础设施。\n"
            "【硬性要求】\n"
            "1. 20~28 个,种类丰富不重复,全部必须贴合该世界的题材、时代与科技水平;\n"
            "2. 必须覆盖生存必要类型:补给(店铺/集市)、住宿(旅馆/酒店)、餐饮(饭馆/食堂)、"
            "医疗(医院/诊所)、据点(聚居/集会之所),缺一不可;\n"
            "3. 还要包含不少社交/娱乐/约会场所(茶馆酒馆、戏台剧场、温泉澡堂、公园花园、"
            "观景看台、夜市市集、约会胜地等),让世界热闹有烟火气;\n"
            "4. 其中至少 2 个能打工赚钱(填 work 职业,职业符合世界观);\n"
            "5. 名字 2~6 字,融入世界观,不要套用现代模板。\n"
            '严格输出 JSON:{"infra":[{"kind":"类型","name":"设施名",'
            '"desc":"功能/氛围一句话(≤20字)","work":"打工职业(无则不填)"}]}'
        )
        user = self._with_material(user, material)
        d = await self._ask_json(sys, user, use_tools=True)
        if d and d.get("infra"):
            infra = self._norm_infra(d)
            if len(infra) >= 5:
                return BrainResult(True, {"infra": infra})
            # 数量不足:带纠正提示重试一次
            d2 = await self._ask_json(
                sys,
                user + "\n\n【纠正】设施太少或不合规,请补全:至少 20 个、种类丰富,"
                       "覆盖补给/住宿/餐饮/医疗/据点五类并含社交娱乐约会场所,至少 2 个可打工。",
                use_tools=True)
            if d2 and self._norm_infra(d2):
                return BrainResult(True, {"infra": self._norm_infra(d2)})
        return BrainResult(False, {"infra": []})

    async def enrich_user_world(self, name: str, desc: str, material: str = "") -> BrainResult:
        """用户自设世界落地时补全细节(失败也能用原始描述)。"""
        sys = self.style
        user = (
            f"玩家自设了一个世界。名称:{name}\n描述:{desc}\n"
            "请补全它的题材标签、氛围、规则、独特之处、6~10个NPC(身份多样,各有日常行踪与小怪癖)、"
            "独有事件灵感。并设计 20~28 个种类丰富的贴合设定的基础设施(含社交娱乐约会场所、至少2个能打工)、"
            "一段世界主线、可供购置的住处——贴合该世界设定,勿套模板。\n"
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
        for n in (d.get("npcs") or [])[:16]:
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
            "desc": str(d.get("desc", desc_hint or "一个尚未展开的世界。"))[:4000],
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
            w["desc"] = desc[:4000]
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
            else f"主角是 {char.persona_line()},背景:{char.backstory[:600] or '未详'}。"
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
                f"角色{i}:{c.persona_line()},背景:{c.backstory[:600] or '未详'},"
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
            "【连贯性铁律】结算必须紧接上面这场遭遇续写:同一时间、同一地点、同一批在场人物,"
            "从做出选择之后的下一秒写起。严禁跳跃到新的时间/地点,严禁引入遭遇场景里没有的新人物;"
            "dialogues 的 speaker 必须使用遭遇中出现过的角色本名(禁止「少女」「神秘人」之类代称)。\n"
            "请结算:叙述结果(轻小说式,120~220字:画面感+心理细节+余味或小转折),并给出数值变化。"
            "属性键:" + attrs_names + "。\n"
            '"dialogues":事件中人物的多轮对话(2~5轮,IM聊天体,每条"speaker"≤8字、"text"≤60字,可含(动作)小注)。'
            "禁止独角戏:至少 2 个不同说话人,事件人物必须开口回应,不能只有主角一人自说自话。\n"
            '严格输出 JSON:{"narration":"结果叙述","dialogues":[{"speaker":"","text":""}],"effects":{"stamina":±,"mood":±,"gold":±,"exp":0-25,'
            '"attrs":{"force":0}}, "memory":"第三人称一句话记忆存档", "state":{"type":"囚禁|束缚|被困...","reason":"一句原因"}, "state_lift":true, '
            '"items_gain":[{"name":"获得物品≤12字","note":"来历≤20字"}],"items_lose":["失去/消耗的物品名"]}\n'
            "items_gain/items_lose 只在剧情自然涉及物品得失时输出(拾获/受赠/被掳/消耗),通常为空数组;物品名要贴合世界观。\n"
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
                    "items_gain": self._norm_items(d)[0],
                    "items_lose": self._norm_items(d)[1],
                },
            )
        return BrainResult(False, dict(FB_RESOLVE))

    async def resolve_life_event(self, *, world, chars, event: dict, choice_idx: int,
                                 rels: str = "", material: str = "") -> BrainResult:
        """结算一次群像生活事件:叙述这次交集的结果 + 各角色效果 + 羁绊变化。"""
        opts = event.get("options") or []
        pick = opts[choice_idx] if 0 <= choice_idx < len(opts) else {"label": "顺其自然", "hint": ""}
        cast = "\n".join(
            f"角色{i}:{c.persona_line()},背景:{c.backstory[:600] or '未详'},体力{c.stamina}/心情{c.mood}/金币{c.gold}"
            for i, c in enumerate(chars, 1)
        )
        rel_line = ("\n" + rels) if rels else ""
        sys = self.style
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n{cast}{rel_line}\n"
            f"他们正在这场交集里:「{event.get('title')}」——{event.get('scene')}\n"
            f"大家共同选择了「{pick['label']}」({pick.get('hint','')})。\n"
            "【连贯性铁律】结算必须紧接这场交集续写:同一时间、同一地点、同一批在场角色,"
            "严禁跳到新的时间/地点或引入场景里没有的新人物;dialogues 的 speaker 用角色本名。\n"
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
            b_ps = f"\nB资料:{b.persona_line()},背景:{b.backstory[:600] or '未详'},体力{b.stamina}/心情{b.mood}"
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
            f"A:{a.persona_line()},背景:{a.backstory[:600] or '未详'},体力{a.stamina}/心情{a.mood}/金币{a.gold}"
            f"{b_ps}\n{rel_line}\n"
            f"互动:「{mode}」" + (f"({detail[:60]})" if detail else "") + state_line + "\n"
            f"【切题铁律】这段叙述演的必须就是上面这场「{mode}」互动"
            + (f"({detail[:60]})" if detail else "") + "。"
            "A与B是仅有的主角,写两人相处的过程与氛围(如约会就是两人约会:地点/话题/氛围/情感流动);"
            "严禁跑题:不得凭空插入与互动无关的遭遇/战斗/陌生人物/超展开;"
            "知识库素材只作风味点缀,不得改变本次互动的主题、场景与人物关系。\n"
            "dialogues 的 speaker 必须用 A/B 的本名,禁止「少女」「神秘人」之类代称。\n"
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

    async def propose_bond(self, *, world, a, b, label: str, rel_score: int,
                           rel_stage: str = "", material: str = "") -> BrainResult:
        """自定义关系提案:A 想成为 B 的「label」(如爸爸/主人/女仆),
        由 B 的性格与两人关系判断是否同意。仅限搞怪/生活向称谓,亲密关系已在代码层拦截。"""
        sys = self.style
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"A(提案人):{a.persona_line()},背景:{a.backstory[:400] or '未详'}\n"
            f"B(被提案):{b.persona_line()},背景:{b.backstory[:400] or '未详'}\n"
            f"两人当前关系:{rel_score}分({rel_stage})\n"
            f"A 想成为 B 的「{label}」。\n"
            "【判定要求】以 B 的视角与性格判断是否接受(agree true/false),并写出这段提案交锋的场景。"
            "贴合两人亲疏:关系好或提案够好笑可以爽快答应,关系差或提案离谱就嫌弃地拒绝,理由要符合人设。"
            "这是搞怪/生活向关系,严禁发展出恋人/情侣/夫妻等亲密内容。\n"
            "【切题铁律】只写这场提案交锋,A与B是仅有主角;"
            "严禁凭空插入无关事件/战斗/陌生人物;知识库素材只作风味点缀。\n"
            "dialogues 的 speaker 必须用 A/B 的本名,禁止代称。\n"
            "请输出提案经过与结果(120~200字:画面感+心理细节+余味)。\n"
            '严格输出 JSON:{"agree":true,"narration":"经过与结果",'
            '"dialogues":[{"speaker":"A名","text":""},{"speaker":"B名","text":""}],'
            '"effects":{"mood":±,"exp":±},"memory":"第三人称一句话存档"}'
            "dialogues 2~4轮;effects 数值克制(±3~10,只给 mood/exp)。"
        )
        user = self._with_material(user, material)
        d = await self._ask_fixed_dialogues(sys, user, counterpart=b.name, limit=4)
        if d and isinstance(d.get("agree"), bool) and d.get("narration"):
            eff = d.get("effects") if isinstance(d.get("effects"), dict) else {}
            eff.pop("gold", None)
            return BrainResult(
                True,
                {
                    "agree": bool(d["agree"]),
                    "narration": str(d["narration"])[:300],
                    "dialogues": self._norm_dialogues(d.get("dialogues"), 4),
                    "effects": _clamp_effects(eff),
                    "memory": str(d.get("memory", ""))[:120],
                },
            )
        return BrainResult(False, dict(FB_BOND))

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

    async def facility_event(self, *, world, char, facility: dict, action: str,
                             memories: list[str] | None = None, material: str = "") -> BrainResult:
        """造访一处可交互设施(社交/娱乐/约会等),生成一段小事件剧情。
        数值克制,偶尔带点好处/小纠纷,营造烟火气。"""
        mem = "\n".join(memories[:3]) if memories else ""
        fac_name = str(facility.get("name") or "某处")
        fac_kind = str(facility.get("kind") or "设施")
        fac_desc = str(facility.get("desc") or "")
        sys = self.style
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"角色:{char.persona_line()},背景:{char.backstory[:600] or '未详'},体力{char.stamina}/心情{char.mood}/金币{char.gold}\n"
            f"角色去《{world.name}》的「{fac_name}」({fac_kind}——{fac_desc})"
            f"[想做的事:{action[:60]}],想在这里打发时光。\n{mem}\n"
            "写一段在这家场所里的经历(轻小说式,100~200字:环境氛围+与场内人物的互动+一件小事或小插曲+余味)。\n"
            '"dialogues":场内与人的简短对话(2~4轮,IM聊天体,每条"speaker"≤8字、"text"≤60字)。'
            "禁止独角戏:至少 2 个不同说话人,不能只有角色自说自话。\n"
            "属性键:" + "、".join(f"{k}={v}" for k, v in ATTR_NAMES.items()) + "。\n"
            '严格输出 JSON:{"narration":"经历叙述","effects":{"mood":±,"gold":±,"exp":0-10,"attrs":{"force":0}},"memory":"一句话存档", "items_gain":[{"name":"可选≤12字","note":"≤20字"}]}\n'
            "数值克制(大部分±3~12);items_gain 只在自然得到时才给(如买到的纪念品),通常为空;不要输出体力。"
        )
        user = self._with_material(user, material)
        d = await self._ask_fixed_dialogues(sys, user, limit=4)
        if d and d.get("narration"):
            gains, _l = self._norm_items(d)
            return BrainResult(True, {
                "narration": str(d["narration"])[:300],
                "dialogues": self._norm_dialogues(d.get("dialogues"), 4),
                "effects": _clamp_effects(d.get("effects") or {}),
                "memory": str(d.get("memory", ""))[:120],
                "items_gain": gains,
            })
        return BrainResult(False, dict(FB_ACT))

    async def home_event(self, *, world, char, plot: dict,
                         memories: list[str] | None = None, material: str = "") -> BrainResult:
        """回宅时小概率触发的家居事件剧情(日常温馨或一件小意外)。"""
        mem = "\n".join(memories[:3]) if memories else ""
        pname = str(plot.get("name") or "家里")
        sys = self.style
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"角色:{char.persona_line()}\n"
            f"角色回到《{world.name}》的「{pname}」家中,打算歇一歇。\n{mem}\n"
            "写一段回到家里的小剧情(轻小说式,80~150字:归家的画面感+一件温馨的小事或小插曲+余味)。\n"
            '"dialogues":到家后与家人/邻居/生活角色的简短对话(1~3轮,IM聊天体,每条"speaker"≤8字、"text"≤40字)。'
            "禁止独角戏:至少 2 个不同说话人。\n"
            '严格输出 JSON:{"narration":"家居剧情","dialogues":[{"speaker":"","text":""}],"effects":{"mood":±,"exp":0~6},"memory":"一句话"}\n'
            "数值克制(mood/exp ±0~6),不要输出金币和体力。"
        )
        user = self._with_material(user, material)
        d = await self._ask_fixed_dialogues(sys, user, limit=3)
        if d and d.get("narration"):
            return BrainResult(True, {
                "narration": str(d["narration"])[:220],
                "dialogues": self._norm_dialogues(d.get("dialogues"), 3),
                "effects": d.get("effects") if isinstance(d.get("effects"), dict) else {},
                "memory": str(d.get("memory", ""))[:120],
            })
        return BrainResult(False, dict(FB_ARRIVE))

    async def settle_work(self, *, world, char, spot: str, job: str, hours: float,
                          colleague: str | None, material: str = "") -> BrainResult:
        """结算到点的兼职:下班收工叙述 + 与NPC同事的道别互动(数值克制,工钱另算)。"""
        colleague_line = (
            f"今天的同班同事是「{colleague}」,收工时TA与角色有一段自然的道别/闲聊。"
            if colleague else "收工时独自一人,把工具归位后离开。")
        sys = self.style
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"角色:{char.persona_line()}\n"
            f"角色刚结束了在「{spot}」的兼职(职业:{job}),干了约 {hours} 小时。\n"
            f"{colleague_line}\n"
            "写一段下班收工的叙述(轻小说式,100~180字:劳动的画面感+一段小插曲或同事互动+下班时的余味)。\n"
            '"dialogues":在场者与角色的道别对话'
            "(1~3轮,IM聊天体,每条 speaker≤8字、text≤40字),至少 2 个不同说话人。\n"
            '严格输出 JSON:{"narration":"下班叙述","dialogues":[{"speaker":"","text":""}],'
            '"effects":{"mood":±,"exp":0~8,"attrs":{}},'
            '"items_gain":[{"name":"可选:雇主塞的小谢礼≤12字","note":"≤20字"}]}'
            "数值克制(mood/exp ±0~8);items_gain 只在剧情自然时给一件,通常为空;不要输出金币(工钱另算)。"
        )
        user = self._with_material(user, material)
        d = await self._ask_fixed_dialogues(sys, user, counterpart=colleague or "", limit=3)
        if d and d.get("narration"):
            effects = d.get("effects") if isinstance(d.get("effects"), dict) else {}
            effects.pop("gold", None)
            gains, _loses = self._norm_items(d)
            return BrainResult(True, {
                "narration": str(d["narration"])[:280],
                "dialogues": self._norm_dialogues(d.get("dialogues"), 3),
                "effects": effects,
                "items_gain": gains,
            })
        return BrainResult(False, {
            "narration": f"{char.name}在「{spot}」忙了约 {hours} 小时,收工时腰酸背痛,揣着工钱走进暮色里。",
            "dialogues": [], "effects": {"mood": 3, "exp": 4}, "items_gain": [],
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
            f"角色:{char.persona_line()},背景:{char.backstory[:600] or '未详'},"
            f"当前体力{char.stamina}/心情{char.mood}/金币{char.gold}\n"
            f"今日于《{world.name}》执行行动:「{action_name}」{detail[:80]}\n{risk_line}\n{mem}\n"
            "请写出这次行动的经过与结果(轻小说式,100~200字:画面感+心理细节+余味),"
            "结合世界设定与角色性格。\n"
            '"dialogues":行动中与场景人物的简短对话(2~4轮,IM聊天体,每条"speaker"≤8字、"text"≤60字)。'
            "禁止独角戏:至少 2 个不同说话人,场景人物必须回应,不能只有角色自说自话。\n"
            "属性键:" + attrs_names + "。日常型行动要消耗的体力由系统扣除,效果表里不要写体力。\n"
            '严格输出 JSON:{"narration":"行动叙述",'
            '"effects":{"mood":±,"gold":±,"exp":0-25,"stamina":±(仅风险型可写),"attrs":{"force":0}},"memory":"一句话存档", "state":{"type":"...","reason":"..."}, "state_lift":true, '
            '"items_gain":[{"name":"获得物品≤12字","note":"来历≤20字"}],"items_lose":["失去/消耗的物品名"]}\n'
            "items_gain/items_lose 只在行动自然涉及物品得失时输出(拾获/缴获/受赠/消耗/损坏),通常为空数组;物品名要贴合世界观。"
            "数值克制:日常型大部分±5~15、exp 5~18、金币±0~40;风险型可到 exp 5~30、金币 0~80,失败时给负反馈但不要毁灭性打击。"
            "state 与 state_lift 只在处境变化时输出(见规则说明),否则两字段都不要出现。"
        )
        user += state_line
        user = self._with_material(user, material)
        d = await self._ask_fixed_dialogues(sys, user, limit=4)
        if d and d.get("narration"):
            gains, loses = self._norm_items(d)
            return BrainResult(
                True,
                {
                    "narration": str(d["narration"])[:300],
                    "dialogues": self._norm_dialogues(d.get("dialogues"), 4),
                    "effects": _clamp_effects(d.get("effects") or {}),
                    "memory": str(d.get("memory", ""))[:120],
                    "state": d.get("state") if isinstance(d.get("state"), dict) else {},
                    "state_lift": bool(d.get("state_lift")),
                    "items_gain": gains,
                    "items_lose": loses,
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
            f"【切题铁律】这段对话演的必须就是角色的「{action[:40]}」这个行为,只涉及角色与该NPC两人;"
            "严禁跑题:不得凭空插入与行为无关的遭遇/战斗/陌生人物/超展开;"
            "知识库素材只作风味点缀,不得改变本次互动的主题、场景与人物关系。\n"
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
            f"【设定描述】{text[:4000]}\n"
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
                "backstory": str(d.get("backstory") or "").strip()[:4000],
                "attrs": attrs,
            })
        return BrainResult(False, {})

    async def parse_persona_update(self, *, cur_name: str, cur_gender: str,
                                   cur_tags: list, cur_backstory: str, text: str) -> BrainResult:
        """从一段自由描述中判断要修改哪些人设字段。
        只返回需要更新的字段;tags/backstory 给出合并旧设定后的完整新值。"""
        user = (
            f"角色「{cur_name}」当前人设:性别 {cur_gender};性格标签:{'、'.join(cur_tags) or '无'};"
            f"背景设定:{cur_backstory[:1000] or '无'}\n"
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
            out["backstory"] = str(d["backstory"]).strip()[:4000]
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
    @staticmethod
    def _norm_items(d: dict) -> tuple[list[dict], list[str]]:
        """规范化 LLM 输出的物品变化:items_gain(获得)/items_lose(失去)。"""
        gains, loses = [], []
        for g in (d.get("items_gain") or [])[:2]:
            if isinstance(g, dict) and str(g.get("name", "")).strip():
                gains.append({"name": str(g["name"]).strip()[:12],
                              "note": str(g.get("note", "")).strip()[:20]})
        for name in (d.get("items_lose") or [])[:2]:
            if str(name).strip():
                loses.append(str(name).strip()[:12])
        return gains, loses

    @staticmethod
    def _norm_quest_steps(steps, npc_names: list[str], life_names: list[str]) -> list[dict]:
        """规范化任务步骤:类型限 act/npc/life/social/work/item,npc/life 校验名单。"""
        out = []
        for s in (steps or [])[:3]:
            if not isinstance(s, dict):
                continue
            t = str(s.get("type", "")).strip()
            desc = str(s.get("desc", "")).strip()[:30]
            if t == "act":
                kws = [str(k).strip()[:6] for k in (s.get("keywords") or [])[:4] if str(k).strip()]
                if not kws:
                    continue
                out.append({"type": "act", "desc": desc or "完成一次主动行动", "keywords": kws, "done": False})
            elif t == "npc":
                npc = str(s.get("npc", "")).strip()[:10]
                if not npc:
                    continue
                out.append({"type": "npc", "desc": desc or f"找{npc}互动", "npc": npc, "done": False})
            elif t == "life":
                npc = str(s.get("npc", "")).strip()[:10]
                if not npc:
                    continue
                out.append({"type": "life", "desc": desc or f"找{npc}互动", "npc": npc, "done": False})
            elif t == "social":
                out.append({"type": "social", "desc": desc or "与一位群友互动", "done": False})
            elif t == "work":
                out.append({"type": "work", "desc": desc or "完成一次兼职打工", "done": False})
            elif t == "item":
                item = str(s.get("item", "")).strip()[:12]
                if not item:
                    continue
                out.append({"type": "item", "desc": desc or f"取得{item}", "item": item, "done": False})
        # npc/life 目标尽量贴合名单(双向包含容错)
        known = list(npc_names) + list(life_names)
        for s in out:
            if s["type"] in ("npc", "life") and known:
                if not any(s["npc"] in nm or nm in s["npc"] for nm in known):
                    near = next((nm for nm in known if s["npc"][0] == nm[0]), None)
                    if near:
                        s["npc"] = near
        return out

    async def gen_quests(self, *, world, char, npc_names: list[str] | None = None,
                         life_names: list[str] | None = None,
                         facilities: list[dict] | None = None,
                         memories: list[str] | None = None,
                         material: str = "") -> BrainResult:
        """生成 3 个由设施/委托人驱动的任务:每个任务有委托人、发布设施与
        1~3 个可验证步骤(主动行动/找NPC/找生活角色/群友互动/兼职/取得物品)。"""
        mem = "\n".join(memories[:4]) if memories else ""
        npc_line = "、".join((npc_names or [])[:10]) or "暂无"
        life_line = "、".join((life_names or [])[:8]) or "暂无"
        facs = (facilities or [])[:10]
        fac_line = "\n".join(f"- {f.get('name')}({f.get('kind','')}){'·可打工:'+f['work'] if f.get('work') else ''}"
                             for f in facs) or "- 暂无"
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"氛围:{world.atmosphere};世界规则:{';'.join(world.rules or [])}\n"
            f"角色:{char.persona_line()},背景:{char.backstory[:600] or '未详'}\n"
            f"世界知名NPC:{npc_line}\n生活角色:{life_line}\n"
            f"世界设施(任务的发布地点从这里选):\n{fac_line}\n"
            f"{mem}\n"
            "请给这个角色生成今天的 3 个委托任务,由设施的委托板/当值者发布。每个任务包含:\n"
            '1) "giver":委托人(2~6字)——优先用设施相关的组织(如冒险者公会/商会/茶馆掌柜)或上面的NPC,也可临时虚构一个贴合世界观的普通委托人;'
            '严禁把其他玩家/群友写成委托人\n'
            '2) "place":发布设施名(必须从上面设施清单里选)\n'
            '3) "text":任务名≤16字,"hint":完成提示≤20字\n'
            '4) "steps":1~3 个可验证步骤,type 只能从以下选:\n'
            '   {"type":"act","desc":"≤20字","keywords":["关键词2~4个"]} —— 需要玩家用「冒险/打怪」完成,keywords 要能出现在玩家的行动描述里\n'
            '   {"type":"npc","desc":"≤20字","npc":"NPC名"} —— 与某位世界NPC互动;npc 必须用名单本名\n'
            '   {"type":"life","desc":"≤20字","npc":"生活角色名"} —— 与某位生活角色互动;必须用名单本名\n'
            '   {"type":"social","desc":"≤20字"} —— 与任意群友互动一次(不指定具体人)\n'
            '   {"type":"work","desc":"≤20字"} —— 完成一次兼职打工\n'
            '   {"type":"item","desc":"≤20字","item":"物品名≤12字"} —— 取得某件物品(通过冒险/事件获得)\n'
            "要求:3 个任务难度递进(至少1个单步日常,最多1个三步任务);步骤必须与任务文本逻辑一致;"
            "结合世界观,生活气息或小冒险皆可。严禁把其他玩家/群友写成任务目标或委托人。\n"
            '严格输出 JSON:{"quests":[{"text":"","giver":"","place":"","hint":"",'
            '"steps":[{"type":"","desc":"","keywords":[],"npc":"","item":""}]}]}\n恰好 3 个。'
        )
        user = self._with_material(user, material)
        d = await self._ask_json(self.style, user)
        if d and d.get("quests"):
            quests = []
            for q in d["quests"][:3]:
                if not (isinstance(q, dict) and str(q.get("text", "")).strip()):
                    continue
                steps = self._norm_quest_steps(q.get("steps"), npc_names or [], life_names or [])
                if not steps:
                    continue
                fac_names = [f.get("name", "") for f in facs]
                place = str(q.get("place", "")).strip()[:12]
                if fac_names and not any(place in fn or fn in place for fn in fac_names):
                    place = fac_names[0]  # 发布设施必须是清单里的
                quests.append({
                    "text": str(q["text"]).strip()[:24],
                    "hint": str(q.get("hint", "")).strip()[:30],
                    "giver": str(q.get("giver", "")).strip()[:10] or place,
                    "place": place,
                    "steps": steps,
                })
            if quests:
                return BrainResult(True, {"quests": quests})
        return BrainResult(False, {"quests": []})

    async def finish_quest(self, *, world, char, quest: str, giver: str = "", place: str = "",
                           steps_desc: list[str] | None = None,
                           memories: list[str] | None = None,
                        material: str = "") -> BrainResult:
        """向委托人交付任务:交付场景 + 委托人的反应 + 很小的奖励(数值克制)。
        出场人物锁死:只允许角色与委托人(组织则由当值代理人出面),严禁他人乱入。"""
        mem = "\n".join(memories[:3]) if memories else ""
        giver = (giver or "委托人").strip()
        place = (place or "").strip()
        steps_line = "".join(f"\n- ✅ {s}" for s in (steps_desc or [])) or "\n- ✅(单步任务)"
        user = (
            f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
            f"角色:{char.persona_line()},背景:{char.backstory[:600] or '未详'}\n"
            f"角色完成了委托任务:「{quest[:30]}」\n"
            f"委托人:{giver}(发布地点:{place or '不祥'})"
            "——若委托人是组织/设施,由当值代理人出面交付;若是个人委托人则以本名出场\n"
            f"已完成的步骤:{steps_line}\n"
            f"{mem}\n"
            "【出场铁律】这是交付场景:只允许角色与委托人两方出场,"
            "严禁出现其他群友/生活角色/无关NPC/凭空新人物,严禁把交付成果交给或送给别人。\n"
            "写一段交付场景(轻小说式,80~150字:委托人的验收反应+简短交代+余味),并给一点委托酬劳。\n"
            f'"dialogues":角色与「{giver}」的交割对话(1~3轮,IM聊天体,每条"speaker"≤8字、"text"≤40字)。'
            "禁止独角戏:至少 2 个不同说话人,不能只有角色一人说话。\n"
            '严格输出 JSON:{"narration":"交付叙述","effects":{"exp":5~12,"gold":0~25,"mood":0~3},'
            '"items_gain":[{"name":"可选:委托人额外送的谢礼≤12字","note":"≤20字"}]}'
            "items_gain 只在委托人明确会给实物谢礼时才输出,通常为空。"
        )
        user = self._with_material(user, material)
        d = await self._ask_fixed_dialogues(self.style, user, counterpart=giver, limit=3)
        if d and d.get("narration"):
            eff_in = d.get("effects") if isinstance(d.get("effects"), dict) else {}
            eff = {}
            for k, lo, hi in (("exp", 0, 15), ("gold", 0, 25), ("mood", 0, 5)):
                try:
                    eff[k] = max(lo, min(hi, int(round(float(eff_in.get(k) or 0)))))
                except (TypeError, ValueError):
                    pass
            gains, _loses = self._norm_items(d)
            return BrainResult(True, {"narration": str(d["narration"])[:250],
                                     "dialogues": self._norm_dialogues(d.get("dialogues"), 3),
                                     "effects": eff, "items_gain": gains})
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
        """带对话质量守卫的 JSON 请求:
        若返回的 dialogues 是独角戏(只有一个说话人)或对方没开口,带着纠正提示重试一次;
        重试后仍是真独角戏才丢弃对话(宁缺毋滥);仅「对方名字没对上」时保留对话 ——
        叙述已交代在场关系,气泡照常渲染,否则互动卡经常一个气泡都不剩。"""
        d = await self._ask_json(system, user)
        dlg = self._norm_dialogues(d.get("dialogues"), limit) if isinstance(d, dict) else []
        if dlg and not self._dialogue_ok(dlg, counterpart=counterpart):
            speakers = {str(x.get("speaker") or "").strip() for x in dlg}
            speakers.discard("")
            mono = len(speakers) < 2
            user2 = (
                user
                + "\n\n【重要纠正】你刚才的对话"
                + ("是独角戏(只有一个说话人)" if mono else f"没有让「{counterpart}」开口")
                + ",这不合要求。重写 dialogues:必须你来我往、至少 2 个不同的说话人"
                + (f",且「{counterpart}」必须以本名开口回应" if counterpart else "")
                + ";每条 speaker≤8字、text≤60字。"
            )
            d2 = await self._ask_json(system, user2)
            if isinstance(d2, dict) and d2.get("narration"):
                dlg2 = self._norm_dialogues(d2.get("dialogues"), limit)
                if dlg2 and self._dialogue_ok(dlg2, counterpart=counterpart):
                    return d2  # 纠正成功
                sp2 = {str(x.get("speaker") or "").strip() for x in (dlg2 or [])}
                sp2.discard("")
                d2["dialogues"] = dlg2 if len(sp2) >= 2 else []  # 双向对话保留,真独角戏丢弃
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
            f"告白者:{a.persona_line()},背景:{a.backstory[:600] or '未详'}\n"
            f"被告白者:{b.persona_line()},背景:{b.backstory[:600] or '未详'}\n"
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

FB_BOND = {
    "agree": True,
    "narration": "提案荒唐又好笑,B愣了两秒,噗嗤笑出声,摆摆手应了下来——这关系认就认了,"
                 "日子反正要热闹着过,多一个名头不多。",
    "dialogues": [],
    "effects": {"mood": 4},
    "memory": "",
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
