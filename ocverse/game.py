"""玩法引擎:日程调度 / 事件 / 互动 / 世界变动 / 穿越 / 成长 / 运势。

设计约定:
- 本模块不 import astrbot,靠注入的 db / brain / memory / cfg_get 运行(可独立测试)
- 所有「即将广播到群里的卡片」都以纯 dict view 返回,由 main.py 渲染+发送
- LLM 挂了 → 内置 fallback,流程不断
"""

from __future__ import annotations

import random
import re
import time
import zlib
import json
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


def _fire_remember(mem: MemoryStore, gid: str, uid: str, scope: str, text: str, ref: str = ""):
    """同步路径记世界记忆:直接用同步嵌入写入,避免为异步任务埋雷(仍写日志备份)。"""
    try:
        mem.remember_sync(gid, uid, scope, text, ref=ref)
    except Exception:
        pass


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
    KO_TYPE = "重伤昏迷"   # 生命值归零的强制状态(无法行动,日切时被救醒)

    def _state(self, ch: Char) -> dict:
        """当前特殊状态 {type, reason, since},无则空 dict。"""
        s = (ch.flags or {}).get("_state")
        return s if isinstance(s, dict) else {}

    def _is_locked(self, ch: Char) -> bool:
        return bool(self._state(ch)) or self._is_ko(ch)

    def _is_ko(self, ch: Char) -> bool:
        """生命值归零 = 重伤昏迷,无法行动(与特殊状态同等工作)。"""
        return int(getattr(ch, "hp", C.HP_MAX) or 0) <= 0

    # ── 兼职时段制(上工 → 到点自动下班结算) ────────────────
    WORK_SHIFT_H = 2.0  # 一个班次的现实时长(小时),同班同事随机

    def _work(self, ch: Char) -> dict | None:
        """读取当前兼职班次(未开始返回 None)。"""
        f = (ch.flags or {}).get("_work")
        if not isinstance(f, dict) or not f.get("until"):
            return None
        return f

    def _work_note(self, ch: Char) -> str:
        """兼职中的提示语(未在班次返回空串)。"""
        f = self._work(ch)
        if not f:
            return ""
        left = max(0, int((float(f["until"]) - _now()) / 60)) + 1
        job = str(f.get("job") or "打零工")
        spot = str(f.get("infra") or "某处")
        return f"在「{spot}」当{job}(约{left}分钟后下班)"

    def _state_note(self, ch: Char) -> str:
        """状态的一句话描述(供注入 LLM),无状态返回空串。"""
        if self._is_ko(ch) and not self._state(ch):
            return self.KO_TYPE
        s = self._state(ch)
        if not s:
            return ""
        typ = str(s.get("type") or "特殊状态").strip()
        reason = str(s.get("reason") or "").strip()
        return f"{typ}" + (f"({reason})" if reason else "")

    # ══════════════ 生命值(HP)══════════════
    def _apply_hp(self, ch: Char, delta: int) -> str:
        """增减生命值并处理归零昏迷/苏醒。返回人话变更(可能为空)。"""
        hp = int(getattr(ch, "hp", C.HP_MAX) or 0)
        if hp <= 0 and delta <= 0:
            return ""   # 已昏迷,不再叠加伤害
        ch.hp = max(0, min(C.HP_MAX, hp + int(delta)))
        if ch.hp <= 0:
            self._set_state(ch, self.KO_TYPE, "伤势过重,不省人事")
            self.db.upsert_char(ch)
            return f"💔 生命归零:{self.KO_TYPE}!(被送到安全处救治,明天在世界设施/医院/家中苏醒)"
        # 恢复到正数:解除昏迷
        st = self._state(ch)
        if st.get("type") == self.KO_TYPE:
            self._lift_state(ch)
            self.db.upsert_char(ch)
            return f"❤ 生命+{delta}(苏醒了)"
        self.db.upsert_char(ch)
        return f"生命{'+' if delta > 0 else ''}{delta}"

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
                if int(getattr(ch, "hp", C.HP_MAX)) <= 0:
                    ch.hp = C.HP_WAKEUP
                    changes.append(f"❤ 被从鬼门关拉了回来(生命 {ch.hp}/{C.HP_MAX})")
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
            zones=wdata.get("zones", []),
            heal_items=wdata.get("heal_items", []),
            source=source,
            visited=visited,
            created_by=by,
        )
        # 生存必要设施与打工位保底(不足则按世界观风格补齐模板设施)
        w.infra, _notes = self._ensure_infra_baseline(w, w.infra)
        # 危险区域与治疗物品保底(LLM 没生成或旧数据缺失时按题材风格兜底)
        w.zones = self._ensure_zones_baseline(w, w.zones)
        w.heal_items = self._ensure_heal_items_baseline(w, w.heal_items)
        # 主线兜底:LLM 没给时用世界自己的 NPC/区域拼三幕(不再是千篇一律的占位文本)
        w.mainline = self._ensure_mainline_baseline(w, w.mainline)
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
        再次定义同名生活角色 = 重设其背景设定/性格标签,但保留等级、财产、羁绊、记忆与关系状态。
        名字与真人玩家分身重名则拒绝。"""
        if not name:
            raise GameError("名字不能为空")
        if self.db.get_char(gid, name):
            raise GameError("这个名字已被占用")
        existing = self.db.get_char(gid, npc_uid(gid, name))
        if existing is not None:
            # 已有同名生活角色 → 覆盖设定,保留经历
            desc = (desc or "").strip() or "一名生活在这个群世界里的角色。"
            existing.backstory = desc[:4000]
            existing.tags = ["生活角色"]
            existing.gender = "保密"
            self.db.upsert_char(existing)
            self.db.append_log(gid, "", "misc", f"生活角色「{existing.name}」的设定被重新描述了")
            return existing
        ch = Char(
            gid=gid, uid=npc_uid(gid, name), name=name[:12],
            gender="保密",
            tags=["生活角色"],
            backstory=(desc or "").strip()[:4000] or "一名生活在这个群世界里的角色。",
        )
        ch.attrs = self._attrs_from_setting(
            f"{name} {ch.backstory}",
            {k: random.randint(20, 45) for k in C.ATTR_KEYS},
        )
        self.db.upsert_char(ch)
        self.db.append_log(gid, "", "misc", f"一位生活角色「{ch.name}」加入了群世界")
        return ch

    _PLOT_TIERS = [
        ("转角小屋", 700), ("街边平房", 1400), ("老宅", 2400),
        ("临街铺面", 3600), ("花园洋房", 5200), ("湖畔庄园", 8200),
    ]

    def _seed_plots(self, gid: str, w: World, plots: list):
        """把世界生成里 LLM 设计的入手住处建成地块记录(价格弱拍进分档区间)。"""
        if self.db.plots(gid, w.id):
            return  # 已种过(重建世界刷新区块时可能残留则跳过旧世界)
        tiers = [p for p in self._PLOT_TIERS if p[1] >= 1000]
        k = 0
        for i, p in enumerate(plots, 1):
            if not isinstance(p, dict) or not (p.get("name") or ""):
                continue
            price = int(p.get("price") or 0) or 0
            if price < 600:
                # 太便宜就提升到分档档位,保证房产是攒钱目标
                _, price = tiers[k % len(tiers)]; k += 1
            self.db.plot_add(gid, w.id, i, p.get("kind") or "房", p.get("name") or "住处",
                             p.get("desc") or "", price)
        if not self.db.plots(gid, w.id):
            # 完全没设计出住处时给默认地块,保证可买
            for i, (nm, pr) in enumerate(self._PLOT_TIERS, 1):
                self.db.plot_add(gid, w.id, i, "房", nm, f"《{w.name}》的一处落脚处", pr)

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
                  backstory=(backstory or "").strip()[:4000])
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
        w = self.db.cur_world(gid)
        for ch in self.db.list_chars(gid):
            ch.stamina = min(C.STAMINA_MAX, ch.stamina + rec)
            ch.mood = min(C.MOOD_MAX, max(0, ch.mood + (5 if ch.mood < 40 else 0)))
            self._daily_hp_reset(gid, ch, w)
            self.db.upsert_char(ch)
        # 世界人口流动:当前世界的系统NPC可能来去/换工作
        self._npc_turnover(gid)
        # 世界设施流转:小概率新开/建成/倒闭(贴合同世界题材,保证生存基线)
        self._infra_turnover(gid)
        # 危险区域每日不定时变动(与讨伐任务/素材联动)
        self._zones_turnover(gid)
        # 旧世界数据兜底:zones/heal_items 缺失时按题材补齐
        self.ensure_world_content(gid)
        self.db.update_group(gid, day_key=day)

    def _daily_hp_reset(self, gid: str, ch: Char, w: World | None):
        """日切的生命处理:昏迷者被救醒;其余人自然恢复少量生命。
        昏迷者苏醒地点优先级:自己家 > 医院(医疗设施) > 据点/避难所类设施(泛指安全处)。"""
        hp = int(getattr(ch, "hp", C.HP_MAX) or 0)
        st = self._state(ch)
        is_ko = hp <= 0 or st.get("type") == self.KO_TYPE
        if is_ko:
            # 苏醒地点
            place = "收容所的临时床铺"
            pid = (ch.flags or {}).get("home_plot")
            if w is not None and pid:
                p = self.db.plot_get(int(pid))
                if p and str(p.get("world_id")) == str(w.id):
                    place = f"自己的住处「{p.get('name')}」"
            if w is not None and place.startswith("收容所"):
                med = next((i for i in (w.infra or []) if C.is_medical_infra(i)), None)
                base = next((i for i in (w.infra or [])
                             if any(k in (str(i.get('kind', '')) + str(i.get('name', ''))) for k in ("据点", "基地", "营地", "避难", "驿", "大殿"))), None)
                if med:
                    place = f"「{med.get('name')}」的病床"
                elif base:
                    place = f"「{base.get('name')}」的角落"
            ch.hp = C.HP_WAKEUP
            if st:
                ch.flags = dict(ch.flags or {})
                ch.flags.pop("_state", None)
            self.db.append_log(gid, ch.uid, "misc",
                               f"{ch.name} 在{place}醒了过来——伤势仍然不轻(生命{ch.hp}/{C.HP_MAX}),记得治疗", w.name if w else "")
            _fire_remember(self.mem, gid, ch.uid, "char",
                           f"{ch.name}重伤昏迷后在{place}苏醒(生命{ch.hp})", ref=f"ko:{_now():.0f}")
            return
        # 未昏迷:自然恢复少量生命(回家睡的恢复更多,由 回家 指令单独结算)
        if hp < C.HP_MAX:
            ch.hp = min(C.HP_MAX, hp + C.HP_DAILY_RECOVER)

    # ══════════════ 世界设施:基线保底 / 每日流转 / AI 重新规划 ══════════════
    INFRA_WORK_MIN = 2      # 每个世界至少 2 处可打工的设施
    HOME_EVENT_P = 0.30     # 回宅时小概率触发家居事件剧情(每天一次)
    INFRA_OPEN_P = 0.06     # 每日「新开/建成」一家设施的概率
    INFRA_CLOSE_P = 0.04    # 每日「倒闭」一家设施的概率

    def _ensure_infra_baseline(self, world: World, infra: list) -> tuple[list[dict], list[str]]:
        """生存必要设施与打工位保底:缺补给/住宿/餐饮/医疗/据点或打工位 <2 时,
        按世界观题材风格补齐模板设施。返回(补齐后的 infra, 补充说明列表)。"""
        pack = C.infra_style_for(world.genre, world.desc)
        infra = [dict(i) for i in (infra or []) if isinstance(i, dict) and str(i.get("name", "")).strip()]
        notes: list[str] = []
        used = {str(i.get("name", "")) for i in infra}

        def add_tpl(tpl: dict, note: str):
            name, n = tpl["name"], 2
            while name in used:
                name = f"{tpl['name']}{n}"
                n += 1
            item = {**tpl, "name": name}
            infra.append(item)
            used.add(name)
            notes.append(note)

        # 1) 打工位保底(≥2)
        work_n = sum(1 for i in infra if str(i.get("work", "")).strip())
        guard = 0
        while work_n < self.INFRA_WORK_MIN and guard < 8:
            guard += 1
            tpl = next((dict(t) for t in pack[5:] if t.get("work") and t["name"] not in used), None) \
                or next((dict(t) for t in pack if t.get("work")), None)
            if tpl is None:
                break
            add_tpl(tpl, f"补齐打工设施「{tpl['name']}」({tpl.get('work', '')})")
            work_n += 1
        # 2) 生存必要类别:补给/住宿/餐饮/医疗/据点
        for idx, (cat, kws) in enumerate(C.INFRA_ESSENTIALS):
            covered = any(
                any(kw in (i.get("kind", "") + i.get("name", "")) for kw in kws)
                for i in infra
            )
            if not covered:
                tpl = pack[idx] if idx < len(pack) else pack[0]
                add_tpl(dict(tpl), f"补齐{cat}设施「{tpl['name']}」")
        return infra, notes

    def _infra_turnover(self, gid: str):
        """世界设施小概率流转:新开/建成一家,或经营不善倒闭一家。
        题材风格随世界;流转后保证生存基线(必要设施与打工位不会被破坏)。"""
        w = self.db.cur_world(gid)
        if not w:
            return
        pack = C.infra_style_for(w.genre, w.desc)
        infra = [dict(i) for i in (w.infra or []) if isinstance(i, dict) and str(i.get("name", "")).strip()]
        changed = False
        essential_kws = [kws for _cat, kws in C.INFRA_ESSENTIALS]

        def is_essential(i: dict) -> bool:
            blob = i.get("kind", "") + i.get("name", "")
            return any(kw in blob for kws in essential_kws for kw in kws)

        work_n = sum(1 for i in infra if str(i.get("work", "")).strip())
        # 倒闭:只关非必要设施,且打工位不跌破保底、总数不缩到冷清
        if random.random() < self.INFRA_CLOSE_P and len(infra) > 4:
            cands = [i for i in infra
                     if not is_essential(i)
                     and not (str(i.get("work", "")).strip() and work_n <= self.INFRA_WORK_MIN)]
            if cands:
                victim = random.choice(cands)
                if str(victim.get("work", "")).strip():
                    work_n -= 1
                infra.remove(victim)
                self.db.append_log(gid, "", "misc",
                                   f"《{w.name}》的「{victim.get('name')}」(「{victim.get('kind', '')}」)"
                                   "经营不善,倒闭了",
                                   w.name)
                _fire_remember(self.mem, gid, "", "world",
                                f"《{w.name}》的「{victim.get('name')}」经营不善倒闭了",
                                ref=f"infra:{_now():.0f}")
                changed = True
        # 新开/建成:从同风格模板池里挑一家(名字避重)
        if random.random() < self.INFRA_OPEN_P and len(infra) < C.INFRA_MAX:
            used = {i.get("name") for i in infra}
            pool = [dict(t) for t in pack if t["name"] not in used] or [dict(t) for t in pack]
            tpl = random.choice(pool)
            flavor = random.choice(["新开业", "建成完工,正式启用", "重新开张"])
            infra.append(tpl)
            self.db.append_log(gid, "", "misc",
                               f"《{w.name}》的「{tpl['name']}」({tpl.get('kind', '')}){flavor}",
                               w.name)
            _fire_remember(self.mem, gid, "", "world",
                                             f"《{w.name}》的「{tpl['name']}」{flavor}", ref=f"infra:{_now():.0f}")
            changed = True
        # 生存基线保底
        infra, notes = self._ensure_infra_baseline(w, infra)
        if changed or notes:
            self.db.update_world(w.id, infra=infra)
            for note in notes:
                self.db.append_log(gid, "", "misc", f"《{w.name}》{note}", w.name)

    def _ensure_zones_baseline(self, world: World, zones: list) -> list[dict]:
        """危险区域保底:不足 ZONES_MIN(5)片时按题材风格补齐模板区域(上限 ZONES_MAX)。"""
        zones = [dict(z) for z in (zones or []) if isinstance(z, dict) and str(z.get("name", "")).strip()]
        if len(zones) >= C.ZONES_MIN:
            return zones
        pack = C.zone_style_for(world.genre, world.desc)
        used = {z.get("name") for z in zones}
        guard = 0
        while len(zones) < C.ZONES_MIN and guard < 16:
            guard += 1
            tpl = next((dict(t) for t in pack if t.get("name") not in used), None)
            if tpl is None:
                break
            zones.append(tpl)
            used.add(tpl.get("name"))
        return zones

    def _ensure_heal_items_baseline(self, world: World, items: list) -> list[dict]:
        """治疗物品保底:缺失时按题材风格给 3 档(商店售卖/掉落/使用统一命名)。"""
        items = [dict(h) for h in (items or []) if isinstance(h, dict) and str(h.get("name", "")).strip()]
        if items:
            return items
        return [dict(h) for h in C.heal_style_for(world.genre, world.desc)]

    ZONE_NEW_P = 0.14      # 每日出现新区域概率
    ZONE_CALM_P = 0.08     # 每日一片区域平息/消退概率
    ZONE_MUTATE_P = 0.35   # 每日某区域敌人/危险度变动概率

    def _zones_turnover(self, gid: str):
        """危险区域每日不定时变动:新区域浮现/旧区域平息/敌人与危险度异动。
        与任务(讨伐/素材)和打怪场景联动;世界日志与记忆同步沉淀。"""
        w = self.db.cur_world(gid)
        if not w:
            return
        zones = [dict(z) for z in (w.zones or []) if isinstance(z, dict) and str(z.get("name", "")).strip()]
        changed = False
        pack = C.zone_style_for(w.genre, w.desc)
        # 1) 新区域浮现
        if random.random() < self.ZONE_NEW_P and len(zones) < C.ZONES_MAX:
            used = {z.get("name") for z in zones}
            tpl = next((dict(t) for t in pack if t.get("name") not in used), None)
            if tpl:
                zones.append(tpl)
                self.db.append_log(gid, "", "misc",
                                   f"《{w.name}》的「{tpl.get('name')}」({tpl.get('kind','')})出现了新的动静——一片危险区域浮现了", w.name)
                _fire_remember(self.mem, gid, "", "world",
                               f"《{w.name}》出现新危险区域「{tpl.get('name')}」", ref=f"zone:{_now():.0f}")
                changed = True
        # 2) 区域平息(至少保留 ZONES_MIN 片)
        if random.random() < self.ZONE_CALM_P and len(zones) > C.ZONES_MIN:
            victim = random.choice(zones)
            zones.remove(victim)
            self.db.append_log(gid, "", "misc",
                               f"《{w.name}》的「{victim.get('name')}」恢复了平静,不再是危险区域", w.name)
            _fire_remember(self.mem, gid, "", "world",
                           f"《{w.name}》的「{victim.get('name')}」平息了", ref=f"zone:{_now():.0f}")
            changed = True
        # 3) 敌人/危险度异动
        if zones and random.random() < self.ZONE_MUTATE_P:
            zi = random.randrange(len(zones))
            z = dict(zones[zi])
            pack_used = [t for t in pack if t.get("name") != z.get("name")]
            src_tpl = random.choice(pack_used) if pack_used else None
            if src_tpl and src_tpl.get("enemies"):
                z["enemies"] = src_tpl["enemies"]
                if src_tpl.get("loot"):
                    z["loot"] = src_tpl["loot"]
                old_d = int(z.get("danger") or 1)
                z["danger"] = max(1, min(5, old_d + random.choice((-1, 1))))
                zones[zi] = z
                en = "、".join(e.get("name", "") for e in (z.get("enemies") or []))
                self.db.append_log(gid, "", "misc",
                                   f"《{w.name}》的「{z.get('name')}」中的活动异动:出没者变成了{en or '不明'}(危险度{z.get('danger')})", w.name)
                _fire_remember(self.mem, gid, "", "world",
                               f"《{w.name}》的「{z.get('name')}」敌人异动:{en}", ref=f"zone:{_now():.0f}")
                changed = True
        if changed:
            zones = self._ensure_zones_baseline(w, zones)
            self.db.update_world(w.id, zones=zones)

    async def regen_infra(self, gid: str, world_id: int | None = None) -> tuple[str, list[dict]]:
        """管理员:让 AI 重新规划世界设施(贴合世界观),并保证生存基线。
        world_id 为空时规划当前世界。返回(给管理员的总结文本, 新设施列表)。"""
        if world_id is not None:
            w = self.db.get_world(int(world_id))
            if not w or w.gid != gid:
                raise GameError("世界不存在或不属于该群")
        else:
            w = self.db.cur_world(gid)
            if not w:
                raise GameError("世界尚未初始化")
        r = await self.brain.regen_infra(world=w,
                                         material=await self._kb_ctx(gid, "设施 规划 世界"))
        new_infra = r.data.get("infra") if r.ok else None
        base = list(new_infra) if new_infra else list(w.infra or [])
        base, notes = self._ensure_infra_baseline(w, base)
        self.db.update_world(w.id, infra=base)
        self.db.append_log(gid, "", "misc",
                           f"《{w.name}》设施重新规划完成,共 {len(base)} 处"
                           + ("" if new_infra else "(AI 不可用,保留原设施并补齐基线)"),
                           w.name)
        names = "、".join(f"{i.get('name')}({i.get('kind', '')})" for i in base)
        msg = f"《{w.name}》设施已重新规划,共 {len(base)} 处:\n{names}"
        if notes:
            msg += "\n基线补齐:" + ";".join(notes)
        if not new_infra:
            msg = "(AI 规划不可用,保留原设施并补齐生存基线)\n" + msg
        return msg, base

    async def regen_zones_heals(self, gid: str, world_id: int | None = None) -> tuple[str, list[dict], list[dict]]:
        """管理员:让 AI 按世界观重新生成危险区域与治疗物品(旧数据/生成失败自动兜底)。
        world_id 为空时重绘当前世界。返回(总结文本, 新zones, 新heal_items)。"""
        if world_id is not None:
            w = self.db.get_world(int(world_id))
            if not w or w.gid != gid:
                raise GameError("世界不存在或不属于该群")
        else:
            w = self.db.cur_world(gid)
            if not w:
                raise GameError("世界尚未初始化")
        r = await self.brain.regen_zones_heals(world=w,
                                               material=await self._kb_ctx(gid, "危险区域 治疗物品 世界观"))
        new_zones = r.data.get("zones") if r.ok else None
        new_heals = r.data.get("heal_items") if r.ok else None
        base_zones = list(new_zones) if new_zones else list(w.zones or [])
        base_heals = list(new_heals) if new_heals else list(w.heal_items or [])
        base_zones = self._ensure_zones_baseline(w, base_zones)
        base_heals = self._ensure_heal_items_baseline(w, base_heals)
        self.db.update_world(w.id, zones=base_zones, heal_items=base_heals)
        self.db.append_log(gid, "", "misc",
                           f"《{w.name}》的舆图重绘完成:{len(base_zones)} 片危险区域、"
                           f"{len(base_heals)} 档治疗物品"
                           + ("" if new_zones and new_heals else "(AI 不可用,保留原名录并补齐基线)"),
                           w.name)
        _fire_remember(self.mem, gid, "", "world",
                       f"《{w.name}》的舆图重绘:危险区域与治疗物品更新了", ref=f"zone:{_now():.0f}")
        zone_names = "、".join(f"{z.get('name')}({z.get('kind', '')},★{z.get('danger', '?')})" for z in base_zones)
        heal_names = "、".join(f"{h.get('name')}({h.get('price', '?')}金/+{h.get('heal', '?')})" for h in base_heals)
        msg = (f"《{w.name}》舆图已重绘。\n危险区域({len(base_zones)}):{zone_names}\n"
               f"治疗物品({len(base_heals)}):{heal_names}")
        if not (new_zones and new_heals):
            msg = "(AI 重绘不可用,保留原名录并补齐基线)\n" + msg
        return msg, base_zones, base_heals

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
            _fire_remember(self.mem, gid, "", "world",
                                             f"《{w.name}》的「{victim.get('name')}」搬去了别处", ref=f"npc:{_now():.0f}")
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
                _fire_remember(self.mem, gid, "", "world",
                                                 f"《{w.name}》的「{mover.get('name')}」换了营生", ref=f"npc:{_now():.0f}")
                changed = True
        # 3) 小概率迎来一位新面孔(用模板,后续LLM互动时自然补全)
        if random.random() < 0.10 and len(out) < self._cfgi("max_npcs_per_world", 32):
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
        chars = [c for c in self._player_chars(gid) if not self._on_expedition(c)]
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
        world = self.ensure_world_content(gid) or world
        world = await self.ensure_world_mainline(gid) or world
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
                "event_id": ev.id,
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
            "event_id": ev.id,
            "char_name": char.name if char else "",
            "world_name": world.name,
            "payload": r.data,
            "ok_llm": r.ok,
            "expires_min": self._cfgi("event_expire_minutes", 45),
        }

    def _pick_life_group(self, gid: str) -> list[Char]:
        """随机选 2~LIFE_MULTI_MAX 名未被囚禁的角色组成生活群像(玩家+生活角色共演)。
        保证至少一名真人玩家(事件由真人抉择),生活角色作为共演增添鲜活感。"""
        free = [c for c in self.db.list_chars(gid)
                if not self._is_locked(c) and not self._on_expedition(c)
                and not self._exp_companion_of(gid, c.uid)]
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
        """应用效果表,返回人话变更列表;处理升级/生命/声望。"""
        if not effects:
            return []
        before_lv = ch.level
        changes: list[str] = []
        m = effects.get("hp")
        if m:
            self.db.upsert_char(ch)   # 先落盘当前值,再走 HP 变更(内部含昏迷/苏醒处理)
            hp_chg = self._apply_hp(ch, int(m))
            if hp_chg:
                changes.append(hp_chg)
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
        rep = effects.get("reputation")
        if rep:
            w = self.db.cur_world(ch.gid)
            if w is not None and not is_npc_uid(ch.uid):
                new_rep = self.db.rep_add(ch.gid, ch.uid, w.id, int(rep), "事件/行动")
                changes.append(f"声誉{'+' if int(rep) > 0 else ''}{int(rep)}({C.rep_level_label(new_rep)})")
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

    @staticmethod
    def _multi_includes(ev, uid: str) -> bool:
        """群像多人事件的参与者名单里是否有 uid。"""
        parts = ev.payload.get("participants") or []
        return any(isinstance(p, dict) and p.get("uid") == uid for p in parts)

    def choose_locked(self, ev, uid: str) -> bool:
        """该事件是否对 uid 锁定:别人的个人事件 / 没带上他的多人事件(非全群)。"""
        if ev.uid:
            return ev.uid != uid
        if ev.kind == "life_multi":
            return not self._multi_includes(ev, uid)
        return False

    async def choose(self, gid: str, uid: str, idx: int, ev=None) -> dict:
        """结算某人「选择 idx」。

        ev: 要结算的事件(指令层从引用(回复)的事件卡 №标签识别后传入);
        不传则回落到最新一张「该用户可抉择」的已送达事件。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身,先「/分身 创建 名字」")
        exp = self._on_expedition(ch)
        if exp:
            raise GameError(f"⚔ 你正在「{exp.get('title')}」远征途中,无暇他顾"
                            f"(约还需 {self._exp_left_h(exp):.1f} 小时归来)。")
        if ev is None:
            for cand in self.db.pending_sent_events(gid, uid):
                if not self.choose_locked(cand, uid):
                    ev = cand
                    break
            if ev is None:
                raise GameError("当前没有等待抉择的事件")
        if ev.gid != gid:
            raise GameError("这张事件卡不属于本群")
        if ev.uid and ev.uid != uid:
            other = self.db.get_char(gid, ev.uid)
            raise GameError(f"这次遭遇是冲「{other.name if other else '别人'}」来的,让 TA 来抉择吧")
        if ev.kind == "life_multi" and not self._multi_includes(ev, uid):
            names = "、".join(str(p.get("name", "")) for p in (ev.payload.get("participants") or []))
            raise GameError(f"这场交集是「{names}」的,没带上你就不能替他们做主啦")
        # 有效期严格校验(清理循环约1分钟一次,存在短暂窗口期)
        if 0 < ev.expires_at < _now():
            self.db.expire_event(ev.id)
            raise GameError("这张事件卡已经过了有效期,悄然错过了")
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
        heal_note = self._backpack_heal_note(gid, target_char.uid, world) if target_char else ""
        r = await self.brain.resolve_event(
            world=world, char=target_char, event=ev.payload, choice_idx=idx, previous=prev,
            state_note=state_note, heal_note=heal_note,
            material=await self._kb_ctx(gid, "事件结算 结果 剧情 氛围"))
        data = r.data
        changes: list[str] = []
        if target_char:
            changes = self._apply_effects(target_char, data.get("effects") or {})
            if state_note:  # 事件可脱困/加深/换困境(仅个人事件;群事件不动状态)
                changes += self._apply_state_result(target_char, data)
            # 事件中的物品得失(拾获/受赠/被掳/消耗)
            changes += self._apply_items(gid, target_char.uid, data.get("items_gain"), data.get("items_lose"))
        else:
            # 全员事件:同样效果落到每个角色(金币不重复发放,避免通胀)
            ge = dict(data.get("effects") or {})
            ge.pop("gold", None)
            parts = []
            for c in self.db.list_chars(gid):
                if self._on_expedition(c) or self._exp_companion_of(gid, c.uid):
                    continue   # 远征者与其随队队友不在现场
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
        echo = await self.mainline_echo(gid, mem_owner, ctx=f"{ev.payload.get('title','')}:「{opts[idx]['label']}」{(data.get('narration') or '')[:60]}")
        return {
            "type": "result",
            "echo": echo,
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
        # 兼职时段:上工中无法互动
        a_wn = self._work_note(a)
        if a_wn:
            raise GameError(f"⚒ 你正{a_wn},上班摸鱼被老板盯上可就不好啦——下班再找「{b.name}」吧。")
        # 特殊状态:被困者不能主动与群友互动(等别人来救/脱困后再说)
        if self._is_ko(a):
            raise GameError(
                "💔 你已重伤昏迷,没法主动找人。等群友来救你(让 TA「/分身 与 你」互动即可),"
                "或等日切时被救醒。"
            )
        a_exp = self._on_expedition(a)
        if a_exp:
            raise GameError(f"⚔ 你正在「{a_exp.get('title')}」远征途中,联系不上你"
                            f"(约还需 {self._exp_left_h(a_exp):.1f} 小时归来)。")
        b_exp = self._on_expedition(b)
        if b_exp:
            raise GameError(f"⚔ {b.name} 正在「{b_exp.get('title')}」远征途中,联系不上TA"
                            f"(约还需 {self._exp_left_h(b_exp):.1f} 小时归来)。")
        comp = self._exp_companion_of(gid, uid_b)
        if comp:
            cexp, leader = comp
            raise GameError(f"⚔ {b.name} 正跟随「{leader.name}」的「{cexp.get('title')}」远征队在外,"
                            f"联系不上TA(约还需 {self._exp_left_h(cexp):.1f} 小时归来)。")
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
            rep_note=self._rep_note(gid, uid_a, world),
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
        # 任务进度:群友互动(social)/生活角色互动(life)
        if is_npc_uid(uid_b):
            self._quest_progress(gid, uid_a, "life", name=b.name)
        else:
            self._quest_progress(gid, uid_a, "social")
        echo = await self.mainline_echo(gid, uid_a, ctx=f"与{b.name}「{mode}」{(data.get('narration') or '')[:60]}")
        return {
            "type": "interact",
            "echo": echo,
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

    # ══════════════ 自定义关系(搞怪称谓)══════════════
    async def propose_bond(self, gid: str, uid: str, target_uid: str, label: str) -> dict:
        """自定义关系提案:A 想成为 target 的「label」(爸爸/麻麻/主人/女仆…),
        由 AI 以 target 的性格与双方关系判断是否同意。
        亲密关系(恋人/情侣/夫妻等)在代码层直接拒绝,不走 LLM。"""
        a = self.db.get_char(gid, uid)
        if not a:
            raise GameError("你还没有创建分身")
        b = self.db.get_char(gid, target_uid)
        if not b:
            raise GameError("对方还没有创建分身")
        if uid == target_uid:
            raise GameError("不能和自己确立关系哦")
        a_exp = self._on_expedition(a)
        if a_exp:
            raise GameError(f"⚔ 你正在「{a_exp.get('title')}」远征途中,没法提这事。")
        label = label.strip()[:12]
        if not label:
            raise GameError("请写上想要的称谓,如:/分身 关系 @群友 爸爸")
        if C.is_intimate_bond(label):
            raise GameError(
                "亲密关系(恋人/情侣/夫妻等)没法自定义哦,那是互动里水到渠成的事～"
                "试试爸爸/麻麻/主人/女仆/兄弟/师父这类搞怪称谓吧"
            )
        old = self.db.get_bond(gid, uid, target_uid)
        if old and old.get("status") == "agreed" and old.get("label") == label:
            raise GameError(f"你已经是「{b.name}」的{label}了,不用再提一遍～")
        world = self.db.cur_world(gid)
        if not world:
            raise GameError("世界尚未初始化")
        pre = self.db.get_rel_full(gid, uid, target_uid)
        r = await self.brain.propose_bond(
            world=world, a=a, b=b, label=label,
            rel_score=pre["score"], rel_stage=C.rel_stage_label(pre["score"], pre["state"]),
            material=await self._kb_ctx(gid, f"关系 提案 {label}"),
        )
        data = r.data
        agree = bool(data.get("agree"))
        if agree:
            self.db.set_bond(gid, uid, target_uid, label, "agreed")
        changes = []
        if agree:
            changes.append(f"🤝 关系成立:你是「{b.name}」的{label}!")
        else:
            changes.append("提案被嫌弃地拒绝了,关系如旧")
        eff = dict(data.get("effects") or {})
        changes += self._apply_effects(a, eff)
        eff_b = {k: v for k, v in eff.items() if k != "gold"}
        changes += [f"(对方){x}" for x in self._apply_effects(b, eff_b)]
        mem_text = data.get("memory") or (
            f"{a.name}向{b.name}提议当TA的{label}," + ("被接受了" if agree else "被拒绝了"))
        await self.mem.remember(gid, uid, "char", mem_text, ref=f"bond:{target_uid}")
        await self.mem.remember(gid, target_uid, "char", mem_text, ref=f"bond:{uid}")
        self.db.append_log(gid, uid, "bond",
                           f"{a.name} 向 {b.name} 提议当TA的「{label}」 —— " + ("成立" if agree else "被拒"),
                           world.name)
        return {
            "type": "result",
            "gid": gid,
            "uid": uid,
            "card_title": "🤝 自定义关系",
            "char_name": a.name,
            "world_name": world.name,
            "event_title": f"关系提案 · {a.name} → {b.name}",
            "chosen": f"「{label}」,TA 答应了!" if agree else f"「{label}」,被嫌弃地拒绝了",
            "narration": data.get("narration", ""),
            "dialogues": data.get("dialogues") or [],
            "avatars": self._avatar_map(gid),
            "changes": changes,
            "ok_llm": r.ok,
        }

    def bonds_of(self, gid: str, uid: str) -> list[dict]:
        """某人成立的全部自定义关系(供角色卡展示)。"""
        return self.db.bonds_for(gid, uid)

    async def npc_interact(self, gid: str, uid: str, npc_name: str, action: str) -> dict:
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        world = self.db.cur_world(gid)
        if not world:
            raise GameError("世界尚未初始化")
        wn = self._work_note(ch)
        if wn:
            raise GameError(f"⚒ 你正{wn},上班时间还是先把活干完吧——下班再来找「{npc_name}」。")
        if self._is_ko(ch):
            raise GameError("💔 你已重伤昏迷,没法去找 NPC。等日切时被救醒,或等群友来救。")
        exp = self._on_expedition(ch)
        if exp:
            raise GameError(f"⚔ 你正在「{exp.get('title')}」远征途中,没法去找 NPC"
                            f"(约还需 {self._exp_left_h(exp):.1f} 小时归来)。")
        march = self._exp_npc_on_march(gid, npc_name)
        if march:
            raise GameError(f"⚔ 「{npc_name}」正随「{march.get('title')}」远征队在外,"
                            f"不在世界里(约还需 {self._exp_left_h(march):.1f} 小时归来)。")
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
        self._attach_rep_line(gid, uid, world)
        r = await self.brain.npc_chat(world=world, npc=npc, char=ch, action=action,
                                      memories=mems, previous=prev, state_note=state_note,
                                      rep_note=self._rep_note(gid, uid, world),
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
        # 任务进度:世界NPC互动
        self._quest_progress(gid, uid, "npc", name=npc_name)
        echo = await self.mainline_echo(gid, uid, ctx=f"与{npc_name}:{action[:40]} {(data.get('reply') or '')[:40]}")
        return {
            "type": "npc",
            "echo": echo,
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
    # ══════════════ 远征系统(世界观内的大队远征)══════════════
    EXP_ISSUER_KEYS = ("公会", "据点", "基地", "议会", "宗门", "门派", "仙盟", "驿站", "大殿",
                       "公司", "总部", "哨站", "哨所", "补给", "营", "队", "所", "殿", "盟",
                       "阁", "府", "站", "社", "署", "部")

    def _on_expedition(self, ch: Char) -> dict | None:
        """当前远征状态(flags._exp),未在远征返回 None。"""
        f = (ch.flags or {}).get("_exp")
        return f if isinstance(f, dict) and f.get("until") else None

    def _exp_left_h(self, exp: dict) -> float:
        return max(0.0, (float(exp.get("until") or 0) - _now()) / 3600.0)

    def _exp_companion_of(self, gid: str, uid: str) -> tuple[dict, Char] | None:
        """uid(生活角色)是否正作为队友跟随某场远征:返回 (远征快照, 队长)。"""
        for c in self.db.list_chars(gid):
            exp = self._on_expedition(c)
            if exp and uid in (exp.get("life_teammates") or []):
                return exp, c
        return None

    def _exp_npc_on_march(self, gid: str, npc_name: str) -> dict | None:
        """世界 NPC 是否正随某场远征在外:返回远征快照。"""
        for c in self.db.list_chars(gid):
            exp = self._on_expedition(c)
            if exp and npc_name in (exp.get("teammates") or []):
                return exp
        return None

    def _exp_note(self, ch: Char) -> str:
        exp = self._on_expedition(ch)
        if not exp:
            return ""
        return f"「{exp.get('title', '')}」远征中(目标「{exp.get('zone', '')}」,约还需 {self._exp_left_h(exp):.1f} 小时归来)"

    def ensure_world_content(self, gid: str) -> World | None:
        """确保当前世界的危险区域/治疗物品存在(旧数据自动按题材补齐并落库)。

        v1.1 之前的世界没有 zones/heal_items 字段——在事件/打怪/远征/查看等
        入口调用本方法,保证旧世界平滑获得新内容,叙事不缺席。"""
        w = self.db.cur_world(gid)
        if not w:
            return None
        changed = False
        zones = [z for z in (w.zones or []) if isinstance(z, dict) and str(z.get("name", "")).strip()]
        if len(zones) < C.ZONES_MIN:
            w.zones = self._ensure_zones_baseline(w, zones)
            changed = True
        heals = [h for h in (w.heal_items or []) if isinstance(h, dict) and str(h.get("name", "")).strip()]
        if not heals:
            w.heal_items = self._ensure_heal_items_baseline(w, heals)
            changed = True
        if changed:
            self.db.update_world(w.id, zones=w.zones, heal_items=w.heal_items)
            self.db.append_log(gid, "", "misc",
                               f"《{w.name}》的舆图被重新勘绘:危险区域与治疗物品名录补齐了", w.name)
            _fire_remember(self.mem, gid, "", "world",
                           f"《{w.name}》补齐了危险区域与治疗物品的名录", ref=f"zone:{_now():.0f}")
        return w

    # ══════════════ 主线补全(空/缺失数据 → LLM 立即重生成)══════════════
    MAINLINE_REGEN_FAILS_PER_DAY = 3

    @staticmethod
    def _mainline_valid(ml) -> list[dict]:
        """有效主线小节:dict 且 stage 非空。"""
        return [m for m in (ml or []) if isinstance(m, dict) and str(m.get("stage") or "").strip()]

    def _ensure_mainline_baseline(self, world: World, ml) -> list[dict]:
        """主线兜底:不足 2 个有效小节时,用世界自己的 NPC/危险区域拼三幕(贴合世界观)。"""
        valid = [dict(m) for m in self._mainline_valid(ml)]
        if len(valid) >= 2:
            return valid
        npc = next((str(n.get("name")) for n in (world.npcs or [])
                    if isinstance(n, dict) and n.get("name")), "一位老住户")
        zone = next((str(z.get("name")) for z in (world.zones or [])
                     if isinstance(z, dict) and z.get("name")), "一处神秘之地")
        return valid + [
            {"stage": "风言渐起", "desc": f"关于{zone}的怪谈在街头巷尾流传,线索散落各处。"},
            {"stage": "身入漩涡", "desc": f"{npc}的委托把主线推向明处:是陷阱也是机遇。"},
            {"stage": "真相之门", "desc": "靠近事情的核心,做出属于你自己的选择。"},
        ]

    async def ensure_world_mainline(self, gid: str, force: bool = False) -> World | None:
        """主线为空/缺失数据时,立即调用 LLM 重新生成(LLM 失败回填世界观基线,不让玩法空转)。
        正常情况下原样返回,不耗 LLM;每日失败重试有上限(防风暴)。"""
        w = self.db.cur_world(gid)
        if not w:
            return None
        if not force and len(self._mainline_valid(w.mainline)) >= 2:
            return w
        day = self._day_key()
        fail_key = f"ml_fail:{day}"
        try:
            fails = int(self.db.kv_get(gid, fail_key) or 0)
        except (TypeError, ValueError):
            fails = 0
        if not force and fails >= self.MAINLINE_REGEN_FAILS_PER_DAY:
            w.mainline = self._ensure_mainline_baseline(w, w.mainline)
            self.db.update_world(w.id, mainline=w.mainline)
            return w
        r = await self.brain.regen_mainline(world=w,
                                            material=await self._kb_ctx(gid, "世界主线 剧情 推进"))
        nodes = r.data.get("mainline") if r.ok else []
        if len(nodes) >= 2:
            w.mainline = nodes
            self.db.update_world(w.id, mainline=nodes)
            self.db.kv_set(gid, fail_key, "0")
            self.db.append_log(gid, "", "misc",
                               f"《{w.name}》的主线剧情被造世者重新补全({len(nodes)} 节)", w.name)
            _fire_remember(self.mem, gid, "", "world",
                           f"《{w.name}》的主线剧情线重新确立", ref=f"mainline:{w.id}")
            return w
        if not force:
            self.db.kv_incr(gid, fail_key)
        w.mainline = self._ensure_mainline_baseline(w, w.mainline)
        self.db.update_world(w.id, mainline=w.mainline)
        return w

    async def regen_mainline(self, gid: str, world_id: int | None = None) -> tuple[str, list[dict]]:
        """管理员:让 AI 按世界观重新生成主线(force,无视失败上限)。返回(总结文本, 新小节)。"""
        if world_id is not None:
            w = self.db.get_world(int(world_id))
            if not w or w.gid != gid:
                raise GameError("世界不存在或不属于该群")
        else:
            w = self.db.cur_world(gid)
            if not w:
                raise GameError("世界尚未初始化")
        r = await self.brain.regen_mainline(world=w,
                                            material=await self._kb_ctx(gid, "世界主线 剧情 推进"))
        nodes = r.data.get("mainline") if r.ok else []
        if len(nodes) < 2:
            nodes = self._ensure_mainline_baseline(w, w.mainline)
        w.mainline = nodes
        self.db.update_world(w.id, mainline=nodes)
        self.db.append_log(gid, "", "misc",
                           f"《{w.name}》主线重铸完成({len(nodes)} 节)"
                           + ("" if r.ok else "(AI 不可用,使用基线剧情)"), w.name)
        _fire_remember(self.mem, gid, "", "world",
                       f"《{w.name}》的主线被重铸:{nodes[0].get('stage', '')}拉开帷幕", ref=f"mainline:{w.id}")
        lines = [f"《{w.name}》主线已重铸({len(nodes)} 节):"]
        for m in nodes:
            goal = f" ⚑{m.get('goal_note', '')}" if m.get("goal_type") else ""
            lines.append(f"· {m.get('stage', '')} — {m.get('desc', '')}{goal}")
        if not r.ok:
            lines.append("(AI 不可用,使用的是基线剧情)")
        return "\n".join(lines), nodes

    def _exp_issuer(self, world: World) -> str:
        """远征发布方:优先设施里带组织色彩的(公会/据点/宗门/公司…),贴合世界观;
        兑底用第一个设施,再兑底『本地卫队』。"""
        facs = [i for i in (world.infra or []) if isinstance(i, dict) and str(i.get("name", "")).strip()]
        for it in facs:
            blob = str(it.get("kind", "")) + str(it.get("name", ""))
            if any(k in blob for k in self.EXP_ISSUER_KEYS):
                return str(it.get("name"))
        if facs:
            return str(facs[0].get("name"))
        return "本地卫队"

    def _exp_teammates(self, gid: str, uid: str, world: World) -> list[dict]:
        """远征队友:优先持久生活角色(成功归来可积累羁绊),再补世界NPC。"""
        pool: list[dict] = []
        for c in self.db.list_chars(gid):
            if c.uid == uid or not is_npc_uid(c.uid):
                continue
            pool.append({"name": c.name, "kind": "life"})
        used = {p["name"] for p in pool}
        for n in (world.npcs or []):
            nm = str(n.get("name", "")).strip()
            if nm and nm not in used:
                pool.append({"name": nm, "kind": "npc"})
                used.add(nm)
        random.shuffle(pool)
        return pool[:random.randint(C.EXPEDITION_TEAM_MIN, C.EXPEDITION_TEAM_MAX)]

    def _exp_success_rate(self, ch: Char, zone: dict, teammates_n: int) -> int:
        """预估远征成功率(%):危险度重压,等级/六维/队友/生命/补给回血。"""
        danger = max(1, min(5, int(zone.get("danger") or 3)))
        rate = 0.95 - 0.14 * danger
        rate += min(0.20, 0.02 * max(0, (int(getattr(ch, "level", 1) or 1) - 1)))
        try:
            attrs_avg = sum(int(v) for v in (ch.attrs or {}).values()) / max(1, len(C.ATTR_KEYS))
        except Exception:
            attrs_avg = 30
        rate += (attrs_avg - 25) / 400.0
        rate += min(0.12, 0.04 * max(0, teammates_n))
        rate += (int(getattr(ch, "hp", C.HP_MAX)) / C.HP_MAX - 0.8) * 0.10
        return int(round(max(0.08, min(0.95, rate)) * 100))

    def _exp_offer_view(self, gid: str, uid: str, ch: Char, world: World | None, offer: dict) -> dict:
        return {
            "type": "expedition",
            "phase": "offer",
            "gid": gid, "uid": uid,
            "char_name": ch.name,
            "world_name": world.name if world else "",
            "offer": offer,
            "narration": str(offer.get("briefing") or ""),
            "changes": [
                f"⚔ 目标:{offer.get('zone_name')}(危险度★{offer.get('danger', '?')})",
                f"⏱ 行程:约 {offer.get('duration_h', '?')} 小时",
                "🛡 同行:" + ("、".join(offer.get("teammates") or []) or "待定"),
                f"🎯 预估成功率:{offer.get('rate', '?')}%(属性过低可能失败)",
                f"💰 {offer.get('teaser') or '报酬从优'}",
            ],
            "ok_llm": bool(offer.get("llm")),
        }

    async def ensure_expedition_offer(self, gid: str, uid: str) -> dict:
        """查看今日远征委托(无则生成:按世界观由设施颁布,当日有效)。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        if self._on_expedition(ch):
            raise GameError(f"⚔ 你{self._exp_note(ch)},先等远征归来。用「/分身 远征 状态」查看进度。")
        world = self.db.cur_world(gid)
        if not world:
            raise GameError("世界尚未初始化")
        day = self._day_key()
        key = f"exp_offer:{day}:{uid}"
        cached = self.db.kv_get(gid, key)
        if cached:
            try:
                return self._exp_offer_view(gid, uid, ch, world, json.loads(cached))
            except Exception:
                pass
        world = self.ensure_world_content(gid) or world
        zones = [z for z in (world.zones or []) if isinstance(z, dict) and str(z.get("name", "")).strip()]
        if not zones:
            zones = self._ensure_zones_baseline(world, world.zones)
        # 目标区域:按等级加权(远征要有挑战,但别送死)
        pref = max(1, min(5, 1 + int(getattr(ch, "level", 1) or 1) // 6))
        weights = [1.0 / (1 + abs(int(z.get("danger") or 1) - pref)) for z in zones]
        zone = random.choices(zones, weights=weights, k=1)[0]
        danger = max(1, min(5, int(zone.get("danger") or 3)))
        lo, hi = C.EXPEDITION_DURATIONS.get(danger, (6, 16))
        duration_h = random.randint(lo, hi)
        issuer = self._exp_issuer(world)
        team = self._exp_teammates(gid, uid, world)
        team_names = [t["name"] for t in team]
        rate = self._exp_success_rate(ch, zone, len(team_names))
        offer = {
            "zone_name": str(zone.get("name")), "zone_kind": str(zone.get("kind", "")),
            "danger": danger, "duration_h": duration_h,
            "issuer": issuer, "teammates": team_names, "rate": rate, "day": day, "llm": False,
        }
        r = await self.brain.expedition_offer(world=world, char=ch, zone=zone, issuer=issuer,
                                              teammates=team_names, duration_h=duration_h, rate=rate)
        if r.ok:
            offer.update({"title": r.data["title"], "briefing": r.data["briefing"],
                          "teaser": r.data.get("teaser", ""), "llm": True})
        else:
            enemies = "、".join(e.get("name", "") for e in (zone.get("enemies") or []) if isinstance(e, dict))
            offer.update({
                "title": f"远征令·{zone.get('name')}",
                "briefing": (f"{issuer}征召勇毅之士远征「{zone.get('name')}」({zone.get('kind', '')})——"
                             + (f"该地出没{enemies}," if enemies else "")
                             + f"行程约 {duration_h} 小时,风险自负,酬金与战利品从优。同行:{'、'.join(team_names) or '待定'}。"),
                "teaser": "酬金与战利品丰厚",
            })
        self.db.kv_set(gid, key, json.dumps(offer, ensure_ascii=False))
        return self._exp_offer_view(gid, uid, ch, world, offer)

    async def accept_expedition(self, gid: str, uid: str) -> dict:
        """接下今日远征委托,进入远征状态(期间无法进行其他操作)。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        if self._on_expedition(ch):
            raise GameError(f"⚔ 你{self._exp_note(ch)}。")
        if self._is_locked(ch):
            raise GameError(f"⛓ 你正被「{self._state_note(ch)}」困住,接不下远征委托。")
        if self._work_note(ch):
            raise GameError(f"⚒ 你正{self._work_note(ch)},先下班再说。")
        world = self.db.cur_world(gid)
        if not world:
            raise GameError("世界尚未初始化")
        day = self._day_key()
        cached = self.db.kv_get(gid, f"exp_offer:{day}:{uid}")
        if not cached:
            raise GameError("今天还没有属于你的远征委托。先发「/分身 远征」查看布告。")
        try:
            offer = json.loads(cached)
        except Exception:
            raise GameError("远征委托数据异常,请重新「/分身 远征」刷新")
        cur_zones = {str(z.get("name") or "") for z in (world.zones or []) if isinstance(z, dict)}
        if str(offer.get("zone_name") or "") not in cur_zones:
            # 世界变动后旧委托作废
            self.db.kv_set(gid, f"exp_offer:{day}:{uid}", "")
            raise GameError("这份远征委托随世界变动作废了,重新发「/分身 远征」领取新的吧。")
        started = _now()
        duration_h = float(offer.get("duration_h") or 6)
        report_every_h = max(1, self._cfgi("expedition_report_hours", C.EXPEDITION_REPORT_HOURS))
        flags = dict(ch.flags or {})
        life_uids = {c.name: c.uid for c in self.db.list_chars(gid) if is_npc_uid(c.uid)}
        flags["_exp"] = {
            "title": str(offer.get("title") or "远征"), "zone": str(offer.get("zone_name") or "未知之地"),
            "kind": str(offer.get("zone_kind") or ""), "danger": int(offer.get("danger") or 3),
            "duration_h": duration_h, "issuer": str(offer.get("issuer") or ""),
            "teammates": [str(t) for t in (offer.get("teammates") or [])],
            "life_teammates": [life_uids[t] for t in (offer.get("teammates") or []) if t in life_uids],
            "started": started, "until": started + duration_h * 3600,
            "report_every_h": report_every_h, "next_report": started + report_every_h * 3600,
            "reports": 0, "supplies_used": 0,
        }
        ch.flags = flags
        ch.stamina = max(0, ch.stamina - 15)
        self.db.upsert_char(ch)
        wn = world.name if world else ""
        self.db.append_log(gid, uid, "act",
                           f"{ch.name} 接下「{offer.get('title')}」远征委托,随队向「{offer.get('zone_name')}」进发"
                           f"(约 {duration_h:.0f} 小时,成功率约 {offer.get('rate', '?')}%)", wn)
        await self.mem.remember(gid, uid, "char",
                                f"接受了「{offer.get('title')}」远征,目标{offer.get('zone_name')},"
                                f"同行:{'、'.join(offer.get('teammates') or []) or '同伴'}", ref=f"exp:{started:.0f}")
        await self.mem.remember(gid, "", "world",
                                f"《{wn}》{offer.get('issuer') or '当局'}颁布远征委托,「{ch.name}」一队向「{offer.get('zone_name')}」进发",
                                ref=f"exp:{started:.0f}")
        exp_snap = dict(flags["_exp"], progress=0)
        r = await self.brain.expedition_report(world=world, char=ch, exp=exp_snap,
                                               phase="誓师出发") if world is not None else None
        if r is not None and r.ok:
            narration = str(r.data["narration"])
            dialogues = r.data.get("dialogues") or []
        else:
            narration = (f"「{offer.get('issuer') or '当局'}」的号令下,{ch.name} 与 "
                         f"{'、'.join(offer.get('teammates') or []) or '同伴们'} 整装出发,"
                         f"踏上前往「{offer.get('zone_name')}」的征途。号角/引擎/驼铃——声音很快被荒野吞没。")
            dialogues = []
        return {
            "type": "expedition",
            "phase": "depart",
            "gid": gid, "uid": uid,
            "char_name": ch.name,
            "world_name": wn,
            "title": str(offer.get("title") or "远征"),
            "narration": narration,
            "dialogues": dialogues,
            "changes": ["体力-15", f"⏱ 预计 {duration_h:.0f} 小时后归来",
                        f"🎯 成功率约 {offer.get('rate', '?')}%",
                        "⚔ 远征期间无法进行其他操作,途中每几小时播报一次"],
            "ok_llm": r.ok,
        }

    async def expedition_report(self, gid: str, uid: str) -> dict | None:
        """远征途中的一次剧情播报(由主循环按间隔调用)。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            return None
        exp = self._on_expedition(ch)
        if not exp:
            return None
        now = _now()
        if now < float(exp.get("next_report") or 0):
            return None
        world = self.db.cur_world(gid)
        started = float(exp.get("started") or now)
        until = float(exp.get("until") or now)
        span = max(1.0, until - started)
        progress = int(min(99, (now - started) / span * 100))
        phase = "行军" if progress < 35 else ("遭遇战" if progress < 70 else "险境")
        # 补给消耗:背包有治疗物品时过半概率消耗一份维持状态(减轻损耗)
        drain_hp = max(1, int(exp.get("danger") or 3))
        drain_st = 6
        used_item = ""
        heal_names = {h["name"] for h in self.heal_items_of(gid, world)}
        usable = [it["name"] for it in self.db.items_list(gid, uid) if it["name"] in heal_names]
        if usable and random.random() < 0.55:
            used_item = random.choice(usable)
            self.db.item_remove(gid, uid, used_item, 1)
            drain_hp = 0
            drain_st = 3
        supplies_note = (f"队伍刚消耗了背包里的「{used_item}」维持状态,叙述中自然带过即可,不必再输出物品变化。"
                         if used_item else "")
        hp_before = int(getattr(ch, "hp", C.HP_MAX))
        ch.hp = max(1, hp_before - drain_hp)   # 途中不轻易昏迷,失败结算才可能重伤
        ch.stamina = max(0, ch.stamina - drain_st)
        exp["reports"] = int(exp.get("reports") or 0) + 1
        if used_item:
            exp["supplies_used"] = int(exp.get("supplies_used") or 0) + 1
        exp["progress"] = progress
        exp["next_report"] = now + max(1, int(exp.get("report_every_h") or C.EXPEDITION_REPORT_HOURS)) * 3600
        ch.flags = dict(ch.flags or {})
        ch.flags["_exp"] = exp
        self.db.upsert_char(ch)
        r = await self.brain.expedition_report(world=world, char=ch, exp=exp, phase=phase,
                                               supplies_note=supplies_note) if world is not None else None
        if r is not None and r.ok:
            narration = str(r.data["narration"])
            dialogues = r.data.get("dialogues") or []
        else:
            narration = (f"远征队伍在「{exp.get('zone')}」的深处推进:绕开哨卫、补齐饮水、轮流守夜,"
                         f"{phase}的痕迹随处可见。前方仍未到头,但每个人都在往前走。")
            dialogues = []
        changes = [f"体力-{drain_st}"]
        if drain_hp:
            changes.append(f"生命-{drain_hp}")
        if used_item:
            changes.append(f"🎒 消耗「{used_item}」")
        self.db.append_log(gid, uid, "act",
                           f"【远征·{phase}】{ch.name} 的队伍深入「{exp.get('zone')}」({progress}%):"
                           f"{narration[:60]}…", world.name if world else "")
        return {
            "type": "expedition",
            "phase": "report",
            "gid": gid, "uid": uid,
            "char_name": ch.name,
            "world_name": world.name if world else "",
            "title": str(exp.get("title") or "远征"),
            "phase_name": phase,
            "progress": progress,
            "narration": narration,
            "dialogues": dialogues,
            "changes": changes,
            "ok_llm": r.ok,
        }

    async def settle_expedition(self, gid: str, uid: str) -> dict:
        """远征归来结算:成功率判定;成功=丰厚奖励+高潮叙述;失败=重伤+损失+重要剧情叙述。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        exp = self._on_expedition(ch)
        if not exp:
            raise GameError("你没有进行中的远征。")
        flags = dict(ch.flags or {})
        flags.pop("_exp", None)
        ch.flags = flags
        self.db.upsert_char(ch)   # 先摘标记防并发
        world = self.db.cur_world(gid)
        wn = world.name if world else ""
        zone_danger = max(1, min(5, int(exp.get("danger") or 3)))
        teammates = [str(t) for t in (exp.get("teammates") or [])]
        rate = self._exp_success_rate(ch, {"danger": zone_danger}, len(teammates))
        outcome = "success" if random.randint(1, 100) <= rate else "fail"
        changes: list[str] = []
        items_gain: list[str] = []
        if outcome == "success":
            dur_factor = 1.0 + min(1.5, float(exp.get("duration_h") or 6) / 48.0)
            gold = int((60 + 90 * zone_danger) * dur_factor * random.uniform(0.85, 1.3))
            exp_gain = 25 + 18 * zone_danger + int(getattr(ch, "level", 1) or 1) * 2
            rep = 4 + zone_danger
            ch.gold += gold
            zone = next((z for z in ((world.zones if world else None) or [])
                         if z.get("name") == exp.get("zone")), {}) or {}
            loot = [str(x) for x in (zone.get("loot") or []) if str(x).strip()]
            random.shuffle(loot)
            for lt in loot[:random.randint(1, 2)]:
                self.db.item_add(gid, uid, lt[:12], 1, f"远征「{exp.get('zone')}」缴获"[:20])
                items_gain.append(lt[:12])
            heals = self.heal_items_of(gid, world)
            if heals:
                hi = heals[-1]
                self.db.item_add(gid, uid, hi["name"], 1, str(hi.get("note", ""))[:20])
                items_gain.append(hi["name"])
            changes += self._apply_effects(ch, {"exp": exp_gain, "reputation": rep})
            changes.append(f"金币+{gold}")
            boost = 2 if zone_danger >= 4 else 1
            attr_names = list(C.ATTR_KEYS)
            random.shuffle(attr_names)
            for k in attr_names[:boost]:
                ch.attrs[k] = min(100, ch.attrs.get(k, 0) + boost)
                changes.append(f"{C.ATTR_NAMES.get(k, k)}+{boost}(远征淬炼)")
            for it in items_gain:
                changes.append(f"🎒 获得「{it}」")
            self.db.kv_incr(gid, "defeats_total", zone_danger)
            reward_line = (f"报酬 {gold} 金币;经验 +{exp_gain};声望 +{rep};"
                           + (f"战利品:{'、'.join(items_gain)};" if items_gain else "")
                           + (f"属性小幅提升(+{boost});" if attr_names else "")
                           + "队伍凯旋,讨伐计数累计。")
        else:
            hp_loss = random.randint(25, 45) + 3 * zone_danger
            gold_loss = random.randint(0, 40)
            if gold_loss:
                ch.gold = max(0, ch.gold - gold_loss)
            hp_chg = self._apply_hp(ch, -hp_loss)
            if hp_chg:
                changes.append(hp_chg)
            if gold_loss:
                changes.append(f"金币-{gold_loss}")
            changes += self._apply_effects(ch, {"exp": 5, "reputation": -2})
            reward_line = (f"远征失败:重伤(生命-{hp_loss})"
                           + (f",损失 {gold_loss} 金币" if gold_loss else "")
                           + ",经验+5,声望-2。")
        # 成功归来:与生活角色队友的羁绊+
        if outcome == "success":
            life_names = {c.name: c.uid for c in self.db.list_chars(gid) if is_npc_uid(c.uid)}
            for t in teammates:
                if t in life_names:
                    self.db.bump_rel(gid, uid, life_names[t], 4, "并肩远征")
                    changes.append(f"💞 与{t}羁绊+4")
        self.db.upsert_char(ch)
        r = await self.brain.expedition_settle(world=world, char=ch, exp=exp, outcome=outcome,
                                               reward_line=reward_line) if world is not None else None
        if r is not None and r.ok:
            narration = str(r.data["narration"])
            dialogues = r.data.get("dialogues") or []
        else:
            narration = ("远征落幕。队伍在最后一段路上拼尽了全力——"
                         + ("战利品与捷报一同回到了出发的地方,人人带伤,个个挺立。"
                            if outcome == "success" else
                            "他们在夜色里撤了下来,带着伤、教训和活着回来的庆幸。"))
            dialogues = []
        outcome_txt = "大获全胜" if outcome == "success" else "折戟而归"
        await self.mem.remember(gid, uid, "char",
                                f"远征「{exp.get('title')}」{outcome_txt}:{reward_line[:70]}",
                                ref=f"exp:{float(exp.get('started') or 0):.0f}")
        await self.mem.remember(gid, "", "world",
                                f"《{wn}》远征队归来:「{exp.get('title')}」{outcome_txt}({exp.get('zone')})",
                                ref=f"exp:{float(exp.get('started') or 0):.0f}")
        self.db.append_log(gid, uid, "act",
                           f"⚔ 远征「{exp.get('title')}」{outcome_txt}:{reward_line[:80]}", wn)
        return {
            "type": "expedition",
            "phase": "return",
            "gid": gid, "uid": uid,
            "char_name": ch.name,
            "world_name": wn,
            "title": str(exp.get("title") or "远征"),
            "outcome": outcome,
            "narration": narration,
            "dialogues": dialogues,
            "changes": changes,
            "ok_llm": r.ok,
        }

    def abort_expedition(self, gid: str, uid: str) -> dict:
        """中途撤离(逃兵):放弃远征,声望重挫+损失,立即恢复自由。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        exp = self._on_expedition(ch)
        if not exp:
            raise GameError("你没有进行中的远征。")
        flags = dict(ch.flags or {})
        flags.pop("_exp", None)
        ch.flags = flags
        gold_loss = 50
        ch.gold = max(0, ch.gold - gold_loss)
        changes = self._apply_effects(ch, {"reputation": -8, "exp": 2, "mood": -5})
        changes.append(f"金币-{gold_loss}(违约金)")
        hp_chg = self._apply_hp(ch, -15)
        if hp_chg:
            changes.append(hp_chg)
        self.db.upsert_char(ch)
        wn = self.db.cur_world(gid)
        self.db.append_log(gid, uid, "act",
                           f"🏳 {ch.name} 从「{exp.get('title')}」远征中途撤离(声望-8)", wn.name if wn else "")
        return {
            "type": "expedition",
            "phase": "abort",
            "gid": gid, "uid": uid,
            "char_name": ch.name,
            "world_name": wn.name if wn else "",
            "title": str(exp.get("title") or "远征"),
            "narration": (f"{ch.name} 在途中选择了撤离。队伍目送TA的背影消失在来路上——"
                          "违约的代价要自己承担,但至少,人还完整。"),
            "dialogues": [],
            "changes": changes,
            "ok_llm": False,
        }

    def expedition_status(self, gid: str, uid: str) -> str:
        """远征进度文本(供「远征 状态」)。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        exp = self._on_expedition(ch)
        if not exp:
            return "当前没有进行中的远征。发「/分身 远征」查看今日委托。"
        now = _now()
        started = float(exp.get("started") or now)
        until = float(exp.get("until") or now)
        progress = int(min(99, (now - started) / max(1.0, until - started) * 100))
        next_in = max(0, int((float(exp.get("next_report") or 0) - now) / 60))
        return (
            f"⚔ {ch.name} 的远征简报\n"
            f"· 委托:「{exp.get('title')}」(发布:{exp.get('issuer') or '?'})\n"
            f"· 目标:「{exp.get('zone')}」(危险度★{exp.get('danger', '?')})\n"
            f"· 同行:{'、'.join(exp.get('teammates') or []) or '同伴'}\n"
            f"· 行程:约 {progress}%,预计 {self._exp_left_h(exp):.1f} 小时后归来\n"
            f"· 播报:已收到 {exp.get('reports', 0)} 段,下一段约 {next_in} 分钟后\n"
            f"· 补给:途中已消耗 {exp.get('supplies_used', 0)} 份\n"
            "远征期间无法进行其他操作,耐心等捷报。"
        )

    async def _sweep_expeditions(self, gid: str) -> list[dict]:
        """主循环扫描:到点的远征播报/归来结算(一次广播)。"""
        views: list[dict] = []
        for ch in self.db.list_chars(gid):
            exp = self._on_expedition(ch)
            if not exp:
                continue
            now = _now()
            try:
                if now >= float(exp.get("until") or 0):
                    views.append(await self.settle_expedition(gid, ch.uid))
                elif now >= float(exp.get("next_report") or 0):
                    v = await self.expedition_report(gid, ch.uid)
                    if v:
                        views.append(v)
            except GameError:
                continue
            except Exception:
                # 单次播报/结算失败不阻塞:下个周期会重试(next_report/until 未推进时)
                continue
        return views

    # ══════════════ 讨伐(打怪 × 危险区域 融合)══════════════
    def _hunt_zone_from_quests(self, gid: str, uid: str, zones: list) -> dict | None:
        """今日开放委托的讨伐步骤(keywords)指向的区域/敌人——打怪未点名时自动对齐委托。"""
        day = self._day_key()
        kws: list[str] = []
        for q in self.db.list_quests(gid, uid, day):
            if q["state"] != "open":
                continue
            steps = json.loads(q.get("steps") or "[]") if isinstance(q.get("steps"), str) else (q.get("steps") or [])
            for s in steps:
                if isinstance(s, dict) and str(s.get("type") or "") == "act":
                    kws += [str(k).strip() for k in (s.get("keywords") or []) if str(k).strip()]
        for kw in kws:
            for z in zones:
                zn = str(z.get("name", ""))
                if kw and (kw in zn or zn in kw):
                    return z
                for e in (z.get("enemies") or []):
                    en = str(e.get("name", "")) if isinstance(e, dict) else ""
                    if en and (kw in en or en in kw):
                        return z
        return None

    def _hunt_zone_match(self, gid: str, uid: str, world: World, ch: Char, detail: str) -> tuple[dict | None, str]:
        """为『打怪』锁定讨伐区域(融合危险区域系统):
        1) 玩家在描述中点名了区域/敌人 → 精确锁定;
        2) 未点名但今日委托的讨伐步骤指向某区域/敌人 → 自动对齐委托(环环相扣);
        3) 都没有 → 按角色等级挑危险度适配的区域(低等级不轻易送死)。
        返回 (区域, 给 LLM 的锁定说明)。"""
        zones = [z for z in (world.zones or []) if isinstance(z, dict) and str(z.get("name", "")).strip()]
        if not zones:
            return None, ""
        text = (detail or "").strip()

        def _hit(z: dict, s: str) -> bool:
            if not s:
                return False
            zn = str(z.get("name", ""))
            if s in zn or zn in s:
                return True
            for e in (z.get("enemies") or []):
                en = str(e.get("name", "")) if isinstance(e, dict) else ""
                if en and (s in en or en in s):
                    return True
            return False

        zone = next((z for z in zones if _hit(z, text)), None)
        if zone is not None:
            how = "玩家指定"
        else:
            zone = self._hunt_zone_from_quests(gid, uid, zones)
            how = "今日委托对齐" if zone else ""
        if zone is None:
            pref = max(1, min(5, 1 + int(getattr(ch, "level", 1) or 1) // 8))
            weights = [1.0 / (1 + abs(int(z.get("danger") or 1) - pref)) for z in zones]
            zone = random.choices(zones, weights=weights, k=1)[0]
            how = "按历练挑选"
        enemy_names = "、".join(str(e.get("name", "")) for e in (zone.get("enemies") or []) if isinstance(e, dict))
        note = (
            f"地点「{zone.get('name')}」({zone.get('kind', '')},危险度{zone.get('danger', '?')},{how})"
            + (f",主要出没:{enemy_names}" if enemy_names else "")
            + f",可掉落:{'、'.join((zone.get('loot') or [])[:3]) or '无'}。"
        )
        return zone, note

    async def act(self, gid: str, uid: str, act_key: str, detail: str = "") -> dict:
        """玩家主动行动一次。act_key: 预设施名或'冒险'(自定义)。消耗体力+每日次数,概率触发机缘奖励。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身,先「/分身 创建 名字」")
        world = self.db.cur_world(gid)
        if not world:
            raise GameError("世界尚未初始化,管理员:「/分身 初始化世界」")
        world = self.ensure_world_content(gid) or world
        world = await self.ensure_world_mainline(gid) or world
        preset = C.ACTIONS.get(act_key) or C.ACTIONS["冒险"]
        name = preset["name"]
        # 兼职时段:上工中无法主动行动
        wn = self._work_note(ch)
        if wn:
            raise GameError(f"⚒ 你正{wn},下班前没法自由行动。先专心把班上完吧。")
        # 特殊状态:被困时只能靠『冒险』拼脱困,预设施名(练习/健身/打怪)一律禁止
        state_note = self._state_note(ch)
        if self._is_ko(ch):
            raise GameError(
                "💔 你已重伤昏迷(生命0),动弹不得。日切时会被送到安全处(家/医院/据点)救醒;"
                "在此之前只能等群友来救(互动),或用声望好的朋友送医。"
            )
        exp = self._on_expedition(ch)
        if exp:
            raise GameError(f"⚔ 你正在「{exp.get('title')}」远征途中,无暇分身"
                            f"(约还需 {self._exp_left_h(exp):.1f} 小时归来)。")
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
        # 打怪 × 危险区域融合:锁定讨伐地点(玩家指定 > 委托对齐 > 等级适配)
        zone = None
        zone_note = ""
        if name == "打怪":
            zone, zone_note = self._hunt_zone_match(gid, uid, world, ch, detail)
            if zone_note:
                action_hint = f"{action_hint} 讨伐目标:{zone_note}"
        mems = await self.mem.related(gid, f"{ch.name} {name} {detail}", uid=uid)
        r = await self.brain.resolve_action(
            world=world, char=ch, action_name=name, detail=action_hint,
            kind=preset["kind"], memories=mems, state_note=state_note,
            heal_note=self._backpack_heal_note(gid, uid, world),
            zone_note=zone_note,
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
        # 物品得失(冒险捡到/缴获/消耗)
        changes += self._apply_items(gid, uid, r.data.get("items_gain"), r.data.get("items_lose"))
        # 讨伐计数(主线 defeat 门槛用):打怪推演成功即计一次
        if name == "打怪" and r.ok:
            self.db.kv_incr(gid, "defeats_total", 1)
        # 任务进度:冒险/打怪 行动(叙述也参与关键词匹配:区域/敌人名在叙述中出现即算完成)
        if name in ("冒险", "打怪"):
            lead = "讨伐" if name == "打怪" else ""
            self._quest_progress(gid, uid, "act", name=name,
                                 text=f"{lead} {ch.name} {detail[:60]} {(r.data.get('narration') or '')[:80]}")
        mem_text = r.data.get("memory") or f"{ch.name}在《{world.name}》「{name}」:{detail[:30]}"
        await self.mem.remember(gid, uid, "char", mem_text, ref=f"act:{_now():.0f}")
        await self.mem.remember(gid, "", "world",
                                f"《{world.name}》{ch.name}「{name}」:{(r.data.get('narration') or '')[:60]}")
        self.db.append_log(gid, uid, "act",
                           f"{ch.name}「{name}」:{(r.data.get('narration') or '')[:90]} ", world.name)
        echo = await self.mainline_echo(gid, uid, ctx=f"「{name}」{(r.data.get('narration') or '')[:70]}")
        return {
            "type": "act",
            "echo": echo,
            "zone": (zone.get("name") if zone else ""),
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
        if len(npcs) >= self._cfgi("max_npcs_per_world", 32):
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
                was_ko = int(getattr(ch, "hp", 1)) <= 0
                ch.flags.pop("_state", None)
                if was_ko:
                    ch.hp = C.HP_WAKEUP
                self.db.append_log(gid, ch.uid, "misc", f"世界变动把被困的「{ch.name}」一并卷走,牢笼/束缚在时空震荡中崩解"
                                   + ("——新世界的医生把TA从昏迷中救醒了" if was_ko else ""))
            if (ch.flags or {}).get("_exp"):
                ch.flags.pop("_exp", None)
                self.db.append_log(gid, ch.uid, "misc",
                                   f"时空震荡把正在远征的「{ch.name}」一队人卷了回来——远征中断,奖励无从谈起")
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
        exp = self._on_expedition(ch)
        if exp:
            raise GameError(f"⚔ 你正在「{exp.get('title')}」远征途中,无法开启穿越之门"
                            f"(约还需 {self._exp_left_h(exp):.1f} 小时归来)。")
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
        w = World(gid=gid, name=name[:16], genre="玩家自设", desc=desc[:4000],
                  source="user", visited=0, created_by=uid)
        w.id = self.db.add_world(w)
        self.db.append_log(gid, uid, "misc", f"{ch.name} 在世界书里写下了《{name}》(等待降临)")
        return {"name": name, "id": w.id}

    # ══════════════ 基础设施 / 世界主线 / 房产 ══════════════
    async def mainline_progress(self, gid: str, uid: str) -> dict:
        """推进世界主线一步:推进者需满足当前小节的阶段门槛(声望/任务/讨伐)。
        全部小节完成后,LLM 续写『尾声』新篇章——结局之后,世界仍在继续。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        state_note = self._state_note(ch)
        if self._is_ko(ch):
            raise GameError("💔 你已重伤昏迷,没法去推进主线。先等苏醒/治疗。")
        exp = self._on_expedition(ch)
        if exp:
            raise GameError(f"⚔ 你正在「{exp.get('title')}」远征途中,没法推进主线"
                            f"(约还需 {self._exp_left_h(exp):.1f} 小时归来)。")
        if state_note:
            raise GameError(f"⛓ 你正被「{state_note}」困住,暂时没法去推进主线。先脱困再说。")
        w = self.db.cur_world(gid)
        if not w:
            raise GameError("世界尚未初始化")
        w = await self.ensure_world_mainline(gid) or w   # 主线空/缺失 → 立即 LLM 重生成
        ml = [dict(m) for m in (w.mainline or []) if isinstance(m, dict)]
        if not ml:
            raise GameError("这个世界暂时没有可推进的主线(等新一轮世界变动或重新生成)。")
        cur = next((m for m in ml if not m.get("done")), None)
        # ── 全部完成 → 续写尾声新篇章(结局后的世界仍在继续)──
        if cur is None:
            r = await self.brain.gen_epilogue(world=w)
            stages = [s for s in (r.data.get("stages") or []) if s.get("stage")]
            narration = r.data.get("narration") or ""
            if not stages:
                raise GameError("这个世界的篇章已全部落幕。去别的世界看看,或等一次世界变动开启新故事。")
            ml.extend(stages)
            self.db.update_world(w.id, mainline=ml)
            await self.mem.remember(gid, "", "world",
                                    f"《{w.name}》篇章完结,尾声新篇章开启({len(stages)}节)", ref=f"mainline:{w.id}")
            self.db.append_log(gid, uid, "event",
                               f"{ch.name} 见证了《{w.name}》旧篇章的落幕——尾声新篇章开启", w.name)
            return {
                "type": "mainline",
                "gid": gid,
                "world_name": w.name,
                "stage": "尾声 · 新篇章",
                "narration": (narration + "\n新篇章:「" + "」「".join(s["stage"] for s in stages) + "」已开启,"
                              "用「/分身 主线 推进」继续。").strip(),
                "changes": [],
                "remaining": len(stages),
                "ok_llm": r.ok,
            }
        # ── 阶段门槛检查:声望 / 任务 / 讨伐 ──
        goal_note = ""
        gt = str(cur.get("goal_type") or "").strip()
        gv = int(cur.get("goal_value") or 0)
        if gt and gv:
            if gt == "reputation":
                have = self.db.rep_get(gid, uid, w.id)
                progress = f"你的声望 {have}/{gv}"
            elif gt == "quest":
                have = int(self.db.kv_get(gid, "quests_done_total") or 0)
                progress = f"全群累计完成任务 {have}/{gv} 件"
            elif gt == "defeat":
                have = int(self.db.kv_get(gid, "defeats_total") or 0)
                progress = f"全群累计讨伐 {have}/{gv} 次"
            else:
                have, progress = None, ""
            if have is not None and have < gv:
                raise GameError(
                    f"主线「{cur['stage']}」的门槛尚未达成:{progress}。"
                    f"({cur.get('goal_note', '')})先去积累历练,水到渠成后再来推进。")
            goal_note = f"{cur.get('goal_note', '')}(当前:{progress})"
        self._attach_rep_line(gid, uid, w)
        # 篇章终章(推完这一节主线即完结)→ 高潮剧情权重;其余关键节点 → 重要剧情
        remaining_after = [m for m in ml if m is not cur and not m.get("done")]
        weight = "climax" if not remaining_after else "major"
        # 让 LLM 结算这一步
        r = await self.brain.resolve_mainline(
            world=w, char=ch, stage=cur, goal_note=goal_note, weight=weight,
            material=await self._kb_ctx(gid, "世界主线 剧情 推进"))
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
        exp = self._on_expedition(ch)
        if exp:
            raise GameError(f"⚔ 你正在「{exp.get('title')}」远征途中(目标「{exp.get('zone')},"
                            f"约还需 {self._exp_left_h(exp):.1f} 小时归来),远征结束前无法{what}。")
        wn = self._work_note(ch)
        if wn:
            raise GameError(f"⚒ 你正{wn},下班前没法{what}。先专心把班上完吧。")
        return ch

    def work_today(self, gid: str, uid: str) -> dict | None:
        """上工:在世界基础设施里找一份班(约 WORK_SHIFT_H 小时后自动下班结算)。
        上工期间无法主动行动/互动/再兼职,到点由主循环 _sweep_work 统一结算。
        返回 view(phase=start)或 None(没有可打工的地方)。"""
        ch = self._require_free(gid, uid, "去上工")
        w = self.db.cur_world(gid)
        if not w:
            raise GameError("世界尚未初始化")
        infra = [x for x in (w.infra or []) if x.get("work")]
        if not infra:
            raise GameError(f"《{w.name}》里暂时没有能打工的地方(试试冒险、打工指令,或重新生成世界)。")
        spot = random.choice(infra)
        cost = 25
        if ch.stamina < cost:
            raise GameError(f"体力不足({ch.stamina}/{cost}),先去歇歇。")
        flags = dict(ch.flags or {})
        if flags.get("_work"):
            raise GameError("你正在上一班里,等下班再说。")
        # 世界 NPC 名字池里挑一位同班同事
        colleague = ""
        npcs = [n.get("name") for n in (w.npcs or []) if str(n.get("name", "")).strip()]
        if npcs:
            colleague = random.choice(npcs)
        until = _now() + self.WORK_SHIFT_H * 3600
        flags["_work"] = {
            "until": until,
            "started": _now(),
            "infra": str(spot.get("name") or "某处"),
            "job": str(spot.get("work") or "打零工"),
            "colleague": colleague,
        }
        ch.stamina = max(0, ch.stamina - cost)
        ch.flags = flags
        self.db.upsert_char(ch)
        self.db.append_log(gid, uid, "act", f"{ch.name} 在《{w.name}》的「{spot['name']}」({spot.get('work')})上工了", w.name)
        left_min = int(self.WORK_SHIFT_H * 60)
        return {
            "type": "work",
            "phase": "start",
            "gid": gid,
            "char_name": ch.name,
            "world_name": w.name,
            "spot": str(spot.get("name") or "某处"),
            "occupation": str(spot.get("work") or "打零工"),
            "colleague": colleague,
            "until_min": left_min,
            "hours": self.WORK_SHIFT_H,
            "cost": cost,
            "changes": [f"体力-{cost}"],
        }

    async def settle_work(self, gid: str, uid: str) -> dict:
        """到点自动结算兼职:摘除班次标记 → 本地计算工钱 → LLM 写下班叙述+同事道别。
        从 flags 摘掉 _work 后再结算,避免并发重复结算。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        f = self._work(ch)
        if not f:
            raise GameError("你现在没有在班的兼职。")
        flags = dict(ch.flags or {})
        flags.pop("_work", None)
        ch.flags = flags
        self.db.upsert_char(ch)  # 先摘标记,防并发
        w = self.db.cur_world(gid)
        world_name = w.name if w else ""
        spot = str(f.get("infra") or "某处")
        job = str(f.get("job") or "打零工")
        colleague = str(f.get("colleague") or "") or None
        started = float(f.get("started") or _now())
        hours = max(0.1, round((_now() - started) / 3600, 1))
        earn = random.randint(25, 55) + (getattr(ch, "level", 1) or 1) * 3
        ch.gold += earn
        self.db.upsert_char(ch)
        # LLM 下班叙述(可选:world 不在/失败时用兜底叙述)
        if w is not None:
            try:
                r = await self.brain.settle_work(
                    world=w, char=ch, spot=spot, job=job, hours=hours,
                    colleague=colleague, material=await self._kb_ctx(gid, "下班 收工 兼职"))
            except Exception:
                r = None
        else:
            r = None
        changes = []
        if r and r.ok:
            eff = dict(r.data.get("effects") or {})
            eff.pop("gold", None)  # 工钱本地入账,防双重
            changes += self._apply_effects(ch, eff)
            narration = str(r.data.get("narration") or "")[:300]
            dialogues = self._norm_r_dialogues(r.data)
            gains, loses = r.data.get("items_gain") or [], r.data.get("items_lose") or []
            changes += self._apply_items(gid, uid, gains, loses)
            ok_llm = True
        else:
            narration = (f"{ch.name}在「{spot}」忙了约 {hours} 小时,收工时腰酸背痛,"
                         f"揣着 {earn} 金币的工钱走进暮色里。")
            dialogues = []
            changes += self._apply_effects(ch, {"mood": 3, "exp": 4})
            ok_llm = False
        changes.append(f"金币+{earn}")
        await self.mem.remember(gid, uid, "char", f"在「{spot}」做{job}下班,赚了{earn}金币", ref=f"work:{_now():.0f}")
        if world_name:
            await self.mem.remember(gid, "", "world", f"《{world_name}》{ch.name}在「{spot}」下班收工", ref=f"work:{_now():.0f}")
        self.db.append_log(gid, uid, "act",
                           f"{ch.name} 在《{world_name}》的「{spot}」({job})下班,赚了{earn}金币 ", world_name)
        self._quest_progress(gid, uid, "work")
        return {
            "type": "work",
            "phase": "done",
            "gid": gid,
            "uid": uid,
            "char_name": ch.name,
            "world_name": world_name,
            "spot": spot,
            "occupation": job,
            "colleague": colleague or "",
            "hours": hours,
            "earn": earn,
            "narration": narration,
            "dialogues": dialogues,
            "changes": changes,
            "ok_llm": ok_llm,
        }

    async def _sweep_work(self, gid: str) -> list[dict]:
        """主循环扫描:把到点下班的班次全部自动结算(一次广播)。"""
        now = _now()
        views = []
        for ch in self.db.list_chars(gid):
            f = self._work(ch)
            if not f or now < float(f.get("until") or 0):
                continue
            try:
                views.append(await self.settle_work(gid, ch.uid))
            except GameError:
                continue
            except Exception:
                # 摘标记失败/其他异常:清掉班次,避免永久卡住
                flags = dict(ch.flags or {}); flags.pop("_work", None)
                ch.flags = flags; self.db.upsert_char(ch)
        return views

    @staticmethod
    def _norm_r_dialogues(r_data: dict) -> list:
        """兼容:LLM 返回的对话可能有嵌套 structures;统一为 [{speaker,text}]。"""
        dlg = r_data.get("dialogues")
        if isinstance(dlg, list):
            out = []
            for x in dlg[:6]:
                if isinstance(x, dict):
                    out.append({"speaker": str(x.get("speaker", "")).strip()[:8],
                                "text": str(x.get("text", "")).strip()[:60]})
            return out
        return []

    def _apply_items(self, gid: str, uid: str, gains: list | None, loses: list | None) -> list[str]:
        """应用 LLM/事件给出的物品变化,并推进相关任务步骤(item 类)。"""
        tags = []
        for g in (gains or [])[:2]:
            if not (isinstance(g, dict) and str(g.get("name", "")).strip()):
                continue
            name = str(g["name"]).strip()[:12]
            self.db.item_add(gid, uid, name, 1, str(g.get("note", "")).strip()[:20])
            tags.append(f"🎒 获得「{name}」")
            self._quest_progress(gid, uid, "item", item=name)
        for name in (loses or [])[:2]:
            name = str(name).strip()[:12]
            if name and self.db.item_remove(gid, uid, name, 1):
                tags.append(f"🎒 失去「{name}」")
        return tags

    def _quest_progress(self, gid: str, uid: str, kind: str, name: str = "", text: str = "", item: str = ""):
        """推进今日开放任务的步骤进度(act/npc/life/social/work/item/facility),持久化 done 标记。"""
        if kind not in ("act", "npc", "life", "social", "work", "item", "facility"):
            return
        day = self._day_key()
        qs = self.db.list_quests(gid, uid, day)
        for q in qs:
            if q["state"] != "open":
                continue
            steps = json.loads(q.get("steps") or "[]") if isinstance(q.get("steps"), str) else (q.get("steps") or [])
            changed = False
            for s in steps:
                if not isinstance(s, dict) or s.get("done"):
                    continue
                t = str(s.get("type") or "")
                if t == "act" and kind == "act" and name in ("冒险", "打怪") and text:
                    kws = [str(k).strip() for k in (s.get("keywords") or []) if str(k).strip()]
                    if kws and any(k in text for k in kws):
                        s["done"] = True; changed = True
                elif t == "npc" and kind == "npc" and name:
                    target = str((s.get("npc") or "")).strip()
                    if target and (target in name or name in target):
                        s["done"] = True; changed = True
                elif t == "life" and kind == "life" and name:
                    target = str((s.get("npc") or "")).strip()
                    if target and (target in name or name in target):
                        s["done"] = True; changed = True
                elif t == "social" and kind == "social":
                    s["done"] = True; changed = True
                elif t == "work" and kind == "work":
                    s["done"] = True; changed = True
                elif t == "item" and kind == "item" and item:
                    target = str((s.get("item") or "")).strip()
                    if target and (target in item or item in target):
                        s["done"] = True; changed = True
                elif t == "facility" and kind == "facility" and name:
                    target = str((s.get("facility") or "")).strip()
                    if target and (target in name or name in target):
                        s["done"] = True; changed = True
            if changed:
                self.db.update_quest_steps(q["id"], steps)

    def _quest_mentions(self, gid: str, uid: str) -> list[str]:
        """今日开放任务中提及的设施相关文本(place / facility 步骤目标 / 任务名 / 步骤描述),
        用于判断某设施是否为当前任务的办理地点——任务需要时才允许去普通(非社交娱乐)设施。"""
        day = self._day_key()
        texts: list[str] = []
        for q in self.db.list_quests(gid, uid, day):
            if q["state"] != "open":
                continue
            if str(q.get("place") or "").strip():
                texts.append(str(q["place"]).strip())
            if str(q.get("text") or "").strip():
                texts.append(str(q["text"]).strip())
            steps = json.loads(q.get("steps") or "[]") if isinstance(q.get("steps"), str) else (q.get("steps") or [])
            for s in steps:
                if not isinstance(s, dict):
                    continue
                if str(s.get("facility") or "").strip():
                    texts.append(str(s["facility"]).strip())
                if str(s.get("desc") or "").strip():
                    texts.append(str(s["desc"]).strip())
        return texts

    async def visit_facility(self, gid: str, uid: str, name: str, action: str = "") -> dict:
        """去一家设施消磨时光/办事,产生一段事件剧情。每个设施每天限 1 次;打工中/被困不可前往。
        社交/娱乐/约会类场所可直接光顾;普通设施(店铺/工坊等)在今日任务指向它时也放行
        (place / facility 步骤 / 任务名中提及),并推进对应任务步骤。"""
        ch = self._require_free(gid, uid, "去光顾")
        w = self.db.cur_world(gid)
        if not w:
            raise GameError("世界尚未初始化")
        facs = [i for i in (w.infra or []) if isinstance(i, dict) and str(i.get("name", "")).strip()]
        it = next((i for i in facs if name == i["name"] or name in i["name"] or i["name"] in name), None)
        if it is None:
            names = "、".join(f"{i.get('name')}" for i in facs[:20]) or "无"
            raise GameError(f"《{w.name}》没有「{name}」这处地方。现有设施:{names}")
        # 医疗/店铺设施的特殊办理:治疗与购买走系统结算(不耗每日光顾次数、不耗 LLM)
        act = (action or "").strip()
        if C.is_medical_infra(it) and any(k in act for k in ("治疗", "看伤", "就诊", "治伤", "就医", "疗伤")):
            return self.heal_at_hospital(gid, uid)
        if act.startswith(("买", "购买")) and (C.is_medical_infra(it) or C.is_shop_infra(it)):
            goods = re.sub(r"^(买|购买|买下|买一[些个])\s*", "", act).strip()
            return self.buy_item(gid, uid, goods, facility=it.get("name", ""))
        # 任务需要:开放任务中提及该设施(办理地点/步骤目标)时,普通设施也放行
        task_wants = any(
            t and (t == it["name"] or t in it["name"] or it["name"] in t)
            for t in self._quest_mentions(gid, uid))
        if not C.infra_interactable(it) and not task_wants:
            raise GameError(
                f"「{it.get('name')}」只是一处普通设施(去「{it.get('kind','')}」最好用「/分身 兼职」打工、"
                "或「/分身 npc」拜访;社交/娱乐/约会类场所才可光顾消遣,除非今日任务需要去这里)。")

        day = self._day_key()
        flags = dict(ch.flags or {})
        fk = f"_fac:{it['name']}:{day}"
        if int(flags.get(fk, 0)) >= 1:
            raise GameError(f"「{it['name']}」今天你已经去过了,明天再来逛逛吧。")
        flags[fk] = int(flags.get(fk, 0)) + 1
        ch.flags = flags
        self.db.upsert_char(ch)
        mems = await self.mem.related(gid, f"{ch.name} 去 {it['name']} {action}", uid=uid, k=3)
        self._attach_rep_line(gid, uid, w)
        r = await self.brain.facility_event(
            world=w, char=ch, facility=it, action=action, memories=mems,
            material=await self._kb_ctx(gid, f"设施 社交 娱乐 {it['name']}"))
        data = r.data
        changes = self._apply_effects(ch, data.get("effects") or {})
        changes += self._apply_items(gid, uid, data.get("items_gain"), data.get("items_lose"))
        self.db.upsert_char(ch)
        await self.mem.remember(gid, uid, "char",
                                data.get("memory") or f"{ch.name}去{it['name']}消磨了时光",
                                ref=f"fac:{_now():.0f}")
        await self.mem.remember(gid, "", "world",
                                f"《{w.name}》{ch.name}光顾「{it['name']}」:{(data.get('narration') or '')[:40]}",
                                ref=f"fac:{_now():.0f}")
        self.db.append_log(gid, uid, "act",
                           f"{ch.name} 光顾「{it['name']}」:{(data.get('narration') or '')[:80]}", w.name)
        # 任务需要时推进「去该设施」步骤(facility 类型)
        if task_wants:
            self._quest_progress(gid, uid, "facility", name=it["name"])
        echo = await self.mainline_echo(gid, uid, ctx=f"在{it['name']} {(data.get('narration') or '')[:60]}")
        return {
            "type": "facility",
            "echo": echo,
            "gid": gid,
            "char_name": ch.name,
            "world_name": w.name,
            "facility": it,
            "action": action,
            "narration": data.get("narration", ""),
            "dialogues": data.get("dialogues") or [],
            "avatars": self._avatar_map(gid),
            "title": f"🏮 {it['name']} · {it.get('kind','')}",
            "changes": changes,
            "ok_llm": r.ok,
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

    @staticmethod
    def _home_hp_recovery(price: int) -> int:
        """按房价分档的回家生命恢复:越贵的房子恢复越多(单次)。"""
        if price >= 8000:
            return 40
        if price >= 5000:
            return 32
        if price >= 3200:
            return 26
        if price >= 2000:
            return 20
        if price >= 1000:
            return 15
        return 10

    @staticmethod
    def _home_recovery(price: int) -> tuple[int, int]:
        """按房价分档的回家恢复:越贵回得越多(体力, 心情)。"""
        if price >= 8000:
            return 70, 25
        if price >= 5000:
            return 55, 20
        if price >= 3200:
            return 42, 15
        if price >= 2000:
            return 32, 12
        if price >= 1000:
            return 24, 9
        return 15, 6

    async def my_home(self, gid: str, uid: str) -> dict:
        """回宅休整:每天一次;按房价分档回体力/心情;小概率触发家居事件剧情。"""
        ch = self._require_free(gid, uid, "回宅休息")
        w = self.db.cur_world(gid)
        pid = (ch.flags or {}).get("home_plot")
        if not pid:
            raise GameError("你还没有房产。用「/分身 房产」或「/分身 买房 <编号>」购置一处吧。")
        p = self.db.plot_get(int(pid))
        if not p or str(p.get("world_id")) != str(w.id if w else -1):
            raise GameError("你的房产不在这里(穿越后到了另一个世界)。")
        day = self._day_key()
        flags = dict(ch.flags or {})
        if flags.get("_home_day") == day:
            raise GameError("今天已经回过宅补充过体力了,明天再回来歇歇吧。")
        st_gain, mo_gain = self._home_recovery(int(p.get("price") or 1000))
        hp_gain = self._home_hp_recovery(int(p.get("price") or 1000))
        ch.mood = min(C.MOOD_MAX, ch.mood + mo_gain)
        ch.stamina = min(C.STAMINA_MAX, ch.stamina + st_gain)
        hp_chg = ""
        hp_actual = 0
        if int(getattr(ch, "hp", C.HP_MAX)) < C.HP_MAX:
            before = int(ch.hp)
            ch.hp = min(C.HP_MAX, before + hp_gain)
            hp_actual = int(ch.hp) - before
            hp_chg = f"生命+{hp_actual}"
        flags["_home_day"] = day
        ch.flags = flags
        self.db.upsert_char(ch)
        self.db.append_log(gid, uid, "misc",
                           f"{ch.name} 回《{w.name}》的「{p['name']}」休息了一阵"
                           f"(体力+{st_gain}/心情+{mo_gain}" + (f"/{hp_chg}" if hp_chg else "") + ")", w.name)
        view = {
            "type": "home",
            "gid": gid,
            "char_name": ch.name,
            "world_name": w.name if w else "",
            "plot": p,
            "stamina_gain": st_gain,
            "mood_gain": mo_gain,
            "hp_gain": hp_actual,
            "changes": [f"心情+{mo_gain}", f"体力+{st_gain}"] + ([hp_chg] if hp_chg else []),
            "ok_llm": False,
        }
        # 小概率触发家居事件剧情(每天一次,因为回家本身每天一次)
        try:
            if w is not None and random.random() < self.HOME_EVENT_P:
                r = await self.brain.home_event(
                    world=w, char=ch, plot=p,
                    memories=await self.mem.related(gid, f"{ch.name} 回家 家居", uid=uid, k=2),
                    material=await self._kb_ctx(gid, "回家 家居 日常"))
                if r.ok and r.data.get("narration"):
                    ev_eff = dict(r.data.get("effects") or {})
                    ev_eff.pop("gold", None); ev_eff.pop("stamina", None)
                    view["changes"] += self._apply_effects(ch, ev_eff)
                    view["narration"] = str(r.data.get("narration"))[:220]
                    view["dialogues"] = self._norm_r_dialogues(r.data)
                    view["event_title"] = f"🏠 归家的插曲 · {p.get('name')}"
                    view["ok_llm"] = True
                    self.db.upsert_char(ch)
                    await self.mem.remember(gid, uid, "char",
                                            r.data.get("memory") or f"回了{ p.get('name') }家歇息",
                                            ref=f"home:{_now():.0f}")
        except Exception:
            pass
        return view

    # ══════════════ 每日小任务(轻松、按世界生成)══════════════
    async def ensure_quests(self, gid: str, uid: str) -> list[dict]:
        """领取/查看今日任务:无则按世界+设施+NPC/生活角色+记忆生成 3 个带委托人/步骤的委托。"""
        day = self._day_key()
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身,先「/分身 创建 名字」")
        state_note = self._state_note(ch)
        if state_note:
            raise GameError(
                f"⛓ 你正被「{state_note}」困住,连今日的委托也无从下手。"
                "先脱困再说——「/分身 冒险 <描述>」、找特殊NPC求助,或等群友来救 / 时机变化。"
            )
        exp = self._on_expedition(ch)
        if exp:
            raise GameError(f"⚔ 你正在「{exp.get('title')}」远征途中,顾不上今日委托"
                            f"(约还需 {self._exp_left_h(exp):.1f} 小时归来)。")
        qs = self.db.list_quests(gid, uid, day)
        if qs:
            return qs
        world = self.db.cur_world(gid)
        if not world:
            raise GameError("世界尚未初始化")
        npc_names = list(world.npc_names())
        life_names = [c.name for c in self.db.list_chars(gid) if is_npc_uid(c.uid)]
        facs = [i for i in (world.infra or []) if isinstance(i, dict) and str(i.get("name", "")).strip()]
        mems = await self.mem.related(gid, f"{ch.name} 委托 任务", uid=uid, k=3)
        r = await self.brain.gen_quests(world=world, char=ch, npc_names=npc_names,
                                        life_names=life_names, facilities=facs, memories=mems,
                                        material=await self._kb_ctx(gid, "委托 任务 世界观"))
        quests = r.data.get("quests") if r.ok else []
        if not quests:
            # AI 兜底:设施/NPC 驱动的本地模板
            fac = facs[0] if facs else {}
            quests = [{
                "text": "公会委托:讨伐作乱妖物", "giver": str(fac.get("name") or "冒险者公会"),
                "place": str(fac.get("name") or "冒险者公会"),
                "hint": "用「分身 冒险」去讨伐,关键词带【讨伐】",
                "steps": [{"type": "act", "desc": "讨伐一次作乱妖物", "keywords": ["讨伐"], "done": False},
                           {"type": "npc", "desc": "找回一位NPC", "npc": (npc_names or ["老铁"])[0], "done": False}],
            }, {
                "text": "帮委托人跑腿", "giver": (npc_names or ["季小姐"])[0] if len(npc_names) > 1 else "旧识",
                "place": str(fac.get("name") or "集市"), "hint": "找对方聊几句就算数",
                "steps": [{"type": "npc", "desc": "与委托人碰面", "npc": (npc_names or ["季小姐"])[-1], "done": False}],
            }, {
                "text": "赚点外快", "giver": "杂货铺掌柜", "place": str(fac.get("name") or "集市"),
                "hint": "「分身 兼职」上一班",
                "steps": [{"type": "work", "desc": "完成一次兼职打工", "done": False}],
            }]
        for t in quests[:3]:
            if not t.get("text"):
                continue
            self.db.add_quest(gid, uid, day, t["text"], t.get("hint", ""),
                              steps=t.get("steps"), giver=t.get("giver", ""), place=t.get("place", ""))
        return self.db.list_quests(gid, uid, day)

    async def complete_quest(self, gid: str, uid: str, idx: int) -> dict:
        """向委托人交付任务:所有步骤都完成才能交;空步骤兼容旧行为。上场人物锁定委托人。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        if self._work_note(ch):
            raise GameError(f"⚒ 你正{self._work_note(ch)},等下班再去交付委托吧。")
        state_note = self._state_note(ch)
        if state_note:
            raise GameError(
                f"⛓ 你正被「{state_note}」困住,没法去完成委托。"
                "先脱困再说——「/分身 冒险 <描述>」、找特殊NPC求助,或等群友来救 / 时机变化。"
            )
        exp = self._on_expedition(ch)
        if exp:
            raise GameError(f"⚔ 你正在「{exp.get('title')}」远征途中,没法去交付委托"
                            f"(约还需 {self._exp_left_h(exp):.1f} 小时归来)。")
        world = self.db.cur_world(gid)
        day = self._day_key()
        open_qs = [q for q in self.db.list_quests(gid, uid, day) if q["state"] == "open"]
        if not open_qs:
            raise GameError("今天没有可完成的任务了(先发「/分身 任务」领取)")
        if not (0 <= idx < len(open_qs)):
            raise GameError(f"请选择 1~{len(open_qs)} 之间的编号")
        q = open_qs[idx]
        steps = json.loads(q.get("steps") or "[]") if isinstance(q.get("steps"), str) else (q.get("steps") or [])
        undone = [s for s in steps if isinstance(s, dict) and not s.get("done")]
        if steps and undone:
            bullets = "\n".join(f"  ☐ {s.get('desc','')}" for s in undone[:3])
            raise GameError(
                f"委托「{q['text']}」还没完成全部步骤:需要先把这些做到——\n{bullets}\n"
                "做完后再来交付即可。")
        if not self.db.resolve_quest_if_open(q["id"]):
            raise GameError("这个委托刚被交付了")
        steps_desc = [f"{s.get('desc','')}" for s in steps if isinstance(s, dict)]
        giver = q.get("giver") or "委托人"
        place = q.get("place") or ""
        mems = await self.mem.related(gid, f"{ch.name} 交付 {q['text']} {giver}", uid=uid, k=2)
        r = await self.brain.finish_quest(world=world, char=ch, quest=q["text"], giver=giver,
                                          place=place, steps_desc=steps_desc, memories=mems,
                                          material=await self._kb_ctx(gid, "交付 委托 报酬"))
        changes = self._apply_effects(ch, r.data.get("effects") or {})
        changes += self._apply_items(gid, uid, r.data.get("items_gain"), [])
        # 全群累计完成任务数(主线 quest 门槛用)
        self.db.kv_incr(gid, "quests_done_total", 1)
        await self.mem.remember(gid, uid, "char", f"交付委托「{q['text']}」(委托人:{giver})", ref=f"quest:{q['id']}")
        await self.mem.remember(gid, "", "world",
                                f"《{world.name}》{ch.name}完成了委托「{q['text']}」", ref=f"quest:{q['id']}")
        self.db.append_log(gid, uid, "quest",
                           f"完成委托「{q['text']}」:{(r.data.get('narration') or '')[:80]}", world.name)
        echo = await self.mainline_echo(gid, uid, ctx=f"交付委托「{q['text']}」{(r.data.get('narration') or '')[:60]}")
        return {
            "type": "result",
            "echo": echo,
            "gid": gid,
            "uid": uid,
            "char_name": ch.name,
            "world_name": world.name,
            "event_title": f"委托交付 · {q['text']}",
            "chosen": f"向{giver}交任务",
            "narration": r.data.get("narration", ""),
            "dialogues": r.data.get("dialogues") or [],
            "avatars": self._avatar_map(gid),
            "changes": changes,
            "ok_llm": r.ok,
        }

    # ══════════════ 声望(每个世界一份)══════════════
    def rep_of(self, gid: str, uid: str, world_id: int | None = None) -> int:
        w = self.db.get_world(int(world_id)) if world_id else self.db.cur_world(gid)
        if w is None:
            return 0
        return self.db.rep_get(gid, uid, w.id)

    def _rep_note(self, gid: str, uid: str, world: World) -> str:
        """角色当前世界声望的一句话(注入 prompt,影响 NPC 态度)。"""
        if is_npc_uid(uid):
            return ""
        ch = self.db.get_char(gid, uid)
        if ch is None:
            return ""
        score = self.db.rep_get(gid, uid, world.id)
        label = C.rep_level_label(score)
        return f"{ch.name}在本世界的声望:{score}({label})——NPC/商人按「{label}」的居民对待TA"

    def _attach_rep_line(self, gid: str, uid: str, world: World):
        """把声望一句话挂到 char._rep_line,供 prompts._rep_short 读取。"""
        ch = self.db.get_char(gid, uid)
        if ch is None:
            return
        score = self.db.rep_get(gid, uid, world.id)
        ch._rep_line = f"在本世界的声望{score}({C.rep_level_label(score)})"

    def rep_panel(self, gid: str, uid: str) -> dict:
        """声望面板:全部世界声望 + 当前世界声望榜。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        w = self.db.cur_world(gid)
        rows = self.db.rep_list_for(gid, uid)
        cur = None
        if w is not None:
            cur = {"world": w.name, "score": self.db.rep_get(gid, uid, w.id),
                   "label": C.rep_level_label(self.db.rep_get(gid, uid, w.id))}
        return {"char_name": ch.name, "current": cur,
                "list": [{"world": r.get("name") or f"世界#{r['world_id']}", "score": int(r["score"]),
                          "label": C.rep_level_label(int(r["score"]))} for r in rows],
                "top": [{"name": r.get("name") or r["uid"][:8], "score": int(r["score"])} for r in (self.db.rep_top(gid, w.id, 5) if w else [])]}

    # ══════════════ 治疗物品 / 购买 / 治疗 ══════════════
    def heal_items_of(self, gid: str, world: World | None = None) -> list[dict]:
        """当前世界治疗物品(缺失时按题材风格兜底)。"""
        w = world or self.db.cur_world(gid)
        if w is None:
            return [dict(h) for h in C.DEFAULT_HEAL_ITEMS]
        items = [dict(h) for h in (w.heal_items or []) if isinstance(h, dict) and str(h.get("name", "")).strip()]
        if not items:
            items = [dict(h) for h in C.heal_style_for(w.genre, w.desc)]
            self.db.update_world(w.id, heal_items=items)
            w.heal_items = items
        return items

    def _backpack_heal_note(self, gid: str, uid: str, world: World) -> str:
        """角色背包中的治疗物品清单(注入 prompt,让 LLM 在剧情中自主使用)。"""
        names = {h["name"] for h in self.heal_items_of(gid, world)}
        inv = [it for it in self.db.items_list(gid, uid) if it.get("name") in names]
        if not inv:
            return ""
        info = "、".join(f"{it['name']}×{it['count']}" for it in inv[:4])
        return f"背包中的治疗物品:{info}(若剧情自然需要,可让角色使用其中之一:items_lose 该物品名 + effects.hp 恢复)"

    def buy_item(self, gid: str, uid: str, item_name: str, facility: str = "") -> dict:
        """在店铺/医疗类设施购买治疗物品(去命令:/分身 去 <设施> 买治疗药 / 分身 购买 <物品名>)。
        声望越高折扣越大(最高七五折)。返回 view。"""
        ch = self._require_free(gid, uid, "去店里买东西")
        w = self.db.cur_world(gid)
        if not w:
            raise GameError("世界尚未初始化")
        items = self.heal_items_of(gid, w)
        target = None
        q = (item_name or "").strip()
        for h in items:
            if h["name"] == q or (q and (q in h["name"] or h["name"] in q)):
                target = h
                break
        if target is None:
            names = "、".join(h["name"] for h in items)
            raise GameError(f"店里没有「{item_name}」。这个世界能买到的治疗物品:{names}"
                            f"(用「/分身 去 <设施名> 买 <物品名>」或「/分身 购买 <物品名>」)")
        # 校验设施:指定了设施名 → 必须是医疗/店铺类且存在;未指定 → 默认就近医疗/店铺
        fac = None
        facs = [i for i in (w.infra or []) if isinstance(i, dict) and str(i.get("name", "")).strip()]
        if facility:
            fac = next((i for i in facs if facility == i["name"] or facility in i["name"] or i["name"] in facility), None)
            if fac is None:
                raise GameError(f"《{w.name}》没有「{facility}」这处地方")
            if not (C.is_medical_infra(fac) or C.is_shop_infra(fac)):
                raise GameError(f"「{fac.get('name')}」不卖这些东西(要去店铺/药铺/诊所/医院类设施)")
        else:
            fac = next((i for i in facs if C.is_medical_infra(i)), None) \
                or next((i for i in facs if C.is_shop_infra(i)), None)
            if fac is None:
                raise GameError("这个世界上没有能买东西的地方(让管理员重建设施试试)")
        rep = self.db.rep_get(gid, uid, w.id)
        discount = 1.0 - min(max(rep, 0), 100) / 400.0   # 声望0=原价,100=七五折
        price = max(5, int(round(int(target.get("price") or 30) * discount)))
        if ch.gold < price:
            raise GameError(f"金币不足:{ch.gold}/{price}。{fac.get('name')}的「{target['name']}」可不便宜")
        ch.gold -= price
        self.db.upsert_char(ch)
        self.db.item_add(gid, uid, target["name"], 1, str(target.get("note", ""))[:20])
        self.db.append_log(gid, uid, "act",
                           f"{ch.name} 在「{fac.get('name')}」买了一剂「{target['name']}」({price}金币)", w.name)
        self._quest_progress(gid, uid, "facility", name=fac.get("name", ""))
        return {
            "type": "buy",
            "gid": gid,
            "char_name": ch.name,
            "world_name": w.name,
            "facility": fac,
            "item": target,
            "price": price,
            "rep_discount": discount < 1.0,
            "narration": (f"{ch.name}走进「{fac.get('name')}」,付了 {price} 金币,收下一剂「{target['name']}」"
                          f"({target.get('note', '疗伤圣品')})。"
                          + (f"掌柜认得这位{C.rep_level_label(rep)}的客人,主动抹了零头。" if discount < 1.0 else "")),
            "changes": [f"金币-{price}", f"🎒 获得「{target['name']}」"],
            "ok_llm": False,
        }

    def use_heal_item(self, gid: str, uid: str, item_name: str = "") -> dict:
        """使用背包里的治疗物品(留空则自动用恢复量最小的能用的)。返回 view。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        exp = self._on_expedition(ch)
        if exp:
            raise GameError("⚔ 远征途中的补给由队伍统一调配,你没法手动用药。")
        w = self.db.cur_world(gid)
        world_name = w.name if w else ""
        if int(getattr(ch, "hp", C.HP_MAX)) >= C.HP_MAX:
            raise GameError("生命值是满的,不用浪费药")
        inv = {it["name"]: it for it in self.db.items_list(gid, uid)}
        heal_items = {h["name"]: h for h in self.heal_items_of(gid, w)}
        usable = [(name, heal_items[name]) for name in inv if name in heal_items]
        if not usable:
            raise GameError("背包里没有治疗物品。可以「/分身 去 <诊所/药铺名> 买 <治疗物品名>」购买,"
                            "或在冒险/事件中获取")
        if item_name:
            item_name = item_name.strip()
            usable = [u for u in usable if u[0] == item_name or item_name in u[0] or u[0] in item_name]
            if not usable:
                raise GameError(f"背包里没有能用的「{item_name}」(治疗物品请用世界通名)")
        usable.sort(key=lambda x: x[1].get("heal", 0))   # 默认用最小的
        name, h = usable[0]
        self.db.item_remove(gid, uid, name, 1)
        before = int(ch.hp)
        chg = self._apply_hp(ch, int(h.get("heal") or 30))
        self.db.upsert_char(ch)
        self.db.append_log(gid, uid, "act", f"{ch.name} 使用了「{name}」({before}→{ch.hp}生命)", world_name)
        return {
            "type": "heal",
            "gid": gid,
            "char_name": ch.name,
            "world_name": world_name,
            "item": h,
            "item_name": name,
            "before": before,
            "after": int(ch.hp),
            "narration": f"{ch.name} 用掉了「{name}」——{h.get('note', '一股暖流散入四肢百骸')}"
                         f"(生命 {before} → {ch.hp}/{C.HP_MAX})。",
            "changes": [f"🎒 失去「{name}」"] + ([chg] if chg else []),
            "ok_llm": False,
        }

    def heal_at_hospital(self, gid: str, uid: str) -> dict:
        """去医院(泛指医疗性质设施)付费治疗:按缺失生命计费,声望折扣。每天不限次,花钱就行。"""
        ch = self._require_free(gid, uid, "去医院治疗")
        w = self.db.cur_world(gid)
        if not w:
            raise GameError("世界尚未初始化")
        missing = C.HP_MAX - int(getattr(ch, "hp", C.HP_MAX))
        if missing <= 0:
            raise GameError("生命值是满的,不用去医院花冤枉钱")
        med = [i for i in (w.infra or []) if isinstance(i, dict) and C.is_medical_infra(i)]
        if not med:
            raise GameError(f"《{w.name}》暂时没有医疗性质的设施(诊所/医院/药铺)。"
                            "可以先用背包里的治疗物品,或等日切自然恢复")
        fac = random.choice(med)
        rep = self.db.rep_get(gid, uid, w.id)
        discount = 1.0 - min(max(rep, 0), 100) / 400.0
        cost = max(5, int(round(missing * C.HEAL_PRICE_PER_HP * discount)))
        if ch.gold < cost:
            raise GameError(
                f"治好这一身伤要 {cost} 金币,你只有 {ch.gold}。"
                "(声望越高折扣越大;也可以用背包里的治疗物品,或等明天体力/生命自然恢复)")
        ch.gold -= cost
        chg = self._apply_hp(ch, missing)
        self.db.upsert_char(ch)
        self.db.append_log(gid, uid, "act",
                           f"{ch.name} 在「{fac.get('name')}」接受了治疗({cost}金币,生命回满)", w.name)
        self._quest_progress(gid, uid, "facility", name=fac.get("name", ""))
        return {
            "type": "heal",
            "gid": gid,
            "char_name": ch.name,
            "world_name": w.name,
            "facility": fac,
            "cost": cost,
            "before": C.HP_MAX - missing,
            "after": int(ch.hp),
            "rep_discount": discount < 1.0,
            "narration": (f"{ch.name} 躺进了「{fac.get('name')}」的诊台。清创、上药、包扎,一气呵成——"
                          f"付了 {cost} 金币,生命回满({C.HP_MAX}/{C.HP_MAX})。"
                          + (f"医者认得这位{C.rep_level_label(rep)}的客人,收费实在。" if discount < 1.0 else "")),
            "changes": ([f"金币-{cost}", "❤ 生命回满"] + ([chg] if chg and chg.startswith("❤") else [])),
            "ok_llm": False,
        }

    # ══════════════ 危险区域查看 ══════════════
    def list_zones(self, gid: str) -> list[dict]:
        w = self.db.cur_world(gid)
        if not w:
            raise GameError("世界尚未初始化")
        zones = [dict(z) for z in (w.zones or []) if isinstance(z, dict) and str(z.get("name", "")).strip()]
        if len(zones) < 2:
            zones = self._ensure_zones_baseline(w, zones)
            self.db.update_world(w.id, zones=zones)
        return zones

    # ══════════════ 主线回响(小概率穿插)══════════════
    MAINLINE_ECHO_P = 0.12   # 事件/行动/互动结算后触发主线回响的概率

    async def mainline_echo(self, gid: str, uid: str, ctx: str) -> str:
        """结算后小概率生成一段『主线回响』叙述(伏笔/呼应,不改数值)。"""
        try:
            ch = self.db.get_char(gid, uid)
            w = self.db.cur_world(gid)
            if ch is None or w is None or is_npc_uid(uid):
                return ""
            ml = [m for m in (w.mainline or []) if isinstance(m, dict)]
            undone = [m for m in ml if not m.get("done")]
            if not undone or random.random() >= self.MAINLINE_ECHO_P:
                return ""
            r = await self.brain.mainline_echo(world=w, char=ch, stage=undone[0], ctx=ctx)
            if r.ok and r.data.get("narration"):
                self.db.append_log(gid, uid, "misc",
                                   f"【主线回响】{str(r.data['narration'])[:90]}", w.name)
                return str(r.data["narration"])
        except Exception:
            return ""
        return ""

    def _avatar_map(self, gid: str) -> dict:
        """群内所有 OC 的名字→头像路径(仅已设置头像者),供 IM 对话气泡使用。"""
        return {c.name: c.avatar for c in self.db.list_chars(gid) if c.avatar}

    async def world_memory_panel(self, gid: str, world_name: str, k: int = 5) -> list[str]:
        """世界记忆面板:世界范畴(world/npc 等)的最近大事记,供「分身 世界」展示。"""
        try:
            rows = await self.mem.related(gid, f"《{world_name}》 世界 主线 NPC 大事记",
                                          uid="", scopes=["world", "npc", "misc"], k=k)
        except Exception:
            return []
        if not rows:
            # fallback:最近的非 char 记忆行(世界级事件/流转)
            try:
                fres = self.db.mem_rows(gid, scopes=["world", "npc"])
                rows = [r["text"] for r in (fres or [])][-k:][::-1]
            except Exception:
                rows = []
        return list(rows)


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
