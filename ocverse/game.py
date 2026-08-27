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


def day_key_of(dt: datetime, rollover_hour: int) -> str:
    """按日切时刻折算"游戏日"。凌晨 rollover_hour 点前仍算前一天。"""
    return (dt - timedelta(hours=rollover_hour)).strftime("%Y-%m-%d")


def _now() -> float:
    return time.time()


def _hhmm_now() -> str:
    return datetime.now().strftime("%H:%M")


class GameError(Exception):
    """面向用户的错误信息。"""


class Game:
    def __init__(self, db: Database, brain: Brain, memory: MemoryStore, cfg_get):
        self.db = db
        self.brain = brain
        self.mem = memory
        self.cfg = cfg_get  # cfg_get(key, default) -> value

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

    # ══════════════ 世界初始化 ══════════════
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
            source=source,
            visited=visited,
            created_by=by,
        )
        w.id = self.db.add_world(w)
        self.db.update_group(gid, cur_world_id=w.id, init_done=1)
        self.db.append_log(gid, "", "arrive", f"世界落成:《{w.name}》[{w.genre}]", w.name)
        return w

    async def init_world(self, gid: str, desc: str | None, by: str) -> dict:
        """管理员初始化/重建群世界。返回抵达 view。"""
        self._ensure_group(gid)
        prev = self.db.cur_world(gid)
        r = await self.brain.gen_world(desc)
        w = self._install_world(gid, r.data, source="llm" if r.ok else "default", by=by)
        if r.ok:
            arr_data = (await self.brain.compose_arrival(world=w, prev_name=prev.name if prev else "", via="init")).data
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
                    tags: list[str], backstory: str) -> Char:
        if not self.is_initialized(gid):
            # 自动落内置默认世界(零 LLM 开销,管理员之后可重建)
            hint = (self.cfg("default_world_hint", "") or "").strip()
            wdata = dict(C.DEFAULT_WORLD)
            if hint:
                wdata["desc"] = hint
            self._install_world(gid, wdata, source="default")
        maxn = self._cfgi("max_chars_per_group", 30)
        if self.db.count_chars(gid) >= maxn:
            raise GameError(f"本群角色数已达上限({maxn}),暂无法创建新角色")
        if self.db.get_char(gid, uid):
            raise GameError("你已经有一个分身了(一人一角色)。可先用「分身 删除角色」")
        if self.db.get_char_by_name(gid, name):
            raise GameError("这个名字已被本群其他分身占用")
        ch = Char(gid=gid, uid=uid, name=name, gender=gender or "保密",
                  tags=[t.strip() for t in tags if t.strip()][:6],
                  backstory=(backstory or "").strip()[:400])
        # 初始六维:由背景气质做个轻量倾斜,其余随机 18~40
        text = f"{name} {' '.join(ch.tags)} {ch.backstory}"
        h = zlib.crc32(text.encode())
        random.seed(h)
        w0 = self.db.cur_world(gid)
        wname = w0.name if w0 else "未知之地"
        for i, k in enumerate(C.ATTR_KEYS):
            base = random.randint(18, 40)
            if text and (i + ord(text[0])) % 3 == 0:
                base += random.randint(4, 9)   # 让每个角色的强项不同
            ch.attrs[k] = min(60, base)
        random.seed()
        self.db.upsert_char(ch)
        self.db.append_log(gid, uid, "create", f"{name} 在《{wname}》降生", wname)
        return ch

    def delete_char(self, gid: str, uid: str) -> str:
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身")
        self.db.delete_char(gid, uid)
        self.db.append_log(gid, uid, "misc", f"分身「{ch.name}」悄然离场")
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
        for ch in self.db.list_chars(gid):
            ch.stamina = min(C.STAMINA_MAX, ch.stamina + 40)
            ch.mood = min(C.MOOD_MAX, max(0, ch.mood + (5 if ch.mood < 40 else 0)))
            self.db.upsert_char(ch)
        self.db.update_group(gid, day_key=day)

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
        chars = self.db.list_chars(gid)
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
        """到点生成一次遭遇,落库并返回卡片 view。

        char_uid: 被动事件引爆时传入说话者 uid —— 事件"是冲着 TA 来的"。
        没有角色时返回 None。
        """
        world = self.db.cur_world(gid)
        chars = self.db.list_chars(gid)
        if not world or not chars:
            return None
        is_group = char_uid is None and random.random() < EVENT_PICK_GROUP_PROB
        char = None
        if not is_group:
            if char_uid:
                char = self.db.get_char(gid, char_uid)
            if char is None:
                char = self._pick_char(gid)
            if char is None:
                is_group = True
        npc = None
        if world.npcs and random.random() < EVENT_NPC_PROB:
            npc = random.choice(world.npcs)
        mems = await self.mem.related(
            gid, f"{char.name if char else '群事件'} {world.name} {npc['name'] if npc else ''}",
            uid=char.uid if char else None,
        )
        r = await self.brain.make_event(world=world, char=char, kind="npc" if npc else ("group" if is_group else "solo"),
                                        npc=npc, memories=mems, ideas=world.event_ideas)
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
            raise GameError("你还没有创建分身,先「分身 创建 名字」")
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
        target_char = self.db.get_char(gid, ev.uid) if ev.uid else None
        r = await self.brain.resolve_event(world=world, char=target_char, event=ev.payload, choice_idx=idx)
        data = r.data
        changes: list[str] = []
        if target_char:
            changes = self._apply_effects(target_char, data.get("effects") or {})
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
        r = await self.brain.resolve_interaction(
            world=world, a=a, b=b, npc=None, mode=mode, detail=detail,
            rel_score=self.db.get_rel(gid, uid_a, uid_b),
        )
        data = r.data
        changes = self._apply_effects(a, data.get("a_effects") or {})
        changes += [f"(对方){x}" for x in self._apply_effects(b, data.get("b_effects") or {})]
        rel = self.db.bump_rel(gid, uid_a, uid_b, int(data.get("rel_delta") or 0), mode)
        # 计数/称号
        a.flags = a.flags or {}
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
            "changes": changes,
            "rel": rel,
            "rel_label": C.rel_label(rel),
            "ok_llm": r.ok,
        }

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
        r = await self.brain.npc_chat(world=world, npc=npc, char=ch, action=action, memories=mems)
        data = r.data
        changes = self._apply_effects(ch, data.get("effects") or {})
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
            "changes": changes,
            "ok_llm": r.ok,
        }

    # ══════════════ 主动行动(练习/健身/打工/打怪/冒险)══════════════
    async def act(self, gid: str, uid: str, act_key: str, detail: str = "") -> dict:
        """玩家主动行动一次。act_key: 预设施名或'冒险'(自定义)。消耗体力+每日次数,概率触发机缘奖励。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("你还没有创建分身,先「分身 创建 名字」")
        world = self.db.cur_world(gid)
        if not world:
            raise GameError("世界尚未初始化,管理员:「分身 初始化世界」")
        preset = C.ACTIONS.get(act_key) or C.ACTIONS["冒险"]
        name = preset["name"]
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
            kind=preset["kind"], memories=mems,
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
            "changes": changes,
            "ok_llm": r.ok,
        }

    # ══════════════ 世界 NPC 自定义(添加/删除/列表)══════════════
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
            raise GameError("世界尚未初始化,管理员:「分身 初始化世界」")
        return target

    async def add_npc(self, gid: str, uid: str, name: str, role: str, persona: str,
                      hook: str, world_ref: str = "") -> tuple[str, dict]:
        """给(当前/指定)世界添加一位 NPC。返回 (world_name, npc)。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            raise GameError("创建分身后才能为世界添加NPC(你是这个世界的住民了)")
        w = self._find_world(gid, world_ref)
        if not name:
            raise GameError("格式:分身 添加NPC <名字> | 职业 | 性格 | 钩子")
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
                r = await self.brain.enrich_user_world(w.name, w.desc)
                if r.ok:
                    d = r.data
                    self.db.update_world(w.id, genre=d.get("genre", w.genre), desc=d.get("desc", w.desc),
                                         atmosphere=d.get("atmosphere", ""), rules=d.get("rules", []),
                                         features=d.get("features", []), npcs=d.get("npcs", []),
                                         event_ideas=d.get("event_ideas", []))
                self.db.update_world(w.id, visited=1)
                self.db.update_group(gid, cur_world_id=w.id)  # 降临即成为当前世界
                world = self.db.get_world(w.id)
        if world is None:
            avoid = [w.name for w in self.db.list_worlds(gid)]
            r = await self.brain.gen_world(None, avoid_names=avoid)
            wdata = r.data
            world = self._install_world(gid, wdata, source="llm" if r.ok else "default")
        self.db.update_group(gid, last_shift_at=_now())
        # 全员穿越奖励 + 计数
        for ch in self.db.list_chars(gid):
            ch.flags = ch.flags or {}
            ch.flags["shifts"] = int(ch.flags.get("shifts", 0)) + 1
            if not ch.flags.get("traveler"):
                ch.flags["traveler"] = 1
                self.db.append_log(gid, ch.uid, "title", f"{ch.name} 获得称号「{C.FLAG_TITLES['traveler']}」")
            ch.mood = min(C.MOOD_MAX, ch.mood + 5)
            ch.exp += 8
            self.db.upsert_char(ch)
            self._check_flags(ch)
        arr = await self.brain.compose_arrival(world=world, prev_name=prev_name, via="shift")
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
            raise GameError("没找到这个世界。注意:只有「穿越过」的世界才能自由穿越(用「分身 世界列表」查看编号)")
        if cur and target_w.id == cur.id:
            raise GameError("你们已经在这个世界了")
        self.db.update_group(gid, cur_world_id=target_w.id, last_travel_at=_now())
        arr = await self.brain.compose_arrival(world=target_w, prev_name=cur.name if cur else "", via="travel")
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
            raise GameError("格式:分身 定义世界 名称 描述…")
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

    # ══════════════ 晨报 ══════════════
    async def fire_morning(self, gid: str) -> dict | None:
        world = self.db.cur_world(gid)
        chars = self.db.list_chars(gid)
        if not world:
            return None
        r = await self.brain.morning_brief(
            world=world, chars=chars,
            day_note=f"{self._day_key()} 第{self._world_day(gid)}天",
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
