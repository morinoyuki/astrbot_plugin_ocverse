"""SQLite 存储层。

- WAL 模式,NAS 友好(读写并行不阻塞)
- 单连接 + RLock(插件全部运行在同一事件循环,低频写,足够)
- 内存不缓存业务数据,查库即得,避免多处状态不一致
- 表结构与版本化迁移统一在 migrations.py 管理(本文件只做数据读写)
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from .migrations import apply_migrations
from .models import Char, EventRow, World

def _pack(vec: list[float]) -> bytes:
    import struct

    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob) -> list[float]:
    import struct

    if not blob:
        return []
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _j(v, default):
    if v in (None, ""):
        return default
    try:
        r = json.loads(v)
        return r if r is not None else default
    except Exception:
        return default


class Database:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            c = self.conn.cursor()
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            # 表结构与版本化迁移统一在 migrations.py 维护
            self.migrations = apply_migrations(self.conn)

    def close(self):
        with self._lock:
            try:
                self.conn.commit()
                self.conn.close()
            except Exception:
                pass

    def _ex(self, sql, args=(), fetch: str | None = None):
        with self._lock:
            cur = self.conn.execute(sql, args)
            if fetch == "one":
                r = cur.fetchone()
                self.conn.commit()
                return r
            if fetch == "all":
                r = cur.fetchall()
                self.conn.commit()
                return r
            self.conn.commit()
            return cur

    # ── groups ────────────────────────────────────────────────
    def get_group(self, gid: str):
        return self._ex("SELECT * FROM groups WHERE gid=?", (gid,), "one")

    def ensure_group(self, gid: str, defaults: dict) -> dict:
        """保证群记录存在,返回扁平 dict(供 game 层读取)。"""
        row = self.get_group(gid)
        if row is None:
            self._ex(
                "INSERT INTO groups (gid, event_min, event_max, shift_percent, "
                "user_world_share, travel_cooldown_h, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    gid,
                    defaults.get("event_min", 2),
                    defaults.get("event_max", 4),
                    defaults.get("shift_percent", 8),
                    defaults.get("user_world_share", 40),
                    defaults.get("travel_cooldown_h", 6),
                    time.time(),
                ),
            )
            row = self.get_group(gid)
        return dict(row)

    def update_group(self, gid: str, **fields):
        if not fields:
            return
        cols = ",".join(f"{k}=?" for k in fields)
        self._ex(f"UPDATE groups SET {cols} WHERE gid=?", (*fields.values(), gid))

    def list_groups(self) -> list[dict]:
        rows = self._ex("SELECT * FROM groups WHERE init_done=1", (), "all")
        return [dict(r) for r in rows]

    # ── worlds ────────────────────────────────────────────────
    def add_world(self, w: World) -> int:
        cur = self._ex(
            "INSERT INTO worlds (gid,name,genre,desc,atmosphere,rules,features,npcs,"
            "event_ideas,infra,mainline,source,visited,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                w.gid, w.name, w.genre, w.desc, w.atmosphere,
                json.dumps(w.rules, ensure_ascii=False),
                json.dumps(w.features, ensure_ascii=False),
                json.dumps(w.npcs, ensure_ascii=False),
                json.dumps(w.event_ideas, ensure_ascii=False),
                json.dumps(w.infra, ensure_ascii=False),
                json.dumps(w.mainline, ensure_ascii=False),
                w.source, w.visited, w.created_by, time.time(),
            ),
        )
        return int(cur.lastrowid)

    def update_world(self, wid: int, **fields):
        """fields 允许 rules/features/npcs/event_ideas/infra/mainline(自动 json)。"""
        jsoned = {}
        for k, v in fields.items():
            if k in ("rules", "features", "npcs", "event_ideas", "infra", "mainline"):
                jsoned[k] = json.dumps(v, ensure_ascii=False)
            else:
                jsoned[k] = v
        cols = ",".join(f"{k}=?" for k in jsoned)
        self._ex(f"UPDATE worlds SET {cols} WHERE id=?", (*jsoned.values(), wid))

    def get_world(self, wid: int) -> World | None:
        row = self._ex("SELECT * FROM worlds WHERE id=?", (wid,), "one")
        return World.from_row(row) if row else None

    def cur_world(self, gid: str) -> World | None:
        g = self.get_group(gid)
        if not g or not g["cur_world_id"]:
            return None
        return self.get_world(int(g["cur_world_id"]))

    def list_worlds(self, gid: str, only_visited: bool = False) -> list[World]:
        sql = "SELECT * FROM worlds WHERE gid=?"
        if only_visited:
            sql += " AND visited=1"
        sql += " ORDER BY visited DESC, id ASC"
        return [World.from_row(r) for r in self._ex(sql, (gid,), "all")]

    def count_group_worlds(self, gid: str) -> int:
        r = self._ex("SELECT COUNT(*) c FROM worlds WHERE gid=?", (gid,), "one")
        return int(r["c"])

    # ── chars ─────────────────────────────────────────────────
    def upsert_char(self, ch: Char):
        self._ex(
            "INSERT INTO chars (gid,uid,name,gender,tags,backstory,avatar,attrs,level,"
            "exp,gold,mood,stamina,title,flags,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(gid,uid) DO UPDATE SET "
            "name=excluded.name, gender=excluded.gender, tags=excluded.tags, "
            "backstory=excluded.backstory, avatar=excluded.avatar, attrs=excluded.attrs, "
            "level=excluded.level, exp=excluded.exp, gold=excluded.gold, "
            "mood=excluded.mood, stamina=excluded.stamina, title=excluded.title, "
            "flags=excluded.flags, updated_at=excluded.updated_at",
            (
                ch.gid, ch.uid, ch.name, ch.gender,
                json.dumps(ch.tags, ensure_ascii=False),
                ch.backstory, ch.avatar,
                json.dumps(ch.attrs, ensure_ascii=False),
                ch.level, ch.exp, ch.gold, ch.mood, ch.stamina,
                ch.title,
                json.dumps(ch.flags, ensure_ascii=False),
                ch.created_at, time.time(),
            ),
        )

    def update_char(self, gid: str, uid: str, **fields):
        jsoned = {}
        for k, v in fields.items():
            if k in ("tags", "attrs", "flags"):
                jsoned[k] = json.dumps(v, ensure_ascii=False)
            else:
                jsoned[k] = v
        cols = ",".join(f"{k}=?" for k in jsoned)
        self._ex(
            f"UPDATE chars SET {cols}, updated_at=? WHERE gid=? AND uid=?",
            (*jsoned.values(), time.time(), gid, uid),
        )

    def get_char(self, gid: str, uid: str) -> Char | None:
        row = self._ex("SELECT * FROM chars WHERE gid=? AND uid=?", (gid, uid), "one")
        return Char.from_row(row) if row else None

    def delete_char(self, gid: str, uid: str) -> bool:
        cur = self._ex("DELETE FROM chars WHERE gid=? AND uid=?", (gid, uid))
        return cur.rowcount > 0

    def list_chars(self, gid: str) -> list[Char]:
        rows = self._ex("SELECT * FROM chars WHERE gid=? ORDER BY level DESC", (gid,), "all")
        return [Char.from_row(r) for r in rows]

    def count_chars(self, gid: str) -> int:
        r = self._ex("SELECT COUNT(*) c FROM chars WHERE gid=?", (gid,), "one")
        return int(r["c"])

    def char_recency(self, gid: str) -> dict[str, float]:
        """每个角色最近一次被事件触达的时间(uid -> ts),用于公平挑选事件目标。"""
        rows = self._ex(
            "SELECT uid, MAX(created_at) m FROM events WHERE gid=? AND uid!='' GROUP BY uid",
            (gid,), "all",
        )
        return {r["uid"]: float(r["m"] or 0) for r in rows}

    def get_char_by_name(self, gid: str, name: str) -> Char | None:
        row = self._ex(
            "SELECT * FROM chars WHERE gid=? AND name=? LIMIT 1", (gid, name), "one"
        )
        return Char.from_row(row) if row else None

    # ── rels ──────────────────────────────────────────────────
    @staticmethod
    def _pair(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    def get_rel(self, gid: str, a: str, b: str) -> int:
        a, b = self._pair(a, b)
        row = self._ex("SELECT score FROM rels WHERE gid=? AND a=? AND b=?", (gid, a, b), "one")
        return int(row["score"]) if row else 0

    def get_rel_full(self, gid: str, a: str, b: str) -> dict:
        """完整关系信息:{score, state, crush_by}。state: crush/lovers/couple/married 或空。"""
        a, b = self._pair(a, b)
        row = self._ex(
            "SELECT score, state, crush_by FROM rels WHERE gid=? AND a=? AND b=?",
            (gid, a, b), "one",
        )
        if not row:
            return {"score": 0, "state": "", "crush_by": ""}
        return {"score": int(row["score"] or 0),
                "state": row["state"] or "",
                "crush_by": row["crush_by"] or ""}

    def set_rel_score(self, gid: str, a: str, b: str, score: int):
        a, b = self._pair(a, b)
        self._ex(
            "INSERT INTO rels (gid,a,b,score) VALUES (?,?,?,?) "
            "ON CONFLICT(gid,a,b) DO UPDATE SET score=excluded.score",
            (gid, a, b, max(-100, min(100, int(score)))),
        )

    def set_rel_state(self, gid: str, a: str, b: str, state: str, crush_by: str = ""):
        a, b = self._pair(a, b)
        self._ex(
            "INSERT INTO rels (gid,a,b,score,state,crush_by) VALUES (?,?,?,0,?,?) "
            "ON CONFLICT(gid,a,b) DO UPDATE SET state=excluded.state, crush_by=excluded.crush_by",
            (gid, a, b, state, crush_by),
        )

    def special_partner(self, gid: str, uid: str) -> str | None:
        """uid 的恋人/情侣/伴侣对象 uid(若有)。"""
        row = self._ex(
            "SELECT a,b FROM rels WHERE gid=? AND state IN ('lovers','couple','married') "
            "AND (a=? OR b=?) LIMIT 1",
            (gid, uid, uid), "one",
        )
        if not row:
            return None
        return row["b"] if row["a"] == uid else row["a"]

    def bump_rel(self, gid: str, a: str, b: str, delta: int, note: str = "") -> int:
        if a == b:
            return 0
        a, b = self._pair(a, b)
        self._ex(
            "INSERT INTO rels (gid,a,b,score,note) VALUES (?,?,?,MAX(-100,MIN(100,?)),?) "
            "ON CONFLICT(gid,a,b) DO UPDATE SET "
            "score=MAX(-100,MIN(100,score+excluded.score)), note=excluded.note",
            (gid, a, b, delta, note),
        )
        return self.get_rel(gid, a, b)

    def list_rels_for(self, gid: str, uid: str, k: int = 5) -> list[tuple[str, int]]:
        """返回与 uid 关系最深的 k 个 (对方uid, score),按 |score| 降序。"""
        rows = self._ex(
            "SELECT a,b,score FROM rels WHERE gid=? AND (a=? OR b=?) "
            "ORDER BY ABS(score) DESC LIMIT ?",
            (gid, uid, uid, k), "all",
        )
        out = []
        for r in rows:
            other = r["b"] if r["a"] == uid else r["a"]
            out.append((other, int(r["score"])))
        return out

    # ── plans ─────────────────────────────────────────────────
    def get_plan(self, gid: str, day: str) -> list | None:
        row = self._ex("SELECT items FROM plans WHERE gid=? AND day=?", (gid, day), "one")
        return _j(row["items"], None) if row else None

    def put_plan(self, gid: str, day: str, items: list):
        self._ex(
            "INSERT INTO plans (gid,day,items) VALUES (?,?,?) "
            "ON CONFLICT(gid,day) DO UPDATE SET items=excluded.items",
            (gid, day, json.dumps(items, ensure_ascii=False)),
        )

    def list_plan_days(self) -> list[tuple[str, str]]:
        rows = self._ex("SELECT gid, day FROM plans", (), "all")
        return [(r["gid"], r["day"]) for r in rows]

    # ── events ────────────────────────────────────────────────
    def insert_event(self, ev: EventRow) -> int:
        cur = self._ex(
            "INSERT INTO events (gid,uid,world_id,kind,state,payload,created_at,expires_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                ev.gid, ev.uid, ev.world_id, ev.kind, ev.state,
                json.dumps(ev.payload, ensure_ascii=False),
                time.time(), ev.expires_at,
            ),
        )
        return int(cur.lastrowid)

    def update_event(self, eid: int, **fields):
        if "payload" in fields:
            fields["payload"] = json.dumps(fields["payload"], ensure_ascii=False)
        if "effects" in fields:
            fields["effects"] = json.dumps(fields["effects"], ensure_ascii=False)
        cols = ",".join(f"{k}=?" for k in fields)
        self._ex(f"UPDATE events SET {cols} WHERE id=?", (*fields.values(), eid))

    def latest_pending_event(self, gid: str, uid: str) -> EventRow | None:
        """某人视角下最新的 pending 事件。
        个人事件与群事件混排、纯粹按 id 倒序(= 用户眼前最新展示的那张事件卡);
        只取 sent=1(卡片真正送达过)的事件 —— 从未展示过的卡不可被抉择,
        避免「对着没见过的遭遇做选择」的割裂感。
        同一作用域同时至多一张由 game 层的串行化(新事件顶替旧事件)保证。"""
        row = self._ex(
            "SELECT * FROM events WHERE gid=? AND state='pending' AND sent=1 AND (uid=? OR uid='') "
            "ORDER BY id DESC LIMIT 1",
            (gid, uid), "one",
        )
        return EventRow.from_row(row) if row else None

    def mark_event_sent(self, eid: int) -> bool:
        """标记事件卡片已真正送达群里;只有发送过的事件才可被回落结算。"""
        cur = self._ex("UPDATE events SET sent=1 WHERE id=? AND state='pending'", (eid,))
        return cur.rowcount > 0

    def pending_sent_events(self, gid: str, uid: str) -> list[EventRow]:
        """某人视角下可回落结算的 pending 事件(卡片已送达:个人 + 本群群事件),新→旧。

        引用识别失败/未引用时的兑底候选:仅一张时直接结算,多张则提示引用对应卡。"""
        rows = self._ex(
            "SELECT * FROM events WHERE gid=? AND state='pending' AND sent=1 AND (uid=? OR uid='') "
            "ORDER BY id DESC",
            (gid, uid), "all",
        )
        return [EventRow.from_row(r) for r in rows]

    def get_event(self, eid: int) -> EventRow | None:
        row = self._ex("SELECT * FROM events WHERE id=?", (eid,), "one")
        return EventRow.from_row(row) if row else None

    def expired_pendings(self) -> list[EventRow]:
        rows = self._ex(
            "SELECT * FROM events WHERE state='pending' AND expires_at>0 AND expires_at<?",
            (time.time(),), "all",
        )
        return [EventRow.from_row(r) for r in rows]

    def list_events(self, gid: str, limit: int = 50) -> list[EventRow]:
        """某群最近的事件(新→旧,含 pending/resolved/expired),供后台管理查看。"""
        rows = self._ex(
            "SELECT * FROM events WHERE gid=? ORDER BY id DESC LIMIT ?",
            (gid, max(1, int(limit))), "all",
        )
        return [EventRow.from_row(r) for r in rows]

    def count_pending_events(self, gid: str) -> int:
        r = self._ex(
            "SELECT COUNT(*) AS n FROM events WHERE gid=? AND state='pending'", (gid,), "one"
        )
        return int(r["n"]) if r else 0

    def expire_event(self, eid: int) -> bool:
        """仅当事件仍处于 pending 时标记过期,返回是否成功(防与结算竞态)。"""
        cur = self._ex(
            "UPDATE events SET state='expired', result=? WHERE id=? AND state='pending'",
            ("(超时,平静地过去了)", eid),
        )
        return cur.rowcount > 0

    def resolve_event_if_pending(self, eid: int, chosen: int) -> bool:
        """仅当事件仍处于 pending 时标记为已结算(防双人同时选择竞态)。"""
        cur = self._ex(
            "UPDATE events SET state='resolved', chosen=? WHERE id=? AND state='pending'",
            (chosen, eid),
        )
        return cur.rowcount > 0

    # ── interactions ──────────────────────────────────────────
    def add_interaction(self, gid: str, name: str, descr: str, by: str):
        self._ex(
            "INSERT INTO interactions (gid,name,descr,by,created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(gid,name) DO UPDATE SET descr=excluded.descr",
            (gid, name, descr, by, time.time()),
        )

    def del_interaction(self, gid: str, name: str) -> bool:
        cur = self._ex("DELETE FROM interactions WHERE gid=? AND name=?", (gid, name))
        return cur.rowcount > 0

    # ── 自定义关系(搞怪称谓:爸爸/麻麻/主人/女仆…,亲密关系在代码层拒绝) ──
    def set_bond(self, gid: str, proposer: str, target: str, label: str, status: str = "agreed"):
        """记录/替换某个方向的自定义关系(proposer 是 target 的 label)。"""
        self._ex(
            "INSERT INTO bonds (gid,proposer,target,label,status,created_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(gid,proposer,target) DO UPDATE SET label=excluded.label, "
            "status=excluded.status, created_at=excluded.created_at",
            (gid, proposer, target, label[:24], status, time.time()),
        )

    def get_bond(self, gid: str, proposer: str, target: str) -> dict | None:
        row = self._ex(
            "SELECT * FROM bonds WHERE gid=? AND proposer=? AND target=?",
            (gid, proposer, target), "one",
        )
        return dict(row) if row else None

    def bonds_for(self, gid: str, uid: str) -> list[dict]:
        """某人全部成立的自定义关系(含我是谁的X/谁是我的X)。"""
        rows = self._ex(
            "SELECT * FROM bonds WHERE gid=? AND status='agreed' AND (proposer=? OR target=?) "
            "ORDER BY created_at DESC",
            (gid, uid, uid), "all",
        )
        return [dict(r) for r in rows]

    def list_interactions(self, gid: str) -> list[dict]:
        rows = self._ex(
            "SELECT name,descr,by FROM interactions WHERE gid=? ORDER BY created_at",
            (gid,), "all",
        )
        return [dict(r) for r in rows]

    # ── timeline ──────────────────────────────────────────────
    def append_log(self, gid: str, uid: str, kind: str, text: str, world_name: str = ""):
        self._ex(
            "INSERT INTO timeline (gid,uid,ts,kind,text,world_name) VALUES (?,?,?,?,?,?)",
            (gid, uid, time.time(), kind, text[:500], world_name),
        )

    def recent_logs(self, gid: str, uid: str | None = None, limit: int = 20, offset: int = 0) -> list[dict]:
        if uid:
            rows = self._ex(
                "SELECT * FROM timeline WHERE gid=? AND uid=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (gid, uid, limit, offset), "all",
            )
        else:
            rows = self._ex(
                "SELECT * FROM timeline WHERE gid=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (gid, limit, offset), "all",
            )
        return [dict(r) for r in rows]

    def count_logs(self, gid: str, uid: str | None = None) -> int:
        if uid:
            r = self._ex("SELECT COUNT(*) c FROM timeline WHERE gid=? AND uid=?", (gid, uid), "one")
        else:
            r = self._ex("SELECT COUNT(*) c FROM timeline WHERE gid=?", (gid,), "one")
        return int(r["c"])

    def recent_similar_logs(self, gid: str, uid: str, patterns: list[str], k: int = 3) -> list[str]:
        """最近 k 条同时命中所有 patterns 的日志原文(防复读:喂给 LLM 要求情节明显不同)。"""
        rows = self._ex(
            "SELECT text FROM timeline WHERE gid=? AND uid=? AND kind IN ('interaction','npc','event') "
            "ORDER BY id DESC LIMIT 200",
            (gid, uid), "all",
        )
        pats = [p for p in (patterns or []) if p]
        out = []
        for r in rows:
            t = r["text"] or ""
            if all(p in t for p in pats):
                out.append(t)
                if len(out) >= k:
                    break
        return out

    # ── memories (raw; 语义在 MemoryStore) ─────────────────────
    def mem_add(self, gid: str, uid: str, scope: str, text: str, vec: list[float], ref: str = "") -> int:
        cur = self._ex(
            "INSERT INTO memories (gid,uid,scope,text,vec,ref,created_at) VALUES (?,?,?,?,?,?,?)",
            (gid, uid, scope, text[:600], _pack(vec), ref, time.time()),
        )
        return int(cur.lastrowid)

    def mem_rows(self, gid: str, scopes: list[str] | None = None) -> list[dict]:
        if scopes:
            q = ",".join("?" for _ in scopes)
            rows = self._ex(
                f"SELECT id,uid,scope,text,vec,ref,created_at FROM memories "
                f"WHERE gid=? AND scope IN ({q})",
                (gid, *scopes), "all",
            )
        else:
            rows = self._ex(
                "SELECT id,uid,scope,text,vec,ref,created_at FROM memories WHERE gid=?",
                (gid,), "all",
            )
        out = []
        for r in rows:
            d = dict(r)
            d["vec"] = _unpack(d["vec"])
            out.append(d)
        return out

    def mem_delete_ids(self, ids: list[int]):
        if not ids:
            return
        q = ",".join("?" for _ in ids)
        self._ex(f"DELETE FROM memories WHERE id IN ({q})", tuple(ids))

    def mem_count(self, gid: str, uid: str = "", scope: str = "") -> int:
        sql = "SELECT COUNT(*) c FROM memories WHERE gid=?"
        args: list = [gid]
        if uid:
            sql += " AND uid=?"
            args.append(uid)
        if scope:
            sql += " AND scope=?"
            args.append(scope)
        r = self._ex(sql, tuple(args), "one")
        return int(r["c"])

    # ── kv (杂项状态) ─────────────────────────────────────────
    def kv_set(self, gid: str, key: str, value: str):
        self._ex(
            "INSERT INTO kv (gid,key,value) VALUES (?,?,?) "
            "ON CONFLICT(gid,key) DO UPDATE SET value=excluded.value",
            (gid, key, value),
        )

    def kv_get(self, gid: str, key: str) -> str | None:
        row = self._ex("SELECT value FROM kv WHERE gid=? AND key=?", (gid, key), "one")
        return row["value"] if row else None

    # ── quests (每日小任务) ────────────────────────────────────
    def add_quest(self, gid: str, uid: str, day: str, text: str, hint: str = "",
                  steps: list | None = None, giver: str = "", place: str = "") -> int:
        cur = self._ex(
            "INSERT INTO quests (gid,uid,day,text,hint,steps,giver,place,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (gid, uid, day, text[:48], hint[:60],
             json.dumps(steps or [], ensure_ascii=False),
             (giver or "")[:24], (place or "")[:24], time.time()),
        )
        return int(cur.lastrowid)

    def update_quest_steps(self, qid: int, steps: list):
        """同步任务步骤进度(done 标记)。"""
        self._ex("UPDATE quests SET steps=? WHERE id=?",
                 (json.dumps(steps, ensure_ascii=False), qid))

    # ── 背包(角色物品) ────────────────────────────────────
    def item_add(self, gid: str, uid: str, name: str, count: int = 1, note: str = "") -> int:
        """获得物品:存在则叠加数量并刷新备注,不存在则新建。返回当前数量。"""
        name = (name or "").strip()[:16]
        if not name:
            return 0
        self._ex(
            "INSERT INTO items (gid,uid,name,count,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(gid,uid,name) DO UPDATE SET "
            "count = count + excluded.count, "
            "note = CASE WHEN excluded.note != '' THEN excluded.note ELSE items.note END, "
            "updated_at = excluded.updated_at",
            (gid, uid, name, max(1, int(count)), (note or "")[:40], time.time(), time.time()),
        )
        row = self._ex("SELECT count FROM items WHERE gid=? AND uid=? AND name=?",
                       (gid, uid, name), "one")
        return int(row["count"]) if row else 0

    def item_remove(self, gid: str, uid: str, name: str, count: int = 1) -> bool:
        """失去/消耗物品:数量不足则整体移除。返回是否确实减少。"""
        name = (name or "").strip()[:16]
        row = self._ex("SELECT id,count FROM items WHERE gid=? AND uid=? AND name=?",
                       (gid, uid, name), "one")
        if not row:
            return False
        left = int(row["count"]) - max(1, int(count))
        if left > 0:
            self._ex("UPDATE items SET count=?, updated_at=? WHERE id=?",
                     (left, time.time(), row["id"]))
        else:
            self._ex("DELETE FROM items WHERE id=?", (row["id"],))
        return True

    def items_list(self, gid: str, uid: str) -> list[dict]:
        rows = self._ex(
            "SELECT id,name,count,note,updated_at FROM items "
            "WHERE gid=? AND uid=? ORDER BY updated_at DESC",
            (gid, uid), "all",
        )
        return [dict(r) for r in rows]

    def item_get(self, gid: str, uid: str, name: str) -> dict | None:
        row = self._ex("SELECT * FROM items WHERE gid=? AND uid=? AND name=?",
                       (gid, uid, (name or "").strip()[:16]), "one")
        return dict(row) if row else None

    def list_quests(self, gid: str, uid: str, day: str) -> list[dict]:
        rows = self._ex(
            "SELECT * FROM quests WHERE gid=? AND uid=? AND day=? ORDER BY id",
            (gid, uid, day), "all",
        )
        return [dict(r) for r in rows]

    def resolve_quest_if_open(self, qid: int) -> bool:
        """仅当任务仍 open 时标记完成(防重复结算竞态)。"""
        cur = self._ex("UPDATE quests SET state='done' WHERE id=? AND state='open'", (qid,))
        return cur.rowcount > 0

    def expire_open_quests(self, gid: str, day: str | None = None):
        """世界变动/穿越后,旧世界的待办任务全部作废(可重新领取新世界的)。"""
        if day is not None:
            self._ex("UPDATE quests SET state='expired' WHERE gid=? AND day=? AND state='open'", (gid, day))
        else:
            self._ex("UPDATE quests SET state='expired' WHERE gid=? AND state='open'", (gid,))

    def purge_char_data(self, gid: str, uid: str):
        """删除角色的所有伴生数据:日志/记忆/羁绊/待决事件/任务/自定义关系。"""
        self._ex("DELETE FROM timeline WHERE gid=? AND uid=?", (gid, uid))
        self._ex("DELETE FROM memories WHERE gid=? AND uid=?", (gid, uid))
        self._ex("DELETE FROM rels WHERE gid=? AND (a=? OR b=?)", (gid, uid, uid))
        self._ex("DELETE FROM events WHERE gid=? AND uid=? AND state='pending'", (gid, uid))
        self._ex("DELETE FROM quests WHERE gid=? AND uid=?", (gid, uid))
        self._ex("DELETE FROM items WHERE gid=? AND uid=?", (gid, uid))
        self._ex("DELETE FROM bonds WHERE gid=? AND (proposer=? OR target=?)", (gid, uid, uid))

    # ── 知识库(素材库:联网采集的著作/轻小说等,供所有生成功能注入) ──
    def kb_add(self, gid: str, source: str, theme: str, kind: str, content: str,
               vec: list[float]) -> int:
        cur = self._ex(
            "INSERT INTO kb (gid,source,theme,kind,content,vec,created_at) VALUES (?,?,?,?,?,?,?)",
            (gid, (source or "")[:60], (theme or "")[:30], (kind or "work")[:12],
             (content or "").strip()[:1500], _pack(vec), time.time()),
        )
        return int(cur.lastrowid)

    def kb_rows(self, gid: str) -> list[dict]:
        rows = self._ex("SELECT * FROM kb WHERE gid=? ORDER BY id", (gid,), "all")
        out = []
        for r in rows:
            d = dict(r)
            d["vec"] = _unpack(d["vec"])
            out.append(d)
        return out

    def kb_count(self, gid: str) -> int:
        r = self._ex("SELECT COUNT(*) c FROM kb WHERE gid=?", (gid,), "one")
        return int(r["c"])

    def kb_sources(self, gid: str) -> list[str]:
        rows = self._ex("SELECT source FROM kb WHERE gid=?", (gid,), "all")
        return [r["source"] or "" for r in rows]

    def kb_trim(self, gid: str, keep: int):
        """知识库超上限时,删除最旧的条目,保留最新的 keep 条(按 id 即入库先后)。"""
        keep = max(0, int(keep))
        self._ex(
            "DELETE FROM kb WHERE gid=? AND id NOT IN "
            "(SELECT id FROM kb WHERE gid=? ORDER BY id DESC LIMIT ?)",
            (gid, gid, keep),
        )

    # ── 地块/住宅(住宅区/村庄)──────────────
    def plot_add(self, gid, world_id, bid, kind, name, desc, price):
        cur = self._ex(
            "INSERT INTO plots (gid,world_id,bid,kind,name,desc,price,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (gid, world_id, bid, kind, name[:16], desc[:120], int(price), time.time()),
        )
        return int(cur.lastrowid)

    def plots(self, gid, world_id) -> list[dict]:
        rows = self._ex(
            "SELECT * FROM plots WHERE gid=? AND world_id=? ORDER BY bid", (gid, world_id), "all")
        out = []
        for r in rows:
            d = dict(r)
            d["amenities"] = _j(d.get("amenities"), {})
            out.append(d)
        return out

    def plot_get(self, pid: int) -> dict | None:
        row = self._ex("SELECT * FROM plots WHERE id=?", (pid,), "one")
        if not row:
            return None
        d = dict(row)
        d["amenities"] = _j(d.get("amenities"), {})
        return d

    def plot_update(self, pid: int, **fields):
        jsoned = {}
        for k, v in fields.items():
            if k == "amenities":
                jsoned[k] = json.dumps(v, ensure_ascii=False)
            else:
                jsoned[k] = v
        cols = ",".join(f"{k}=?" for k in jsoned)
        self._ex(f"UPDATE plots SET {cols} WHERE id=?", (*jsoned.values(), pid))
