"""astrbot_plugin_ocverse · 分身的世界

把群聊变成一个会变动的世界:
- 每人创造一个 OC 分身(性格/背景/头像上传),在群世界里生活
- 每天活跃时段随机触发事件(基于世界观/性格/属性/记忆由 LLM 生成)
- 与群友分身、世界 NPC 互动;羁绊系统
- 小概率世界变动:全员穿越(随机用自设世界或 LLM 生成新世界)
- 自由穿越仅限"穿越过"的世界;世界书可由玩家添加
- 记忆系统:时间线日志 + 轻量语义向量检索(默认零依赖哈希词向量,NAS 友好)
- 纯 Pillow 渲染 IM 聊天卡片,无桌面/无浏览器依赖
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
import time
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType, PermissionType
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig  # noqa: F401
from astrbot.core.message.components import At, Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.quoted_message.extractor import QuotedMessageExtractor

from .ocverse import config as C
from .ocverse.admin import AdminPanel
from .ocverse.avatar_store import AvatarStore
from .ocverse.db import Database
from .ocverse.embedder import make_embedder
from .ocverse.game import Game, GameError, npc_uid
from .ocverse.imcard import (
    fortune_card,
    help_card,
    log_card,
    memory_card,
    profile_card,
    render_views,
    roster_card,
    world_card,
    world_list_card,
)
from .ocverse.llm_engine import Brain
from .ocverse.memory import KnowledgeStore, MemoryStore


def _guard(fn):
    """指令守卫:GameError/意外异常都会以文本回复用户,并记录日志。

    必须放在 @oc.command 之下(更靠近函数)。functools.wraps 保留原签名,
    AstrBot 的指令参数解析(inspect.signature)不受影响。
    """

    @functools.wraps(fn)
    async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
        try:
            async for r in fn(self, event, *args, **kwargs):
                yield r
        except GameError as e:
            yield event.plain_result(f"❌ {e}")
        except Exception as e:
            logger.exception(f"ocverse: 指令执行异常: {e}")
            yield event.plain_result(f"❌ 指令执行出错:{e}")
    return wrapper


AUTHOR = "morinoyuki"
VERSION = "1.0.0"
PLUGIN_NAME = "astrbot_plugin_ocverse"
REPO = "https://github.com/morinoyuki/astrbot_plugin_ocverse"


class OcversePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config

        def _cfg(key, default=None):
            try:
                v = (config or {}).get(key, default)
            except Exception:
                return default
            return default if v is None else v

        self._cfg = _cfg

        data_dir = self._data_dir()
        self.data_dir = data_dir
        self.db = Database(os.path.join(data_dir, "ocverse.sqlite3"))
        for version, desc in getattr(self.db, "migrations", []) or []:
            logger.info(f"ocverse: 数据库迁移 v{version}: {desc}")
        emb, fb = make_embedder(_cfg, lambda: self.context.get_all_embedding_providers())
        self.mem = MemoryStore(self.db, emb, fb, top_k=self._cfgi("memory_top_k", 6))
        self.kb = KnowledgeStore(self.db, emb, fb, top_k=3, max_items=self._cfgi("knowledge_base_max", 40))
        self.brain = Brain(raw_call=self._llm_raw, style_extra=str(_cfg("style_prompt", "") or ""),
                           raw_call_tools=self._llm_raw_enriched)
        self.game = Game(self.db, self.brain, self.mem, _cfg, kb=self.kb)
        self.avatars = AvatarStore(data_dir)

        self._task: asyncio.Task | None = None
        self._admin: AdminPanel | None = None
        self._sem = asyncio.Semaphore(2)
        self._umo_map: dict[str, str] = {}      # gid -> unified_msg_origin(本会话内观察)
        self._pending: dict[str, list] = {}     # gid -> 主动卡片积压(无法主动发时)
        self._glocks: dict[str, asyncio.Lock] = {}  # 每群一把锁:LLM 调用期间锁定改数据的指令,防竞态
        self._confirm: dict[str, float] = {}    # 二次确认状态
        self._default_mode_hint = {m: d for m, d in C.DEFAULT_INTERACTIONS}
        self._web_tools = None  # 联网搜索工具集(懒加载缓存)

    # ═══════════════════════════ 基础设施 ═══════════════════════════
    def _cfgi(self, key, default):
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _data_dir(self) -> str:
        # 4.x: get_data_dir() 自动按插件名创建 data/plugin_data/<插件名>/
        return str(StarTools.get_data_dir())

    def _card_cfg(self) -> dict:
        return {
            "card_width": self._cfgi("card_width", 1024),
            "card_font_size": self._cfgi("card_font_size", 34),
            "card_theme": self._cfg("card_theme", "dark"),
        }

    # ── LLM provider ──────────────────────────────────────────────
    def _web_tool_set(self):
        """收集可用的联网搜索类 LLM 工具(如 astrbot-plugin-tavily 注册的),构建 ToolSet。"""
        if self._web_tools is not None:
            return self._web_tools
        self._web_tools = []  # 空列表也作为"已探测"的缓存
        try:
            pm = getattr(self.context, "provider_manager", None)
            llm_tools = getattr(pm, "llm_tools", None) if pm else None
            get_func = getattr(llm_tools, "get_func", None) if llm_tools else None
            if get_func is None:
                return []
            # 延迟导入,版本不含时走 except 分支
            from astrbot.core.agent.tool import ToolSet
            ts = ToolSet()
            for name in ("web_search_tavily", "web_search", "search_web",
                         "tavily_extract_web_page", "webpage_extract"):
                t = get_func(name)
                if t is not None:
                    ts.add_tool(t)
            self._web_tools = ts if len(ts) else []
        except Exception as e:
            logger.debug(f"ocverse: 构建联网工具集失败: {e}")
            self._web_tools = []
        return self._web_tools

    @staticmethod
    def _extract_completion(resp) -> str:
        """从 tool_loop_agent 的返回中提取最终文本(兼容 LLMResponse / result_chain 形态)。"""
        if resp is None:
            return ""
        t = getattr(resp, "completion_text", None)
        if t:
            return t
        rc = getattr(resp, "result_chain", None)
        if rc is not None:
            try:
                for comp in getattr(rc, "chain", None) or []:
                    if isinstance(comp, Plain):
                        return comp.text or ""
            except Exception:
                pass
        return ""

    async def _llm_raw_enriched(self, system: str, user: str) -> str | None:
        """联网增强通道:tool_loop_agent + 搜索工具,扩充世界/规则等生成的知识面。
        任何不可用(未装搜索插件/版本不支持/失败)都返回 None,由 Brain 自动回退普通通道。"""
        if not self._cfg("web_search_world_gen", True):
            return None
        tools = self._web_tool_set()
        fn = getattr(self.context, "tool_loop_agent", None)
        if not tools or fn is None:
            return None
        try:
            async with self._sem:
                resp = await fn(system_prompt=system, prompt=user, contexts=[], tools=tools,
                                max_steps=self._cfgi("web_search_max_steps", 4))
            return (self._extract_completion(resp) or "").strip() or None
        except TypeError:
            return None  # 版本签名不符 → 回普通通道
        except Exception as e:
            logger.debug(f"ocverse: 联网增强生成失败,回退普通通道: {e}")
            return None

    async def _get_provider(self):
        try:
            pid = str(self._cfg("provider_id", "") or "").strip()
            pm = getattr(self.context, "provider_manager", None)
            if pid and pm is not None and hasattr(pm, "get_provider_by_id"):
                prov = await pm.get_provider_by_id(pid)
                if prov is not None:
                    return prov
            umo = self._umo_map.get("__last__")
            if umo and hasattr(self.context, "get_current_chat_provider_id"):
                try:
                    cpid = await self.context.get_current_chat_provider_id(umo=umo)
                except Exception:
                    cpid = None
                if cpid and pm is not None and hasattr(pm, "get_provider_by_id"):
                    prov = await pm.get_provider_by_id(cpid)
                    if prov is not None:
                        return prov
            gp = getattr(self.context, "get_using_provider", None)
            if gp:
                prov = gp()
                if prov is not None:
                    return prov
            if pm is not None:
                pl = getattr(pm, "providers", None) or []
                if pl:
                    return pl[0]
        except Exception as e:
            logger.debug(f"ocverse: 获取 provider 失败: {e}")
        return None

    async def _llm_raw(self, system: str, user: str) -> str | None:
        prov = await self._get_provider()
        if prov is None:
            logger.warning("ocverse: 无可用 LLM 提供商,本次使用内置降级内容")
            return None
        prompt = f"{system}\n\n{user}" if system else user
        async with self._sem:
            text = None
            for _ in range(2):
                try:
                    resp = await prov.text_chat(prompt=prompt, contexts=[])
                    text = (getattr(resp, "completion_text", None) or "").strip()
                    if text:
                        break
                except TypeError:
                    try:
                        resp = await prov.text_chat(prompt=prompt)
                        text = (getattr(resp, "completion_text", None) or "").strip()
                        if text:
                            break
                    except Exception as e:
                        logger.debug(f"ocverse: LLM 调用失败: {e}")
                except Exception as e:
                    logger.debug(f"ocverse: LLM 调用失败: {e}")
            return text or None

    # ── 消息解析工具 ──────────────────────────────────────────────
    @staticmethod
    def _gid(event: AstrMessageEvent) -> str:
        gid = event.get_group_id()
        return gid or ""

    @staticmethod
    def _uid(event: AstrMessageEvent) -> str:
        return str(event.get_sender_id() or "unknown")

    def _glock(self, gid: str) -> asyncio.Lock:
        """每群一把锁:所有会改动数据的游戏操作在锁内执行,避免并发读-改-写竞态。"""
        lock = self._glocks.get(gid)
        if lock is None:
            lock = asyncio.Lock()
            self._glocks[gid] = lock
        return lock

    def _remember_umo(self, event: AstrMessageEvent):
        gid = self._gid(event)
        if gid:
            self._umo_map[gid] = event.unified_msg_origin
        self._umo_map["__last__"] = event.unified_msg_origin

    @staticmethod
    def _rest(event: AstrMessageEvent, *names: str) -> str:
        """提取命令 token 之后的所有文本(自动跳过唤醒前缀)。"""
        text = (event.message_str or "").strip()
        # 先匹配更长的命令词(如「创建角色」优先于「创建」),
        # 否则「分身 创建角色 凛」会被「创建」截断成名字「角色 凛」
        for n in sorted(names, key=len, reverse=True):
            idx = text.find(n)
            if idx >= 0:
                return text[idx + len(n):].strip()
        # 兜底:去掉第一个 token
        parts = text.split(None, 1)
        return parts[1].strip() if len(parts) > 1 else ""

    @staticmethod
    def _images(event: AstrMessageEvent) -> list[Image]:
        try:
            return [c for c in event.get_messages() if isinstance(c, Image)]
        except Exception:
            return []

    async def _images_with_quoted(self, event: AstrMessageEvent) -> list[Image]:
        """取图片:当前消息无图时回退到引用(回复)消息的图片。

        手机端常无法在同一消息里同时发文字+图片,引用一张图再发指令是常见操作。
        引用通道由 QuotedMessageExtractor 解析成 URL/base64/本地路径,统一包成
        Image 组件(convert_to_file_path 能处理以上全部形态)。
        """
        imgs = self._images(event)
        if imgs:
            return imgs
        try:
            refs = await QuotedMessageExtractor(event=event).images()
        except Exception as e:
            logger.debug(f"ocverse: 解析引用图片失败: {e}")
            return []
        out: list[Image] = []
        seen: set[str] = set()
        for ref in refs or []:
            ref = (ref or "").strip()
            if not ref or ref in seen:
                continue
            seen.add(ref)
            out.append(Image(file=ref))
        return out

    def _at_target(self, event: AstrMessageEvent) -> str:
        try:
            me = self._uid(event)
            for c in event.get_messages():
                if isinstance(c, At):
                    t = str(getattr(c, "qq", "") or getattr(c, "target_id", "") or "")
                    if t and t.isdigit() and t != me:
                        return t
        except Exception:
            pass
        return ""

    # ── 渲染与发送 ────────────────────────────────────────────────
    def _save_card(self, img) -> str | None:
        d = os.path.join(self.data_dir, "tmp")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{int(time.time() * 1000)}.png")
        try:
            img.convert("RGB").save(path, "PNG")
        except Exception as e:
            logger.warning(f"ocverse: 卡片保存失败: {e}")
            return None
        try:
            files = sorted(
                (f for f in os.listdir(d) if f.endswith(".png")),
                key=lambda f: os.path.getmtime(os.path.join(d, f)),
            )
            for f in files[:-100]:
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass
        except OSError:
            pass
        return path

    def _chain(self, imgs: list, extra: str = "") -> list:
        chain: list = []
        for im in imgs:
            p = self._save_card(im)
            if p:
                chain.append(Image.fromFileSystem(p))
        if not chain:
            return []
        if extra:
            chain.append(Plain(extra))
        return chain

    def _chain_views(self, views: list[dict], extra: str = "") -> list:
        """把 view 列表渲染成消息链。

        事件卡在其图片后附上 №编号纯文本(与卡片底部标签一致):
        群友引用(回复)事件卡时,QuotedMessageExtractor 能把这段文本原样带回,
        指令层据此精确定位要结算的事件,多卡并存也不会结算错。"""
        chain: list = []
        for v in views:
            eid = v.get("event_id") if v.get("type") == "event" else None
            n_img = 0
            for im in render_views([v], self._card_cfg()):
                p = self._save_card(im)
                if p:
                    chain.append(Image.fromFileSystem(p))
                    n_img += 1
            tag = C.event_tag(eid) if (eid and n_img) else ""
            if tag:
                chain.append(Plain(tag))
        if not chain:
            return []
        if extra:
            chain.append(Plain(extra))
        return chain

    async def _quoted_event_id(self, event: AstrMessageEvent) -> int | None:
        """从引用(回复)消息的文本中解析事件№编号。
        (平台不会保留图片路径,纯文本标签是唯一可靠的识别通道)"""
        try:
            qe = QuotedMessageExtractor(event=event)
        except Exception:
            return None
        try:
            txt = await qe.text()
        except Exception as e:
            logger.debug(f"ocverse: 解析引用文本失败: {e}")
            txt = None
        eid = C.parse_event_tag(txt or "")
        if eid:
            return eid
        return None

    # ── 主动发送 / 积压补发 ─────────────────────────────────────────
    async def _send_to(self, umo: str, chain: list) -> bool:
        fn = getattr(self.context, "send_message", None)
        if fn is None or not chain:
            return False
        try:
            await fn(umo, MessageChain(chain=chain))
            return True
        except Exception as e:
            logger.debug(f"ocverse: 主动发送失败: {e}")
        return False

    def _view_deliverable(self, v: dict) -> bool:
        """事件卡仅在事件仍 pending 时投递;晨报/世界变动等无 event_id 的卡片恒投递。

        同批生成时后一张事件可能已顶替前一张(事件串行化),失效卡发出只会让
        群友对着死卡抉择,造成事件与后续结算割裂。"""
        eid = v.get("event_id")
        if not eid:
            return True
        ev = self.db.get_event(int(eid))
        return bool(ev and ev.state == "pending")

    def _mark_sent(self, v: dict):
        """卡片真正送达后标记;只有「发送过」的事件才可被「选择」结算。"""
        eid = v.get("event_id")
        if eid:
            try:
                self.db.mark_event_sent(int(eid))
            except Exception as e:
                logger.warning(f"ocverse: 标记事件已发送失败: {e}")

    async def _broadcast(self, views: list[dict]):
        by_gid: dict[str, list] = {}
        for v in views:
            by_gid.setdefault(v["gid"], []).append(v)
        for gid, gviews in by_gid.items():
            gviews = [v for v in gviews if self._view_deliverable(v)]
            if not gviews:
                continue
            umo = self._umo_map.get(gid) or (self.db.kv_get(gid, "umo") or "")
            chain = self._chain_views(gviews)
            if umo and await self._send_to(umo, chain):
                for v in gviews:
                    self._mark_sent(v)  # 主动送达成功 → 事件可被抉择
                continue
            # 无法主动发送 → 积压,等群消息时补发(补发送达后同样标记,之后才可抉择)
            pend = self._pending.setdefault(gid, [])
            pend.extend(gviews)
            del pend[3:]

    # ── 后台调度 ──────────────────────────────────────────────────
    async def initialize(self):
        self._task = asyncio.create_task(self._loop())
        logger.info("ocverse: 后台调度已启动")
        # 后台管理:注册进 Dashboard(admin_enable=false 时不注册)。
        # 页面本体在 pages/admin/,由 Dashboard 自动发现并以 iframe + bridge 加载;
        # 鉴权由 Dashboard 登录态统一处理,无需任何密钥。
        try:
            self._admin = AdminPanel(self.db, self.game, self._cfg, self._admin_ops(),
                                     plugin_name=PLUGIN_NAME)
            self._admin.register(self.context)
        except Exception as e:
            logger.warning(f"ocverse: 后台管理注册失败(不影响插件运行): {e}")

    def _admin_ops(self):
        """管理页的触发/删除操作:走主循环同一把群锁,结果照常广播到群里。"""
        plugin = self

        class _Ops:
            async def trigger(self_, gid: str, kind: str) -> str:
                async with plugin._glock(gid):
                    if kind == "shift":
                        v = await plugin.game.world_shift(gid, manual=True)
                    elif kind == "morning":
                        v = await plugin.game.fire_morning(gid)
                    else:
                        v = await plugin.game.fire_event(gid)
                if v:
                    await plugin._broadcast([v])
                if not v:
                    return "没有产出(可能无人创建分身,或世界变动冷却中)"
                t, p = v.get("type"), v.get("payload") or {}
                if t == "event":
                    return f"事件已生成并推送:「{p.get('title', '')}」{str(p.get('scene', ''))[:60]}"
                if t == "arrive":
                    w = v.get("world")
                    return f"世界变动完成 → 《{getattr(w, 'name', '?')}》"
                if t == "morning":
                    return "晨报已生成并推送"
                return f"已完成({t})"

            async def delete_char(self_, gid: str, uid: str) -> str:
                async with plugin._glock(gid):
                    return plugin.game.delete_char(gid, uid)

            async def regen_infra(self_, gid: str, world_id: int | None = None) -> dict:
                async with plugin._glock(gid):
                    msg, infra = await plugin.game.regen_infra(gid, world_id=world_id)
                return {"message": msg, "infra": infra}

        return _Ops()

    async def terminate(self):
        if self._task:
            self._task.cancel()
        self.db.close()
        logger.info("ocverse: 插件已卸载")

    async def _loop(self):
        """计划任务主循环:按真实日历天推进。

        - 每天的日程(晨报/随机事件/世界变动)在世界时区进入新的一天时生成并持久化
        - 唤醒对齐到整分钟:到点的事件在分钟级别准时触发;空闲时仅做极轻量的检查
        """
        logger.info("ocverse: 计划任务已启动(按真实日历天调度)")
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"ocverse: 调度异常: {e}")
            try:
                if self._cfg("knowledge_collect_enabled", True):
                    await self._kb_maintenance()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"ocverse: 知识库维护异常: {e}")
            await self._sleep_to_next_minute()

    @staticmethod
    async def _sleep_to_next_minute():
        now = datetime.now()
        secs = 60 - now.second - now.microsecond / 1e6
        await asyncio.sleep(max(1.0, secs))

    async def _tick(self):
        views: list[dict] = []
        for g in self.db.list_groups():
            gid = g["gid"]
            try:
                # 单 tick 每群最多处理 3 个计划项,余下的下个周期继续(NAS/IM 均温和)
                async with self._glock(gid):
                    for item, action in self.game.tick_items(gid)[:3]:
                        try:
                            if action in ("fire", "force"):
                                v = await self._fire_plan_item(gid, item, forced=(action == "force"))
                                if v:
                                    views.append(v)
                                self.game.mark_done(gid, item)
                            elif action == "arm":
                                # 被动事件:埋下伏笔,不推送、不完成,等群里有动静时引爆
                                self.game.arm_passive(gid, item)
                        except GameError as e:
                            logger.warning(f"ocverse: 群{gid} {item['kind']}执行失败: {e}")
                            if action != "arm":
                                self.game.mark_done(gid, item)  # 失败也收掉,避免反复重试刷错
            except Exception as e:
                logger.error(f"ocverse: 群{gid}调度异常: {e}")
        if views:
            await self._broadcast(views)
        # 超时事件统一平淡收场(卡片上的「45分钟内有效」是真的):
        # 每分钟扫描一次 pending 且已过 expires_at 的事件
        try:
            await self.game.expire_sweep()
        except Exception as e:
            logger.warning(f"ocverse: 事件过期扫描失败: {e}")

    # ── 知识库定时采集:每天每组入库一条素材(联网/LLM),供所有生成功能注入 ──
    async def _kb_maintenance(self):
        day = self.game._day_key()
        for g in self.db.list_groups():
            gid = g["gid"]
            if self.db.kv_get(gid, "kb_last") == day:
                continue
            self.db.kv_set(gid, "kb_last", day)  # 占位防重复
            if not self._cfg("knowledge_collect_enabled", True) or self.kb.count(gid) >= self._cfgi("knowledge_base_max", 40):
                continue
            # 每群每天采集(默认1条,可配置),用序号错开题材,避免同批同主题重复
            n = max(0, min(3, self._cfgi("knowledge_collect_daily", 1)))
            base = self.kb.count(gid)
            for i in range(n):
                if self.kb.count(gid) >= self._cfgi("knowledge_base_max", 40):
                    break
                try:
                    await self._collect_kb(gid, offset=base + i)
                except Exception as e:
                    logger.warning(f"ocverse: 知识库采集失败: {e}")

    async def _collect_kb(self, gid: str, offset: int = 0):
        """采集一条轻小说/动漫/漫画风格的著作素材,提炼成可复用条目存入知识库。
        offset: 同批内第几条,用于错开本轮题材(避免一条批次全同题)。"""
        from .ocverse.llm_engine import _extract_json, now_stamp
        themes = ["异世界转生", "校园异能", "末世求生", "机甲战争", "修仙问道", "都市怪谈",
                  "奇幻冒险", "科幻末日", "怪盗群像", "婚约恋爱", "英灵群像", "蒸汽朋克"]
        theme = themes[(self.game._world_day(gid) + self.kb.count(gid) + offset) % len(themes)]
        have = self.db.kb_sources(gid)[-6:]
        avoid = ("已在库的作品:" + "、".join(x for x in have if x)) if have else ""
        user = (
            "你在为一个群聊文字游戏扩充素材库。请(优先联网搜索,或凭你广博的知识)选取一部动漫/轻小说/漫画"
            "或其典型桥段,提炼成一条可复用的创作素材,要能服务于后续的世界生成、随机事件、每日小任务、"
            "角色对话等。\n"
            f"本轮题材偏向:{theme}。{avoid}\n"
            f"{now_stamp()}\n"
            "严格输出 JSON:{\"source\":作品名(≤20字),\"theme\":题材标签(≤12字),"
            "\"kind\":\"work|idea|dialogue|rule\"之一,"
            "\"content\":素材正文(120~300字,有设定感、可直接当世界观/钩子/台词风格使用,不要照搬原剧情主线)}\n"
        )
        system = self.brain.style
        text = await self._llm_raw_enriched(system, user)  # 联网优先
        if not text:
            text = await self._llm_raw(system, user)        # 回退普通
        if not text:
            return
        d = _extract_json(text) or {}
        content = (str(d.get("content") or "")).strip()
        if len(content) < 40:  # 内容过短(拆不出可用素材)则丢弃,不进库
            return
        await self.kb.add(gid,
                          str(d.get("source") or "")[:60],
                          str(d.get("theme") or theme)[:30],
                          str(d.get("kind") or "work")[:12],
                          content[:1500])
        await asyncio.sleep(0)  # 让出事件循环

    async def _fire_plan_item(self, gid: str, item: dict, forced: bool = False) -> dict | None:
        kind = item["kind"]
        if kind == "event":
            return await self.game.fire_event(gid)
        if kind == "shift":
            return await self.game.world_shift(gid)
        if kind == "morning":
            return await self.game.fire_morning(gid)
        _ = forced
        return None

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    @_guard
    async def on_group_msg(self, event: AstrMessageEvent):
        """观察群消息:记录 umo + 补发积压 + 引爆已武装的被动事件。"""
        gid = self._gid(event)
        if not gid:
            return
        self._remember_umo(event)
        pend = self._pending.get(gid)
        if pend:
            v = None
            while pend:  # 跳过已失效的事件卡(被顶替/过期/结算),死卡补发只会误导抉择
                cand = pend.pop(0)
                if self._view_deliverable(cand):
                    v = cand
                    break
            if v is not None:
                try:
                    chain = self._chain_views([v])
                    if chain:
                        self._mark_sent(v)  # 补发送出才算「发送过」,之后才可回落结算
                        yield event.chain_result(chain)
                except Exception as e:
                    logger.warning(f"ocverse: 补发卡片失败: {e}")
            return
        # 被动事件:群里有动静,伏笔引爆(每次消息最多一个,自然限流)
        armed = self.game.armed_passives(gid)
        if not armed:
            return
        item = armed[0]
        # 角色事件只能由本人消息引爆:无分身的群友发言不触发,
        # 事件保持待命等本人发言;绝不把别人的角色卷进来
        # (引爆过程不发「请稍候」类提示,事件卡生成后直接送达)
        if not self.db.get_char(gid, self._uid(event)):
            return
        try:
            async with self._glock(gid):
                # 二次确认(等待锁期间可能已被别的消息引爆)
                if not any(it.get("id") == item.get("id") for it in self.game.armed_passives(gid)):
                    return
                v = await self.game.fire_event(gid, char_uid=self._uid(event))
                self.game.mark_done(gid, item)
            if v:
                chain = self._chain_views([v])
                if chain:
                    self._mark_sent(v)  # 事件卡发出后才能被回落结算(引用识别不受此限)
                    yield event.chain_result(chain)
                else:
                    yield event.plain_result("(雾气散去,似乎什么也没有发生…)")
        except GameError as e:
            self.game.mark_done(gid, item)  # 无法触发(如无人建角色)也收掉伏笔
            logger.warning(f"ocverse: 被动事件引爆失败: {e}")
            yield event.plain_result(f"❌ {e}")

    # ═══════════════════════════ 工具方法 ═══════════════════════════
    def _err(self, e: Exception) -> str:
        return f"❌ {e}"

    def _need_gid(self, event) -> str:
        gid = self._gid(event)
        if not gid:
            raise GameError("请在群聊中使用本插件")
        return gid

    def _char_of(self, event) -> object:
        ch = self.db.get_char(self._need_gid(event), self._uid(event))
        if not ch:
            raise GameError("你还没有创建分身,先「/分身 创建 名字」")
        return ch

    # ═══════════════════════════ 指令:引导/管理 ═══════════════════════════
    @filter.command_group("分身", alias={"oc", "ocs"})
    async def oc(self):
        """分身的世界 · 指令组。发送「/分身」或「/分身 帮助」查看全部指令。"""

    @oc.command("帮助", alias={"help", "菜单"})
    @_guard
    async def cmd_help(self, event: AstrMessageEvent):
        """分身 帮助 - 查看玩法与指令"""
        self._remember_umo(event)
        chain = self._chain(help_card(self._card_cfg()))
        if chain:
            yield event.chain_result(chain)

    @filter.permission_type(PermissionType.ADMIN)
    @oc.command("初始化世界", alias={"init_world", "设定世界"})
    @_guard
    async def cmd_init_world(self, event: AstrMessageEvent):
        """分身 初始化世界 [世界观描述…] - 管理员铺设/重建群世界"""
        gid = self._need_gid(event)
        desc = self._rest(event, "初始化世界", "init_world", "设定世界")
        yield event.plain_result("⏳ 正在铺设世界(可能联网搜索),请稍候…")
        async with self._glock(gid):
            v = await self.game.init_world(gid, desc or None, self._uid(event))
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs, "世界已就位!成员们用「/分身 创建 名字」降生吧。")
        if chain:
            yield event.chain_result(chain)

    @filter.permission_type(PermissionType.ADMIN)
    @oc.command("重开世界", alias={"重建世界", "reset_world"})
    @_guard
    async def cmd_reset_world(self, event: AstrMessageEvent):
        """分身 重开世界 - 管理员重建当前世界(保留角色),需发两次确认"""
        gid = self._need_gid(event)
        key = f"reset:{gid}"
        now = time.time()
        if now - self._confirm.get(key, 0) > 120:
            self._confirm[key] = now
            yield event.plain_result("⚠ 将为全群重新生成一个世界(角色与记忆保留)。确认请再发一次「/分身 重开世界」")
            return
        self._confirm.pop(key, None)
        yield event.plain_result("⏳ 正在重铸世界,请稍候…")
        async with self._glock(gid):
            v = await self.game.init_world(gid, None, self._uid(event))
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs, "世界已重铸。")
        if chain:
            yield event.chain_result(chain)

    @filter.permission_type(PermissionType.ADMIN)
    @oc.command("重建设施", alias={"regen_infra", "重新生成设施"})
    @_guard
    async def cmd_regen_infra(self, event: AstrMessageEvent):
        """分身 重建设施 - 管理员让 AI 重新规划当前世界设施(贴合世界观,保证生存基线)"""
        gid = self._need_gid(event)
        yield event.plain_result("⏳ 正在重新规划世界设施…")
        async with self._glock(gid):
            msg, _infra = await self.game.regen_infra(gid)
        yield event.plain_result(msg)

    @oc.command("定义世界", alias={"add_world", "世界书"})
    @_guard
    async def cmd_define_world(self, event: AstrMessageEvent):
        """分身 定义世界 <名称> <描述…> - 把自设世界写进世界书(等待变动降临)"""
        gid = self._need_gid(event)
        rest = self._rest(event, "定义世界", "add_world", "世界书")
        parts = rest.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result("格式:/分身 定义世界 <名称> <描述…>\n例如:/分身 定义世界 机械都市 一切由齿轮与蒸汽驱动,情感被禁止…")
            return
        name, desc = parts[0][:16], parts[1]
        async with self._glock(gid):
            r = await self.game.define_world(gid, self._uid(event), name, desc)
        yield event.plain_result(f"📖 《{r['name']}》已写进世界书。它会在某次世界变动时降临——降临后即可自由穿越。")

    @oc.command("定义角色", alias={"招募", "招募角色", "添加生活角色", "define_npc"})
    @_guard
    async def cmd_define_npc(self, event: AstrMessageEvent):
        """分身 定义角色 <名字> [描述…] - 创造一个不属于任何真人的持久『生活角色』,参与群世界生活"""
        gid = self._need_gid(event)
        rest = self._rest(event, "定义角色", "招募", "招募角色", "添加生活角色", "define_npc")
        parts = rest.split(None, 1)
        if not parts or not parts[0]:
            yield event.plain_result(
                "格式:/分身 定义角色 <名字> [描述…]\n"
                "例如:/分身 定义角色 绫波 住在雾码头的老婆婆,神秘而热心\n"
                "创造出的生活角色会像群友一样参与生活事件、与你互动/发展关系/结婚、并被卷入世界变动;"
                "之后可用「/分身 找 <名字>」互动、「/分身 设置头像 <名字>」+图 给它换头像。")
            return
        name = parts[0][:12]
        desc = (parts[1] if len(parts) > 1 else "").strip()
        existing = self.db.get_char(gid, npc_uid(gid, name))
        async with self._glock(gid):
            ch = self.game.define_npc_char(gid, name, desc, self._uid(event))
        if existing:
            yield event.plain_result(
                f"✏️ 已重设生活角色「{ch.name}」的设定(等级/财产/羁绊/关系保留)。\n"
                "用「/分身 看 <名字>」查看新设定。")
        else:
            yield event.plain_result(
                f"🎭 一位生活角色「{ch.name}」融入了群世界!\n"
                "用「/分身 找 <名字> <方式>」与TA互动(可能发展关系/告白求婚);"
                "「/分身 设置头像 <名字>」+ 图片可给它换头像。\n"
                "设定写错了?再发一次「/分身 定义角色 <名字> <新描述>」即可重设,不会丢失等级/关系。")

    @filter.permission_type(PermissionType.ADMIN)
    @oc.command("触发变动", alias={"world_shift", "手动变动"})
    @_guard
    async def cmd_trigger_shift(self, event: AstrMessageEvent):
        """分身 触发变动 - 管理员立即触发一次世界变动(有冷却)"""
        gid = self._need_gid(event)
        yield event.plain_result("⏳ 正在触发世界变动,请稍候…")
        async with self._glock(gid):
            v = await self.game.world_shift(gid, manual=True)
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    @filter.permission_type(PermissionType.ADMIN)
    @oc.command("事件频率", alias={"event_freq"})
    @_guard
    async def cmd_event_freq(self, event: AstrMessageEvent, a: str = "", b: str = ""):
        """分身 事件频率 <min> <max> - 每日事件数量范围(管理员)"""
        gid = self._need_gid(event)
        try:
            emin, emax = int(a), int(b)
            assert 0 <= emin <= emax <= 12
        except Exception:
            g = self.db.get_group(gid)
            cur = self.game._limits_for(dict(g)) if g else (2, 4, 0, 0, 0)
            yield event.plain_result(f"当前每日事件数:{cur[0]}~{cur[1]}。修改:分身 事件频率 <min> <max>")
            return
        self.db.ensure_group(gid, {})
        self.db.update_group(gid, event_min=emin, event_max=emax)
        yield event.plain_result(f"✅ 每日事件数已设为 {emin}~{emax} 个")

    @filter.permission_type(PermissionType.ADMIN)
    @oc.command("变动概率", alias={"shift_prob"})
    @_guard
    async def cmd_shift_prob(self, event: AstrMessageEvent, p: str = ""):
        """分身 变动概率 <p> - 每日世界变动概率 %(管理员)"""
        gid = self._need_gid(event)
        try:
            pct = int(p)
            assert 0 <= pct <= 80
        except Exception:
            g = self.db.get_group(gid)
            cur = self.game._limits_for(dict(g)) if g else (0, 0, 8, 0, 0)
            yield event.plain_result(f"当前世界变动概率:{cur[2]}%/日。修改:分身 变动概率 <p>(0~80)")
            return
        self.db.ensure_group(gid, {})
        self.db.update_group(gid, shift_percent=pct)
        yield event.plain_result(f"✅ 世界变动概率已设为 {pct}%/日")

    # ═══════════════════════════ 指令:角色 ═══════════════════════════
    @oc.command("创建", alias={"create", "创建角色"})
    @_guard
    async def cmd_create(self, event: AstrMessageEvent):
        """分身 创建 <名字> [设定描述…] - 创建你的 OC 分身(AI 自动整理人设,一人一个)"""
        gid = self._need_gid(event)
        rest = self._rest(event, "创建", "create", "创建角色")
        if not rest:
            yield event.plain_result(
                "格式:/分身 创建 <名字> [设定描述…](AI 自动整理成人设)\n"
                "例如:/分身 创建 森森 外观白发蓝瞳戴眼镜的帅哥,白色兜帽卫衣黑色内衬长裤,超级聪明的大天才,性格生人勿近 天不怕地不怕 喜欢独来独往 超有钱\n"
                "也兼容竖线速写:/分身 创建 <名字> |性别|性格,性格|背景设定…\n"
                "设定可留空之后用「分身 编辑」补全;头像用「/分身 设置头像」+ 图片"
            )
            return
        if "|" in rest:
            # 旧竖线速写:切段即可,不耗 AI
            parts = [p.strip() for p in rest.split("|")]
            if not parts[0]:
                yield event.plain_result("名字不能为空:/分身 创建 <名字> |性别|性格,性格|背景设定…")
                return
            name = parts[0][:12]
            gender = parts[1][:8] if len(parts) > 1 and parts[1] else "保密"
            tags = [t for t in re.split(r"[、,，/]", parts[2]) if t.strip()][:6] if len(parts) > 2 else []
            backstory = parts[3][:4000] if len(parts) > 3 else ""
        else:
            # 自然语言:首词为名字,余下整段描述交给 AI 整理成人设
            toks = rest.split(None, 1)
            name = toks[0][:12]
            desc = (toks[1] if len(toks) > 1 else "").strip()
            gender, tags, backstory, llm_attrs = "保密", [], "", None
            if desc:
                yield event.plain_result("⏳ 正在整理人设,请稍候…")
                r = await self.brain.parse_persona(desc)
                if r.ok:
                    gender, tags, backstory = r.data["gender"], r.data["tags"], r.data["backstory"]
                    llm_attrs = r.data.get("attrs") or None
                else:
                    backstory = desc[:4000]  # AI 不可用:描述原文入背景,不丢信息
        async with self._glock(gid):
            ch = self.game.create_char(gid, self._uid(event), name, gender, tags, backstory, attrs=llm_attrs)
            tip = "" if (tags and backstory) else "\n💡 建议用「/分身 编辑 性格/背景 <内容>」补全人设,事件会更有戏"
            attached = await self._images_with_quoted(event)  # 只解析一次,避免重复处理引用消息
            if attached and await self._save_avatar_bytes(ch, attached[0]):
                tip += "\n🖼️ 已顺手把随指令的图片设为头像"
            w = self.db.cur_world(gid)
            v = await self._profile_view(gid, self._uid(event))
        text = f"✨ {ch.name} 降临于《{w.name if w else '?'}》{tip}"
        try:
            chain = self._chain(self._render_profile(v), text)
        except Exception as e:
            logger.warning(f"ocverse: 创建角色卡片渲染失败: {e}")
            chain = []
        if chain:
            yield event.chain_result(chain)
        else:
            # 卡片渲染不可用时也必须给出成功提示,不能沉默
            yield event.plain_result(text)

    async def _save_avatar_bytes(self, ch, img_comp) -> bool:
        try:
            src = await img_comp.convert_to_file_path()
            with open(src, "rb") as f:
                data = f.read()
            p = self.avatars.save_avatar(f"group_{ch.gid}", ch.name, data)
            if not p:
                logger.warning("ocverse: 头像保存失败(图片数据无效)")
                return False
            self.db.update_char(ch.gid, ch.uid, avatar=p)
            return True
        except Exception as e:
            logger.warning(f"ocverse: 头像保存失败: {e}")
            return False

    @oc.command("删除角色", alias={"delete_char", "删号"})
    @_guard
    async def cmd_delete_char(self, event: AstrMessageEvent):
        """分身 删除角色 - 删除你的分身(二次确认)"""
        gid = self._need_gid(event)
        key = f"del:{gid}:{self._uid(event)}"
        now = time.time()
        ch = self.db.get_char(gid, self._uid(event))
        if not ch:
            yield event.plain_result("你还没有分身")
            return
        if now - self._confirm.get(key, 0) > 120:
            self._confirm[key] = now
            yield event.plain_result(f"⚠ 将删除分身「{ch.name}」(角色卡、头像、日志与记忆全部清除)。确认请再发一次「/分身 删除角色」")
            return
        self._confirm.pop(key, None)
        async with self._glock(gid):
            name = self.game.delete_char(gid, self._uid(event))
        self.avatars.delete(f"group_{gid}", name)
        yield event.plain_result(f"👋 「{name}」的身影淡出了这个世界。")

    @oc.command("设置头像", alias={"set_avatar", "头像"})
    @_guard
    async def cmd_set_avatar(self, event: AstrMessageEvent):
        """分身 设置头像 (随指令附图,或回复一张图) - 设置分身头像"""
        gid = self._need_gid(event)
        rest = self._rest(event, "设置头像", "set_avatar", "头像").strip()
        ch = self._char_of(event)
        is_life = False
        if rest:
            nb = self.db.get_char(gid, npc_uid(gid, rest))
            if nb:
                ch = nb
                is_life = True
        imgs = await self._images_with_quoted(event)
        if not imgs:
            yield event.plain_result(
                "请随指令发一张图片,或引用(回复)一张图片后发送:分身 设置头像\n"
                "(手机端无法同发文字+图时,引用图片即可)"
            )
            return
        ok = await self._save_avatar_bytes(ch, imgs[0])
        if not ok:
            yield event.plain_result("❌ 头像保存失败,请换一张图片重试")
            return
        if is_life:
            yield event.plain_result(f"🖼️ 已更新生活角色「{ch.name}」的头像")
            return
        v = await self._profile_view(gid, self._uid(event))
        try:
            chain = self._chain(self._render_profile(v), "🖼️ 头像已更新")
        except Exception as e:
            logger.warning(f"ocverse: 头像卡片渲染失败: {e}")
            chain = []
        if chain:
            yield event.chain_result(chain)
        else:
            yield event.plain_result("🖼️ 头像已更新")

    @oc.command("编辑", alias={"edit"})
    @_guard
    async def cmd_edit(self, event: AstrMessageEvent):
        """分身 编辑 性别|性格|背景(设定) <内容> 或直接自由描述 - 修改人设"""
        ch = self._char_of(event)
        content = self._rest(event, "编辑", "edit")
        if not content:
            yield event.plain_result(self._edit_usage())
            return
        # 「背景设定」要放在「背景」前面,否则会被截成 背景 + "设定 …"
        m = re.match(r"^(性别|性格|背景设定|背景)(?:\s+|$)(.*)$", content, re.S)
        if m:
            f, val = m.group(1), m.group(2).strip()
            if not val:
                yield event.plain_result("内容不能为空")
                return
            if f == "性别":
                self.db.update_char(ch.gid, ch.uid, gender=val[:8])
            elif f == "性格":
                tags = [t for t in re.split(r"[、,，/]", val) if t.strip()][:6]
                self.db.update_char(ch.gid, ch.uid, tags=tags)
            else:
                self.db.update_char(ch.gid, ch.uid, backstory=val[:4000])
            yield event.plain_result(f"✅ 已更新「{ch.name}」的{f}")
            return
        # 自由描述:让 AI 判断要改哪些字段(合并保留未提及的旧设定)
        yield event.plain_result("⏳ 正在整理修改内容,请稍候…")
        r = await self.brain.parse_persona_update(
            cur_name=ch.name, cur_gender=ch.gender, cur_tags=list(ch.tags or []),
            cur_backstory=ch.backstory or "", text=content)
        if not r.ok:
            yield event.plain_result(self._edit_usage() + "\n(AI 整理失败,请改用「/分身 编辑 性别/性格/背景 <内容>」)")
            return
        d = r.data
        changed = []
        async with self._glock(ch.gid):
            if d.get("gender"):
                self.db.update_char(ch.gid, ch.uid, gender=d["gender"][:8])
                changed.append("性别")
            if d.get("tags"):
                self.db.update_char(ch.gid, ch.uid, tags=d["tags"][:6])
                changed.append("性格")
            if d.get("backstory"):
                self.db.update_char(ch.gid, ch.uid, backstory=d["backstory"][:4000])
                changed.append("背景设定")
        if changed:
            yield event.plain_result(f"✅ AI 已更新「{ch.name}」的:{'、'.join(changed)}")
        else:
            yield event.plain_result("没识别出要改的内容。\n" + self._edit_usage())

    @staticmethod
    def _edit_usage() -> str:
        return (
            "格式一:/分身 编辑 性别|性格|背景(设定) <内容>\n"
            "　　　例如:/分身 编辑 性格 高冷,毒舌,护短\n"
            "格式二:/分身 编辑 <自由描述>(AI 自动判断改什么)\n"
            "　　　例如:/分身 编辑 我现在改成白发蓝瞳,性格变得开朗大胆"
        )

    async def _profile_view(self, gid: str, uid: str) -> dict:
        ch = self.db.get_char(gid, uid)
        world = self.db.cur_world(gid)
        rels = self.db.list_rels_for(gid, uid, 4)
        name_map = {c.uid: c.name for c in self.db.list_chars(gid)}
        rel_named = [(name_map.get(u, u[:8]), s) for u, s in rels]
        rel_labels = {name_map.get(u, u[:8]): self.game.rel_stage_label(gid, uid, u)
                      for u, s in rels}
        mems = await self.mem.related(gid, f"{ch.name} 最近 经历", uid=uid, k=3)
        badges = [C.FLAG_TITLES[k] for k in ("traveler", "socialite", "survivor")
                  if (ch.flags or {}).get(k)]
        # 自定义搞怪关系(我是谁的X / 谁是我的X)
        for bd in self.game.bonds_of(gid, uid)[:3]:
            other = name_map.get(bd["target"] if bd["proposer"] == uid else bd["proposer"], "?")
            if bd["proposer"] == uid:
                badges.append(f"🤝 我是{other}的{bd['label']}")
            else:
                badges.append(f"🤝 {other}是我的{bd['label']}")
        return {"__profile__": True, "ch": ch, "world": world, "rels": rel_named,
                "rel_labels": rel_labels,
                "mems": mems, "badges": badges}

    def _render_profile(self, v: dict) -> list:
        """把 _profile_view 的 view 渲染成角色卡图片列表(统一渲染入口)。"""
        return profile_card(v["ch"], v["world"], v["rels"], v["mems"], self._card_cfg(),
                            rel_names=v.get("rel_labels"),
                            extra_badges=v.get("badges") or [])

    async def _yield_profile(self, event, gid: str, uid: str, extra: str = ""):
        v = await self._profile_view(gid, uid)
        chain = self._chain(self._render_profile(v), extra)
        if chain:
            yield event.chain_result(chain)
        else:
            yield event.plain_result(extra or "⚠ 角色卡渲染失败,请稍后重试")

    @oc.command("我的卡片", alias={"card", "状态"})
    @_guard
    async def cmd_card(self, event: AstrMessageEvent):
        """分身 我的卡片 - 展示角色卡(属性/羁绊/记忆)"""
        gid = self._need_gid(event)
        self._char_of(event)
        async for r in self._yield_profile(event, gid, self._uid(event)):
            yield r

    @oc.command("看", alias={"look", "情报", "查看", "卡片"})
    @_guard
    async def cmd_look(self, event: AstrMessageEvent):
        """分身 看 <名字> - 查看任意角色(玩家分身或持久生活角色)的完整角色卡"""
        gid = self._need_gid(event)
        rest = self._rest(event, "看", "look", "情报", "查看", "卡片").strip()
        if not rest:
            yield event.plain_result(
                "格式:/分身 看 <名字>\n"
                "可查看任意角色(包括持久生活角色)的属性/羁绊/记忆。常见生活角色见「/分身 名册」。")
            return
        # 先按真人玩家名字找,再按生活角色名字找
        target = self.db.get_char(gid, rest)
        if target is None:
            target = self.db.get_char(gid, npc_uid(gid, rest))
        if target is None:
            names = "、".join(c.name for c in self.db.list_chars(gid)) or "无"
            yield event.plain_result(f"找不到叫「{rest}」的角色。现有居民:{names}")
            return
        async for r in self._yield_profile(event, gid, target.uid):
            yield r

    @oc.command("名册", alias={"roster", "居民"})
    @_guard
    async def cmd_roster(self, event: AstrMessageEvent):
        """分身 名册 - 全群分身一览"""
        gid = self._need_gid(event)
        chars = self.db.list_chars(gid)
        w = self.db.cur_world(gid)
        imgs = roster_card(chars, self._card_cfg(), w.name if w else "")
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    # ═══════════════════════════ 指令:世界 ═══════════════════════════
    @oc.command("世界", alias={"world"})
    @_guard
    async def cmd_world(self, event: AstrMessageEvent):
        """分身 世界 - 当前世界档案(NPC/规则/独特之处)"""
        gid = self._need_gid(event)
        w = self.db.cur_world(gid)
        if not w:
            yield event.plain_result("世界尚未初始化。管理员:「/分身 初始化世界 [世界观描述]」")
            return
        day = self.game._world_day(gid)
        imgs = world_card(w, self._card_cfg(), is_current=True, day=day)
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    @oc.command("世界列表", alias={"worlds", "世界书列表"})
    @_guard
    async def cmd_worlds(self, event: AstrMessageEvent):
        """分身 世界列表 - 已解锁(穿越过)与沉眠中的世界"""
        gid = self._need_gid(event)
        visited = self.db.list_worlds(gid, only_visited=True)
        pending = [w for w in self.db.list_worlds(gid) if not w.visited]
        cur = self.db.cur_world(gid)
        imgs = world_list_card(visited, pending, cur.id if cur else -1, self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    @oc.command("穿越世界", alias={"travel", "穿越"})
    @_guard
    async def cmd_travel(self, event: AstrMessageEvent):
        """分身 穿越世界 <编号/名称> - 自由穿越到去过的世界"""
        gid = self._need_gid(event)
        target = self._rest(event, "穿越世界", "穿越", "travel")
        if not target:
            yield event.plain_result("格式:/分身 穿越世界 <编号/名称>(编号见「/分身 世界列表」)")
            return
        yield event.plain_result("⏳ 正在开启穿越之门,请稍候…")
        async with self._glock(gid):
            v = await self.game.travel(gid, self._uid(event), target.strip())
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    # ═══════════ 指令:生活/基建/主线/房产 ═══════════
    @oc.command("设施", alias={"基建", "infra", "设施列表"})
    @_guard
    async def cmd_infra(self, event: AstrMessageEvent):
        """分身 设施 - 查看当前世界的基础设施(商店/饭馆/工作等)"""
        gid = self._need_gid(event)
        w = self.db.cur_world(gid)
        if not w:
            yield event.plain_result("世界尚未初始化。管理员:「/分身 初始化世界」")
            return
        if not w.infra:
            yield event.plain_result(f"《{w.name}》暂时没有特别的基础设施。")
            return
        lines = [f"🏙 《{w.name}》的基础设施", ""]
        for i, it in enumerate(w.infra, 1):
            wk = f"｜可打工:{it.get('work')}" if it.get("work") else ""
            lines.append(f"{i}. {it.get('kind','')}·{it.get('name','')} — {it.get('desc','')}{wk}")
        lines.append("")
        lines.append("用「/分身 兼职」去合适的地方打工赚钱。")
        yield event.plain_result("\n".join(lines))

    @oc.command("主线", alias={"story", "主线列表"})
    @_guard
    async def cmd_mainline(self, event: AstrMessageEvent):
        """分身 主线 [推进] - 查看当前世界主线,或推进一步"""
        gid = self._need_gid(event)
        rest = self._rest(event, "主线", "story", "主线列表").strip()
        w = self.db.cur_world(gid)
        if not w:
            yield event.plain_result("世界尚未初始化。")
            return
        if "推进" in rest or "进度" in rest:
            yield event.plain_result("⏳ 正在推进主线…")
            async with self._glock(gid):
                v = await self.game.mainline_progress(gid, self._uid(event))
            imgs = render_views([v], self._card_cfg())
            chain = self._chain(imgs)
            if chain:
                yield event.chain_result(chain)
            return
        ml = w.mainline or []
        if not ml:
            yield event.plain_result("这个世界暂时没有主线。")
            return
        lines = [f"📜 《{w.name}》世界主线", ""]
        for i, m in enumerate(ml, 1):
            mark = "✅" if m.get("done") else "⬜"
            lines.append(f"{mark} {i}. {m.get('stage','')}{'⚠已完成' if m.get('done') else ''}")
            lines.append(f"　 └ {m.get('desc','')}")
        lines.append("")
        lines.append("用「/分身 主线 推进」推进当前一步,推进完会解锁下一环。")
        yield event.plain_result("\n".join(lines))

    @oc.command("兼职", alias={"parttime", "打半天工"})
    @_guard
    async def cmd_workday(self, event: AstrMessageEvent):
        """分身 兼职 - 在当前世界找个基础设施打半天工,赚金币(不耗行动次数)"""
        gid = self._need_gid(event)
        async with self._glock(gid):
            v = self.game.work_today(gid, self._uid(event))
        if v is None:
            yield event.plain_result("(暂时没有适合你打工的地方)")
            return
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)
        else:
            yield event.plain_result(f"你在「{v['spot']}」干了半天,赚了 {v['earn']} 金币。")

    @oc.command("房产", alias={"住房", "物业", "买楼"})
    @_guard
    async def cmd_plots(self, event: AstrMessageEvent):
        """分身 房产 [买房 <编号>|回家] - 查看/购买/回宅"""
        gid = self._need_gid(event)
        rest = self._rest(event, "房产", "住房", "物业", "买楼").strip()
        if "回" in rest or "回家" in rest:
            yield event.plain_result("⏳ 正在回宅…")
            async with self._glock(gid):
                hv = await self.game.my_home(gid, self._uid(event))
            imgs = render_views([hv], self._card_cfg())
            chain = self._chain(imgs)
            if chain:
                yield event.chain_result(chain)
            else:
                yield event.plain_result("你回宅休整了一番。")
            return
        m = re.match(r"^(买|购买|buy)\s*(\d+)", rest)
        if m:
            idx = int(m.group(2)) - 1
            yield event.plain_result("⏳ 正在办理购房…")
            async with self._glock(gid):
                w, p, chg = self.game.buy_plot(gid, self._uid(event), idx)
            yield event.plain_result(
                f"🏠 你购入《{w.name}》的「{p['name']}」({p['kind']})吧。\n"
                + "\n".join(f"· {c}" for c in chg)
                + "\n用「/分身 房产 回家」回宅休整。")
            return
        w = self.db.cur_world(gid)
        if not w:
            yield event.plain_result("世界尚未初始化。")
            return
        plots = self.db.plots(gid, w.id)
        ch = self.db.get_char(gid, self._uid(event))
        mine_pid = (ch.flags or {}).get("home_plot") if ch else None
        if not plots:
            yield event.plain_result(f"《{w.name}》暂时没有可购置的房产。")
            return
        lines = [f"🏘 《{w.name}》房产", ""]
        for i, p in enumerate(plots, 1):
            own = "〔你已购〕" if p["id"] == mine_pid else ("〔已售〕" if p.get("owner_uid") else "〔在售〕")
            lines.append(f"{i}. {p.get('kind','')}·{p.get('name','')} {own} — {p.get('desc','')}")
            if not p.get("owner_uid"):
                lines.append(f"　 └ 价格 {p.get('price',0)} 金币")
        lines.append("")
        lines.append("用「/分身 房产 买 <编号>」购置,「/分身 房产 回家」回宅休整。")
        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════ 指令:事件/互动 ═══════════════════════════
    @oc.command("选择", alias={"choose"})
    @_guard
    async def cmd_choose(self, event: AstrMessageEvent, idx: str = ""):
        """分身 选择 <编号> - 回复(引用)事件卡后,对 TA 的遭遇做出抉择"""
        gid = self._need_gid(event)
        uid = self._uid(event)
        n = re.sub(r"\D", "", idx or self._rest(event, "选择", "choose"))
        if not n:
            yield event.plain_result("格式:回复要抉择的事件卡,发送「/分身 选择 <编号>」(选项编号见卡片)")
            return
        # 强制引用识别:没引用/解析不出№标签 → 不执行,提示后返回
        eid = await self._quoted_event_id(event)
        if eid is None:
            yield event.plain_result(
                "❌ 没有识别到要抉择的事件:请回复(引用)对应的事件卡后,再发送「/分身 选择 编号」")
            return
        yield event.plain_result("⏳ 正在结算抉择,请稍候…")
        async with self._glock(gid):
            # 引用识别:精确结算被回复的那张事件卡
            ev = self.db.get_event(eid)
            if not ev or ev.gid != gid:
                yield event.plain_result("❌ 这张事件卡无效或已不存在(可能已被清理)")
                return
            if ev.state != "pending":
                yield event.plain_result("❌ 这张事件卡已经结束了(可能已被结算或过期)")
                return
            if ev.uid and ev.uid != uid:
                other = self.db.get_char(gid, ev.uid)
                yield event.plain_result(f"这次遭遇是冲「{other.name if other else '别人'}」来的,让 TA 来抉择吧")
                return
            if ev.kind == "life_multi" and not self.game._multi_includes(ev, uid):
                names = "、".join(str(p.get("name", "")) for p in (ev.payload.get("participants") or []))
                yield event.plain_result(f"这场交集是「{names}」的,没带上你就不能替他们做主啦")
                return
            v = await self.game.choose(gid, uid, int(n) - 1, ev=ev)
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    @oc.command("与", alias={"互动"})
    @_guard
    async def cmd_interact(self, event: AstrMessageEvent):
        """分身 与 @群友 [互动方式/自由行动…] - 与别人的分身互动"""
        gid = self._need_gid(event)
        target = self._at_target(event)
        if not target:
            yield event.plain_result("请 @ 对方:/分身 与 @某人 [打招呼/闲聊/请客/切磋/吐槽/送礼/倾听/合影,或自由描述]")
            return
        raw = self._rest(event, "与", "互动").strip()
        raw = re.sub(r"@\S+", "", raw).strip()
        mode, detail = "", ""
        if raw:
            first = raw.split()[0]
            custom = {i["name"]: i["descr"] for i in self.db.list_interactions(gid)}
            if first in self._default_mode_hint:
                mode, detail = first, self._default_mode_hint[first]
            elif first in custom:
                mode, detail = first, custom[first]
            else:
                mode, detail = "自由互动", raw
        else:
            mode, detail = "打招呼", self._default_mode_hint["打招呼"]
        yield event.plain_result("⏳ 正在演绎这段互动,请稍候…")
        async with self._glock(gid):
            v = await self.game.interact(gid, self._uid(event), target, mode, detail)
        views = [v] + (v.pop("extra_views", []) or [])  # 事件触发的求婚等附加场景卡
        imgs = render_views(views, self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    @oc.command("找", alias={"交往", "结识", "找TA", "talk"})
    @_guard
    async def cmd_char_interact(self, event: AstrMessageEvent):
        """分身 找 <生活角色名> [方式/自由描述…] - 与持久生活角色互动(可发展关系/结婚)"""
        gid = self._need_gid(event)
        raw = self._rest(event, "找", "交往", "结识", "找TA", "talk").strip()
        if not raw:
            names = "、".join(c.name for c in self.game._npc_chars(gid)) or "无"
            yield event.plain_result(
                "格式:/分身 找 <生活角色名> [方式]\n"
                "可先「/分身 定义角色 <名字> <描述>」创造属于这个群世界的生活角色,再与TA互动/发展关系/结婚。"
                f"当前生活角色:{names}")
            return
        parts = raw.split(None, 1)
        name = parts[0]
        mode, detail = "打招呼", self._default_mode_hint["打招呼"]
        if len(parts) > 1:
            m0 = parts[1].split()[0]
            custom = {i["name"]: i["descr"] for i in self.db.list_interactions(gid)}
            if m0 in self._default_mode_hint:
                mode, detail = m0, self._default_mode_hint[m0]
            elif m0 in custom:
                mode, detail = m0, custom[m0]
            else:
                mode, detail = "自由互动", parts[1]
        yield event.plain_result("⏳ 正在演绎这段互动,请稍候…")
        async with self._glock(gid):
            v = await self.game.interact_life_char(gid, self._uid(event), name, mode, detail)
        views = [v] + (v.pop("extra_views", []) or [])
        imgs = render_views(views, self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    @oc.command("关系", alias={"bond", "自定义关系"})
    @_guard
    async def cmd_bond(self, event: AstrMessageEvent):
        """分身 关系 @群友 <称谓> - 提议自定义搞怪关系(如想当TA爸爸),AI 判断对方答不答应"""
        gid = self._need_gid(event)
        target = self._at_target(event)
        if not target:
            yield event.plain_result(
                "格式:/分身 关系 @群友 <称谓>\n"
                "例:/分身 关系 @老徐 爸爸(你想当 TA 的爸爸)/女仆/主人/师父/冤种弟弟…\n"
                "AI 会以对方的性格与你们的交情判断答不答应;亲密关系(恋人/情侣/夫妻等)不可自定义"
            )
            return
        label = re.sub(r"@\S+", "", self._rest(event, "关系", "bond", "自定义关系")).strip()
        if not label:
            yield event.plain_result("请写上想要的称谓,如:/分身 关系 @群友 爸爸")
            return
        yield event.plain_result("⏳ 对方正在认真考虑这个提案…")
        async with self._glock(gid):
            v = await self.game.propose_bond(gid, self._uid(event), target, label)
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    @oc.command("互动菜单", alias={"interactions"})
    @_guard
    async def cmd_inter_menu(self, event: AstrMessageEvent):
        """分身 互动菜单 - 查看可用的互动方式"""
        gid = self._need_gid(event)
        lines = [f"· {name} — {d}" for name, d in C.DEFAULT_INTERACTIONS]
        custom = self.db.list_interactions(gid)
        if custom:
            lines.append("")
            lines += [f"· {i['name']}(自定)— {i['descr']}" for i in custom]
        lines.append("")
        lines.append("用法:/分身 与 @群友 互动方式;也可以直接自由描述一段行动,由你们的性格与羁绊决定走向。")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(PermissionType.ADMIN)
    @oc.command("添加互动", alias={"add_interaction"})
    @_guard
    async def cmd_add_inter(self, event: AstrMessageEvent, name: str = "", descr: str = ""):
        """分身 添加互动 <名称> <说明> - 添加群自定义互动(管理员)"""
        gid = self._need_gid(event)
        if not name or not descr:
            yield event.plain_result("格式:/分身 添加互动 <名称> <说明>")
            return
        key = name[:8]  # 与库里存储名一致,否则后续「删除互动」按全名匹配不到
        self.db.add_interaction(gid, key, descr[:80], self._uid(event))
        yield event.plain_result(f"✅ 互动「{key}」已加入本群菜单")

    @filter.permission_type(PermissionType.ADMIN)
    @oc.command("删除互动", alias={"del_interaction"})
    @_guard
    async def cmd_del_inter(self, event: AstrMessageEvent, name: str = ""):
        """分身 删除互动 <名称> - 删除群自定义互动(管理员)"""
        gid = self._need_gid(event)
        ok = self.db.del_interaction(gid, name or "")
        yield event.plain_result(f"{'✅ 已删除' if ok else '❌ 没有找到'}「{name}」")

    @oc.command("npc", alias={"NPC", "Npc", "与npc"})
    @_guard
    async def cmd_npc(self, event: AstrMessageEvent):
        """分身 npc <名字> <想做什么> - 与当前世界的NPC互动"""
        gid = self._need_gid(event)
        rest = self._rest(event, "npc", "NPC", "Npc", "与npc")
        parts = rest.split(None, 1)
        if len(parts) < 2:
            w = self.db.cur_world(gid)
            names = "、".join(w.npc_names()) if w else "无"
            yield event.plain_result(f"格式:/分身 npc <名字> <想做什么>\n当前世界NPC:{names}")
            return
        yield event.plain_result("⏳ 正在与NPC对话,请稍候…")
        async with self._glock(gid):
            v = await self.game.npc_interact(gid, self._uid(event), parts[0][:12], parts[1][:80])
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    # ═══════════════════════════ 指令:主动行动 ═══════════════════════════
    async def _run_act(self, event, act_key: str, *aliases):
        """执行一次主动行动:先回执提示(推演耗时),再渲染结果卡片链。"""
        gid = self._need_gid(event)
        self._char_of(event)
        yield event.plain_result("⏳ 正在推演这次行动,请稍候…")
        detail = self._rest(event, *aliases).strip()
        async with self._glock(gid):
            v = await self.game.act(gid, self._uid(event), act_key, detail)
        yield event.chain_result(self._chain(render_views([v], self._card_cfg())))

    @oc.command("练习", alias={"训练", "train", "practice"})
    @_guard
    async def cmd_practice(self, event: AstrMessageEvent):
        """分身 练习 <想练什么…> - 主动修习/训练,精进一门手艺(耗体力)"""
        async for r in self._run_act(event, "练习", "练习", "训练", "train", "practice"):
            yield r

    @oc.command("健身", alias={"fitness", "锻炼"})
    @_guard
    async def cmd_fitness(self, event: AstrMessageEvent):
        """分身 健身 [附加描述…] - 锻炼体魄,强健力量/敏捷(耗体力)"""
        async for r in self._run_act(event, "健身", "健身", "锻炼", "fitness"):
            yield r

    @oc.command("打怪", alias={"fight", "狩猎"})
    @_guard
    async def cmd_fight(self, event: AstrMessageEvent):
        """分身 打怪 <目标…> - 去危险地带猎杀怪物,搏战利品与名声(风险行动)"""
        async for r in self._run_act(event, "打怪", "打怪", "狩猎", "fight"):
            yield r

    @oc.command("冒险", alias={"行动"})
    @_guard
    async def cmd_adventure(self, event: AstrMessageEvent):
        """分身 冒险 <自由行动描述…> - 完全自定义的主动行动(风险,回报与危险并存)"""
        gid = self._need_gid(event)
        self._char_of(event)
        detail = self._rest(event, "冒险", "行动").strip()
        if not detail:
            lines = [
                "你可以主动行动,让角色推进故事。现成的行动:",
                "· /分身 练习 <练什么> — 修习技艺,精进属性(耗体力)",
                "· /分身 健身 — 锻炼体魄(力量/敏捷)",
                "· /分身 兼职 — 在世界上找份基建活干,赚金币",
                "· /分身 打怪 - 去危险地带挑战怪物(高风险高回报)",
                "",
                "· /分身 冒险 <自由描述> — 比如:去雾夜集市帮绫婆婆看摊 / 溜进灯塔偷看旧笔记",
                "每次行动消耗体力,一天限次。属性/金币/心情都会随之起落!",
            ]
            yield event.plain_result("\n".join(lines))
            return
        yield event.plain_result("⏳ 正在推演这次冒险,请稍候…")
        async with self._glock(gid):
            v = await self.game.act(gid, self._uid(event), "冒险", detail)
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    # ═══════════════════════════ 指令:世界NPC自定义 ═══════════════════════════
    def _parse_npc_fields(self, rest: str) -> tuple[str, str, str, str, str]:
        """解析 名字|职业|性格|钩子 与可选 [世界] 标签。"""
        world_ref = ""
        if rest.rstrip().endswith("]"):
            m = re.match(r"^(.*)\s*\[([^\[\]]+)\]\s*$", rest, re.S)
            if m:
                rest = m.group(1).strip()
                world_ref = m.group(2).strip()
        parts = [p.strip() for p in rest.split("|")]
        name = parts[0] if parts else ""
        role = parts[1] if len(parts) > 1 else ""
        persona = parts[2] if len(parts) > 2 else ""
        hook = parts[3] if len(parts) > 3 else ""
        return name, role, persona, hook, world_ref

    @oc.command("添加NPC", alias={"添加npc", "add_npc", "new_npc", "新npc"})
    @_guard
    async def cmd_add_npc(self, event: AstrMessageEvent):
        """分身 添加NPC <名字> [描述…] [世界名] - 给世界添加一位NPC(AI 自动整理档案)"""
        gid = self._need_gid(event)
        rest = self._rest(event, "添加NPC", "添加npc", "add_npc", "new_npc", "新npc")
        if not rest:
            yield event.plain_result(
                "格式:/分身 添加NPC <名字> [描述…] [世界名](AI 自动整理档案)\n"
                "例如:/分身 添加NPC 鱼婆 雾码头卖鱼的老婆婆,神神秘秘,似乎认得每一条旧船 [锈海城]\n"
                "也兼容竖线:/分身 添加NPC <名字>|职业|性格|钩子 [世界名]"
            )
            return
        if "|" in rest:
            # 旧竖线路径(含 [世界名] 后缀解析)
            name, role, persona, hook, world_ref = self._parse_npc_fields(rest)
            if not name:
                yield event.plain_result("名字不能为空:/分身 添加NPC <名字>|职业|性格|钩子 [世界名]")
                return
        else:
            # 自然语言:首词为名字,余下描述连同世界数据一起交给 AI,确保档案贴合世界观
            toks = rest.split(None, 1)
            name = toks[0][:12]
            desc = (toks[1] if len(toks) > 1 else "").strip()
            world_ref = ""
            m = re.match(r"^(.*?)\s*\[([^\[\]]+)\]\s*$", desc, re.S)
            if m:
                desc, world_ref = m.group(1).strip(), m.group(2).strip()
            w = self.game._find_world(gid, world_ref)  # 提前定位世界(报错更及时)
            self.game._require_user_world(w)           # 系统世界直接拦截,不浪费 AI 调用
            role, persona, hook = "", "", ""
            if desc:
                yield event.plain_result("⏳ 正在整理NPC档案,请稍候…")
                r = await self.brain.parse_npc(name, desc, world=w, npc_names=w.npc_names())
                if r.ok:
                    role, persona, hook = r.data["role"], r.data["persona"], r.data["hook"]
                else:
                    hook = desc[:40]  # AI 不可用:描述原文兜底进钩子
        async with self._glock(gid):
            wname, npc = await self.game.add_npc(gid, self._uid(event), name, role, persona, hook, world_ref)
        yield event.plain_result(
            f"🗣 已在《{wname}》添加NPC「{npc['name']}」\n"
            f"职业:{npc['role']} | 性格:{npc['persona']} | 钩子:{npc['hook']}"
            f"\n可用「/分身 npc {npc['name']} <想做什么>」与TA互动"
        )

    @oc.command("删除NPC", alias={"删除npc", "del_npc"})
    @_guard
    async def cmd_del_npc(self, event: AstrMessageEvent):
        """分身 删除NPC <名字> [世界名] - 从(当前/指定)世界移除一位NPC"""
        gid = self._need_gid(event)
        name, _role, _p, _h, world_ref = self._parse_npc_fields(self._rest(event, "删除NPC", "删除npc", "del_npc"))
        async with self._glock(gid):
            wname, rm = self.game.del_npc(gid, self._uid(event), name, world_ref)
        yield event.plain_result(f"🕳《{wname}》的NPC「{rm}」已默默离开了。")

    @oc.command("NPC列表", alias={"npcs", "npc列表"})
    @_guard
    async def cmd_npc_list(self, event: AstrMessageEvent):
        """分身 NPC列表 [世界名] - 看目前有哪些NPC"""
        gid = self._need_gid(event)
        _n, _r, _p, _h, world_ref = self._parse_npc_fields(self._rest(event, "NPC列表", "npcs", "npc列表"))
        w, npcs = self.game.list_npcs(gid, world_ref)
        if not npcs:
            yield event.plain_result(f"《{w.name}》还没有NPC。用「/分身 添加NPC 名字|职业|性格|钩子」添加一位吧。")
            return
        lines = [f"《{w.name}》的住民:({len(npcs)}位)", ""]
        for i, n in enumerate(npcs, 1):
            lines.append(f"{i}. {n.get('name','?')} — {n.get('role','居民')}")
            lines.append(f"   人格:{n.get('persona','')}")
            if n.get('hook'):
                lines.append(f"   钩子:{n.get('hook','')}")
        lines.append("")
        lines.append("用「/分身 npc <名字> <想做什么>」勾搭任意一位。")
        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════ 指令:记忆/杂项 ═══════════════════════════
    @oc.command("日志", alias={"log", "人生日志"})
    @_guard
    async def cmd_log(self, event: AstrMessageEvent, page: str = ""):
        """分身 日志 [页码] - 分身的人生流水(默认自己的)"""
        gid = self._need_gid(event)
        ch = self._char_of(event)
        p = re.sub(r"\D", "", page or self._rest(event, "日志", "log", "人生日志")) or "1"
        page_n = max(1, int(p))
        limit = 20
        total = self.db.count_logs(gid, ch.uid)
        pages = max(1, (total + limit - 1) // limit)
        page_n = min(page_n, pages)  # 页码超出总页数时回到最后一页
        entries = self.db.recent_logs(gid, ch.uid, limit=limit, offset=(page_n - 1) * limit)
        name_map = {c.uid: c.name for c in self.db.list_chars(gid)}
        imgs = log_card(entries, page_n, pages, f"{ch.name} 的人生日志", self._card_cfg(), name_map)
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    @oc.command("回忆", alias={"memory", "回忆检索"})
    @_guard
    async def cmd_memory(self, event: AstrMessageEvent):
        """分身 回忆 <关键词> - 语义检索本群记忆"""
        gid = self._need_gid(event)
        q = self._rest(event, "回忆", "memory", "回忆检索")
        if not q:
            yield event.plain_result("格式:/分身 回忆 <关键词>,例如:/分身 回忆 雾夜")
            return
        results = self.mem.related_by_keyword(gid, q, k=6)
        imgs = memory_card(q, results, self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    @oc.command("运势", alias={"fortune"})
    @_guard
    async def cmd_fortune(self, event: AstrMessageEvent):
        """分身 运势 - 今日运势(免费,不耗LLM)"""
        self._need_gid(event)
        ch = self._char_of(event)
        f = self.game.fortune(ch.uid, ch.name)
        imgs = fortune_card(f, self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    # ═══════════════════════════ 指令:每日小任务 ═══════════════════════════
    @oc.command("任务", alias={"quests", "quest"})
    @_guard
    async def cmd_quests(self, event: AstrMessageEvent):
        """分身 任务 - 领取/查看今天的简单小任务(AI 按世界生成)"""
        gid = self._need_gid(event)
        ch = self._char_of(event)
        yield event.plain_result("⏳ 正在生成今日任务,请稍候…")
        async with self._glock(gid):
            qs = await self.game.ensure_quests(gid, self._uid(event))
        open_qs = [q for q in qs if q["state"] == "open"]
        done = len(qs) - len(open_qs)
        if not open_qs:
            yield event.plain_result(f"🎉 今天给「{ch.name}」的任务都完成啦,明天再来。")
            return
        lines = [f"📌 {ch.name} 今天的小任务({done}/{len(qs)} 已完成)", ""]
        for i, q in enumerate(open_qs, 1):
            lines.append(f"{i}. {q['text']}")
            if q.get("hint"):
                lines.append(f"　 └ {q['hint']}")
        lines.append("")
        lines.append("完成方式:/分身 交任务 <编号>(轻松结算,有小奖励)")
        yield event.plain_result("\n".join(lines))

    @oc.command("交任务", alias={"完成任务", "quest_done"})
    @_guard
    async def cmd_quest_done(self, event: AstrMessageEvent, idx: str = ""):
        """分身 交任务 <编号> - 完成今天的一个小任务,拿点小奖励"""
        gid = self._need_gid(event)
        self._char_of(event)
        n = re.sub(r"\D", "", idx or self._rest(event, "交任务", "完成任务", "quest_done"))
        if not n:
            yield event.plain_result("格式:/分身 交任务 <编号>(编号见「/分身 任务」)")
            return
        yield event.plain_result("⏳ 正在结算任务,请稍候…")
        async with self._glock(gid):
            v = await self.game.complete_quest(gid, self._uid(event), int(n) - 1)
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)
