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

from . import prompts
from .config import ATTRS, ATTR_KEYS

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
    for k in ("stamina", "mood", "gold", "exp", "hp"):
        if k in e:
            out[k] = _clamp(e[k], -100, 100)
    rep = e.get("reputation")
    if rep not in (None, ""):
        try:
            out["reputation"] = _clamp(rep, -15, 15)
        except (TypeError, ValueError):
            pass
    attrs = e.get("attrs") if isinstance(e.get("attrs"), dict) else {}
    ka = {k: _clamp(attrs.get(k, 0), -10, 10) for k, _ in ATTRS}
    if any(v != 0 for v in ka.values()):
        out["attrs"] = ka
    return out


@dataclass
class BrainResult:
    ok: bool
    data: dict


# 剧情权重的字数/对话轮上限(结算侧截断,与 prompts.story_weight_line 呼应)
NARR_CAPS = {"normal": 320, "major": 640, "climax": 1150}
DLG_LIMITS = {"normal": 6, "major": 10, "climax": 16}


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
        s = prompts.STYLE_BASE
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
        from . import config as C

        out = []
        for i, m in enumerate((d.get("mainline") or d.get("stages") or [])[:12]):
            if not isinstance(m, dict) or not (m.get("stage") or ""):
                continue
            item = {
                "stage": str(m.get("stage") or "")[:12],
                "desc": str(m.get("desc") or "")[:90],
                "done": bool(m.get("done")),
            }
            goal = m.get("goal") if isinstance(m.get("goal"), dict) else {}
            gt = str(goal.get("type") or "").strip()
            if gt in ("reputation", "quest", "defeat"):
                try:
                    gv = max(1, int(goal.get("value") or 0))
                except (TypeError, ValueError):
                    gv = 0
                if gv:
                    item["goal_type"] = gt
                    item["goal_value"] = gv
                    item["goal_note"] = C.MAINLINE_GOAL_LABEL[gt].format(v=gv)[:40]
            out.append(item)
        return out

    @staticmethod
    def _norm_zones(d: dict) -> list:
        """规范化危险区域列表(上限 ZONES_MAX)。"""
        from . import config as C

        out = []
        for z in (d.get("zones") or [])[:C.ZONES_MAX]:
            if not isinstance(z, dict) or not str(z.get("name") or "").strip():
                continue
            enemies = []
            for e in (z.get("enemies") or [])[:2]:
                if isinstance(e, dict) and str(e.get("name") or "").strip():
                    enemies.append({"name": str(e["name"])[:10], "desc": str(e.get("desc", ""))[:30]})
            out.append({
                "kind": str(z.get("kind") or "区域")[:10],
                "name": str(z.get("name"))[:12],
                "desc": str(z.get("desc", ""))[:60],
                "danger": max(1, min(5, int(z.get("danger") or 1))) if str(z.get("danger") or "1").isdigit() else 1,
                "enemies": enemies,
                "loot": [str(x)[:10] for x in (z.get("loot") or [])[:3] if str(x).strip()],
            })
        return out

    @staticmethod
    def _norm_heal_items(d: dict) -> list:
        """规范化世界治疗物品(3 档)。"""
        out = []
        for h in (d.get("heal_items") or [])[:4]:
            if not isinstance(h, dict) or not str(h.get("name") or "").strip():
                continue
            try:
                heal = max(10, min(200, int(h.get("heal") or 30)))
            except (TypeError, ValueError):
                heal = 30
            try:
                price = max(10, min(500, int(h.get("price") or heal)))
            except (TypeError, ValueError):
                price = heal
            out.append({
                "name": str(h.get("name"))[:10],
                "note": str(h.get("note", ""))[:24],
                "price": price,
                "heal": heal,
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
        user = prompts.gen_world(desc, avoid_names, theme_hint)
        # 世界生成是低频操作,允许走联网增强通道(搜索工具)扩充知识
        user = self._with_material(user, material)
        d = await self._ask_json(sys, user, use_tools=True)
        if d and d.get("name"):
            # 校验:infra/mainline 必须至少各 1 条,否则带纠正提示重试一次
            if not (self._norm_infra(d) and self._norm_mainline(d)):
                d2 = await self._ask_json(sys, user + prompts.WORLD_CORRECT, use_tools=True)
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
        user = prompts.regen_infra(world)
        user = self._with_material(user, material)
        d = await self._ask_json(sys, user, use_tools=True)
        if d and d.get("infra"):
            infra = self._norm_infra(d)
            if len(infra) >= 5:
                return BrainResult(True, {"infra": infra})
            # 数量不足:带纠正提示重试一次
            d2 = await self._ask_json(
                sys,
                user + prompts.INFRA_CORRECT,
                use_tools=True)
            if d2 and self._norm_infra(d2):
                return BrainResult(True, {"infra": self._norm_infra(d2)})
        return BrainResult(False, {"infra": []})

    async def regen_mainline(self, *, world, material: str = "") -> BrainResult:
        """重新生成世界主线(3~6 节,可带阶段门槛;贴合世界观,避开旧名)。"""
        sys = self.style
        user = prompts.regen_mainline(world)
        user = self._with_material(user, material)
        d = await self._ask_json(sys, user, use_tools=True)
        nodes = self._norm_mainline(d) if isinstance(d, dict) else []
        if len(nodes) >= 2:
            return BrainResult(True, {"mainline": nodes})
        d2 = await self._ask_json(sys, user + prompts.MAINLINE_CORRECT, use_tools=True)
        nodes2 = self._norm_mainline(d2) if isinstance(d2, dict) else []
        if len(nodes2) >= 2:
            return BrainResult(True, {"mainline": nodes2})
        return BrainResult(False, {"mainline": []})

    async def regen_zones_heals(self, *, world, material: str = "") -> BrainResult:
        """管理员:重新生成世界的危险区域与治疗物品(贴合世界观,避开旧名)。"""
        sys = self.style
        user = prompts.regen_zones_heals(world)
        user = self._with_material(user, material)
        d = await self._ask_json(sys, user, use_tools=True)
        zones = self._norm_zones(d) if isinstance(d, dict) else []
        heals = self._norm_heal_items(d) if isinstance(d, dict) else []
        if len(zones) >= 2 and len(heals) >= 2:
            return BrainResult(True, {"zones": zones, "heal_items": heals})
        # 数量不足:带纠正提示重试一次
        d2 = await self._ask_json(sys, user + prompts.ZONES_HEALS_CORRECT, use_tools=True)
        zones2 = self._norm_zones(d2) if isinstance(d2, dict) else []
        heals2 = self._norm_heal_items(d2) if isinstance(d2, dict) else []
        if len(zones2) >= 2 and len(heals2) >= 2:
            return BrainResult(True, {"zones": zones2, "heal_items": heals2})
        return BrainResult(False, {"zones": [], "heal_items": []})

    async def enrich_user_world(self, name: str, desc: str, material: str = "") -> BrainResult:
        """用户自设世界落地时补全细节(失败也能用原始描述)。"""
        sys = self.style
        user = prompts.enrich_user_world(name, desc)
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
            "zones": self._norm_zones(d),
            "heal_items": self._norm_heal_items(d),
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
        from . import config as C
        w.setdefault("zones", [dict(z) for z in C.DEFAULT_ZONES[:3]])
        w.setdefault("heal_items", [dict(h) for h in C.DEFAULT_HEAL_ITEMS])
        w.setdefault("plots", [
            {"kind": "小屋", "name": "转角小屋", "desc": "石板路尽头、旧灯塔能望见的那间", "price": 200},
            {"kind": "平房", "name": "街边平房", "desc": "紧挨着港口集市的一间", "price": 400},
            {"kind": "老宅", "name": "镇口老宅", "desc": "有院门的那座,据说镇子初建时就在了", "price": 600},
        ])
        return w

    # ════════════════ 事件生成 ════════════════
    async def make_event(self, *, world, char=None, kind="solo", npc=None,
                         memories: list[str] | None = None, ideas: list[str] | None = None,
                         state_note: str = "", material: str = "",
                         previous: list[str] | None = None) -> BrainResult:
        """生成一次遭遇。char=None 时为全员群事件。state_note: 若角色处于特殊状态(囚禁等),强调脱困方向。
        previous: 近期已发生事件(防剧情绕圈)。"""
        sys = self.style
        user = prompts.make_event(
            world=world, char=char, npc=npc,
            memories=memories, ideas=ideas, state_note=state_note, previous=previous)
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
                              material: str = "",
                              previous: list[str] | None = None) -> BrainResult:
        """生成一次『群像生活事件』:2~N 名玩家角色在同一世界偶遇/结伴/共同度过一段日常。
        chars: 参与的 Char 列表(≥2);rels: 两两关系简述字符串(由 game 层拼接)。
        返回与 make_event 相同的 {title, scene, options} 结构。previous: 近期事件(防剧情绕圈)。"""
        cast = []
        for i, c in enumerate(chars, 1):
            cast.append(
                f"角色{i}:{c.persona_line()},背景:{c.backstory[:600] or '未详'},"
                f"当前体力{c.stamina}/心情{c.mood}/金币{c.gold}"
            )
        sys = self.style
        user = prompts.make_life_event(
            world=world, chars=chars, rels=rels, memories=memories, previous=previous)
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

    @staticmethod
    def _with_avatars(user: str, avatars) -> str:
        """追加本群可用头像名单(供 <d> 标签 av 属性借用);无名单则原样返回。"""
        note = prompts.avatar_note(avatars)
        if not note:
            return user
        return user + note + "\n"
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
            user2 = user + prompts.FRESH_CORRECT
            d2 = await self._ask_fixed_dialogues(system, user2, counterpart=counterpart, limit=limit)
            if not (isinstance(d2, dict) and d2.get("narration")):
                break
            d = d2
        return d

    async def resolve_event(self, *, world, char=None, event: dict, choice_idx: int,
                            previous: list[str] | None = None,
                        state_note: str = "", material: str = "",
                        heal_note: str = "", avatars: list[str] | None = None) -> BrainResult:
        """结算一次选择。char=None(群事件)时叙述群体结果。
        state_note: 若角色当前被困,提示本次抉择可脱困;输出 state 施加特殊状态 / state_lift 解除。
        heal_note: 角色生命与背包治疗物品提示(供 LLM 在剧情中自然使用)。"""
        sys = self.style
        user = prompts.resolve_event(
            world=world, char=char, event=event, choice_idx=choice_idx,
            state_note=state_note, previous=previous, heal_note=heal_note)
        user = self._with_material(user, material)
        user = self._with_avatars(user, avatars)
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
                    "narration": str(d["narration"])[:340],
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

    async def mainline_echo(self, *, world, char, stage: dict, ctx: str) -> BrainResult:
        """主线回响:日常结算后小概率生成一段主线伏笔/呼应叙述(不改任何数值)。"""
        d = await self._ask_json(self.style, prompts.mainline_echo(world=world, char=char, stage=stage, ctx=ctx))
        if d and d.get("narration"):
            return BrainResult(True, {"narration": str(d["narration"])[:200]})
        return BrainResult(False, {})

    async def resolve_life_event(self, *, world, chars, event: dict, choice_idx: int,
                                 rels: str = "", material: str = "",
                                 avatars: list[str] | None = None) -> BrainResult:
        """结算一次群像生活事件:叙述这次交集的结果 + 各角色效果 + 羁绊变化。"""
        sys = self.style
        user = prompts.resolve_life_event(
            world=world, chars=chars, event=event, choice_idx=choice_idx, rels=rels)
        user = self._with_material(user, material)
        user = self._with_avatars(user, avatars)
        d = await self._ask_fixed_dialogues(sys, user, limit=6)
        if d and d.get("narration"):
            eff = d.get("effects") if isinstance(d.get("effects"), dict) else {}
            # 规范:effects 键 → 每角色规范化
            per = {}
            for key, e in eff.items():
                if isinstance(e, dict):
                    per[str(key)] = _clamp_effects(e)
            return BrainResult(True, {
                "narration": str(d["narration"])[:360],
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
                                  state_note: str = "", material: str = "",
                                  rep_note: str = "", avatars: list[str] | None = None) -> BrainResult:
        from .config import rel_label

        sys = self.style
        rel = rel_stage or rel_label(rel_score)
        user = prompts.resolve_interaction(
            world=world, a=a, b=b, npc=npc, mode=mode, detail=detail,
            rel_score=rel_score, rel_stage=rel, state_note=state_note, previous=previous,
            rep_note=rep_note)
        user = self._with_material(user, material)
        user = self._with_avatars(user, avatars)
        counterpart = b.name if b else (str(npc.get("name", "")) if npc else "")
        d = await self._ask_fixed_dialogues(
            sys, user, counterpart=counterpart, limit=6,
        )
        d = await self._ensure_fresh(sys, user, d, previous,
                                     counterpart=counterpart, limit=6)
        if d and d.get("narration"):
            return BrainResult(
                True,
                {
                    "narration": str(d["narration"])[:360],
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
                           rel_stage: str = "", material: str = "",
                           avatars: list[str] | None = None) -> BrainResult:
        """自定义关系提案:A 想成为 B 的「label」(如爸爸/主人/女仆),
        由 B 的性格与两人关系判断是否同意。仅限搞怪/生活向称谓,亲密关系已在代码层拦截。"""
        sys = self.style
        user = prompts.propose_bond(
            world=world, a=a, b=b, label=label,
            rel_score=rel_score, rel_stage=rel_stage)
        user = self._with_material(user, material)
        user = self._with_avatars(user, avatars)
        d = await self._ask_fixed_dialogues(sys, user, counterpart=b.name, limit=4)
        if d and isinstance(d.get("agree"), bool) and d.get("narration"):
            eff = d.get("effects") if isinstance(d.get("effects"), dict) else {}
            eff.pop("gold", None)
            return BrainResult(
                True,
                {
                    "agree": bool(d["agree"]),
                    "narration": str(d["narration"])[:360],
                    "dialogues": self._norm_dialogues(d.get("dialogues"), 4),
                    "effects": _clamp_effects(eff),
                    "memory": str(d.get("memory", ""))[:120],
                },
            )
        return BrainResult(False, dict(FB_BOND))

    # ════════════════ 主动行动(练习/健身/打怪/冒险)════════════════
    async def resolve_mainline(self, *, world, char, stage: dict,
                               material: str = "", goal_note: str = "",
                               weight: str = "major",
                               avatars: list[str] | None = None) -> BrainResult:
        """结算世界主线一小节:角色推进这一步,LLM 写出进展与结果。
        weight: major=关键节点(默认), climax=篇章终章决战。"""
        sys = self.style
        user = prompts.resolve_mainline(world=world, char=char, stage=stage, goal_note=goal_note,
                                        weight=weight)
        user = self._with_material(user, material)
        user = self._with_avatars(user, avatars)
        d = await self._ask_fixed_dialogues(sys, user, limit=DLG_LIMITS.get(weight, 10))
        if d and d.get("narration"):
            return BrainResult(True, {
                "narration": str(d["narration"])[:NARR_CAPS.get(weight, 640)],
                "dialogues": self._norm_dialogues(d.get("dialogues"), DLG_LIMITS.get(weight, 10)),
                "effects": _clamp_effects(d.get("effects") or {}),
                "memory": str(d.get("memory", ""))[:120],
            })
        return BrainResult(False, {
            "narration": f"「{stage.get('stage','')}」有了进展——但路还长,先记下这一步。",
            "dialogues": [], "effects": {"exp": 5, "mood": 2},
        })

    async def gen_epilogue(self, *, world) -> BrainResult:
        """主线全篇完结后续写『尾声』新篇章:过渡叙述 + 2~4 个新小节(可带 goal)。"""
        d = await self._ask_json(self.style, prompts.gen_epilogue(world=world))
        if d and (d.get("stages") or d.get("narration")):
            stages = self._norm_mainline({"mainline": d.get("stages") or []})
            return BrainResult(True, {
                "narration": str(d.get("narration") or "旧篇章落幕,新的暗流已在世界深处涌动。")[:300],
                "stages": stages,
            })
        return BrainResult(False, {"narration": "", "stages": []})

    # ════════════════ 远征系统 ════════════════
    async def expedition_offer(self, *, world, char, zone: dict, issuer: str,
                               teammates: list[str], duration_h: int, rate: int) -> BrainResult:
        """远征委托布告(发布方/同伴由系统按世界观给定,布告以其口吻撰写)。"""
        d = await self._ask_json(self.style, prompts.expedition_offer(
            world=world, char=char, zone=zone, issuer=issuer,
            teammates=teammates, duration_h=duration_h, rate=rate))
        if d and d.get("title") and d.get("briefing"):
            return BrainResult(True, {
                "title": str(d["title"])[:14],
                "briefing": str(d["briefing"])[:240],
                "teaser": str(d.get("teaser", ""))[:20],
            })
        return BrainResult(False, {})

    async def life_char_story(self, *, world, char, other=None,
                              memories: list[str] | None = None,
                              avatars: list[str] | None = None) -> BrainResult:
        """持久生活角色的日常小剧场:自带经验/心情/金钱/好感等变化。"""
        sys = self.style
        user = prompts.life_char_story(world=world, char=char, other=other, memories=memories)
        user = self._with_avatars(user, avatars)
        d = await self._ask_fixed_dialogues(
            sys, user,
            counterpart=(other.name if other else ""), limit=3)
        if d and d.get("narration"):
            return BrainResult(True, {
                "narration": str(d["narration"])[:380],
                "dialogues": self._norm_dialogues(d.get("dialogues"), 3),
                "effects": _clamp_effects(d.get("effects") or {}),
                "rel_delta": _clamp(d.get("rel_delta", 0), -3, 4),
                "items_gain": self._norm_items(d)[0],
                "items_lose": self._norm_items(d)[1],
                "memory": str(d.get("memory", ""))[:120],
            })
        return BrainResult(True, {   # 离线兜底:日子照过,轻微变化
            "narration": f"{char.name}的一天照旧过去了:把手头的事做完,守着小小的习惯,"
                         "在熟悉的地方打了个盹。日子往前挪了一步。",
            "dialogues": [],
            "effects": {"mood": 1, "exp": 2},
            "rel_delta": 0,
            "items_gain": [],
            "items_lose": [],
            "memory": f"{char.name}度过了平常的一天。",
        })

    async def expedition_invite(self, *, world, char, target, offer: dict,
                                rel_score: int, rel_stage: str = "",
                                avatars: list[str] | None = None) -> BrainResult:
        """远征邀约:LLM 以被邀请者性格与双方关系判断是否同行。"""
        sys = self.style
        user = prompts.expedition_invite(world=world, char=char, target=target, offer=offer,
                                         rel_score=rel_score, rel_stage=rel_stage)
        user = self._with_avatars(user, avatars)
        d = await self._ask_fixed_dialogues(sys, user, counterpart=target.name, limit=4)
        if d and isinstance(d.get("agree"), bool) and d.get("narration"):
            return BrainResult(True, {
                "agree": bool(d["agree"]),
                "narration": str(d["narration"])[:300],
                "dialogues": self._norm_dialogues(d.get("dialogues"), 4),
                "rel_delta": _clamp(d.get("rel_delta", 0), -3, 5),
            })
        return BrainResult(False, dict(FB_EXP_INVITE))

    async def expedition_report(self, *, world, char, exp: dict, phase: str,
                                supplies_note: str = "",
                                avatars: list[str] | None = None) -> BrainResult:
        """远征途中的剧情片段(120~220字 + 队友对话1~3轮)。"""
        user = prompts.expedition_report(world=world, char=char, exp=exp, phase=phase,
                                         supplies_note=supplies_note)
        user = self._with_avatars(user, avatars)
        d = await self._ask_fixed_dialogues(self.style, user, limit=4)
        if d and d.get("narration"):
            return BrainResult(True, {
                "narration": str(d["narration"])[:380],
                "dialogues": self._norm_dialogues(d.get("dialogues"), 4),
                "items_lose": self._norm_items(d)[1],
            })
        return BrainResult(False, {})

    async def expedition_settle(self, *, world, char, exp: dict, outcome: str,
                                reward_line: str,
                                avatars: list[str] | None = None) -> BrainResult:
        """远征结算:成功=climax(600~1000字/10~16轮),失败=major(300~600字/6~10轮)。"""
        weight = "climax" if outcome == "success" else "major"
        user = prompts.expedition_settle(world=world, char=char, exp=exp, outcome=outcome,
                                         reward_line=reward_line)
        user = self._with_avatars(user, avatars)
        d = await self._ask_fixed_dialogues(
            self.style, user,
            limit=DLG_LIMITS[weight])
        if d and d.get("narration"):
            return BrainResult(True, {
                "narration": str(d["narration"])[:NARR_CAPS[weight]],
                "dialogues": self._norm_dialogues(d.get("dialogues"), DLG_LIMITS[weight]),
                "memory": str(d.get("memory", ""))[:120],
            })
        return BrainResult(False, {})

    async def facility_event(self, *, world, char, facility: dict, action: str,
                             memories: list[str] | None = None, material: str = "",
                             avatars: list[str] | None = None) -> BrainResult:
        """造访一处可交互设施(社交/娱乐/约会等),生成一段小事件剧情。
        数值克制,偶尔带点好处/小纠纷,营造烟火气。"""
        sys = self.style
        user = prompts.facility_event(
            world=world, char=char, facility=facility,
            action=action, memories=memories)
        user = self._with_material(user, material)
        user = self._with_avatars(user, avatars)
        d = await self._ask_fixed_dialogues(sys, user, limit=4)
        if d and d.get("narration"):
            gains, _l = self._norm_items(d)
            return BrainResult(True, {
                "narration": str(d["narration"])[:360],
                "dialogues": self._norm_dialogues(d.get("dialogues"), 4),
                "effects": _clamp_effects(d.get("effects") or {}),
                "memory": str(d.get("memory", ""))[:120],
                "items_gain": gains,
            })
        return BrainResult(False, dict(FB_ACT))

    async def home_event(self, *, world, char, plot: dict,
                         memories: list[str] | None = None, material: str = "",
                         avatars: list[str] | None = None) -> BrainResult:
        """回宅时小概率触发的家居事件剧情(日常温馨或一件小意外)。"""
        sys = self.style
        pname = str(plot.get("name") or "家里")
        user = prompts.home_event(
            world=world, char=char, plot_name=pname, memories=memories)
        user = self._with_material(user, material)
        user = self._with_avatars(user, avatars)
        d = await self._ask_fixed_dialogues(sys, user, limit=3)
        if d and d.get("narration"):
            return BrainResult(True, {
                "narration": str(d["narration"])[:260],
                "dialogues": self._norm_dialogues(d.get("dialogues"), 3),
                "effects": d.get("effects") if isinstance(d.get("effects"), dict) else {},
                "memory": str(d.get("memory", ""))[:120],
            })
        return BrainResult(False, dict(FB_ARRIVE))

    async def settle_work(self, *, world, char, spot: str, job: str, hours: float,
                          colleague: str | None, material: str = "",
                          avatars: list[str] | None = None) -> BrainResult:
        """结算到点的兼职:下班收工叙述 + 与NPC同事的道别互动(数值克制,工钱另算)。"""
        sys = self.style
        user = prompts.settle_work(
            world=world, char=char, spot=spot, job=job,
            hours=hours, colleague=colleague)
        user = self._with_material(user, material)
        user = self._with_avatars(user, avatars)
        d = await self._ask_fixed_dialogues(sys, user, counterpart=colleague or "", limit=3)
        if d and d.get("narration"):
            effects = d.get("effects") if isinstance(d.get("effects"), dict) else {}
            effects.pop("gold", None)
            gains, _loses = self._norm_items(d)
            return BrainResult(True, {
                "narration": str(d["narration"])[:340],
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
                             state_note: str = "", material: str = "",
                             heal_note: str = "", zone_note: str = "",
                             avatars: list[str] | None = None) -> BrainResult:
        """结算一次玩家主动行动。kind: safe | risk(风险型可失败/受伤)。
        state_note: 若角色被困,本次『冒险』即脱困尝试,由 LLM 判定是否成功脱困。
        heal_note: 角色生命与背包治疗物品提示;zone_note: 讨伐区域锁定(打怪)。"""
        sys = self.style
        user = prompts.resolve_action(
            world=world, char=char, action_name=action_name, detail=detail,
            kind=kind, memories=memories, state_note=state_note, heal_note=heal_note,
            zone_note=zone_note)
        user = self._with_material(user, material)
        user = self._with_avatars(user, avatars)
        d = await self._ask_fixed_dialogues(sys, user, limit=4)
        if d and d.get("narration"):
            gains, loses = self._norm_items(d)
            return BrainResult(
                True,
                {
                    "narration": str(d["narration"])[:360],
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
                       state_note: str = "", material: str = "",
                       rep_note: str = "", avatars: list[str] | None = None) -> BrainResult:
        sys = self.style
        user = prompts.npc_chat(
            world=world, npc=npc, char=char, action=action,
            memories=memories, state_note=state_note, previous=previous, rep_note=rep_note)
        counterpart = str(npc.get("name", ""))
        user = self._with_material(user, material)
        user = self._with_avatars(user, avatars)
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
        sys = self.style
        user = prompts.compose_arrival(world=world, prev_name=prev_name, via=via)
        user = self._with_material(user, material)
        d = await self._ask_json(sys, user)
        if d and d.get("narration"):
            return BrainResult(
                True,
                {
                    "narration": str(d["narration"])[:360],
                    "tips": [str(x)[:40] for x in (d.get("tips") or [])][:3],
                },
            )
        return BrainResult(False, dict(FB_ARRIVE, name=world.name))

    async def morning_brief(self, *, world, chars: list, day_note: str,
                        material: str = "") -> BrainResult:
        sys = self.style
        user = prompts.morning_brief(world=world, chars=chars, day_note=day_note)
        user = self._with_material(user, material)
        d = await self._ask_json(sys, user)
        if d and d.get("brief"):
            return BrainResult(
                True, {"brief": str(d["brief"])[:220], "watch": str(d.get("watch", ""))[:40]}
            )
        return BrainResult(False, dict(FB_MORNING))

    async def summarize_core(self, uid_name: str, old_texts: list[str]) -> list[str]:
        sys = self.style
        user = prompts.summarize_core(uid_name, old_texts)
        d = await self._ask_json(sys, user)
        if d and d.get("cores"):
            return [str(x)[:60] for x in d["cores"]][:5]
        return []


    # ════════════════ 自由文本 → 结构化人设(创角/改角/加NPC)════════════════
    async def parse_persona(self, text: str) -> BrainResult:
        """把一段口语化的「设定描述」整理成 {gender, tags, backstory, attrs}。
        attrs = 按设定分配的初始六维(如「大天才」的智力应最高)。
        失败返回 ok=False,由调用方朴素兑底(描述原文入背景,不丢信息)。"""
        user = prompts.parse_persona(text)
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
        user = prompts.parse_persona_update(
            cur_name=cur_name, cur_gender=cur_gender, cur_tags=cur_tags,
            cur_backstory=cur_backstory, text=text)
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
        user = prompts.parse_npc(name, text, world=world, npc_names=npc_names)
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
    def _norm_quest_steps(steps, npc_names: list[str], life_names: list[str],
                          fac_names: list[str] | None = None) -> list[dict]:
        """规范化任务步骤:类型限 act/npc/life/social/work/item/facility,
        npc/life/facility 目标校验名单。"""
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
            elif t == "facility":
                fac = str(s.get("facility", "")).strip()[:12]
                if not fac:
                    continue
                out.append({"type": "facility", "desc": desc or f"去{fac}看看", "facility": fac, "done": False})
        # npc/life 目标尽量贴合名单(双向包含容错)
        known = list(npc_names) + list(life_names)
        for s in out:
            if s["type"] in ("npc", "life") and known:
                if not any(s["npc"] in nm or nm in s["npc"] for nm in known):
                    near = next((nm for nm in known if s["npc"][0] == nm[0]), None)
                    if near:
                        s["npc"] = near
        # facility 目标尽量贴合设施清单(双向包含容错)
        facs = [f for f in (fac_names or []) if f]
        for s in out:
            if s["type"] == "facility" and facs:
                if not any(s["facility"] in f or f in s["facility"] for f in facs):
                    near = next((f for f in facs if s["facility"][0] == f[0]), None)
                    if near:
                        s["facility"] = near
        return out

    async def gen_quests(self, *, world, char, npc_names: list[str] | None = None,
                         life_names: list[str] | None = None,
                         facilities: list[dict] | None = None,
                         memories: list[str] | None = None,
                         material: str = "") -> BrainResult:
        """生成 3 个由设施/委托人驱动的任务:每个任务有委托人、发布设施与
        1~3 个可验证步骤(主动行动/找NPC/找生活角色/群友互动/兼职/取得物品/去设施)。"""
        sys = self.style
        user = prompts.gen_quests(
            world=world, char=char, npc_names=npc_names, life_names=life_names,
            facilities=facilities, memories=memories)
        user = self._with_material(user, material)
        d = await self._ask_json(sys, user)
        if d and d.get("quests"):
            facs = (facilities or [])[:10]
            quests = []
            for q in d["quests"][:3]:
                if not (isinstance(q, dict) and str(q.get("text", "")).strip()):
                    continue
                steps = self._norm_quest_steps(
                    q.get("steps"), npc_names or [], life_names or [],
                    fac_names=[f.get("name", "") for f in facs])
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
                           material: str = "", rep_note: str = "",
                           avatars: list[str] | None = None) -> BrainResult:
        """向委托人交付任务:交付场景 + 委托人的反应 + 很小的奖励(数值克制)。
        出场人物锁死:只允许角色与委托人(组织则由当值代理人出面),严禁他人乱入。"""
        giver = (giver or "委托人").strip()
        place = (place or "").strip()
        sys = self.style
        user = prompts.finish_quest(
            world=world, char=char, quest=quest, giver=giver, place=place,
            steps_desc=steps_desc, memories=memories, rep_note=rep_note)
        user = self._with_material(user, material)
        user = self._with_avatars(user, avatars)
        d = await self._ask_fixed_dialogues(sys, user, counterpart=giver, limit=3)
        if d and d.get("narration"):
            eff_in = d.get("effects") if isinstance(d.get("effects"), dict) else {}
            eff = {}
            for k, lo, hi in (("exp", 0, 15), ("gold", 0, 25), ("mood", 0, 5), ("reputation", 0, 8)):
                try:
                    eff[k] = max(lo, min(hi, int(round(float(eff_in.get(k) or 0)))))
                except (TypeError, ValueError):
                    pass
            gains, _loses = self._norm_items(d)
            return BrainResult(True, {"narration": str(d["narration"])[:300],
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
            user2 = user + "\n\n【重要纠正】" + prompts.dialogue_correction(mono, counterpart)
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
                        material: str = "", avatars: list[str] | None = None) -> BrainResult:
        """告白场景。outcome: success(答应) | crush(婉拒留悬念) | reject(明确拒绝)。"""
        sys = self.style
        user = prompts.confess(world=world, a=a, b=b, score=score, outcome=outcome)
        user = self._with_material(user, material)
        user = self._with_avatars(user, avatars)
        d = await self._ask_fixed_dialogues(sys, user, counterpart=b.name, limit=DLG_LIMITS["major"])
        if d and d.get("narration"):
            return BrainResult(True, {"narration": str(d["narration"])[:NARR_CAPS["major"]],
                                      "dialogues": self._norm_dialogues(d.get("dialogues"), DLG_LIMITS["major"])})
        return BrainResult(False, {"narration": "话到了嘴边,终究是说出口了。结果如何,彼此心里都清楚。",
                                   "dialogues": [{"speaker": a.name, "text": "那个……我喜欢你。"},
                                                 {"speaker": b.name, "text": "……让我想想。"}]})

    async def propose(self, *, world, a, b, score: int,
                        material: str = "", avatars: list[str] | None = None) -> BrainResult:
        """求婚/缔结伴侣场景(重要剧情:300~600字/6~10轮;条件已在游戏层校验,必定成功)。"""
        sys = self.style
        user = prompts.propose(world=world, a=a, b=b, score=score)
        user = self._with_material(user, material)
        user = self._with_avatars(user, avatars)
        d = await self._ask_fixed_dialogues(sys, user, counterpart=b.name, limit=DLG_LIMITS["major"])
        if d and d.get("narration"):
            return BrainResult(True, {"narration": str(d["narration"])[:NARR_CAPS["major"]],
                                      "dialogues": self._norm_dialogues(d.get("dialogues"), DLG_LIMITS["major"])})
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
    "narration": "你把这件事收了尾:该散的都散了,远处钟声又敲了一遍。你掸了掸衣角,把今天这段记进心里——事情有了了结,日子照旧往前过。",
    "dialogues": [
        {"speaker": "路人", "text": "(压低声音)喂,刚才那一幕你也看见了吧?"},
        {"speaker": "老人", "text": "别看啦,这地方奇怪的事,还多着呢。"},
    ],
    "effects": {"exp": 6, "mood": 0},
    "memory": "经历了一场无名的街头遭遇。",
}

FB_BOND = {
    "agree": True,
    "narration": "提案荒唐又好笑,B愣了两秒,噗嗤笑出声,摆摆手应了下来——这关系认就认了,日子反正要热闹着过,多一个名头不多。B还说好了以后就按这个称呼叫你。",    "dialogues": [],
    "effects": {"mood": 4},
    "memory": "",
}

FB_INTERACT = {
    "narration": "你们比划了几句,气氛没有更僵,反而各自笑了。风从巷口掠过,谁都没再提刚才那茬,约好下回再聊——这一趟没白来。",
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
    "reply": "……嗯?稀客。进来坐吧,镇上的事我多少知道些,你想打听哪桩?",
    "dialogues": [
        {"speaker": "NPC", "text": "……嗯?稀客。进来坐吧,有什么想问的?"},
        {"speaker": "自己", "text": "听说你知道不少这镇子的事。"},
        {"speaker": "NPC", "text": "知道一些。你问,我说——能说的我都告诉你。"},
    ],
    "narration": "对方招呼你进屋坐下,给你倒了杯热茶,等你开口问。",
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

FB_EXP_OFFER = {
    "title": "远征令",
    "briefing": "征召勇毅之士,向险地进发:清剿威胁、带回见闻与战利品。行程有险,量力而行。",
    "teaser": "酬金与战利品丰厚",
}

FB_EXP_INVITE = {
    "agree": True,
    "narration": "对方听完成行安排,掂量了一下风险,点头应下——正好也想出去走走。",
    "dialogues": [],
    "rel_delta": 1,
}

FB_EXP_REPORT = {
    "narration": "队伍在艰难中推进:绕开哨卫,补齐饮水,轮流守夜。前方仍未到头,但每个人都在往前走。",
    "dialogues": [],
}

FB_EXP_SETTLE = {
    "narration": "远征落幕。无论成败,这一程都刻进了每个人的骨头里——幸存者带着故事与伤疤回到了熟悉的地方。",
    "dialogues": [],
    "memory": "",
}

FB_QUEST_DONE = {
    "narration": "你把这件小事认真做完了。热汤见了底,连风都变得温柔。日子就是这样一件件小事攒起来的。",
    "dialogues": [
        {"speaker": "摊主", "text": "(递来热汤)慢慢喝,别急。"},
        {"speaker": "自己", "text": "(捧着碗)嗯,今天也麻烦你了。"},
    ],
    "effects": {"exp": 8, "mood": 2},
}
