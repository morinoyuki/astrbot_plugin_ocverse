"""后台管理:注册进 AstrBot Dashboard 的插件 Web API(4.27+)。

- 页面:pages/admin/index.html,由 Dashboard 插件页面机制自动发现并内嵌展示
  (自动注入 bridge SDK,页面内经 AstrBotPluginPage.apiGet/apiPost 调用,
  鉴权由 Dashboard 统一处理,无需插件自行做令牌)。
- 接口:context.register_web_api 注册在 /<插件名>/admin/api/*,
  等效 URL:/api/v1/plugins/extensions/<插件名>/admin/api/*。
- 触发/删除等破坏性操作经 ops 注入(main 层持锁并广播)。

设计约定:依赖 astrbot.api.web(4.27+)与 starlette;game/db 注入运行,可独立测试。
"""

from __future__ import annotations

from dataclasses import asdict

from astrbot.api import logger
from astrbot.api.web import request as web_req
from starlette.responses import HTMLResponse

# 群配置可编辑的白名单(键 → 钳制范围)
_CFG_FIELDS = {
    "event_min": (0, 50),
    "event_max": (0, 50),
    "shift_percent": (0, 100),
    "user_world_share": (0, 100),
    "travel_cooldown_h": (0, 168),
}

# 角色可编辑白名单:标量直接写,tags/attrs/flags 由 update_char 自动 json
_CHAR_SIMPLE = ("name", "gender", "title", "backstory")
_CHAR_INT = ("level", "exp", "gold", "mood", "stamina")
_CHAR_STRUCT = ("attrs", "flags")


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


class AdminPanel:
    """把管理 API 注册进 Dashboard。ops: trigger(gid,kind) / delete_char(gid,uid)。"""

    def __init__(self, db, game, cfg_get, ops=None, plugin_name: str = ""):
        self.db = db
        self.game = game
        self._cfg = cfg_get
        self.ops = ops
        self.plugin_name = plugin_name

    # ── 注册 ──────────────────────────────────────────────────
    def register(self, context) -> int:
        """注册全部管理路由,返回注册数量(admin_enable=false 时不注册)。"""
        if not self._cfg("admin_enable", True):
            return 0
        base = f"/{self.plugin_name}/admin"
        reg = context.register_web_api
        reg(f"{base}/api/overview", self.api_overview, ["GET"], "管理:总览")
        reg(f"{base}/api/chars", self.api_chars, ["GET"], "管理:角色列表")
        reg(f"{base}/api/char", self.api_char_detail, ["GET"], "管理:角色详情")
        reg(f"{base}/api/char", self.api_char_edit, ["POST"], "管理:编辑角色")
        reg(f"{base}/api/char/delete", self.api_char_delete, ["POST"], "管理:删除角色")
        reg(f"{base}/api/world", self.api_world, ["GET"], "管理:世界与NPC")
        reg(f"{base}/api/world", self.api_world_edit, ["POST"], "管理:编辑世界/NPC")
        reg(f"{base}/api/events", self.api_events, ["GET"], "管理:事件列表")
        reg(f"{base}/api/event/expire", self.api_event_expire, ["POST"], "管理:事件收场")
        reg(f"{base}/api/logs", self.api_logs, ["GET"], "管理:时间线日志")
        reg(f"{base}/api/memories", self.api_memories, ["GET"], "管理:记忆列表")
        reg(f"{base}/api/memory/delete", self.api_mem_delete, ["POST"], "管理:删除记忆")
        reg(f"{base}/api/config", self.api_config, ["GET"], "管理:群参数")
        reg(f"{base}/api/config", self.api_config_edit, ["POST"], "管理:编辑群参数")
        reg(f"{base}/api/rel", self.api_rel, ["POST"], "管理:编辑羁绊")
        reg(f"{base}/api/trigger", self.api_trigger, ["POST"], "管理:手动触发")
        logger.info("ocverse: 后台管理 API 已注册(Dashboard → 插件页面)")
        return 16

    # ── 工具 ──────────────────────────────────────────────────
    def _gid(self) -> str:
        return str(web_req.query.get("gid") or "")

    async def _body(self) -> dict:
        d = await web_req.json()
        return d if isinstance(d, dict) else {}

    @staticmethod
    def _ok(data=None):
        return {"ok": True, "data": data if data is not None else {}}

    @staticmethod
    def _err(msg: str):
        return {"ok": False, "error": msg}

    # ── 序列化 ────────────────────────────────────────────────
    @staticmethod
    def _char_json(c) -> dict:
        d = asdict(c)
        d.pop("vec", None)
        return d

    @staticmethod
    def _world_json(w) -> dict:
        return asdict(w)

    @staticmethod
    def _event_json(e) -> dict:
        return {"id": e.id, "uid": e.uid, "kind": e.kind, "state": e.state,
                "title": (e.payload or {}).get("title", ""),
                "scene": (e.payload or {}).get("scene", ""),
                "options": (e.payload or {}).get("options", []),
                "chosen": e.chosen, "result": e.result, "expires_at": e.expires_at}

    # ── 总览 ──────────────────────────────────────────────────
    async def api_overview(self):
        groups = []
        for g in self.db.list_groups():
            gid = g["gid"]
            w = self.db.cur_world(gid)
            chars = self.db.list_chars(gid)
            groups.append({
                "gid": gid,
                "world": {"id": w.id, "name": w.name, "genre": w.genre} if w else None,
                "chars": [{"uid": c.uid, "name": c.name} for c in chars],
                "pending_events": self.db.count_pending_events(gid),
                "config": {k: g.get(k) for k in _CFG_FIELDS},
            })
        return self._ok({"groups": groups})

    # ── 角色 ──────────────────────────────────────────────────
    async def api_chars(self):
        gid = self._gid()
        chars = [self._char_json(c) for c in self.db.list_chars(gid)]
        return self._ok({"chars": chars})

    async def api_char_detail(self):
        gid, uid = self._gid(), str(web_req.query.get("uid") or "")
        c = self.db.get_char(gid, uid)
        if not c:
            return self._err("角色不存在")
        rels = []
        for o in self.db.list_chars(gid):
            if o.uid == uid:
                continue
            full = self.db.get_rel_full(gid, uid, o.uid)
            rels.append({"uid": o.uid, "name": o.name,
                         "score": int(full.get("score", 0) or 0),
                         "state": str(full.get("state", "") or "")})
        logs = self.db.recent_logs(gid, uid, limit=30)
        mems = [m for m in self.db.mem_rows(gid) if m.get("uid") == uid][-30:][::-1]
        for m in mems:
            m.pop("vec", None)
        return self._ok({"char": self._char_json(c), "rels": rels,
                         "logs": logs, "memories": mems})

    async def api_char_edit(self):
        gid, uid = self._gid(), str(web_req.query.get("uid") or "")
        if not self.db.get_char(gid, uid):
            return self._err("角色不存在")
        body = await self._body()
        fields = {}
        for k in _CHAR_SIMPLE:
            if k in body and body[k] is not None:
                fields[k] = str(body[k])[:1200]
        for k in _CHAR_INT:
            if k in body:
                try:
                    fields[k] = int(body[k])
                except (TypeError, ValueError):
                    return self._err(f"{k} 应为整数")
        for k in _CHAR_STRUCT:
            if k in body and isinstance(body[k], dict):
                fields[k] = body[k]
        if "tags" in body and isinstance(body["tags"], list):
            fields["tags"] = [str(t)[:32] for t in body["tags"][:10]]
        if not fields:
            return self._err("没有可更新的字段")
        self.db.update_char(gid, uid, **fields)
        return self._ok(self._char_json(self.db.get_char(gid, uid)))

    async def api_char_delete(self):
        if self.ops is None:
            return self._err("未接入删除操作")
        body = await self._body()
        gid, uid = str(body.get("gid") or ""), str(body.get("uid") or "")
        if not gid or not uid:
            return self._err("需要 gid 与 uid")
        try:
            name = await self.ops.delete_char(gid, uid)
        except Exception as e:
            return self._err(str(e))
        return self._ok({"deleted": name})

    # ── 世界 / NPC ────────────────────────────────────────────
    async def api_world(self):
        gid = self._gid()
        worlds = [self._world_json(w) for w in self.db.list_worlds(gid)]
        return self._ok({"worlds": worlds})

    async def api_world_edit(self):
        gid = self._gid()
        w = self.db.cur_world(gid)
        if not w:
            return self._err("该群尚未初始化世界")
        body = await self._body()
        fields = {}
        for k in ("name", "genre", "atmosphere"):
            if k in body and body[k] is not None:
                fields[k] = str(body[k])[:200]
        if "desc" in body and body["desc"] is not None:
            fields["desc"] = str(body["desc"])[:1200]
        for k in ("rules", "features"):
            if k in body and isinstance(body[k], list):
                fields[k] = [str(x)[:200] for x in body[k][:20]]
        if "npcs" in body and isinstance(body["npcs"], list):
            npcs = []
            for n in body["npcs"][:50]:
                if not isinstance(n, dict) or not str(n.get("name", "")).strip():
                    continue
                npcs.append({
                    "name": str(n.get("name"))[:20], "role": str(n.get("role", ""))[:60],
                    "persona": str(n.get("persona", ""))[:300], "hook": str(n.get("hook", ""))[:200],
                    "daily": str(n.get("daily", ""))[:200], "quirk": str(n.get("quirk", ""))[:200],
                    "builtin": 1 if n.get("builtin") else 0,
                })
            names = [n["name"] for n in npcs]
            if len(names) != len(set(names)):
                return self._err("NPC 名字重复")
            fields["npcs"] = npcs
        if not fields:
            return self._err("没有可更新的字段")
        self.db.update_world(w.id, **fields)
        return self._ok(self._world_json(self.db.get_world(w.id)))

    # ── 事件 ──────────────────────────────────────────────────
    async def api_events(self):
        evs = [self._event_json(e) for e in self.db.list_events(self._gid(), 60)]
        return self._ok({"events": evs})

    async def api_event_expire(self):
        body = await self._body()
        try:
            eid = int(body.get("id"))
        except (TypeError, ValueError):
            return self._err("需要事件 id")
        ok = self.db.expire_event(eid)
        return self._ok({"expired": bool(ok)}) if ok else self._err("事件不存在或已结束")

    # ── 日志 / 记忆 ───────────────────────────────────────────
    async def api_logs(self):
        gid = self._gid()
        uid = str(web_req.query.get("uid") or "") or None
        try:
            limit = _clamp(int(web_req.query.get("limit", 50)), 1, 200)
            offset = _clamp(int(web_req.query.get("offset", 0)), 0, 100000)
        except ValueError:
            return self._err("limit/offset 应为整数")
        rows = self.db.recent_logs(gid, uid, limit=limit, offset=offset)
        return self._ok({"logs": rows, "total": self.db.count_logs(gid, uid)})

    async def api_memories(self):
        gid, uid = self._gid(), str(web_req.query.get("uid") or "")
        rows = self.db.mem_rows(gid)
        if uid:
            rows = [m for m in rows if m.get("uid") == uid]
        for m in rows:
            m.pop("vec", None)
        return self._ok({"memories": rows[::-1]})

    async def api_mem_delete(self):
        body = await self._body()
        ids = [int(i) for i in (body.get("ids") or []) if str(i).lstrip("-").isdigit()]
        if not ids:
            return self._err("没有要删除的记忆 id")
        self.db.mem_delete_ids(ids)
        return self._ok({"deleted": len(ids)})

    # ── 群配置 / 羁绊 / 触发 ──────────────────────────────────
    async def api_config(self):
        row = self.db.get_group(self._gid())
        if not row:
            return self._err("群不存在")
        g = dict(row)
        return self._ok({k: g.get(k) for k in _CFG_FIELDS})

    async def api_config_edit(self):
        gid = self._gid()
        body = await self._body()
        fields = {}
        for k, (lo, hi) in _CFG_FIELDS.items():
            if k in body:
                try:
                    fields[k] = _clamp(int(body[k]), lo, hi)
                except (TypeError, ValueError):
                    return self._err(f"{k} 应为整数")
        if not fields:
            return self._err("没有可更新的字段")
        if int(fields.get("event_min", 0)) > int(fields.get("event_max", 0)):
            return self._err("事件数下限不能大于上限")
        self.db.update_group(gid, **fields)
        g = dict(self.db.get_group(gid))
        return self._ok({k: g.get(k) for k in _CFG_FIELDS})

    async def api_rel(self):
        gid = self._gid()
        body = await self._body()
        a, b = str(body.get("a", "")), str(body.get("b", ""))
        if not a or not b or a == b:
            return self._err("需要 a、b 两个不同的角色 uid")
        if not (self.db.get_char(gid, a) and self.db.get_char(gid, b)):
            return self._err("角色不存在")
        if "score" in body:
            self.db.set_rel_score(gid, a, b, _clamp(int(body["score"]), -100, 100))
        if body.get("state") is not None:
            st = str(body["state"])
            if st not in ("", "crush", "lovers", "couple", "married"):
                return self._err("state 仅支持:空/crush/lovers/couple/married")
            self.db.set_rel_state(gid, a, b, st)
        return self._ok(self.db.get_rel_full(gid, a, b))

    async def api_trigger(self):
        if self.ops is None:
            return self._err("未接入触发操作")
        body = await self._body()
        kind = str(body.get("kind", "event"))
        gid = str(body.get("gid") or self._gid())
        if kind not in ("event", "shift", "morning"):
            return self._err("kind 仅支持:event/shift/morning")
        if not gid:
            return self._err("需要 gid")
        try:
            msg = await self.ops.trigger(gid, kind)
        except Exception as e:
            return self._err(str(e))
        return self._ok({"message": msg})


def build_page_html() -> str:
    """供 pages/admin/index.html 无法被 Dashboard 发现时的兜底(当前页面直接以文件形式提供)。"""
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "pages" / "admin" / "index.html"
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return "<h1>ocverse admin page missing</h1>"


def page_html_handler(panel: "AdminPanel"):
    """GET /<插件名>/admin/page —— 直接输出管理页 HTML(备用入口,
    正常情况请走 Dashboard 的「插件 → 页面」,那里有主题与鉴权桥接)。"""

    async def handler():
        _ = panel
        return HTMLResponse(build_page_html(), status_code=200)

    return handler
