"""后台管理:注册进 AstrBot Dashboard 的插件 Web API(4.27+)。

- 页面:pages/admin/index.html(+ style.css / app.js),由 Dashboard 插件页面
  机制自动发现并以 iframe + bridge SDK 方式加载(鉴权由 Dashboard 统一处理);
  页面 JS 只通过 AstrBotPluginPage.apiGet/apiPost 调用本模块接口。
- 接口:context.register_web_api 注册在 /<插件名>/admin/api/*,
  等效 URL:/api/v1/plugins/extensions/<插件名>/admin/api/*。
  返回裸数据字典(bridge 会原样递给页面);错误走 error_response envelope,
  页面端会收到抛出的 Error(message)。
- 触发/删除等破坏性操作经 ops 注入(main 层持锁并广播)。

设计约定:依赖 astrbot.api.web(4.27+)与 starlette;db/game 注入运行,可独立测试。
"""

from __future__ import annotations

from dataclasses import asdict

from astrbot.api import logger
from astrbot.api.web import error_response, request as web_req

from .config import INFRA_MAX

# 群配置可编辑的白名单(键 → 钳制范围)
_CFG_FIELDS = {
    "event_min": (0, 50),
    "event_max": (0, 50),
    "shift_percent": (0, 100),
    "user_world_share": (0, 100),
    "travel_cooldown_h": (0, 168),
}

# 角色可编辑白名单:name/gender/title/backstory 分别限长(与创角/编辑指令一致),
# tags/attrs/flags 由 update_char 自动 json
_CHAR_SIMPLE = (("name", 24), ("gender", 12), ("title", 32), ("backstory", 4000))
_CHAR_INT = ("level", "exp", "gold", "mood", "stamina", "hp")
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
        routes = (
            (f"{base}/api/overview", self.api_overview, ["GET"], "总览"),
            (f"{base}/api/chars", self.api_chars, ["GET"], "角色列表"),
            (f"{base}/api/char", self.api_char_detail, ["GET"], "角色详情"),
            (f"{base}/api/char", self.api_char_edit, ["POST"], "编辑角色"),
            (f"{base}/api/char/delete", self.api_char_delete, ["POST"], "删除角色"),
            (f"{base}/api/world", self.api_world, ["GET"], "世界与NPC"),
            (f"{base}/api/world", self.api_world_edit, ["POST"], "编辑世界/NPC"),
            (f"{base}/api/events", self.api_events, ["GET"], "事件列表"),
            (f"{base}/api/event/expire", self.api_event_expire, ["POST"], "事件收场"),
            (f"{base}/api/infra/regen", self.api_infra_regen, ["POST"], "AI 重新生成世界设施"),
            (f"{base}/api/content/regen", self.api_content_regen, ["POST"], "AI 重绘危险区域与治疗物品"),
            (f"{base}/api/logs", self.api_logs, ["GET"], "时间线日志"),
            (f"{base}/api/memories", self.api_memories, ["GET"], "记忆列表"),
            (f"{base}/api/memory/delete", self.api_mem_delete, ["POST"], "删除记忆"),
            (f"{base}/api/kb", self.api_kb, ["GET"], "知识库列表"),
            (f"{base}/api/kb/delete", self.api_kb_delete, ["POST"], "删除知识库条目"),
            (f"{base}/api/config", self.api_config, ["GET"], "群参数"),
            (f"{base}/api/config", self.api_config_edit, ["POST"], "编辑群参数"),
            (f"{base}/api/rel", self.api_rel, ["POST"], "编辑羁绊"),
            (f"{base}/api/trigger", self.api_trigger, ["POST"], "手动触发"),
        )
        n = 0
        for route, handler, methods, desc in routes:
            try:
                reg(route, handler, methods, f"ocverse: {desc}")
                n += 1
            except Exception as e:
                logger.warning(f"ocverse: 注册 Web API {route} 失败: {e}")
        if n:
            logger.info(f"ocverse: 后台管理 API 已注册 {n} 条(Dashboard → 插件页面)")
        return n

    # ── 工具 ──────────────────────────────────────────────────
    def _gid(self) -> str:
        try:
            return str(web_req.query.get("gid") or "")
        except RuntimeError:  # 测试直调时无绑定请求
            return ""

    async def _body(self) -> dict:
        try:
            d = await web_req.json(default=None)
        except RuntimeError:
            return {}
        return d if isinstance(d, dict) else {}

    async def _req_ids(self) -> tuple[str, str]:
        """(gid, uid):POST 的 bridge 调用只带 JSON body 不带 query,
        因此 POST 处理器必须优先从 body 取 gid/uid(GET 保持 query 兼容)。"""
        body = await self._body()
        gid = str(body.get("gid") or "") or self._gid()
        uid = str(body.get("uid") or "") or str(web_req.query.get("uid") or "")
        return gid, uid

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
        return {"groups": groups}

    # ── 角色 ──────────────────────────────────────────────────
    async def api_chars(self):
        gid = self._gid()
        return {"chars": [self._char_json(c) for c in self.db.list_chars(gid)]}

    async def api_char_detail(self):
        gid, uid = self._gid(), str(web_req.query.get("uid") or "")
        c = self.db.get_char(gid, uid)
        if not c:
            return error_response("角色不存在", status_code=404)
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
        items = self.db.items_list(gid, uid)
        return {"char": self._char_json(c), "rels": rels, "logs": logs,
                "memories": mems, "items": items}

    async def api_char_edit(self):
        gid, uid = await self._req_ids()
        if not self.db.get_char(gid, uid):
            return error_response("角色不存在", status_code=404)
        body = await self._body()
        fields = {}
        for k, cap in _CHAR_SIMPLE:
            if k in body and body[k] is not None:
                fields[k] = str(body[k])[:cap]
        for k in _CHAR_INT:
            if k in body:
                try:
                    fields[k] = int(body[k])
                except (TypeError, ValueError):
                    return error_response(f"{k} 应为整数", status_code=400)
        for k in _CHAR_STRUCT:
            if k in body and isinstance(body[k], dict):
                fields[k] = body[k]
        if "tags" in body and isinstance(body["tags"], list):
            fields["tags"] = [str(t)[:32] for t in body["tags"][:10]]
        if not fields:
            return error_response("没有可更新的字段", status_code=400)
        self.db.update_char(gid, uid, **fields)
        return self._char_json(self.db.get_char(gid, uid))

    async def api_char_delete(self):
        if self.ops is None:
            return error_response("未接入删除操作", status_code=400)
        body = await self._body()
        gid, uid = str(body.get("gid") or ""), str(body.get("uid") or "")
        if not gid or not uid:
            return error_response("需要 gid 与 uid", status_code=400)
        try:
            name = await self.ops.delete_char(gid, uid)
        except Exception as e:
            return error_response(str(e), status_code=400)
        return {"deleted": name}

    # ── 世界 / NPC ────────────────────────────────────────────
    async def api_world(self):
        gid = self._gid()
        return {"worlds": [self._world_json(w) for w in self.db.list_worlds(gid)]}

    async def api_world_edit(self):
        gid = self._gid()
        body = await self._body()
        if not gid:
            gid = str(body.get("gid") or "")
        # 编辑目标:默认当前世界;传 world_id 可编辑该群任意世界(含沉眠中)
        wid = body.get("world_id")
        if wid is not None:
            try:
                w = self.db.get_world(int(wid))
            except (TypeError, ValueError):
                return error_response("world_id 应为整数", status_code=400)
            if not w or w.gid != gid:
                return error_response("世界不存在或不属于该群", status_code=404)
        else:
            w = self.db.cur_world(gid)
            if not w:
                return error_response("该群尚未初始化世界", status_code=404)
        fields = {}
        # 上限与生成时 _norm_world/_norm_infra 对齐,避免管理页保存被意外截断
        for k, cap in (("name", 16), ("genre", 20), ("atmosphere", 60)):
            if k in body and body[k] is not None:
                fields[k] = str(body[k])[:cap]
        if "desc" in body and body["desc"] is not None:
            fields["desc"] = str(body["desc"])[:4000]
        for k, cap, cnt in (("rules", 40, 4), ("features", 50, 5)):
            if k in body and isinstance(body[k], list):
                fields[k] = [str(x)[:cap] for x in body[k][:cnt]]
        if "infra" in body and isinstance(body["infra"], list):
            items = []
            for it in body["infra"][:INFRA_MAX]:
                if not isinstance(it, dict) or not str(it.get("name", "")).strip():
                    continue
                items.append({
                    "kind": str(it.get("kind", "设施"))[:10],
                    "name": str(it.get("name"))[:16],
                    "desc": str(it.get("desc", ""))[:90],
                    "work": str(it.get("work", ""))[:40],
                })
            names = [i["name"] for i in items]
            if len(names) != len(set(names)):
                return error_response("设施名字重复", status_code=400)
            fields["infra"] = items
        if "zones" in body and isinstance(body["zones"], list):
            zones = []
            for z in body["zones"][:16]:
                if not isinstance(z, dict) or not str(z.get("name", "")).strip():
                    continue
                try:
                    danger = max(1, min(5, int(z.get("danger") or 1)))
                except (TypeError, ValueError):
                    danger = 1
                enemies = []
                for e in (z.get("enemies") or [])[:3]:
                    if isinstance(e, dict) and str(e.get("name", "")).strip():
                        enemies.append({"name": str(e["name"])[:10], "desc": str(e.get("desc", ""))[:30]})
                zones.append({
                    "kind": str(z.get("kind", "区域"))[:10],
                    "name": str(z.get("name"))[:12],
                    "desc": str(z.get("desc", ""))[:60],
                    "danger": danger,
                    "enemies": enemies,
                    "loot": [str(x)[:10] for x in (z.get("loot") or [])[:3] if str(x).strip()],
                })
            names = [z["name"] for z in zones]
            if len(names) != len(set(names)):
                return error_response("区域名字重复", status_code=400)
            fields["zones"] = zones
        if "heal_items" in body and isinstance(body["heal_items"], list):
            heals = []
            for h in body["heal_items"][:5]:
                if not isinstance(h, dict) or not str(h.get("name", "")).strip():
                    continue
                try:
                    heal = max(10, min(200, int(h.get("heal") or 30)))
                except (TypeError, ValueError):
                    heal = 30
                try:
                    price = max(10, min(500, int(h.get("price") or heal)))
                except (TypeError, ValueError):
                    price = heal
                heals.append({"name": str(h.get("name"))[:10], "note": str(h.get("note", ""))[:24],
                              "price": price, "heal": heal})
            fields["heal_items"] = heals
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
                return error_response("NPC 名字重复", status_code=400)
            fields["npcs"] = npcs
        if not fields:
            return error_response("没有可更新的字段", status_code=400)
        self.db.update_world(w.id, **fields)
        return self._world_json(self.db.get_world(w.id))

    # ── 事件 ──────────────────────────────────────────────────
    async def api_events(self):
        evs = [self._event_json(e) for e in self.db.list_events(self._gid(), 60)]
        return {"events": evs}

    async def api_event_expire(self):
        body = await self._body()
        try:
            eid = int(body.get("id"))
        except (TypeError, ValueError):
            return error_response("需要事件 id", status_code=400)
        ok = self.db.expire_event(eid)
        if not ok:
            return error_response("事件不存在或已结束", status_code=400)
        return {"expired": True}

    async def api_infra_regen(self):
        """AI 重新生成世界设施(贴合世界观,保证生存基线与打工位)。
        body/query: gid 必填;world_id 可选(默认当前世界)。"""
        if self.ops is None:
            return error_response("未接入重新生成操作", status_code=400)
        body = await self._body()
        gid = self._gid() or str(body.get("gid") or "")
        world_id = body.get("world_id")
        if not gid:
            return error_response("需要 gid", status_code=400)
        try:
            wid = int(world_id) if world_id is not None else None
        except (TypeError, ValueError):
            return error_response("world_id 应为整数", status_code=400)
        try:
            return await self.ops.regen_infra(gid, wid)
        except Exception as e:
            return error_response(str(e), status_code=400)

    async def api_content_regen(self):
        """AI 重绘危险区域与治疗物品(贴合世界观)。
        body/query: gid 必填;world_id 可选(默认当前世界)。"""
        if self.ops is None or not hasattr(self.ops, "regen_content"):
            return error_response("未接入重绘操作", status_code=400)
        body = await self._body()
        gid = self._gid() or str(body.get("gid") or "")
        world_id = body.get("world_id")
        if not gid:
            return error_response("需要 gid", status_code=400)
        try:
            wid = int(world_id) if world_id is not None else None
        except (TypeError, ValueError):
            return error_response("world_id 应为整数", status_code=400)
        try:
            return await self.ops.regen_content(gid, wid)
        except Exception as e:
            return error_response(str(e), status_code=400)

    # ── 日志 / 记忆 ───────────────────────────────────────────
    async def api_logs(self):
        gid = self._gid()
        uid = str(web_req.query.get("uid") or "") or None
        try:
            limit = _clamp(int(web_req.query.get("limit", 50)), 1, 200)
            offset = _clamp(int(web_req.query.get("offset", 0)), 0, 100000)
        except ValueError:
            return error_response("limit/offset 应为整数", status_code=400)
        rows = self.db.recent_logs(gid, uid, limit=limit, offset=offset)
        return {"logs": rows, "total": self.db.count_logs(gid, uid)}

    async def api_memories(self):
        gid, uid = self._gid(), str(web_req.query.get("uid") or "")
        rows = self.db.mem_rows(gid)
        if uid:
            rows = [m for m in rows if m.get("uid") == uid]
        for m in rows:
            m.pop("vec", None)
        return {"memories": rows[::-1]}

    async def api_mem_delete(self):
        body = await self._body()
        ids = [int(i) for i in (body.get("ids") or []) if str(i).lstrip("-").isdigit()]
        if not ids:
            return error_response("没有要删除的记忆 id", status_code=400)
        self.db.mem_delete_ids(ids)
        return {"deleted": len(ids)}

    # ── 知识库(素材库:所有生成功能共享的世界观素材)──
    @staticmethod
    def _kb_json(r) -> dict:
        return {"id": r["id"], "source": r.get("source") or "", "theme": r.get("theme") or "",
                "kind": r.get("kind") or "", "content": r.get("content") or "",
                "created_at": r.get("created_at")}

    async def api_kb(self):
        """查看当前群知识库(采集的著作/设定素材,注入世界生成/事件/任务等)。"""
        gid = self._gid()
        rows = self.db.kb_rows(gid)
        return {"entries": [self._kb_json(r) for r in rows][::-1],
                "total": len(rows),
                "sources": sorted({r.get("source") or "" for r in rows} - {""})}

    async def api_kb_delete(self):
        body = await self._body()
        ids = [int(i) for i in (body.get("ids") or []) if str(i).lstrip("-").isdigit()]
        if not ids:
            return error_response("没有要删除的知识库 id", status_code=400)
        n = self.db.kb_delete_ids(ids)
        return {"deleted": n}

    # ── 群配置 / 羁绊 / 触发 ──────────────────────────────────
    async def api_config(self):
        row = self.db.get_group(self._gid())
        if not row:
            return error_response("群不存在", status_code=404)
        g = dict(row)
        return {k: g.get(k) for k in _CFG_FIELDS}

    async def api_config_edit(self):
        gid = self._gid()
        body = await self._body()
        if not gid:
            gid = str(body.get("gid") or "")
        fields = {}
        for k, (lo, hi) in _CFG_FIELDS.items():
            if k in body:
                try:
                    fields[k] = _clamp(int(body[k]), lo, hi)
                except (TypeError, ValueError):
                    return error_response(f"{k} 应为整数", status_code=400)
        if not fields:
            return error_response("没有可更新的字段", status_code=400)
        # min>max 校验:未提交的一侧用当前库里的值比较(而非默认 0)
        if "event_min" in fields or "event_max" in fields:
            row = dict(self.db.get_group(gid) or {})
            lo = fields.get("event_min", row.get("event_min", 0))
            hi = fields.get("event_max", row.get("event_max", 0))
            if int(lo) > int(hi):
                return error_response("事件数下限不能大于上限", status_code=400)
        self.db.update_group(gid, **fields)
        g = dict(self.db.get_group(gid))
        return {k: g.get(k) for k in _CFG_FIELDS}

    async def api_rel(self):
        gid = self._gid()
        body = await self._body()
        a, b = str(body.get("a", "")), str(body.get("b", ""))
        if not a or not b or a == b:
            return error_response("需要 a、b 两个不同的角色 uid", status_code=400)
        if not (self.db.get_char(gid, a) and self.db.get_char(gid, b)):
            return error_response("角色不存在", status_code=404)
        if "score" in body:
            try:
                score = int(body["score"])
            except (TypeError, ValueError):
                return error_response("score 应为整数", status_code=400)
            self.db.set_rel_score(gid, a, b, _clamp(score, -100, 100))
        if body.get("state") is not None:
            st = str(body["state"])
            if st not in ("", "crush", "lovers", "couple", "married"):
                return error_response("state 仅支持:空/crush/lovers/couple/married", status_code=400)
            self.db.set_rel_state(gid, a, b, st)
        return self.db.get_rel_full(gid, a, b)

    async def api_trigger(self):
        if self.ops is None:
            return error_response("未接入触发操作", status_code=400)
        body = await self._body()
        kind = str(body.get("kind", "event"))
        gid = str(body.get("gid") or self._gid())
        if kind not in ("event", "shift", "morning"):
            return error_response("kind 仅支持:event/shift/morning", status_code=400)
        if not gid:
            return error_response("需要 gid", status_code=400)
        try:
            msg = await self.ops.trigger(gid, kind)
        except Exception as e:
            return error_response(str(e), status_code=400)
        return {"message": msg}
