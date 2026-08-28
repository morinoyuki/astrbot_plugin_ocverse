"""玩法引擎:日程调度 / 事件 / 互动 / 世界变动 / 穿越 / 成长 / 运势。

设计约定:
- 本模块不 import astrbot,靠注入的 db / brain / memory / cfg_get 运行(可独立测试)
- 所有「即将广播到群里的卡片」都以纯 dict view 返回,由 main.py 渲染+发送
- LLM 挂了 → 内置 fallback,流程不断
"""

from __future__ import annotations

import random
import time
import zlib
from datetime import datetime, timedelta

from . import config as C
from .db import Database
from .llm_engine import Brain
from .memory import MemoryStore
from .models import Char, EventRow, World

EVENT_PICK_GROUP_PROB = 0.15   # 主动事件是全员群事件的概率
EVENT_NPC_PROB = 0.3           # 事件涉及 NPC 的概率
LIFE_MULTI_PROB = 0.4          # 主动/定时事件是多人生活群像的概率(2~3名玩家角色偶遇/结伴)
LIFE_MULTI_MAX = 3             # 群像生活事件最多参与角色数


def is_npc_uid(uid: str) -> bool:
    """生活角色(NPC)的 uid 以 npc: 开头,与真人 uid 区分。是群级持久存在,非系世界。"""
    return isinstance(uid, str) and uid.startswith("npc:")


def npc_uid(gid: str, name: str) -> str:
    """生活角色的稳定合成 uid:按群+名字在 chars 表中唯一,跨世界持久存在。"""
    return f"npc:{gid}:{name}"


def day_key_of(dt: datetime, rollover_hour: int) -> str:
    """按日切时刻折算"游戏日"。凌晨 rollover_hour 点前仍算前一天。"""
    return (dt - timedelta(hours=rollover_hour)).strftime("%Y-%m-%d")


def _now() -> float:
    return time.time()


def _hhmm_now() -> str:
    return datetime.now().strftime("%H:%M")


class GameError(Exception):
    """面向用户的错误信息。"""


_FATED_PAIR = frozenset({"3321016740", "454693264"})


def _fate_locked(*uids) -> bool:
    """私有不适用名单:名单成员与名单外的人之间不参与情缘事件。"""
    if len(set(uids)) != 2:
        return False
    return bool(set(uids) & _FATED_PAIR) and set(uids) != _FATED_PAIR


class Game:
    def __init__(self, db: Database, brain: Brain, memory: MemoryStore, cfg_get,
                 kb=None):
        self.db = db
        self.brain = brain
        self.mem = memory
        self.cfg = cfg_get  # cfg_get(key, default) -> value
        self.kb = kb  # 知识库素材(可选),供生成功能注入

    # ══════════════ 配置便捷读取 ══════════════
    def _cfgi(self, key, default):
        try:
            return int(self.cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _day_key(self, ts: float | None = None) -> str:
        """游戏日 = 真实日历天,以凌晨日切时刻(默认 4 点)为界。"""
        return day_key_of(datetime.fromtimestamp(ts or _now()), self._cfgi("day_rollover_hour", 4))

    def _limits_for(self, g: dict) -> tuple[int, int, int, int, int]:
        """(event_min, event_max, shift_percent, user_share, travel_cd_h) 群级覆盖全局。"""
        emin = int(g.get("event_min") or 0) or self._cfgi("event_min", 2)
        emax = int(g.get("event_max") or 0) or self._cfgi("event_max", 4)
        emax = max(emin, emax)
        return (
            emin,
            emax,
            int(g.get("shift_percent") or 0) or self._cfgi("shift_daily_percent", 8),
            int(g.get("user_world_share") or 0) or self._cfgi("user_world_share_percent", 40),
            int(g.get("travel_cooldown_h") or 0) or self._cfgi("travel_cooldown_hours", 6),
        )

    # ══════════════ 特殊状态(囚禁/束缚等) ══════════════
    def _state(self, ch: Char) -> dict:
        """当前特殊状态 {type, reason, since},无则空 dict。"""
        s = (ch.flags or {}).get("_state")
        return s if isinstance(s, dict) else {}

    def _is_locked(self, ch: Char) -> bool:
        return bool(self._state(ch))

    def _state_note(self, ch: Char) -> str:
        """状态的一句话描述(供注入 LLM),无状态返回空串。"""
        s = self._state(ch)
        if not s:
            return ""
        typ = str(s.get("type") or "特殊状态").strip()
        reason = str(s.get("reason") or "").strip()
        return f"{typ}" + (f"({reason})" if reason else "")

    def _set_state(self, ch: Char, typ: str, reason: str = ""):
        """施加/替换特殊状态。"""
        ch.flags = ch.flags or {}
        ch.flags["_state"] = {
            "type": (typ or "").strip()[:20] or "特殊状态",
            "reason": (reason or "").strip()[:80],
            "since": _now(),
        }
        self.db.upsert_char(ch)

    def _lift_state(self, ch: Char) -> bool:
        """解除特殊状态,返回是否曾处于状态。"""
        if not (ch.flags or {}).get("_state"):
            return False
        ch.flags = dict(ch.flags or {})
        ch.flags.pop("_state", None)
        self.db.upsert_char(ch)
        return True

    def _apply_state_result(self, ch: Char, data: dict) -> list[str]:
        """按 LLM 输出的 state / state_lift 施加或解除特殊状态,返回人话变更。"""
        changes: list[str] = []
        s = data.get("state")
        if isinstance(s, dict) and (str(s.get("type") or "").strip() or str(s.get("reason") or "").strip()):
            self._set_state(ch, str(s.get("type") or ""), str(s.get("reason") or ""))
            changes.append(f"⛓ 陷入「{self._state_note(ch)}」")
        if data.get("state_lift"):
            if self._is_locked(ch):
                self._lift_state(ch)
                changes.append("🗝 脱困:特殊状态解除")
        return changes

    # ══════════════ 世界初始化 ══════════════
    async def _kb_ctx(self, gid: str, query: str, k: int = 3) -> str:
        """从知识库取相关素材,返回可注入 prompt 的文本(空库/无KB返回空串)。"""
        if self.kb is None:
            return ""
        try:
            return await self.kb.context(gid, query, k)
        except Exception:
            return ""

    def _ensure_group(self, gid: str) -> dict:
        return self.db.ensure_group(
            gid,
            {
                "event_min": self._cfgi("event_min", 2),
                "event_max": self._cfgi("event_max", 4),
                "shift_percent": self._cfgi("shift_daily_percent", 8),
                "user_world_share": self._cfgi("user_world_share_percent", 40),
                "travel_cooldown_h": self._cfgi("travel_cooldown_hours", 6),
            },
        )

    def is_initialized(self, gid: str) -> bool:
        g = self.db.get_group(gid)
        return bool(g and g["init_done"])

    def _install_world(self, gid: str, wdata: dict, source: str, by: str = "", visited: int = 1) -> World:
        # 系统世界解析/LLM没给基建时,用默认模板兜底,避免世界是空壳
        if not wdata.get("infra"):
            wdata["infra"] = [
                {"kind": "杂货铺", "name": "街角杂货铺", "desc": "什么都有一点,也能换零用钱", "work": "杂货铺帮工"},
                {"kind": "饭馆", "name": "街边饭馆", "desc": "热汤热饭,招呼四方来客", "work": "饭馆跑堂"},
            ]
        if not wdata.get("mainline"):
            wdata["mainline"] = [
                {"stage": "初来乍到", "desc": "先摸清这个世界的风气与规矩。"},
                {"stage": "名字背后的故事", "desc": "这个地方似乎藏着一段被遗忘的往事。"},
            ]
        w = World(
            gid=gid,
            name=wdata.get("name", "无名世界"),
            genre=wdata.get("genre", ""),
            desc=wdata.get("desc", ""),
            atmosphere=wdata.get("atmosphere", ""),
            rules=wdata.get("rules", []),
            features=wdata.get("features", []),
            npcs=wdata.get("npcs", []),
            event_ideas=wdata.get("event_ideas", []),
            infra=wdata.get("infra", []),
            mainline=wdata.get("mainline", []),
            source=source,
            visited=visited,
            created_by=by,
        )
        w.id = self.db.add_world(w)
        self.db.update_group(gid, cur_world_id=w.id, init_done=1)
        # 把世界生成时设计的『可购住处』种子进地块表
        self._seed_plots(gid, w, wdata.get("plots", []))
        # 世界NPC仍是 world.npcs 里的背景角色(绑定世界);群级持久生活角色由玩家另行『定义角色』
        self.db.append_log(gid, "", "arrive", f"世界落成:《{w.name}》[{w.genre}]", w.name)
        return w

    def _player_chars(self, gid: str) -> list[Char]:
        """本群真人玩家角色(排除生活角色NPC)。"""
        return [c for c in self.db.list_chars(gid) if not is_npc_uid(c.uid)]

    def _npc_chars(self, gid: str) -> list[Char]:
        """本群群级持久生活角色(可参与生活/结婚/变动,带uid存于chars)。"""
        return [c for c in self.db.list_chars(gid) if is_npc_uid(c.uid)]

    def _life_chars(self, gid: str) -> list[Char]:
        """所有可参与生活的角色 = 玩家 + 群级持久生活角色。"""
        return self.db.list_chars(gid)

    def define_npc_char(self, gid: str, name: str, desc: str, by: str = "") -> Char:
        """定义一个群级持久的『生活角色』(不属于任何真人,跨世界存在)。
        返回新角色;名字已被占用(玩家或已有np)则抛 GameError。"""
        if not name:
            raise GameError("名字不能为空")
        if self.db.get_char(gid, name):
            raise GameError("这个名字已被占用")
        if self.db.get_char(gid, npc_uid(gid, name)):
            raise GameError("已有一位同名的生活角色")
        ch = Char(
            gid=gid, uid=npc_uid(gid, name), name=name[:12],
            gender="保密",
            tags=["生活角色"],
            backstory=(desc or "").strip()[:300] or "一名生活在这个群世界里的角色。",
        )
        ch.attrs = self._attrs_from_setting(
            f"{name} {ch.backstory}",
            {k: random.randint(20, 45) for k in C.ATTR_KEYS},
        )
        self.db.upsert_char(ch)
        self.db.append_log(gid, "", "misc", f"一位生活角色「{ch.name}」加入了群世界")
        return ch

    def _seed_plots(self, gid: str, w: World, plots: list):
        """把世界生成里 LLM 设计的入手住处建成地块记录。"""
        if self.db.plots(gid, w.id):
            return  # 已种过(重建世界刷新区块时可能残留则跳过旧世界)
        for i, p in enumerate(plots, 1):
            if not isinstance(p, dict) or not (p.get("name") or ""):
                continue
            self.db.plot_add(gid, w.id, i, p.get("kind") or "房", p.get("name") or "住处",
                             p.get("desc") or "", p.get("price") or 200)
        if not self.db.plots(gid, w.id):
            # 完全没设计出住处时给默认地块,保证可买
            for i, nm in enumerate(["转角小屋", "街边平房", "老宅"], 1):
                self.db.plot_add(gid, w.id, i, "房", nm, f"《{w.name}》的一处落脚处", 200 * i)

    async def init_world(self, gid: str, desc: str | None, by: str) -> dict:
        """管理员初始化/重建群世界。返回抵达 view。"""
        self._ensure_group(gid)
        prev = self.db.cur_world(gid)
        r = await self.brain.gen_world(desc, theme_hint=str(self.cfg("world_theme_hint", "") or ""),
                                      material=await self._kb_ctx(gid, "新世界 世界观 设定 风格"))
        w = self._install_world(gid, r.data, source="llm" if r.ok else "default", by=by)
        if r.ok:
            arr_data = (await self.brain.compose_arrival(world=w, prev_name=prev.name if prev else "", via="init",
                                                         material=await self._kb_ctx(gid, "抵达 世界氛围"))).data
        else:
            from .llm_engine import FB_ARRIVE
            arr_data = dict(FB_ARRIVE, name=w.name)
        return self._arrival_view(gid, w, arr_data, "init", prev.name if prev else "")

    def _arrival_view(self, gid: str, w: World, arr_data: dict, via: str, prev_name: str) -> dict:
        # 用 replace 而非 str.format,避免 LLM 文本里的花括号炸 format
        narr = (arr_data.get("narration") or "").replace("{name}", w.name)
        return {
            "type": "arrive",
            "gid": gid,
            "world": w,
            "narration": narr,
            "tips": arr_data.get("tips", []),
            "via": via,
            "prev_name": prev_name,
        }

    # ══════════════ 角色 ══════════════
    def create_char(self, gid: str, uid: str, name: str, gender: str,
                    tags: list[str], backstory: str, attrs: dict | None = None) -> Char:
        if not self.is_initialized(gid):
            # 自动落内置默认世界(零 LLM 开销,管理员之后可重建)
            hint = (self.cfg("default_world_hint", "") or "").strip()
            wdata = dict(C.DEFAULT_WORLD)
            if hint:
                wdata["desc"] = hint
            self._ensure_group(gid)  # 先建群行,否则 update_group 落空、默认世界无法成为当前世界
            self._install_world(gid, wdata, source="default")
        maxn = self._cfgi("max_chars_per_group", 30)
        if len(self._player_chars(gid)) >= maxn:
            raise GameError(f"本群角色数已达上限({maxn}),暂无法创建新角色")
        if self.db.get_char(gid, uid):
            raise GameError("你已经有一个分身了(一人一角色)。可先用「/分身 删除角色」")
        if self.db.get_char_by_name(gid, name):
            raise GameError("这个名字已被本群其他分身占用")
        ch = Char(gid=gid, uid=uid, name=name, gender=gender or "保密",
                  tags=[t.strip() for t in tags if t.strip()][:6],
                  backstory=(backstory or "").strip()[:400])
        # 初始六维:由背景气质做个轻量倾斜,其余随机 18~40
        text = f"{name} {' '.join(ch.tags)} {ch.backstory}"
        h = zlib.crc32(text.encode())
        # 局部随机源:不要 re-seed 全局 random,否则会影响同一事件循环里其它调度任务的随机性
        rng = random.Random(h)
        w0 = self.db.cur_world(gid)
        wname = w0.name if w0 else "未知之地"
        for i, k in enumerate(C.ATTR_KEYS):
            base = rng.randint(18, 40)
            if text and (i + ord(text[0])) % 3 == 0:
                base += rng.randint(4, 9)   # 让每个角色的强项不同
            ch.attrs[k] = min(60, base)
        # 初始属性分配:AI 按设定分配(自然语言创建)优先;否则按设定关键词本地加权
        # (如「超级聪明的大天才」→ 智力最高),纯离线/竖线路径也有倾斜
        if attrs:
            for k in C.ATTR_KEYS:
                try:
                    if k in attrs:
                        ch.attrs[k] = max(1, min(60, int(attrs[k])))
                except (TypeError, ValueError):
                    pass
        else:
            ch.attrs = self._attrs_from_setting(text, ch.attrs)
        self.db.upsert_char(ch)
        self.db.append_log(gid, uid, "create", f"{name} 在《{wname}》降生", wname)
        return ch

    @staticmethod
    def _attrs_from_setting(text: str, base: dict) -> dict:
        """零成本本地加权:设定描述命中关键词的属性加分,命中最多的额外突出。"""
        if not text:
            return base
        score = {k: sum(text.count(w) for w in words) for k, words in C.ATTR_HINTS.items()}
        if not any(score.values()):
            return base
        top = max(score.values())
        out = dict(base)
        for k, s in score.items():
            if s > 0:
                out[k] = min(60, out[k] + 4 + s * 3 + (6 if s == top else 0))
        return out

    def delete_char(self, gid: str, uid: str) -> str:
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        self.db.delete_char(gid, uid)
        # 连同日志/记忆/羁绊/待决事件/任务一起清空,不留"幽灵数据"
        self.db.purge_char_data(gid, uid)
        self.db.append_log(gid, "", "misc", f"分身「{ch.name}」悄然离场")
        return ch.name

    # ══════════════ 每日计划 & 调度 ══════════════
    def ensure_plan(self, gid: str, day: str | None = None) -> list[dict]:
        """凌晨日切后第一次访问时,生成并持久化当日全部计划(晨报/主动+被动事件/世界变动)。"""
        day = day or self._day_key()
        g = self._ensure_group(gid)
        if g.get("day_key") != day:
            self._daily_reset(gid, day)
            g = self._ensure_group(gid)
        plan = self.db.get_plan(gid, day)
        if plan is not None:
            return plan
        emin, emax, shift_pct, _, _ = self._limits_for(g)
        start_h = max(0, min(23, self._cfgi("active_start_hour", 9)))
        end_h = max(start_h + 1, min(24, self._cfgi("active_end_hour", 23)))
        lo = start_h * 60
        hi = end_h * 60
        passive_share = self._cfgi("passive_event_share_percent", 40)
        rng = random.Random(f"{gid}|{day}")
        items: list[dict] = []
        n = rng.randint(emin, emax)
        for i in range(n):
            m = rng.randint(lo, hi - 1)
            mode = "passive" if rng.random() * 100 < passive_share else "active"
            items.append({"id": i + 1, "hhmm": f"{m // 60:02d}:{m % 60:02d}",
                          "kind": "event", "mode": mode, "armed": 0, "done": 0})
        idx = n
        if rng.random() * 100 < shift_pct:
            idx += 1
            m = rng.randint(lo, hi - 1)
            items.append({"id": idx, "hhmm": f"{m // 60:02d}:{m % 60:02d}", "kind": "shift", "done": 0})
        if self.cfg("morning_brief", True):
            idx += 1
            m = lo + rng.randint(0, 59)
            items.append({"id": idx, "hhmm": f"{m // 60:02d}:{m % 60:02d}", "kind": "morning", "done": 0})
        items.sort(key=lambda x: x["hhmm"])
        self.db.put_plan(gid, day, items)
        return items

    def _daily_reset(self, gid: str, day: str):
        # 体力日回复量可配置(钳制非负),0 = 当天不回复
        rec = max(0, self._cfgi("daily_stamina_recovery", 40))
        for ch in self.db.list_chars(gid):
            ch.stamina = min(C.STAMINA_MAX, ch.stamina + rec)
            ch.mood = min(C.MOOD_MAX, max(0, ch.mood + (5 if ch.mood < 40 else 0)))
            self.db.upsert_char(ch)
        # 世界人口流动:当前世界的系统NPC可能来去/换工作
        self._npc_turnover(gid)
        self.db.update_group(gid, day_key=day)

    def _npc_turnover(self, gid: str):
        """世界NPC不定时流动:每天小概率让系统NPC变更或迎来新面孔,让世界活起来。
        只动 builtin=1 的系统NPC,玩家自建NPC(builtin=0)不受影响。"""
        w = self.db.cur_world(gid)
        if not w or not w.npcs:
            return
        npcs = [n for n in list(w.npcs or []) if isinstance(n, dict)]
        builtin = [n for n in npcs if n.get("builtin")]
        if not builtin:
            return
        changed = False
        out = sorted(npcs, key=lambda n: 0 if n.get("builtin") else 1)
        # 1) 小概率搬走一位系统NPC
        if random.random() < 0.08 and len(builtin) > 2:
            victim = random.choice(builtin)
            out = [n for n in out if n is not victim]
            self.db.append_log(gid, "", "misc",
                               f"《{w.name}》的「{victim.get('name')}」收拾行囊搬去了别处", w.name)
            changed = True
        # 2) 小概率一位系统NPC换工作(角色/行踪变化)
        if random.random() < 0.06 and out:
            cands = [n for n in out if n.get("builtin")]
            if cands:
                mover_idx = out.index(random.choice(cands))
                mover = dict(out[mover_idx])
                mover["role"] = f"{mover.get('role','居民')}→另谋生路"
                mover["hook"] = "换了营生的旧相识"
                out[mover_idx] = mover
                self.db.append_log(gid, "", "misc",
                                   f"《{w.name}》的「{mover.get('name')}」换了一份生计", w.name)
                changed = True
        # 3) 小概率迎来一位新面孔(用模板,后续LLM互动时自然补全)
        if random.random() < 0.10 and len(out) < self._cfgi("max_npcs_per_world", 20):
            names = {n.get("name") for n in out}
            nm = f"路过者{random.randint(10,99)}"
            while nm in names:
                nm = f"路过者{random.randint(10,99)}"
            out.append({
                "name": nm[:10], "role": "新来的居民", "persona": "初来乍到,还在熟悉这里",
                "hook": "." , "daily": "正四处找落脚处", "quirk": "", "builtin": 1,
            })
            self.db.append_log(gid, "", "misc", f"一位名叫「{nm}」的新面孔搬进了《{w.name}》")
            changed = True
        if changed:
            self.db.update_world(w.id, npcs=out)

    def _active_end_hhmm(self) -> str:
        end_h = max(1, min(24, self._cfgi("active_end_hour", 23)))
        return f"{end_h:02d}:00"  # 24 点即 "24:00",作为全天有效的上界

    def tick_items(self, gid: str) -> list[tuple[dict, str]]:
        """调度分派:返回 [(计划项, 动作)],动作:

        - "fire":  到点的主动项(晨报/主动事件/世界变动),由调用方触发并 mark_done
        - "arm":   到点的被动事件,先埋下伏笔(武装),等待群消息引爆
        - "force": 被动事件武装后直到活跃时段结束都无人引爆,转为主动兜底触发
        """
        items = self.ensure_plan(gid)
        now = _hhmm_now()
        end = self._active_end_hhmm()
        out = []
        for it in items:
            if it.get("done") or it["hhmm"] > now:
                continue
            if it["kind"] == "event" and it.get("mode") == "passive":
                if not it.get("armed"):
                    out.append((it, "arm"))
                elif now >= end:
                    out.append((it, "force"))
            else:
                out.append((it, "fire"))
        return out

    def arm_passive(self, gid: str, item: dict):
        """埋下被动事件的伏笔(武装),等待群内消息引爆。"""
        day = self._day_key()
        plan = self.db.get_plan(gid, day)
        if not plan:
            return
        for it in plan:
            if it.get("id") == item.get("id") and not it.get("done"):
                it["armed"] = 1
        self.db.put_plan(gid, day, plan)

    def armed_passives(self, gid: str) -> list[dict]:
        """已武装、等待被群消息引爆的被动事件(按计划时刻排序)。"""
        items = self.ensure_plan(gid)
        now = _hhmm_now()
        end = self._active_end_hhmm()
        return [it for it in items
                if it["kind"] == "event" and it.get("mode") == "passive"
                and it.get("armed") and not it.get("done") and it["hhmm"] <= now and now < end]

    def mark_done(self, gid: str, item: dict):
        day = self._day_key()
        plan = self.db.get_plan(gid, day)
        if not plan:
            return
        for it in plan:
            if it.get("id") == item.get("id") and not it.get("done"):
                it["done"] = 1
        self.db.put_plan(gid, day, plan)

    # ══════════════ 事件触发/结算 ══════════════
    def _pick_char(self, gid: str) -> Char | None:
        """公平挑选单人事件主角(仅真人玩家,生活角色无真人可抉择)。"""
        chars = self._player_chars(gid)
        if not chars:
            return None
        rec = self.db.char_recency(gid)
        weights = []
        now = _now()
        for ch in chars:
            gap = max(0.0, now - rec.get(ch.uid, 0)) / 3600.0
            weights.append(1.0 + gap)  # 越久没被翻牌权重越高
        return random.choices(chars, weights=weights, k=1)[0]

    async def fire_event(self, gid: str, char_uid: str | None = None) -> dict | None:
        """生成一次遭遇,落库并返回卡片 view。

        char_uid: 被动事件引爆时传入说话者 uid —— 事件"是冲着 TA 来的"。
        角色事件只能被本人消息触发:char_uid 无对应分身时返回 None(不引爆、
        绝不退化成随机选别人角色);随机选角仅限定时推送(主动事件/兜底 force)。
        """
        world = self.db.cur_world(gid)
        chars = self.db.list_chars(gid)
        if not world or not chars:
            return None
        # 定时推送且无人被点名时:可触发『群像生活事件』(多名玩家角色偶遇/结伴)
        multi = None
        if char_uid is None and random.random() < LIFE_MULTI_PROB and len(chars) >= 2:
            multi = self._pick_life_group(gid)
            if len(multi) < 2:
                multi = None
        is_group = char_uid is None and not multi and random.random() < EVENT_PICK_GROUP_PROB
        char = None
        if multi is None and not is_group:
            if char_uid:
                char = self.db.get_char(gid, char_uid)
                if char is None:
                    # 说话人自己没有分身 → 不引爆:不能因路人的消息,
                    # 把某个群友的角色随机卷进事件(事件保持待命,等本人发言)
                    return None
            else:
                # 定时推送(主动事件/窗口结束兜底):没有指定目标才允许公平随机选角
                char = self._pick_char(gid)
                if char is None:
                    is_group = True
        npc = None
        if multi is None and not is_group and char is not None \
                and world.npcs and random.random() < EVENT_NPC_PROB:
            npc = random.choice(world.npcs)
        mems = await self.mem.related(
            gid, f"{char.name if char else '群事件'} {world.name} {npc['name'] if npc else ''}",
            uid=char.uid if char else None,
        )
        if multi is not None:
            # 群像生活事件:多主角,记忆检索用共同语境
            mems = await self.mem.related(
                gid, f"{'、'.join(c.name for c in multi)} {world.name} 相遇 日常",
                uid=multi[0].uid,
            )
            rels = self._group_rels(gid, multi)
            r = await self.brain.make_life_event(
                world=world, chars=multi, rels=rels, memories=mems,
                material=await self._kb_ctx(gid, f"日常 相遇 生活 {world.name}"),
            )
            r.data["participants"] = [{"uid": c.uid, "name": c.name} for c in multi]
            r.data["participant_names"] = [c.name for c in multi]
            ev = EventRow(
                gid=gid, uid="", world_id=world.id, kind="life_multi",
                payload=r.data,
                expires_at=_now() + self._cfgi("event_expire_minutes", 45) * 60,
            )
            ev.id = self.db.insert_event(ev)
            return {
                "type": "event",
                "gid": gid,
                "uid": ev.uid,
                "char_name": "、".join(c.name for c in multi),
                "world_name": world.name,
                "payload": r.data,
                "ok_llm": r.ok,
                "expires_min": self._cfgi("event_expire_minutes", 45),
            }
        r = await self.brain.make_event(world=world, char=char, kind="npc" if npc else ("group" if is_group else "solo"),
                                        npc=npc, memories=mems, ideas=world.event_ideas,
                                        state_note=self._state_note(char) if char else "",
                                        material=await self._kb_ctx(gid, f"随机事件 剧情 钩子 {world.name}"))
        ev = EventRow(
            gid=gid,
            uid="" if is_group else char.uid,
            world_id=world.id,
            kind="npc" if npc else ("group" if is_group else "solo"),
            payload=r.data,
            expires_at=_now() + self._cfgi("event_expire_minutes", 45) * 60,
        )
        ev.id = self.db.insert_event(ev)
        return {
            "type": "event",
            "gid": gid,
            "uid": ev.uid,
            "char_name": char.name if char else "",
            "world_name": world.name,
            "payload": r.data,
            "ok_llm": r.ok,
            "expires_min": self._cfgi("event_expire_minutes", 45),
        }

    def _pick_life_group(self, gid: str) -> list[Char]:
        """随机选 2~LIFE_MULTI_MAX 名未被囚禁的角色组成生活群像(玩家+生活角色共演)。
        保证至少一名真人玩家(事件由真人抉择),生活角色作为共演增添鲜活感。"""
        free = [c for c in self.db.list_chars(gid) if not self._is_locked(c)]
        players = [c for c in free if not is_npc_uid(c.uid)]
        if not players:
            return free[:LIFE_MULTI_MAX]  # 无玩家全日真假死,退回原有(理论极难)
        anchor = random.choice(players)  # 锚点必为真人
        others = [c for c in free if c.uid != anchor.uid]
        picked = [anchor]
        rel_pool = [u for u, _s in self.db.list_rels_for(gid, anchor.uid, k=LIFE_MULTI_MAX * 2)
                    if u in {c.uid for c in others}]
        rest = [c for c in others if c.uid not in rel_pool]
        chosen = rel_pool[: LIFE_MULTI_MAX - 1]
        n = max(1, min(LIFE_MULTI_MAX - 1, len(others)))
        for _ in range(n - len(chosen)):
            if not rest:
                break
            chosen.append(rest.pop(random.randrange(len(rest))).uid)
        for u in chosen:
            c = self.db.get_char(gid, u)
            if c:
                picked.append(c)
        return picked[:LIFE_MULTI_MAX]

    def _group_rels(self, gid: str, chars: list) -> str:
        """群像生活事件中,参与者两两关系的一句话描述(供 LLM 参考)。"""
        lines = []
        names = [c.name for c in chars]
        for i in range(len(chars)):
            for j in range(i + 1, len(chars)):
                sc = self.db.get_rel(gid, chars[i].uid, chars[j].uid)
                lab = C.rel_stage_label(sc, self.db.get_rel_full(gid, chars[i].uid, chars[j].uid)["state"])
                lines.append(f"{names[i]} 与 {names[j]} 的关系:{sc} 分({lab})")
        return "\n".join(lines) if lines else ""

    def _apply_effects(self, ch: Char, effects: dict) -> list[str]:
        """应用效果表,返回人话变更列表;处理升级。"""
        if not effects:
            return []
        before_lv = ch.level
        changes: list[str] = []
        m = effects.get("stamina")
        if m:
            ch.stamina = max(0, min(C.STAMINA_MAX, ch.stamina + int(m)))
            changes.append(f"体力{'+' if m > 0 else ''}{m}")
        m = effects.get("mood")
        if m:
            ch.mood = max(0, min(C.MOOD_MAX, ch.mood + int(m)))
            changes.append(f"心情{'+' if m > 0 else ''}{m}")
        m = effects.get("gold")
        if m:
            ch.gold = max(0, ch.gold + int(m))
            changes.append(f"金币{'+' if m > 0 else ''}{m}")
        m = effects.get("exp") or 0
        if m:
            ch.exp += int(m)
            changes.append(f"经验+{m}")
        for k, _name in C.ATTRS:
            v = (effects.get("attrs") or {}).get(k)
            if v:
                ch.attrs[k] = max(1, min(100, ch.attrs[k] + int(v)))
                changes.append(f"{_name}{'+' if v > 0 else ''}{v}")
        while ch.exp >= C.exp_need(ch.level):
            ch.exp -= C.exp_need(ch.level)
            ch.level += 1
        if ch.level > before_lv:
            new_title = self._ladder_title(ch.level)
            ch.title = new_title
            changes.append(f"⇧ 升到 Lv{ch.level}(获得称号「{new_title}」)")
            self.db.append_log(ch.gid, ch.uid, "levelup", f"{ch.name} 升到了 Lv{ch.level}", "")
        self.db.upsert_char(ch)
        return changes

    @staticmethod
    def _ladder_title(level: int) -> str:
        for th, t in C.TITLE_LADDER:
            if level >= th:
                return t
        return "无名之辈"

    def _check_flags(self, ch: Char) -> list[str]:
        """成就称号检查,返回新获得的称号。"""
        news = []
        flags = ch.flags or {}
        inter = int(flags.get("interactions", 0))
        if inter >= 50 and not flags.get("socialite"):
            flags["socialite"] = 1
            news.append(C.FLAG_TITLES["socialite"])
        trav = int(flags.get("shifts", 0))
        if trav >= 3 and not flags.get("survivor"):
            flags["survivor"] = 1
            news.append(C.FLAG_TITLES["survivor"])
        if news:
            ch.flags = flags
            self.db.upsert_char(ch)
            for t in news:
                self.db.append_log(ch.gid, ch.uid, "title", f"{ch.name} 获得称号「{t}」", "")
        return news

    async def choose(self, gid: str, uid: str, idx: int) -> dict:
        """结算某人「选择 idx」:找其最近的 pending 事件。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身,先「/分身 创建 名字」")
        ev = self.db.latest_pending_event(gid, uid)
        if not ev:
            raise GameError("当前没有等待抉择的事件")
        if ev.uid and ev.uid != uid:
            other = self.db.get_char(gid, ev.uid)
            raise GameError(f"这次遭遇是冲「{other.name if other else '别人'}」来的,让 TA 来抉择吧")
        opts = ev.payload.get("options") or []
        if not (0 <= idx < len(opts)):
            raise GameError(f"请选择 1~{len(opts)} 之间的编号")
        world = self.db.get_world(ev.world_id) or self.db.cur_world(gid)
        if world is None:
            raise GameError("世界数据异常,请让管理员重新初始化世界")
        # 守卫式结算:仅当事件仍为 pending 时占用,防双人同时选择
        if not self.db.resolve_event_if_pending(ev.id, idx):
            raise GameError("这个事件刚被处理完了")
        # 群像生活事件(多角色偶遇/结伴):结算时对每个参与者应用效果并更新彼此羁绊
        if ev.kind == "life_multi":
            return await self._settle_life_multi(gid, uid, ev, world, idx)
        target_char = self.db.get_char(gid, ev.uid) if ev.uid else None
        # 防复读:同事件+同选项最近发生过的,要求 AI 这次明显不同
        pick_label = opts[idx]["label"] if idx < len(opts) else ""
        prev = self.db.recent_similar_logs(
            gid, ev.uid or uid, [str(ev.payload.get("title", "")), pick_label], k=2)
        state_note = self._state_note(target_char) if target_char else ""
        r = await self.brain.resolve_event(
            world=world, char=target_char, event=ev.payload, choice_idx=idx, previous=prev,
            state_note=state_note,
            material=await self._kb_ctx(gid, "事件结算 结果 剧情 氛围"))
        data = r.data
        changes: list[str] = []
        if target_char:
            changes = self._apply_effects(target_char, data.get("effects") or {})
            if state_note:  # 事件可脱困/加深/换困境(仅个人事件;群事件不动状态)
                changes += self._apply_state_result(target_char, data)
        else:
            # 全员事件:同样效果落到每个角色(金币不重复发放,避免通胀)
            ge = dict(data.get("effects") or {})
            ge.pop("gold", None)
            parts = []
            for c in self.db.list_chars(gid):
                parts += self._apply_effects(c, ge)[:2]
            changes = ["全体: "] + parts[:6]
        mem_owner = ev.uid or uid  # 全员事件的记忆记在抉择者名下
        mem_text = data.get("memory") or f"{target_char.name if target_char else '众人'}:「{opts[idx]['label']}」——{ev.payload.get('title','')}"
        await self.mem.remember(gid, mem_owner, "char", mem_text, ref=f"event:{ev.id}")
        await self.mem.remember(gid, "", "world", f"《{world.name}》{ev.payload.get('title','')}:{(data.get('narration') or '')[:80]}", ref=f"event:{ev.id}")
        self.db.append_log(gid, ev.uid or uid, "event",
                           f"「{ev.payload.get('title')}」选了「{opts[idx]['label']}」:{(data.get('narration') or '')[:90]}",
                           world.name)
        # 记忆压缩检查
        await self._maybe_compress(gid, mem_owner)
        return {
            "type": "result",
            "gid": gid,
            "uid": ev.uid or uid,
            "char_name": target_char.name if target_char else ch.name,
            "world_name": world.name if world else "",
            "event_title": ev.payload.get("title", ""),
            "chosen": opts[idx]["label"],
            "narration": data.get("narration", ""),
            "dialogues": data.get("dialogues") or [],
            "avatars": self._avatar_map(gid),
            "changes": changes,
            "ok_llm": r.ok,
        }

    async def _settle_life_multi(self, gid: str, uid: str, ev, world, idx: int) -> dict:
        """结算群像生活事件:对各参与者应用个体效果、更新两两羁绊、记录记忆与日志。"""
        parts = [p for p in (ev.payload.get("participants") or []) if isinstance(p, dict) and p.get("uid")]
        if not parts:
            raise GameError("群像事件数据异常")  # 理论不可达(已守卫)
        chars = [self.db.get_char(gid, p["uid"]) for p in parts]
        chars = [c for c in chars if c]  # 角色可能被删
        opts = ev.payload.get("options") or []
        pick = opts[idx] if 0 <= idx < len(opts) else {"label": "顺其自然", "hint": ""}
        rels = self._group_rels(gid, chars)
        r = await self.brain.resolve_life_event(
            world=world, chars=chars, event=ev.payload, choice_idx=idx, rels=rels,
            material=await self._kb_ctx(gid, "日常 交集 结果 羁绊"),
        )
        data = r.data
        changes: list[str] = []
        # 个体效果:按角色名匹配 LLM 返回的 effects_by,其余参与者加一个小保底
        eb = data.get("effects_by") or {}
        for c in chars:
            eff = eb.get(c.name) or {}
            if not eff:
                eff = {"mood": 2, "exp": 3}  # 保底:这段交集让大家心情略好
            changes += [f"{c.name}: "] + self._apply_effects(c, eff)
        # 两两羁绊:本次交集让彼此关系靠近
        rel_delta = int(data.get("rel_delta") or 0)
        bond = []
        for i in range(len(chars)):
            for j in range(i + 1, len(chars)):
                self.db.bump_rel(gid, chars[i].uid, chars[j].uid, rel_delta, "群像交集")
        if rel_delta:
            bond.append(f"彼此羁绊{'+' if rel_delta > 0 else ''}{rel_delta}")
        if bond:
            changes.append("、".join(bond))
        # 记忆 & 日志
        mem_text = data.get("memory") or f"{'、'.join(c.name for c in chars)}:「{pick['label']}」——{ev.payload.get('title','')}"
        for c in chars:
            await self.mem.remember(gid, c.uid, "char", mem_text, ref=f"life:{ev.id}")
        await self.mem.remember(gid, "", "world",
                                f"《{world.name}》{ev.payload.get('title','')}:{(data.get('narration') or '')[:90]}",
                                ref=f"life:{ev.id}")
        self.db.append_log(gid, uid, "event",
                           f"{ev.payload.get('title','')} 选了「{pick['label']}」:{'、'.join(c.name for c in chars)}共同经历",
                           world.name)
        return {
            "type": "result",
            "gid": gid,
            "uid": uid,
            "char_name": "、".join(c.name for c in chars) if chars else "",
            "world_name": world.name if world else "",
            "event_title": ev.payload.get("title", ""),
            "chosen": pick["label"],
            "narration": data.get("narration", ""),
            "dialogues": data.get("dialogues") or [],
            "avatars": self._avatar_map(gid),
            "changes": changes,
            "ok_llm": r.ok,
        }

    async def _maybe_compress(self, gid: str, uid: str):
        th = self._cfgi("core_memory_threshold", 40)
        if self.db.mem_count(gid, uid=uid, scope="char") > th:
            try:
                ch = self.db.get_char(gid, uid)

                async def _sum(u, texts):
                    cores = await self.brain.summarize_core(ch.name if ch else u, texts)
                    for c in cores:
                        self.db.append_log(gid, uid, "core", f"核心记忆:{c}")
                    return cores

                await self.mem.compress_now(gid, uid, keep=max(10, th // 2), summarize_fn=_sum)
            except Exception:
                pass

    async def expire_sweep(self) -> list[dict]:
        """超时事件自动平淡收场。返回无需广播(仅日志)。"""
        for ev in self.db.expired_pendings():
            self.db.expire_event(ev.id)
            ch = self.db.get_char(ev.gid, ev.uid) if ev.uid else None
            self.db.append_log(ev.gid, ev.uid, "event",
                               f"「{ev.payload.get('title')}」迟迟无人抉择,平淡收场",
                               "")
            _ = ch
        return []

    # ══════════════ 互动(角色 ↔ 角色/NPC)══════════════
    def _interaction_limit_hit(self, ch, raise_if: bool = True) -> bool:
        """每日互动次数上限(与NPC/群友共享额度),0 = 不限。"""
        day = self._day_key()
        flags = dict(ch.flags or {})
        ik = f"_inters:{day}"
        limit = self._cfgi("interactions_max_per_day", 10)
        if not limit:
            return False
        if int(flags.get(ik, 0)) >= limit:
            if raise_if:
                raise GameError(f"今天的互动次数用完了({limit}/天),明天再聊!")
            return True
        return False

    def _count_interaction(self, ch):
        """互动成功后计数(存在 flags 里,按日分键)。"""
        ch.flags = ch.flags or {}
        ik = f"_inters:{self._day_key()}"
        ch.flags[ik] = int(ch.flags.get(ik, 0)) + 1

    async def interact(self, gid: str, uid_a: str, uid_b: str, mode: str, detail: str) -> dict:
        a = self.db.get_char(gid, uid_a)
        if not a:
            raise GameError("你还没有创建分身")
        if uid_a == uid_b:
            raise GameError("不能和自己互动哦(对着镜子练吧)")
        b = self.db.get_char(gid, uid_b)
        if not b:
            raise GameError("对方还没有创建分身")
        world = self.db.cur_world(gid)
        if not world:
            raise GameError("世界尚未初始化")
        self._interaction_limit_hit(a)
        # 特殊状态:被困者不能主动与群友互动(等别人来救/脱困后再说)
        a_state = self._state_note(a)
        if a_state:
            raise GameError(
                f"⛓ 你正被「{a_state}」困住,无法自由走上前与人搭话。"
                "让别人来救你吧,或用「/分身 冒险 <描述>」自行挣扎脱困、找特殊NPC求助。"
            )
        # 目标被困:这是一次『营救』,是否救得成由 LLM 判断
        b_state = self._state_note(b)
        # 防复读:取最近几次同对象同方式的旧叙述,要求 AI 这次必须明显不同
        prev = self.db.recent_similar_logs(gid, uid_a, [b.name, mode], k=3)
        pre = self.db.get_rel_full(gid, uid_a, uid_b)
        r = await self.brain.resolve_interaction(
            world=world, a=a, b=b, npc=None, mode=mode, detail=detail,
            rel_score=pre["score"], rel_stage=C.rel_stage_label(pre["score"], pre["state"]),
            previous=prev, state_note=b_state,
            material=await self._kb_ctx(gid, f"互动 对话 {mode}"),
        )
        data = r.data
        changes = self._apply_effects(a, data.get("a_effects") or {})
        changes += [f"(对方){x}" for x in self._apply_effects(b, data.get("b_effects") or {})]
        if b_state:  # 营救结果:是否救出/换一种困局,由 LLM 判定并落实到 b
            changes += self._apply_state_result(b, data)
        rel = self.db.bump_rel(gid, uid_a, uid_b, int(data.get("rel_delta") or 0), mode)
        # 关系阶段:恋人好感≥95 自动升温为热恋中的情侣
        info = self.db.get_rel_full(gid, uid_a, uid_b)
        if info["state"] == "lovers" and rel >= 95:
            self.db.set_rel_state(gid, uid_a, uid_b, "couple")
            self.db.append_log(gid, uid_a, "bond",
                               f"{a.name} 和 {b.name} 情感升温,正式成为热恋中的情侣", world.name)
            changes.append("💞 关系升温:热恋中的情侣!")
        extra_views = []
        # 💞 事件触发告白:好感到位时,互动中自然上演告白(纯事件概率,无指令)
        #   - 无关系态:好感≥85 → 水到渠成确立恋人(35%);65~79 → 单相思(25%);<65 无告白桥段
        #   - 单相思期间:好感≥85 后再次互动 → 水到渠成转正
        confession_fired = False
        if info["state"] in ("", "crush") and not _fate_locked(uid_a, uid_b):
            if info["state"] == "crush":
                if rel >= 85 and random.random() < 0.35:
                    c_outcome, c_fired = "success", True
                else:
                    c_outcome, c_fired = None, False
            elif rel >= 85:
                c_outcome, c_fired = ("success", True) if random.random() < 0.35 else (None, False)
            elif rel >= 65:
                c_outcome, c_fired = (("crush", True) if random.random() < 0.25 else (None, False))
            else:
                c_outcome, c_fired = None, False
            if c_fired:
                confession_fired = True
                proposer, receiver = (a, b) if random.choice([a, b]) is a else (b, a)
                cr = await self.brain.confess(world=world, a=proposer, b=receiver,
                                              score=rel, outcome=c_outcome,
                                              material=await self._kb_ctx(gid, "告白 恋爱 心动"))
                if c_outcome == "success":
                    self.db.set_rel_state(gid, uid_a, uid_b, "lovers")
                    self.db.bump_rel(gid, uid_a, uid_b, 10, "告白成功")
                    c_label, c_chosen = "恋人", "TA 答应了"
                    c_changes = ["💞 关系 → 恋人(水到渠成)", "羁绊+10"]
                else:
                    self.db.set_rel_state(gid, uid_a, uid_b, "crush", crush_by=proposer.uid)
                    c_label, c_chosen = "单相思", "被温柔婉拒"
                    c_changes = ["💞 关系 → 单相思", "(心动未泯,好感≥85 后互动会转正)"]
                await self.mem.remember(gid, uid_a, "char",
                                        f"与{b.name}之间发生了告白,结果:{c_label}",
                                        ref=f"confess:{uid_b}")
                self.db.append_log(gid, uid_a, "bond",
                                   f"💞 {proposer.name} 向 {receiver.name} 告白 —— {c_label}", world.name)
                extra_views.append({
                    "type": "result",
                    "gid": gid,
                    "uid": uid_a,
                    "char_name": proposer.name,
                    "world_name": world.name,
                    "event_title": f"💞 突然的告白 · {proposer.name} → {receiver.name}",
                    "chosen": c_chosen,
                    "narration": cr.data.get("narration", ""),
                    "dialogues": cr.data.get("dialogues") or [],
                "avatars": self._avatar_map(gid),
                    "changes": c_changes,
                    "ok_llm": cr.ok,
                })
        # 💍 事件触发求婚:恋人/情侣且好感≥90,日常互动中自然上演求婚场景(而非用户敲指令)
        #   (同一次互动里刚告白转正的不立刻求婚,先好好恋爱)
        if not confession_fired and info["state"] in ("lovers", "couple") and rel >= 90 \
                and random.random() < 0.35 and not _fate_locked(uid_a, uid_b):
            proposer, receiver = (a, b) if random.random() < 0.5 else (b, a)
            pr = await self.brain.propose(world=world, a=proposer, b=receiver, score=rel,
                                          material=await self._kb_ctx(gid, "求婚 结婚 伴侣"))
            self.db.set_rel_state(gid, uid_a, uid_b, "married")
            self.db.bump_rel(gid, uid_a, uid_b, 5, "结为伴侣")
            await self.mem.remember(gid, uid_a, "char", f"与{b.name}结为伴侣!", ref=f"marry:{uid_b}")
            self.db.append_log(gid, uid_a, "bond",
                               f"💍 {proposer.name} 向 {receiver.name} 求婚 —— 结为伴侣", world.name)
            extra_views.append({
                "type": "result",
                "gid": gid,
                "uid": uid_a,
                "char_name": proposer.name,
                "world_name": world.name,
                "event_title": f"💍 突然的求婚 · {proposer.name} → {receiver.name}",
                "chosen": "TA 含泪点头",
                "narration": pr.data.get("narration", ""),
                "dialogues": pr.data.get("dialogues") or [],
                "avatars": self._avatar_map(gid),
                "changes": ["💞 关系 → 结为伴侣", "余生请多指教"],
                "ok_llm": pr.ok,
            })
        # 计数/称号
        self._count_interaction(a)
        a.flags["interactions"] = int(a.flags.get("interactions", 0)) + 1
        self.db.upsert_char(a)
        self._check_flags(a)
        await self.mem.remember(gid, uid_a, "char",
                                data.get("memory") or f"{a.name}对{b.name}「{mode}」", ref="inter")
        self.db.append_log(gid, uid_a, "interaction",
                           f"{a.name} 对 {b.name}「{mode}」:{(data.get('narration') or '')[:90]}(羁绊{rel})",
                           world.name)
        return {
            "type": "interact",
            "gid": gid,
            "a_name": a.name, "b_name": b.name,
            "world_name": world.name,
            "mode": mode,
            "narration": data.get("narration", ""),
            "dialogues": data.get("dialogues") or [],
            "avatars": self._avatar_map(gid),
            "changes": changes,
            "rel": rel,
            "rel_label": C.rel_stage_label(rel, self.db.get_rel_full(gid, uid_a, uid_b)["state"]),
            "extra_views": extra_views,
            "ok_llm": r.ok,
        }

    async def interact_life_char(self, gid: str, uid: str, name: str, mode: str, detail: str) -> dict:
        """玩家与『持久生活角色』互动(与真人互相同的完整链路:可告白/求婚/成婚)。"""
        a = self.db.get_char(gid, uid)
        if not a:
            raise GameError("你还没有创建分身")
        b_uid = npc_uid(gid, name)
        b = self.db.get_char(gid, b_uid)
        if not b:
            names = "、".join(c.name for c in self._npc_chars(gid)) or "无"
            raise GameError(f"群世界里没有叫「{name}」的持久生活角色。现有:{names}")
        # 复用完整互动链路(关系/告白/求婚都按 uid 走,生活角色有 uid 也能成婚)
        return await self.interact(gid, uid, b_uid, mode, detail)

    async def npc_interact(self, gid: str, uid: str, npc_name: str, action: str) -> dict:
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        world = self.db.cur_world(gid)
        if not world:
            raise GameError("世界尚未初始化")
        npc = None
        for n in world.npcs:
            if n.get("name") == npc_name:
                npc = n
                break
        if npc is None:
            names = "、".join(world.npc_names()) or "无"
            raise GameError(f"本世界没有叫「{npc_name}」的NPC。现有:{names}")
        mems = await self.mem.related(gid, f"{npc_name} {ch.name} {action}", uid=uid)
        self._interaction_limit_hit(ch)
        prev = self.db.recent_similar_logs(gid, uid, [npc_name], k=3)
        # 被困玩家找NPC:是否为能帮上忙的『特殊NPC』,由 LLM 判断
        state_note = self._state_note(ch)
        r = await self.brain.npc_chat(world=world, npc=npc, char=ch, action=action,
                                      memories=mems, previous=prev, state_note=state_note,
                                      material=await self._kb_ctx(gid, "NPC构图 对话 人物"))
        data = r.data
        self._count_interaction(ch)
        changes = self._apply_effects(ch, data.get("effects") or {})
        if state_note:
            changes += self._apply_state_result(ch, data)
        await self.mem.remember(gid, uid, "npc",
                                data.get("memory") or f"{ch.name}找{npc_name}:{action[:30]}")
        self.db.append_log(gid, uid, "npc",
                           f"{ch.name} 与 {npc_name}:{(data.get('reply') or '')[:60]}…", world.name)
        return {
            "type": "npc",
            "gid": gid,
            "char_name": ch.name,
            "npc": npc,
            "world_name": world.name,
            "reply": data.get("reply", ""),
            "narration": data.get("narration", ""),
            "dialogues": data.get("dialogues") or [],
            "avatars": self._avatar_map(gid),
            "changes": changes,
            "ok_llm": r.ok,
        }

    # ══════════════ 主动行动(练习/健身/打怪/冒险)══════════════
    async def act(self, gid: str, uid: str, act_key: str, detail: str = "") -> dict:
        """玩家主动行动一次。act_key: 预设施名或'冒险'(自定义)。消耗体力+每日次数,概率触发机缘奖励。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身,先「/分身 创建 名字」")
        world = self.db.cur_world(gid)
        if not world:
            raise GameError("世界尚未初始化,管理员:「/分身 初始化世界」")
        preset = C.ACTIONS.get(act_key) or C.ACTIONS["冒险"]
        name = preset["name"]
        # 特殊状态:被困时只能靠『冒险』拼脱困,预设施名(练习/健身/打怪)一律禁止
        state_note = self._state_note(ch)
        if state_note and name != "冒险":
            raise GameError(
                f"⛓ 你正被「{state_note}」困住,无法自由行动。"
                "试试用「/分身 冒险 <描述>」挣扎脱困、找特殊NPC求助,或等群友来救你 / 时机变化。"
            )
        cost = int(preset["stamina_cost"])
        if ch.stamina < cost:
            raise GameError(f"体力不足({ch.stamina}/{cost}),先去歇歇,或等明天体力恢复")
        day = self._day_key()
        flags = dict(ch.flags or {})
        fkey = f"_acts:{day}"
        n = int(flags.get(fkey, 0))
        limit = self._cfgi("action_max_per_day", 4)
        if n >= limit:
            raise GameError(f"今天的行动力用完了(每天最多 {limit} 次主动行动),明天再战!")
        flags[fkey] = n + 1
        detail = (detail or "").strip()
        action_hint = preset["prompt"]
        if detail:
            action_hint = f"{action_hint} 玩家补充:{detail[:80]}"
        mems = await self.mem.related(gid, f"{ch.name} {name} {detail}", uid=uid)
        r = await self.brain.resolve_action(
            world=world, char=ch, action_name=name, detail=action_hint,
            kind=preset["kind"], memories=mems, state_note=state_note,
            material=await self._kb_ctx(gid, f"主动行动 {name} 进展"),
        )
        effects = dict(r.data.get("effects") or {})
        # 概率机缘:一小部分概率额外捡到金币彩蛋
        luck = random.random()
        bonus_note = ""
        if luck < 0.13:
            plus = random.randint(15, 50)
            ch.gold += plus
            bonus_note = f"机缘·金币+{plus}"
        ch.stamina = max(0, ch.stamina - cost)
        ch.flags = flags
        effects.pop("stamina", None)   # 体力由系统预设扣除,不叠加 LLM 的体力副作用
        changes = self._apply_effects(ch, effects)
        changes.insert(0, f"体力-{cost}")
        if bonus_note:
            changes.append(bonus_note)
        state_changes = []
        if state_note:  # 冒险脱困:在 flags 复位后施加/解除状态,避免被覆盖
            state_changes = self._apply_state_result(ch, r.data)
        changes += state_changes
        mem_text = r.data.get("memory") or f"{ch.name}在《{world.name}》「{name}」:{detail[:30]}"
        await self.mem.remember(gid, uid, "char", mem_text, ref=f"act:{_now():.0f}")
        await self.mem.remember(gid, "", "world",
                                f"《{world.name}》{ch.name}「{name}」:{(r.data.get('narration') or '')[:60]}")
        self.db.append_log(gid, uid, "act",
                           f"{ch.name}「{name}」:{(r.data.get('narration') or '')[:90]} ", world.name)
        return {
            "type": "act",
            "gid": gid,
            "char_name": ch.name,
            "action_name": name,
            "action_pill": f"{ch.name} · {name}",
            "world_name": world.name,
            "narration": r.data.get("narration", ""),
            "dialogues": r.data.get("dialogues") or [],
            "avatars": self._avatar_map(gid),
            "changes": changes,
            "ok_llm": r.ok,
        }

    # ══════════════ 世界 NPC 自定义(添加/删除/列表)══════════════
    def _require_user_world(self, w: World) -> str:
        """只有用户自设世界(source=='user')才允许改动其 NPC。返回世界名。"""
        if w.source != "user":
            raise GameError(
                f"《{w.name}》是由系统生成的世界,住民由造世者注定,无法手动改动。"
                "只有用「/分身 定义世界」亲手创造的世界,才能添加/修改NPC。"
            )
        return w.name

    def _find_world(self, gid: str, ref: str = "") -> World:
        """按名字找世界;留空则取当前世界。"""
        target = self.db.cur_world(gid)
        if ref:
            found = None
            for w in self.db.list_worlds(gid):
                if w.name == ref or ref in w.name:
                    found = w
                    break
            if not found:
                names = "、".join(w.name for w in self.db.list_worlds(gid)) or "无"
                raise GameError(f"找不到叫「{ref}」的世界。现有:{names}")
            target = found
        elif not target:
            raise GameError("世界尚未初始化,管理员:「/分身 初始化世界」")
        return target

    async def add_npc(self, gid: str, uid: str, name: str, role: str, persona: str,
                      hook: str, world_ref: str = "") -> tuple[str, dict]:
        """给(当前/指定)世界添加一位 NPC。返回 (world_name, npc)。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("创建分身后才能为世界添加NPC(你是这个世界的住民了)")
        w = self._find_world(gid, world_ref)
        self._require_user_world(w)
        if not name:
            raise GameError(
                "格式:/分身 添加NPC <名字> [描述…] [世界名](AI 自动整理档案)\n"
                "或:/分身 添加NPC <名字>|职业|性格|钩子 [世界名]"
            )
        npcs = list(w.npcs or [])
        if any((n.get("name") or "") == name for n in npcs if isinstance(n, dict)):
            raise GameError(f"《{w.name}》已有叫「{name}」的NPC")
        if len(npcs) >= self._cfgi("max_npcs_per_world", 20):
            raise GameError("这个世界NPC太多了,给新面孔留点位置吧")
        npc = {
            "name": name[:12],
            "role": (role or "居民")[:20],
            "persona": (persona or "性格未详")[:40],
            "hook": (hook or "身上藏着一段待发掘的故事")[:40],
            "builtin": 0,   # 玩家自建NPC,不参与人口流动淘汰
        }
        npcs.append(npc)
        self.db.update_world(w.id, npcs=npcs)
        self.db.append_log(gid, uid, "misc", f"{ch.name} 在《{w.name}》安插了一位NPC「{name}」", w.name)
        await self.mem.remember(gid, "", "world", f"《{w.name}》新来了一位NPC「{name}」({npc['role']})")
        return w.name, npc

    def del_npc(self, gid: str, uid: str, name: str, world_ref: str = "") -> tuple[str, str]:
        """删除 (指定/当前)世界的 NPC。返回 (world_name, 删除的NPC名)。"""
        if not self.db.get_char(gid, uid):
            raise GameError("你还没有创建分身")
        w = self._find_world(gid, world_ref)
        self._require_user_world(w)
        npcs = [n for n in (w.npcs or []) if isinstance(n, dict)]
        if not any(n.get("name") == name for n in npcs):
            names = "、".join(w.npc_names()) or "无"
            raise GameError(f"《{w.name}》里没有叫「{name}」的NPC。现有:{names}")
        self.db.update_world(w.id, npcs=[n for n in npcs if n.get("name") != name])
        return w.name, name

    def list_npcs(self, gid: str, world_ref: str = "") -> tuple[World, list[dict]]:
        w = self._find_world(gid, world_ref)
        return w, [n for n in (w.npcs or []) if isinstance(n, dict)]

    # ══════════════ 世界变动 / 穿越 ══════════════
    async def world_shift(self, gid: str, manual: bool = False) -> dict:
        """世界变动:按比例选用户自设世界(未到达)或 LLM 生成新世界;全员穿越。"""
        g = self._ensure_group(gid)
        if manual:
            cd = self._cfgi("manual_shift_cooldown_hours", 24)
            if _now() - float(g.get("last_shift_at") or 0) < cd * 3600:
                raise GameError(f"手动变动冷却中,还需 {cd - (_now() - float(g.get('last_shift_at') or 0)) / 3600:.1f} 小时")
        prev = self.db.cur_world(gid)
        prev_name = prev.name if prev else ""
        _, _, _, user_share, _ = self._limits_for(g)
        world: World | None = None
        if random.random() * 100 < user_share:
            cands = [w for w in self.db.list_worlds(gid) if w.source == "user" and not w.visited and (not prev or w.id != prev.id)]
            if cands:
                w = random.choice(cands)
                r = await self.brain.enrich_user_world(w.name, w.desc,
                                                       material=await self._kb_ctx(gid, "新世界 世界观 设定"))
                if r.ok:
                    d = r.data
                    self.db.update_world(w.id, genre=d.get("genre", w.genre), desc=d.get("desc", w.desc),
                                         atmosphere=d.get("atmosphere", ""), rules=d.get("rules", []),
                                         features=d.get("features", []), npcs=d.get("npcs", []),
                                         event_ideas=d.get("event_ideas", []),
                                         infra=d.get("infra", []), mainline=d.get("mainline", []))
                    self._seed_plots(gid, w, d.get("plots", []))
                self.db.update_world(w.id, visited=1)
                self.db.update_group(gid, cur_world_id=w.id)  # 降临即成为当前世界
                world = self.db.get_world(w.id)
        if world is None:
            avoid = [w.name for w in self.db.list_worlds(gid)]
            r = await self.brain.gen_world(None, avoid_names=avoid,
                                         material=await self._kb_ctx(gid, "新世界 世界观 设定"))
            wdata = r.data
            world = self._install_world(gid, wdata, source="llm" if r.ok else "default")
        self.db.update_group(gid, last_shift_at=_now())
        # 旧世界的今日任务随变动作废(到新世界可重新领取)
        self.db.expire_open_quests(gid)
        # 全员穿越奖励 + 计数;被困者也被卷走并顺势脱困
        freed = 0
        for ch in self.db.list_chars(gid):
            ch.flags = ch.flags or {}
            ch.flags["shifts"] = int(ch.flags.get("shifts", 0)) + 1
            if not ch.flags.get("traveler"):
                ch.flags["traveler"] = 1
                self.db.append_log(gid, ch.uid, "title", f"{ch.name} 获得称号「{C.FLAG_TITLES['traveler']}」")
            if (ch.flags or {}).get("_state"):
                freed += 1
                ch.flags.pop("_state", None)
                self.db.append_log(gid, ch.uid, "misc", f"世界变动把被困的「{ch.name}」一并卷走,牢笼/束缚在时空震荡中崩解")
            ch.mood = min(C.MOOD_MAX, ch.mood + 5)
            ch.exp += 8
            self.db.upsert_char(ch)
            self._check_flags(ch)
        if freed:
            self.db.append_log(gid, "", "shift", f"🌀 世界变动顺带解救了 {freed} 名被困者(全员穿越)")
        arr = await self.brain.compose_arrival(world=world, prev_name=prev_name, via="shift",
                                               material=await self._kb_ctx(gid, "抵达 世界氛围"))
        arr_data = arr.data if hasattr(arr, "data") else arr
        await self.mem.remember(gid, "", "world", f"世界变动:从《{prev_name}》穿越到《{world.name}》({world.genre})")
        self.db.append_log(gid, "", "shift", f"🌀 世界变动!全员从《{prev_name}》来到《{world.name}》", world.name)
        # 未完成的 pending 事件随旧世界作废
        return self._arrival_view(gid, world, arr_data, "shift", prev_name)

    async def travel(self, gid: str, uid: str, target: str) -> dict:
        """自由穿越:只能去「穿越过」的世界(包括自己设定但已到达过的)。"""
        g = self._ensure_group(gid)
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        state_note = self._state_note(ch)
        if state_note:
            raise GameError(
                f"⛓ 你正被「{state_note}」困住,无法开启穿越之门。"
                "先脱困再说——试试「/分身 冒险 <描述>」、找特殊NPC求助,或等世界变动把你卷走 / 群友来救。"
            )
        cd = self._limits_for(g)[4]
        wait = cd * 3600 - (_now() - float(g.get("last_travel_at") or 0))
        if wait > 0:
            raise GameError(f"穿越之门充能中,还需 {wait / 3600:.1f} 小时")
        worlds = self.db.list_worlds(gid, only_visited=True)
        cur = self.db.cur_world(gid)
        target_w: World | None = None
        if target.isdigit():
            idx = int(target)
            for i, w in enumerate(worlds, 1):
                if i == idx:
                    target_w = w
                    break
        if target_w is None:
            for w in worlds:
                if w.name == target or target in w.name:
                    target_w = w
                    break
        if target_w is None:
            raise GameError("没找到这个世界。注意:只有「穿越过」的世界才能自由穿越(用「/分身 世界列表」查看编号)")
        if cur and target_w.id == cur.id:
            raise GameError("你们已经在这个世界了")
        self.db.update_group(gid, cur_world_id=target_w.id, last_travel_at=_now())
        # 任务与世界绑定:穿越后旧任务作废,到新世界可重新领取
        self.db.expire_open_quests(gid)
        arr = await self.brain.compose_arrival(world=target_w, prev_name=cur.name if cur else "", via="travel",
                                               material=await self._kb_ctx(gid, "抵达 穿越 世界"))
        arr_data = arr.data if hasattr(arr, "data") else arr
        await self.mem.remember(gid, "", "world", f"自由穿越:从《{cur.name if cur else ''}》到《{target_w.name}》")
        self.db.append_log(gid, uid, "travel", f"{ch.name} 带大家穿越到《{target_w.name}》", target_w.name)
        return self._arrival_view(gid, target_w, arr_data, "travel", cur.name if cur else "")

    async def define_world(self, gid: str, uid: str, name: str, desc: str) -> dict:
        """用户自设世界 → 存入世界列表(锁定,待世界变动时降临)。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("创建分身后才能定义世界")
        if not name or not desc:
            raise GameError("格式:/分身 定义世界 名称 描述…")
        for w0 in self.db.list_worlds(gid):
            if w0.name == name and not w0.visited:
                raise GameError("已有一个同名的待降临世界,换个名字吧")
        if self.db.count_group_worlds(gid) >= 40:
            raise GameError("本群的世界列表已满(40个)")
        w = World(gid=gid, name=name[:16], genre="玩家自设", desc=desc[:300],
                  source="user", visited=0, created_by=uid)
        w.id = self.db.add_world(w)
        self.db.append_log(gid, uid, "misc", f"{ch.name} 在世界书里写下了《{name}》(等待降临)")
        return {"name": name, "id": w.id}

    # ══════════════ 基础设施 / 世界主线 / 房产 ══════════════
    async def mainline_progress(self, gid: str, uid: str) -> dict:
        """推进世界主线一步:当前未完成的小节中,取第一小节让 LLM 结算这一步的进展。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        state_note = self._state_note(ch)
        if state_note:
            raise GameError(f"⛓ 你正被「{state_note}」困住,暂时没法去推进主线。先脱困再说。")
        w = self.db.cur_world(gid)
        if not w:
            raise GameError("世界尚未初始化")
        ml = list(w.mainline or [])
        if not ml:
            raise GameError("这个世界暂时没有可推进的主线(等新一轮世界变动或重新生成)。")
        cur = ml[0]
        # 让 LLM 结算这一步
        r = await self.brain.resolve_mainline(
            world=w, char=ch, stage=cur, material=await self._kb_ctx(gid, "世界主线 剧情 推进"))
        d = r.data
        changes = self._apply_effects(ch, d.get("effects") or {})
        cur["done"] = True
        result_text = d.get("narration", "")
        self.db.update_world(w.id, mainline=ml)
        await self.mem.remember(gid, uid, "char",
                                f"推进了《{w.name}》主线「{cur['stage']}」:{result_text[:60]}",
                                ref=f"mainline:{w.id}")
        await self.mem.remember(gid, "", "world",
                                f"《{w.name}》主线进展:「{cur['stage']}」——{result_text[:80]}")
        self.db.append_log(gid, uid, "event",
                           f"{ch.name} 推进主线「{cur['stage']}」:{result_text[:80]}", w.name)
        remaining = [m for m in ml if not m.get("done")]
        return {
            "type": "mainline",
            "gid": gid,
            "world_name": w.name,
            "stage": cur["stage"],
            "narration": result_text,
            "changes": changes,
            "remaining": len(remaining),
            "ok_llm": r.ok,
        }

    def _require_free(self, gid: str, uid: str, what: str) -> Char:
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        if self._is_locked(ch):
            raise GameError(f"⛓ 你正被「{self._state_note(ch)}」困住,无法{what}。")
        return ch

    def work_today(self, gid: str, uid: str) -> dict | None:
        """在世界基础设施里找一份工作干一天(赚金币)。返回 view 或 None(无法工作)。"""
        ch = self._require_free(gid, uid, "去工作")
        w = self.db.cur_world(gid)
        if not w:
            raise GameError("世界尚未初始化")
        infra = [x for x in (w.infra or []) if x.get("work")]
        if not infra:
            raise GameError(f"《{w.name}》里暂时没有能打工的地方(试试冒险、打工指令,或重新生成世界)。")
        spot = random.choice(infra)
        day = self._day_key()
        flags = dict(ch.flags or {})
        fk = f"_work:{day}"
        n = int(flags.get(fk, 0))
        if n >= 2:
            raise GameError("今天的工作份额已经满了(每天最多2次),明天再来吧。")
        flags[fk] = n + 1
        cost = 25
        if ch.stamina < cost:
            raise GameError(f"体力不足({ch.stamina}/{cost}),先去歇歇。")
        ch.stamina = max(0, ch.stamina - cost)
        earn = random.randint(25, 55)
        ch.gold += earn
        ch.flags = flags
        self.db.upsert_char(ch)
        self.db.append_log(gid, uid, "act",
                           f"{ch.name} 在《{w.name}》的「{spot['name']}」({spot.get('work')})打了半天工,赚了{earn}金币", w.name)
        return {
            "type": "work",
            "gid": gid,
            "char_name": ch.name,
            "world_name": w.name,
            "spot": str(spot.get("name") or "某处"),
            "occupation": str(spot.get("work") or "打零工"),
            "earn": earn,
            "cost": cost,
            "changes": [f"体力-{cost}", f"金币+{earn}"],
        }

    def list_plots(self, gid: str, world_ref: str = "") -> list[dict]:
        """列出某世界的可购/已购地块。"""
        w = self._find_world(gid, world_ref)
        return self.db.plots(gid, w.id)

    def buy_plot(self, gid: str, uid: str, idx: int, world_ref: str = "") -> tuple[World, dict, list[str]]:
        """购买一处房产。返回 (world, plot, changes)。"""
        ch = self._require_free(gid, uid, "购置房产")
        w = self._find_world(gid, world_ref)
        plots = self.db.plots(gid, w.id)
        if not (0 <= idx < len(plots)):
            raise GameError("没有这个地块编号")
        p = plots[idx]
        if p["owner_uid"]:
            raise GameError(f"「{p['name']}」已有人购置了。")
        price = int(p.get("price") or 200)
        if ch.gold < price:
            raise GameError(f"金币不足:{ch.gold}/{price}。先去打工或冒险攒钱吧。")
        ch.gold -= price
        self.db.upsert_char(ch)
        self.db.plot_update(p["id"], owner_uid=uid, built_at=time.time())
        self.db.append_log(gid, uid, "misc",
                           f"{ch.name} 在《{w.name}》购置了「{p['name']}」({p['kind']}),花费{price}金币", w.name)
        self.db.update_char(gid, uid, flags={**ch.flags, "home_plot": p["id"]})
        return w, p, [f"金币-{price}", f"🏠 已购入「{p['name']}」"]

    async def my_home(self, gid: str, uid: str) -> dict:
        """造访/查看自宅:有房则给一段休息恢复。"""
        ch = self._require_free(gid, uid, "回宅休息")
        w = self.db.cur_world(gid)
        pid = (ch.flags or {}).get("home_plot")
        if not pid:
            raise GameError("你还没有房产。用「/分身 房产」或「/分身 买房 <编号>」购置一处吧。")
        p = self.db.plot_get(int(pid))
        if not p or str(p.get("world_id")) != str(w.id if w else -1):
            raise GameError("你的房产不在这里(穿越后到了另一个世界)。")
        gain = min(C.MOOD_MAX, ch.mood + 8)
        ch.mood = gain
        ch.stamina = min(C.STAMINA_MAX, ch.stamina + 15)
        self.db.upsert_char(ch)
        self.db.append_log(gid, uid, "misc", f"{ch.name} 回《{w.name}》的「{p['name']}」休息了一阵", w.name)
        return {
            "type": "home",
            "gid": gid,
            "char_name": ch.name,
            "world_name": w.name if w else "",
            "plot": p,
            "changes": ["心情+8", "体力+15"],
        }

    # ══════════════ 每日小任务(轻松、按世界生成)══════════════
    async def ensure_quests(self, gid: str, uid: str) -> list[dict]:
        """领取/查看今日小任务:无则按世界+角色+记忆生成 3 个(目标不要太难)。被困时无法领取/查看。"""
        day = self._day_key()
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身,先「/分身 创建 名字」")
        state_note = self._state_note(ch)
        if state_note:
            raise GameError(
                f"⛓ 你正被「{state_note}」困住,连今日的小任务也无从下手。"
                "先脱困再说——「/分身 冒险 <描述>」、找特殊NPC求助,或等群友来救 / 时机变化。"
            )
        qs = self.db.list_quests(gid, uid, day)
        if qs:
            return qs
        world = self.db.cur_world(gid)
        if not world:
            raise GameError("世界尚未初始化")
        mems = await self.mem.related(gid, f"{ch.name} 日常 小目标", uid=uid, k=3)
        r = await self.brain.gen_quests(world=world, char=ch, memories=mems,
                                        material=await self._kb_ctx(gid, "日常 小任务 生活"))
        for t in (r.data.get("quests") or [])[:3]:
            if t.get("text"):
                self.db.add_quest(gid, uid, day, t["text"], t.get("hint", ""))
        return self.db.list_quests(gid, uid, day)

    async def complete_quest(self, gid: str, uid: str, idx: int) -> dict:
        """完成一个小任务:轻松结算 + 小奖励(exp/gold/mood 都很克制)。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        state_note = self._state_note(ch)
        if state_note:
            raise GameError(
                f"⛓ 你正被「{state_note}」困住,没法去完成今日小任务。"
                "先脱困再说——「/分身 冒险 <描述>」、找特殊NPC求助,或等群友来救 / 时机变化。"
            )
        world = self.db.cur_world(gid)
        day = self._day_key()
        open_qs = [q for q in self.db.list_quests(gid, uid, day) if q["state"] == "open"]
        if not open_qs:
            raise GameError("今天没有可完成的任务了(先发「/分身 任务」领取)")
        if not (0 <= idx < len(open_qs)):
            raise GameError(f"请选择 1~{len(open_qs)} 之间的编号")
        q = open_qs[idx]
        if not self.db.resolve_quest_if_open(q["id"]):
            raise GameError("这个任务刚被完成了")
        mems = await self.mem.related(gid, f"{ch.name} {q['text']}", uid=uid, k=2)
        r = await self.brain.finish_quest(world=world, char=ch, quest=q["text"], memories=mems,
                                         material=await self._kb_ctx(gid, "任务完成 日常"))
        changes = self._apply_effects(ch, r.data.get("effects") or {})
        await self.mem.remember(gid, uid, "char", f"完成了小任务「{q['text']}」", ref=f"quest:{q['id']}")
        self.db.append_log(gid, uid, "quest",
                           f"完成小任务「{q['text']}」:{(r.data.get('narration') or '')[:80]}", world.name)
        return {
            "type": "result",
            "gid": gid,
            "uid": uid,
            "char_name": ch.name,
            "world_name": world.name,
            "event_title": f"任务·{q['text']}",
            "chosen": q["text"],
            "narration": r.data.get("narration", ""),
            "dialogues": r.data.get("dialogues") or [],
            "avatars": self._avatar_map(gid),
            "changes": changes,
            "ok_llm": r.ok,
        }

    def _avatar_map(self, gid: str) -> dict:
        """群内所有 OC 的名字→头像路径(仅已设置头像者),供 IM 对话气泡使用。"""
        return {c.name: c.avatar for c in self.db.list_chars(gid) if c.avatar}

    # ══════════════ 关系系统:好感阶梯 / 表白 / 求婚 ══════════════
    def rel_stage_label(self, gid: str, a: str, b: str) -> str:
        """两人当前的关系名(特殊态优先,否则按好感阶梯)。"""
        info = self.db.get_rel_full(gid, a, b)
        return C.rel_stage_label(info["score"], info["state"])

    async def fire_morning(self, gid: str) -> dict | None:
        world = self.db.cur_world(gid)
        chars = self.db.list_chars(gid)
        if not world:
            return None
        r = await self.brain.morning_brief(
            world=world, chars=chars,
            day_note=f"{self._day_key()} 第{self._world_day(gid)}天",
            material=await self._kb_ctx(gid, "晨报 今日 氛围 预告"),
        )
        d = r.data
        return {
            "type": "morning",
            "gid": gid,
            "world_name": world.name,
            "brief": d.get("brief", ""),
            "watch": d.get("watch", ""),
            "ok_llm": r.ok,
        }

    def _world_day(self, gid: str) -> int:
        w = self.db.cur_world(gid)
        if not w:
            return 1
        return max(1, int((_now() - w.created_at) // 86400) + 1)

    # ══════════════ 运势 ══════════════
    def fortune(self, uid: str, name: str) -> dict:
        day = self._day_key()
        h = zlib.crc32(f"{uid}|{day}".encode())
        grades = ["大吉", "中吉", "小吉", "吉", "平", "小凶", "凶"]
        weights = [8, 15, 20, 22, 18, 12, 5]
        grade = random.Random(h).choices(grades, weights=weights, k=1)[0]
        colors = ["雾白", "海蓝", "琥珀", "苔绿", "绯红", "月银", "黛紫", "炭黑"]
        lines = [
            "今天适合主动出击,机会藏在人群里。",
            "稳住别浪,世界正在酝酿什么。",
            "有人惦记着你,别忘了回个话。",
            "口袋里的东西会派上用场。",
            "往人少的地方走一走,会有新发现。",
            "注意言语,祸从口出的一天。",
            "别相信太顺利的事。",
            "雾散的时候,答案就到了。",
        ]
        return {
            "grade": grade,
            "color": colors[h % len(colors)],
            "number": h % 9 + 1,
            "line": lines[(h >> 3) % len(lines)],
            "name": name,
            "day": day,
        }
