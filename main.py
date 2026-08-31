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
from .ocverse.game import Game, GameError, is_npc_uid, npc_uid
from .ocverse.imcard import strip_script  # noqa: F401
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


def _guard_tool(fn):
    """Agent 工具守卫:异常以文本返回(工具结果是给 LLM 看的字符串)。

    functools.wraps 保留 docstring,供 `_build_tool_set` 解析参数生成工具 schema。
    """

    @functools.wraps(fn)
    async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
        try:
            return await fn(self, event, *args, **kwargs)
        except GameError as e:
            return f"❌ {e}"
        except Exception as e:
            logger.exception(f"ocverse: 工具 {getattr(fn, '__name__', '?')} 执行异常: {e}")
            return f"❌ 执行出错:{e}"
    return wrapper


def _parse_docstring_tool(docstring: str) -> tuple[str, dict]:
    """从 llm_tool 风格 docstring 解析出 (description, parameters schema)。

    复用 astrbot.core.star.register.star_handler 的解析思路;
    类型映射复用 astrbot.core.provider.func_tool_manager.PY_TO_JSON_TYPE。

    Args:
        param_name(string): 描述(含 "可选"/"默认"/"=" → 非必填)
    """
    from astrbot.core.provider.func_tool_manager import PY_TO_JSON_TYPE
    import docstring_parser

    if not docstring:
        return "", {"type": "object", "properties": {}}
    parsed = docstring_parser.parse(docstring)
    description = (parsed.description or "").strip()
    properties: dict = {}
    required: list[str] = []
    for arg in parsed.params:
        type_name = (arg.type_name or "").strip().lower()
        if not type_name:
            continue
        sub_type_name = None
        nested = __import__("re").match(r"(\w+)\s*\[\s*(\w+)\s*\]", type_name)
        if nested:
            type_name, sub_type_name = nested.group(1), nested.group(2)
        json_type = PY_TO_JSON_TYPE.get(type_name, type_name)
        if json_type not in ("string", "number", "object", "array", "boolean"):
            continue
        prop: dict = {"type": json_type, "description": (arg.description or "").strip()}
        if sub_type_name:
            sub_json = PY_TO_JSON_TYPE.get(sub_type_name, sub_type_name)
            if json_type == "array":
                prop["items"] = {"type": sub_json}
        properties[arg.arg_name] = prop
        desc_lower = (arg.description or "").lower()
        if "可选" not in desc_lower and "默认" not in desc_lower and "=" not in desc_lower:
            required.append(arg.arg_name)
    parameters: dict = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required
    return description, parameters


AUTHOR = "morinoyuki"
VERSION = "1.2.1"
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
        self._life_idle_cd: dict[str, float] = {}  # gid -> 上次触发静默遭遇的时间(限频)
        self._umo_map: dict[str, str] = {}      # gid -> unified_msg_origin(本会话内观察)
        self._pending: dict[str, list] = {}     # gid -> 主动卡片积压(无法主动发时)
        self._glocks: dict[str, asyncio.Lock] = {}  # 每群一把锁:LLM 调用期间锁定改数据的指令,防竞态
        self._confirm: dict[str, float] = {}    # 二次确认状态
        self._default_mode_hint = {m: d for m, d in C.DEFAULT_INTERACTIONS}
        self._web_tools = None  # 联网搜索工具集(懒加载缓存)
        self._cached_tool_set = None  # 自然语言工具集(懒构建缓存)
        self._global_tools_done = False  # 全局 LLM 工具已注册标记

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
            try:
                # 持久化:重启后晨报/事件/远征等主动推送仍能找到发送通道,
                # 避免落入"积压-补发"路径(补发曾以消息引用形态出现)
                self.db.kv_set(gid, "umo", event.unified_msg_origin)
            except Exception:
                pass
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
        """从消息组件解析被 @ 的目标。
        排除:发送者本人、机器人自身(QQ 官方接口唤醒时消息自带 At(bot),
        此前被误当成互动目标 →「找不到这个目标」)、@全体。
        QQ 官方接口的 openid 可能是非数字,同样接受。"""
        try:
            me = self._uid(event)
            try:
                bot = str(event.get_self_id() or "")
            except Exception:
                bot = ""
            for c in event.get_messages():
                if isinstance(c, At):
                    t = str(getattr(c, "qq", "") or getattr(c, "target_id", "") or "")
                    if t and t not in (me, bot, "all"):
                        return t
        except Exception:
            pass
        return ""

    def _resolve_interact_target(self, gid: str, event, raw: str) -> tuple[str | None, str]:
        """互动/关系类命令的目标解析:@ 组件优先 → 文本里的 @名字 → 直接角色名。
        返回 (目标uid 或 None, 互动方式文本)。"""
        raw = (raw or "").strip()
        text_wo_at = re.sub(r"@\S+", "", raw).strip()
        # 1) @ 消息组件(平台原生):指向的对象必须有分身,
        #    否则视为无效(官方接口的唤醒 At、@ 了没分身的人等)→ 回落文本名字解析
        at_uid = self._at_target(event)
        if at_uid and self.db.get_char(gid, at_uid) is not None:
            return at_uid, text_wo_at
        # 2) 文本首段当名字:兼容「@名字」(官方接口只有纯文本)与直接写角色名
        if raw:
            first, rest = (raw.split(None, 1) + [""])[:2]
            cand = first.lstrip("@").strip()
            if cand:
                tchar = self._char_by_name(gid, cand)
                if tchar is not None:
                    return tchar.uid, (rest.strip() or text_wo_at)
        return None, text_wo_at

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
    def _prime_qqscene(self, umo: str) -> None:
        """QQ 官方适配器主动群消息强依赖其内存里的 scene 缓存(收到群消息时才
        填充);机器人重启后缓存清空,晨报/事件/远征等主动推送会被
        send_by_session 静默 skip("No cached msg_id")而丢弃。这里主动补记
        'group' 场景,使其能走主动推送(配合 _allow_group_proactive_send,不要求 msg_id)。"""
        if not umo or ":" not in umo:
            return
        try:
            from astrbot.core.platform.message_session import MessageSesion
            sess = MessageSesion.from_str(umo)
            if sess.message_type.value != "GroupMessage":
                return
            pm = getattr(self.context, "platform_manager", None)
            insts = getattr(pm, "platform_insts", None) if pm else None
            if not insts:
                return
            for p in insts:
                try:
                    if getattr(p.meta(), "id", None) != sess.platform_name:
                        continue
                    rs = getattr(p, "remember_session_scene", None)
                    if rs is not None:
                        rs(sess.session_id, "group")
                except Exception:
                    continue
        except Exception:
            return

    async def _send_to(self, umo: str, chain: list) -> bool:
        fn = getattr(self.context, "send_message", None)
        if fn is None or not chain:
            return False
        try:
            # 主动发送命中匹配平台;对 qq-official 的问题是重启后 scene 缓存丢失,
            # send_by_session 会静默 return(不抛异常),插件此前误判为成功。
            self._prime_qqscene(umo)
            ok = await fn(umo, MessageChain(chain=chain))
            if not ok:
                logger.debug(f"ocverse: 主动发送未命中平台(umo={umo})")
            return bool(ok)
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
        # 自然语言 Agent 工具:注册进全局 LLM 工具表,普通聊天里可直接用大白话操作
        try:
            self._register_global_tools()
        except Exception as e:
            logger.warning(f"ocverse: 全局工具注册失败: {e}")
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

            async def regen_content(self_, gid: str, world_id: int | None = None) -> dict:
                async with plugin._glock(gid):
                    msg, zones, heals = await plugin.game.regen_zones_heals(gid, world_id=world_id)
                return {"message": msg, "zones": zones, "heal_items": heals}

            async def regen_mainline(self_, gid: str, world_id: int | None = None) -> dict:
                async with plugin._glock(gid):
                    msg, nodes = await plugin.game.regen_mainline(gid, world_id=world_id)
                return {"message": msg, "mainline": nodes}

            async def delete_world(self_, gid: str, world_id: int) -> dict:
                async with plugin._glock(gid):
                    msg = plugin.game.delete_world(gid, str(world_id))
                return {"message": msg}

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
        # 兼职到点自动下班结算(时段工:上工后无需再敲指令,到点发下班结算卡)
        work_views = []
        for g in self.db.list_groups():
            gid = g["gid"]
            try:
                async with self._glock(gid):
                    work_views += await self.game._sweep_work(gid)
            except Exception as e:
                logger.warning(f"ocverse: 群{gid}下班结算失败: {e}")
        if work_views:
            await self._broadcast(work_views)
        # 远征:到点播报剧情片段/归来结算(每几小时一段,归来是高潮大结算)
        exp_views = []
        for g in self.db.list_groups():
            gid = g["gid"]
            try:
                async with self._glock(gid):
                    exp_views += await self.game._sweep_expeditions(gid)
            except Exception as e:
                logger.warning(f"ocverse: 群{gid}远征扫描失败: {e}")
        if exp_views:
            await self._broadcast(exp_views)

    # ── 知识库定时采集:每天每组入库一条素材(联网/LLM),供所有生成功能注入 ──
    async def _kb_maintenance(self):
        """知识库日常采集:每群每天 1~3 条(失败次日重试;全天至多失败 3 次后放弃)。"""
        day = self.game._day_key()
        for g in self.db.list_groups():
            gid = g["gid"]
            try:
                await self._kb_collect_group(gid, day)
            except Exception as e:
                logger.warning(f"ocverse: 知识库维护异常(群{gid}): {e}")

    async def _kb_collect_group(self, gid: str, day: str):
        """单群的当日采集调度(标记键 kb_last2;失败计数 kb_fail:<day>,至多 3 次)。"""
        done_key, fail_key = "kb_last2", f"kb_fail:{day}"
        max_n = self._cfgi("knowledge_base_max", 40)
        if self.kb.count(gid) >= max_n:
            self.db.kv_set(gid, done_key, day)
            return
        if self.db.kv_get(gid, done_key) == day:
            return
        try:
            fails = int(self.db.kv_get(gid, fail_key) or 0)
        except (TypeError, ValueError):
            fails = 0
        if fails >= 3:
            self.db.kv_set(gid, done_key, day)   # 今日放弃,明天再试
            return
        # 每群每天采集(默认1条,可配置),用序号错开题材,避免同批同主题重复
        n = max(0, min(3, self._cfgi("knowledge_collect_daily", 1)))
        base = self.kb.count(gid)
        ok = True
        for i in range(n):
            if self.kb.count(gid) >= max_n:
                break
            try:
                if not await self._collect_kb(gid, offset=base + i):
                    ok = False
            except Exception as e:
                logger.warning(f"ocverse: 知识库采集失败(群{gid}): {e}")
                ok = False
        if ok:
            self.db.kv_set(gid, done_key, day)
            self.db.kv_set(gid, fail_key, "")
        else:
            self.db.kv_incr(gid, fail_key)

    async def _collect_kb(self, gid: str, offset: int = 0) -> bool:
        """采集一条轻小说/动漫/漫画风格的著作素材,提炼成可复用条目存入知识库。
        offset: 同批内第几条,用于错开本轮题材(避免一条批次全同题)。返回是否成功入库。"""
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
            logger.warning("ocverse: 知识库采集失败(本轮):LLM 无可用输出")
            return False
        d = _extract_json(text) or {}
        content = (str(d.get("content") or "")).strip()
        if len(content) < 40:  # 内容过短(拆不出可用素材)则丢弃,不进库
            logger.warning("ocverse: 知识库采集失败(本轮):素材内容过短,已丢弃")
            return False
        source = str(d.get("source") or "")[:60]
        theme_used = str(d.get("theme") or theme)[:30]
        nid = await self.kb.add(gid,
                                source,
                                theme_used,
                                str(d.get("kind") or "work")[:12],
                                content[:1500])
        if nid is None:   # 查重拒绝或入库异常 → 视为本轮失败(有重试上限,不会风暴)
            logger.warning("ocverse: 知识库采集失败(本轮):内容与库内重复或入库异常")
            return False
        logger.info(f"ocverse: 知识库采集入库 #{nid}《{source or '?'}》({theme_used})")
        await asyncio.sleep(0)  # 让出事件循环
        return True

    async def _fire_plan_item(self, gid: str, item: dict, forced: bool = False) -> dict | None:
        kind = item["kind"]
        if kind == "event":
            return await self.game.fire_event(gid)
        if kind == "shift":
            return await self.game.world_shift(gid)
        if kind == "morning":
            return await self.game.fire_morning(gid)
        if kind == "life_event":
            return await self.game.fire_life_event(gid)
        _ = forced
        return None

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    @_guard
    async def on_group_msg(self, event: AstrMessageEvent):
        """观察群消息:记录 umo + 补发积压 + 引爆已武装的被动事件 + 顺带唤醒静默的生活角色。"""
        gid = self._gid(event)
        if not gid:
            return
        self._remember_umo(event)
        # 静默生活角色“活起来”:群里有动静时,若有长期没人搭理的生活角色,
        # 低概率顺带生成TA们的沉默期遭遇(属性/生命/经验/记忆随之变化)。
        try:
            async with self._glock(gid):
                threshold = self._cfgi("life_idle_trigger_hours", 0)
                if threshold > 0:
                    now = time.time()
                    # 每群至多每 30 分钟触发一次,避免刷屏
                    if now - self._life_idle_cd.get(gid, 0) > 1800:
                        idle = self.game.idle_life_chars(gid, float(threshold))
                        if idle:
                            c, hrs = idle[0]
                            v = await self.game.fire_life_event(gid, idle_hours=hrs)
                            self._life_idle_cd[gid] = now
                            if v:
                                chain = self._chain_views([v])
                                umo = self._umo_map.get(gid) or (self.db.kv_get(gid, "umo") or "")
                                if chain and umo:
                                    await self._send_to(umo, chain)
        except Exception as e:
            logger.warning(f"ocverse: 静默生活角色唤醒异常: {e}")
        pend = self._pending.get(gid)
        if pend:
            v = None
            while pend:  # 跳过已失效的事件卡(被顶替/过期/结算),死卡补发只会误导抉择
                cand = pend.pop(0)
                if self._view_deliverable(cand):
                    v = cand
                    break
            if v is not None:
                # 非个人剧情(晨报/主动事件/远征等)一律走主动通道发送:
                # 以消息上下文回复会把卡片挂在触发消息上,引用无关群友的信息
                sent = False
                try:
                    chain = self._chain_views([v])
                    umo = self._umo_map.get(gid) or (self.db.kv_get(gid, "umo") or "")
                    if chain and umo:
                        sent = await self._send_to(umo, chain)
                except Exception as e:
                    logger.warning(f"ocverse: 补发卡片异常: {e}")
                if sent:
                    self._mark_sent(v)  # 送出才算「发送过」,之后才可回落结算
                else:
                    pend.insert(0, v)   # 主动通道暂不可用 → 重新排队,下条消息再试
                    logger.warning(f"ocverse: 群{gid} 积压卡片主动补发失败,已重新排队")
            if sent:
                return
            # 主动通道失败时不阻断:继续检查被动事件引爆(引爆卡是个人的,走消息上下文)
        # 被动事件:群里有动静,伏笔引爆(每次消息最多一个,自然限流)
        # 被动事件:群里有动静,伏笔引爆(每次消息最多一个,自然限流)
        armed = self.game.armed_passives(gid)
        if not armed:
            return
        item = armed[0]
        # 角色事件只能由本人消息引爆:无分身的群友发言不触发,
        # 事件保持待命等本人发言;绝不把别人的角色卷进来
        # (引爆过程不发「请稍候」类提示,事件卡生成后直接送达)
        _speaker = self.db.get_char(gid, self._uid(event))
        if not _speaker:
            return
        if self.game._on_expedition(_speaker):
            return  # 远征途中不在群世界"现场",事件伏笔等TA归来再说
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

    # ═══════════════════════════ 自然语言 Agent 工具(ocverse_*) ═══════════════════════════
    # 两种使用方式:
    # 1) global_nl_tools 开启时,同一批工具注册进 provider_manager.llm_tools ——
    #     用户在普通聊天里用大白话描述意图,主力智体会按需调用;
    # 2) /分身 说 <自然语言>:插件自建 ToolSet 跑 tool_loop_agent,只暴露本插件工具,
    #     不依赖主力智体是否开启函数调用。
    def _build_tool_set(self):
        """收集本类所有 ocverse_* 方法构建 ToolSet(bound method 作 handler,自动解析 docstring)。

        工具集运行时不变,懒缓存避免每轮重建。
        """
        if getattr(self, "_cached_tool_set", None) is not None:
            return self._cached_tool_set
        from astrbot.core.agent.tool import FunctionTool, ToolSet
        ts = ToolSet()
        for attr_name in dir(self):
            if not attr_name.startswith("ocverse_"):
                continue
            attr = getattr(self, attr_name, None)
            if attr is None or not callable(attr):
                continue
            doc = getattr(attr, "__doc__", "") or ""
            desc, params = _parse_docstring_tool(doc)
            ts.add_tool(FunctionTool(name=attr_name, parameters=params,
                                     description=desc or attr_name, handler=attr))
        if len(ts) == 0:
            logger.warning("ocverse: 未找到任何 ocverse_* 工具,自然语言指令不可用")
        self._cached_tool_set = ts
        return ts

    def _register_global_tools(self):
        """把 ocverse_* 工具注册进全局 llm_tools(普通聊天可被主力智体调用)。
        handler 用 bound method,避免 unbound 调用时 event 变成 self 的 bug。"""
        if getattr(self, "_global_tools_done", False):
            return
        self._global_tools_done = True
        if not self._cfg("global_nl_tools", True):
            return
        try:
            pm = getattr(self.context, "provider_manager", None)
            if pm is None or not hasattr(pm, "llm_tools"):
                return
            registry = pm.llm_tools
            add_tool = getattr(registry, "add_tool", None)
            add_func = getattr(registry, "add_func", None)
            if add_tool is None and add_func is None:
                return
            count = 0
            for ft in self._build_tool_set():
                if add_tool is not None:
                    add_tool(ft)  # ToolSet 风格注册(新版本)
                else:
                    # FuncCall 风格:func_args 列表 + handler
                    props = (ft.parameters or {}).get("properties", {})
                    args = [{"type": p.get("type", "string"), "name": pname,
                             "description": p.get("description", "")}
                            for pname, p in props.items()]
                    add_func(ft.name, args, ft.description, ft.handler)
                count += 1
            logger.info(f"ocverse: 已注册 {count} 个自然语言工具到全局 LLM 工具表")
        except Exception as e:
            logger.warning(f"ocverse: 全局工具注册失败(不影响 /分身 说): {e}")

    async def _chat_provider_id(self, event: AstrMessageEvent) -> str:
        """解析本轮可用的 chat provider id(配置优先 → 会话默认 → 全局默认)。"""
        pid = str(self._cfg("provider_id", "") or "").strip()
        if pid:
            return pid
        umo = event.unified_msg_origin or self._umo_map.get("__last__")
        if umo and hasattr(self.context, "get_current_chat_provider_id"):
            try:
                cpid = await self.context.get_current_chat_provider_id(umo=umo)
                if cpid:
                    return str(cpid)
            except Exception:
                pass
        gp = getattr(self.context, "get_using_provider", None)
        try:
            prov = gp() if gp else None
            if prov is not None:
                return str(getattr(prov, "provider_id", "") or "")
        except Exception:
            pass
        return ""

    def _char_by_name(self, gid: str, name: str):
        """按名字查找角色(真人分身或生活角色),先精确后包含。"""
        name = (name or "").strip()
        if not name:
            return None
        c = self.db.get_char_by_name(gid, name)
        if c:
            return c
        chars = self.db.list_chars(gid)
        for c in chars:
            if len(name) >= 2 and name in c.name:
                return c
        for c in chars:
            if len(name) >= 2 and c.name in name:
                return c
        return None

    def _char_text(self, gid: str, uid: str) -> str:
        """角色卡文字版(给 LLM/工具的紧凑摘要)。"""
        ch = self.db.get_char(gid, uid)
        if not ch:
            return "(没有找到该角色)"
        from .ocverse.game import is_npc_uid
        w = self.db.cur_world(gid)
        rels = self.db.list_rels_for(gid, uid, 3)
        name_map = {c.uid: c.name for c in self.db.list_chars(gid)}
        rel_line = "、".join(
            f"{name_map.get(u, u[:8])}({self.game.rel_stage_label(gid, uid, u)})" for u, _s in rels) or "无"
        attrs = " ".join(f"{C.ATTR_NAMES.get(k, k)} {ch.attrs.get(k, 0)}" for k, _ in C.ATTRS)
        mark = "(生活角色)" if is_npc_uid(uid) else "(玩家)"
        lines = [
            f"【{ch.name}】{mark} Lv{ch.level} {ch.title} · {ch.gender}",
            f"性格:{'、'.join(ch.tags or []) or '未设定'}",
            f"背景:{str(ch.backstory or '未详')[:150]}",
            f"体力 {ch.stamina}/100 · 心情 {ch.mood}/100 · 生命 {getattr(ch, 'hp', 100)}/100 · 金币 {ch.gold}",
            f"属性: {attrs}",
            f"关系:{rel_line}",
        ]
        _wn = self.game._work_note(ch)
        if _wn:
            lines.append(f"状态:上班中({_wn})")
        _st = (ch.flags or {}).get("_state")
        if isinstance(_st, dict) and (_st.get("type") or _st.get("reason")):
            lines.append(f"状态:被{_st.get('type')}困住({_st.get('reason')})")
        if w is not None and not is_npc_uid(uid):
            rep = self.game.db.rep_get(gid, uid, w.id)
            lines.append(f"声望:{rep}({C.rep_level_label(rep)})")
        items = self.db.items_list(gid, uid)
        if items:
            tail = f" 等{len(items)}件" if len(items) > 5 else ""
            lines.append("背包:" + "、".join(f"{it['name']}×{it['count']}" for it in items[:5]) + tail)
        lines.append(f"所在世界:《{w.name if w else '?'}》")
        return "\n".join(lines)

    def _view_text(self, v: dict) -> str:
        """把 game 层 view dict 转成紧凑文本(工具返回给 LLM 引用)。"""
        if not isinstance(v, dict):
            return ""
        t = v.get("type", "")
        parts: list[str] = []
        if t == "event":
            p = v.get("payload") or {}
            parts.append(f"事件:{(p.get('title') or '').strip()}")
            parts.append(str(p.get("scene", "")).strip())
            for i, o in enumerate((p.get("options") or [])[:3], 1):
                hint = f"({o.get('hint', '')})" if o.get("hint") else ""
                parts.append(f"选项{i}. {o.get('label', '')}{hint}")
        elif t == "morning":
            p = v.get("payload") or {}
            parts.append(str(p.get("brief", "")).strip())
            if p.get("watch"):
                parts.append(f"今日留意:{p.get('watch')}")
        elif t == "arrive":
            parts.append(str(v.get("narration", "")).strip())
            tips = [str(x) for x in (v.get("tips") or [])]
            if tips:
                parts.append("忠告:" + "、".join(tips))
        elif t == "expedition":
            ph = v.get("phase", "")
            lead = {"offer": "📜 远征委托", "depart": "⚔ 出征", "report": f"⚔ 远征·{v.get('phase_name', '')} {v.get('progress', 0)}%",
                    "invite": f"🛡 远征邀约 · {v.get('target_name', '')}",
                    "return": ("🏆 远征凯旋" if v.get("outcome") == "success" else "💀 远征折戟"),
                    "abort": "🏳 中途撤离"}.get(ph, "⚔ 远征")
            if v.get("title"):
                parts.append(f"{lead} · {v['title']}")
            else:
                parts.append(lead)
            if v.get("narration"):
                parts.append(strip_script(str(v["narration"])))
            for d in (v.get("dialogues") or [])[:6]:
                sp = str(d.get("speaker") or "").strip()
                tx = str(d.get("text") or "").strip()
                if sp and tx:
                    parts.append(f"{sp}: {tx}")
            chg = [str(c) for c in (v.get("changes") or [])[:8]]
            if chg:
                parts.append("变化:" + "、".join(chg))
        else:
            if v.get("event_title"):
                parts.append(str(v["event_title"]).strip())
            if v.get("narration"):
                parts.append(strip_script(str(v["narration"])))
            if v.get("reply"):
                parts.append(str(v["reply"]).strip())
            if v.get("brief"):
                parts.append(str(v["brief"]).strip())
            for d in (v.get("dialogues") or [])[:6]:
                sp = str(d.get("speaker") or "").strip()
                tx = str(d.get("text") or "").strip()
                if sp and tx:
                    parts.append(f"{sp}: {tx}")
            chg = [str(c) for c in (v.get("changes") or [])[:6]]
            if chg:
                parts.append("变化:" + "、".join(chg))
            if v.get("rel_label"):
                parts.append(f"关系:{v['rel_label']}")
            if v.get("echo"):
                parts.append(f"主线回响:{v['echo']}")
        return "\n".join(x for x in parts if x)

    def _mix_view(self, v: dict) -> list[dict]:
        """取主 view 并弹出其 extra_views 一起返回(与指令路径一致)。"""
        return [v] + (v.pop("extra_views", []) or [])

    def _nl_system_prompt(self, gid: str, uid: str) -> str:
        """/分身 说 私域 agent 的系统提示词。"""
        w = self.db.cur_world(gid)
        ch = self.db.get_char(gid, uid)
        world_line = f"《{w.name}》[{w.genre}] {w.desc} 氛围:{w.atmosphere}" if w else "世界尚未初始化"
        me_line = f"你是谁:{ch.name} Lv{ch.level} {ch.title} · {'、'.join(ch.tags or []) or ''}" if ch else "你还没有分身"
        return (
            "你是「分身的世界」这个群聊文字游戏模组的操作员,替用户操作游戏。\n"
            f"当前世界:{world_line}\n{me_line}\n"
            "你的工具能在世界里完成创建/查看/互动/冒险/打工/买房/穿越等几乎所有动作。\n"
            "规则:\n"
            "1. 听到用户请求,先判断需要哪些工具,按顺序调用;能一步做完不要绕弯。\n"
            "2. 工具返回的是游戏演出文本(可能较长),你要改写成一段自然、平实、口语化的中文告诉用户,"
            "保留关键信息(角色名、地点、发生的事、数值变化),不要逐字照抄工具输出。\n"
            "3. 用户没说清细节时用合理默认直接执行(如打招呼/随便转转),不要反复追问。\n"
            "4. 用户还没有分身时先创建;世界未初始化则提示需要管理员初始化。\n"
            "5. 文风:平实具体、不夸张不浮夸、说话直白不玩神秘、凡事给出明确结果;"
            "不用感叹号轰炸,不留没头没尾的话。\n"
            "6. 全程用中文,像游戏助手一样干脆。"
        )

    # ══════════════════ 查看类 ══════════════════
    @_guard_tool
    async def ocverse_help(self, event: AstrMessageEvent, ask: str = ""):
        """了解「分身的世界」玩法与当前群状态的总览(世界/居民/自己/能做什么)。

        Args:
            ask(string): 可选,想了解的具体内容
        """
        gid = self._need_gid(event)
        w = self.db.cur_world(gid)
        ch = self.db.get_char(gid, self._uid(event))
        names = "、".join(c.name for c in self.db.list_chars(gid)) or "无"
        return "\n".join([
            "【分身的世界】群聊 OC 养成模组",
            f"· 当前世界:{w.name if w else '尚未初始化'}",
            f"· 本群居民:{names}",
            f"· 你:{ch.name if ch else '还没有分身(直接告诉我想创建什么样的角色即可)'}",
            "· 可以直接对我说:创建/修改分身、看看世界、去某处逛逛、找某人聊天、练技能、"
            "看看任务、买房子、去别的世界、接远征委托、买治疗药、查声望……",
            "· 图文卡片、事件抉择、传图换头像:用「/分身 帮助」看基础指令",
        ])

    @_guard_tool
    async def ocverse_show_character(self, event: AstrMessageEvent, name: str = ""):
        """查看某个角色的分身卡(等级/属性/资源/关系),不填名字看自己。

        Args:
            name(string): 可选,角色名,留空查看自己
        """
        gid = self._need_gid(event)
        uid = self._uid(event)
        if (name or "").strip():
            target = self._char_by_name(gid, name)
            if target is None:
                names = "、".join(c.name for c in self.db.list_chars(gid)) or "无"
                return f"找不到叫「{name}」的角色。现有居民:{names}"
            uid = target.uid
        else:
            self._char_of(event)
        return self._char_text(gid, uid)

    @_guard_tool
    async def ocverse_roster(self, event: AstrMessageEvent):
        """查看本群所有分身/生活角色一览。"""
        gid = self._need_gid(event)
        from .ocverse.game import is_npc_uid
        chars = self.db.list_chars(gid)
        if not chars:
            return "本群还没有任何角色,让群友创建分身吧。"
        lines = [f"本群居民({len(chars)}):"]
        for c in chars:
            mark = "生活角色" if is_npc_uid(c.uid) else "玩家"
            lines.append(f"· {c.name}({mark}) Lv{c.level} {c.title} · {'、'.join(c.tags or [])[:30]}")
        return "\n".join(lines)

    @_guard_tool
    async def ocverse_show_world(self, event: AstrMessageEvent):
        """查看当前所在世界的档案(题材/氛围/规则/独特之处/NPC/设施概况)。"""
        gid = self._need_gid(event)
        w = self.db.cur_world(gid)
        if not w:
            return "世界尚未初始化,请管理员先创建。"
        return "\n".join([
            f"《{w.name}》[{w.genre}] 第{self.game._world_day(gid)}天",
            f"氛围:{w.atmosphere}",
            f"描述:{w.desc}",
            f"规则:{'；'.join(w.rules or [])}",
            f"独特之处:{'、'.join(w.features or [])}",
            f"NPC:{'、'.join(f"{x.get('name','')}({x.get('role','')})" for x in (w.npcs or [])[:12])}",
            f"设施 {len(w.infra or [])} 处 · 主线 {len(w.mainline or [])} 节",
        ])

    @_guard_tool
    async def ocverse_show_worlds(self, event: AstrMessageEvent):
        """查看世界书:已经去过(可穿越)与还没去过(沉眠)的世界。"""
        gid = self._need_gid(event)
        visited = self.db.list_worlds(gid, only_visited=True)
        pending = [w for w in self.db.list_worlds(gid) if not w.visited]
        cur = self.db.cur_world(gid)
        lines = [f"当前世界:《{cur.name}》" if cur else "当前还没有世界"]
        if visited:
            lines.append("已解锁(可穿越):")
            for w in visited:
                mark = " ←当前" if cur and cur.id == w.id else ""
                lines.append(f"· {w.id}.《{w.name}》[{w.genre}]{mark}")
        if pending:
            lines.append("沉眠中(等待世界变动降临):")
            for w in pending:
                lines.append(f"· {w.id}.《{w.name}》[{w.genre}]")
        return "\n".join(lines)

    @_guard_tool
    async def ocverse_show_facilities(self, event: AstrMessageEvent):
        """查看当前世界的设施(商店/饭馆/工作/消遣去处)。"""
        gid = self._need_gid(event)
        w = self.db.cur_world(gid)
        if not w:
            return "世界尚未初始化。"
        if not w.infra:
            return f"《{w.name}》暂时没有特别的基础设施。"
        lines = [f"《{w.name}》的设施:"]
        for i, it in enumerate(w.infra, 1):
            wk = f"｜可打工:{it.get('work')}" if it.get("work") else ""
            lines.append(f"{i}. {it.get('kind','')}·{it.get('name','')} — {it.get('desc','')}{wk}")
        return "\n".join(lines)

    @_guard_tool
    async def ocverse_show_quests(self, event: AstrMessageEvent):
        """查看(必要时自动领取)今天的委托任务列表。"""
        gid = self._need_gid(event)
        ch = self._char_of(event)
        async with self._glock(gid):
            qs = await self.game.ensure_quests(gid, self._uid(event))
        open_qs = [q for q in qs if q["state"] == "open"]
        done = len(qs) - len(open_qs)
        if not open_qs:
            return f"{ch.name} 今天的委托都完成啦({done}/{len(qs)}),明天再来。"
        lines = [f"{ch.name} 今天的委托({done}/{len(qs)} 已完成):"]
        for i, q in enumerate(open_qs, 1):
            lines.append(f"{i}. {q['text']}(委托人:{q.get('giver') or '委托人'})")
            if q.get("place"):
                lines.append(f"　发布于「{q['place']}」")
            if q.get("hint"):
                lines.append(f"　💡 {q['hint']}")
            for s in (q.get("steps") or [])[:3]:
                if isinstance(s, dict):
                    mk = "☑" if s.get("done") else "☐"
                    lines.append(f"　 {mk} {s.get('desc', '')}")
        lines.append("(做完后对我说「交第 N 个任务」)")
        return "\n".join(lines)

    @_guard_tool
    async def ocverse_inventory(self, event: AstrMessageEvent):
        """查看自己的背包物品。"""
        gid = self._need_gid(event)
        ch = self._char_of(event)
        items = self.db.items_list(gid, self._uid(event))
        if not items:
            return f"{ch.name} 的背包空空如也。"
        lines = [f"{ch.name} 的背包({len(items)} 件):"]
        for i, it in enumerate(items, 1):
            note = f" — {it['note']}" if it.get("note") else ""
            lines.append(f"{i}. {it['name']} ×{it['count']}{note}")
        return "\n".join(lines)

    @_guard_tool
    async def ocverse_heal(self, event: AstrMessageEvent, item_name: str = ""):
        """治疗自己的分身:使用背包治疗物品(item_name 留空自动选),或去医院付费治疗。

        Args:
            item_name(string): 可选,要使用的治疗物品名(留空则:有药用要,没药去医院)
        """
        gid = self._need_gid(event)
        uid = self._uid(event)
        self._char_of(event)
        async with self._glock(gid):
            if (item_name or "").strip():
                v = self.game.use_heal_item(gid, uid, item_name.strip())
            else:
                try:
                    v = self.game.use_heal_item(gid, uid)
                except GameError:
                    v = self.game.heal_at_hospital(gid, uid)
        return self._view_text(v)

    @_guard_tool
    async def ocverse_buy_item(self, event: AstrMessageEvent, item_name: str, place: str = ""):
        """在世界的店铺/药铺/诊所购买治疗物品(声望越高折扣越大)。

        Args:
            item_name(string): 治疗物品名(用「分身 区域/设施」了解;或直接说「买治疗药」)
            place(string): 可选,在哪家设施买(留空自动就近)
        """
        gid = self._need_gid(event)
        self._char_of(event)
        async with self._glock(gid):
            v = self.game.buy_item(gid, self._uid(event), (item_name or "").strip(), (place or "").strip())
        return self._view_text(v)

    @_guard_tool
    async def ocverse_show_zones(self, event: AstrMessageEvent):
        """查看当前世界的危险区域(野外/遗迹/地下城/敌对阵营,含敌人与素材,每日变动)。"""
        gid = self._need_gid(event)
        zones = self.game.list_zones(gid)
        if not zones:
            return "当前世界没有已探明的危险区域。"
        lines = ["当前世界的危险区域(每日不定时变动):"]
        for z in zones:
            stars = "★" * max(1, min(5, int(z.get("danger") or 1)))
            en = "、".join(e.get("name", "") for e in (z.get("enemies") or []) if isinstance(e, dict))
            loot = "、".join(z.get("loot") or [])
            lines.append(f"· {z.get('name')}({z.get('kind', '')}) 危险度{stars} — {z.get('desc', '')}")
            if en:
                lines.append(f"  出没:{en}" + (f" | 素材:{loot}" if loot else ""))
        lines.append("(打怪/讨伐会进入这些区域;击败敌人可能掉落素材与治疗物品)")
        return "\n".join(lines)

    @_guard_tool
    async def ocverse_show_reputation(self, event: AstrMessageEvent, name: str = ""):
        """查看分身在世界间的声望(声望高 NPC 更友好、买东西有折扣、主线门槛需要它)。"""
        gid = self._need_gid(event)
        uid = self._uid(event)
        if (name or "").strip():
            t = self._char_by_name(gid, name)
            if t is None:
                return f"找不到叫「{name}」的角色。"
            uid = t.uid
        else:
            self._char_of(event)
        p = self.game.rep_panel(gid, uid)
        lines = [f"{p['char_name']} 的世界声望:"]
        if p.get("current"):
            c = p["current"]
            lines.append(f"· 当前世界《{c['world']}》:{c['score']}({c['label']})")
        for r in p.get("list", [])[:8]:
            lines.append(f"· 《{r['world']}》:{r['score']}({r['label']})")
        if p.get("top"):
            top = "、".join(f"{t['name']}({t['score']})" for t in p["top"])
            lines.append(f"当前世界声望榜:{top}")
        return "\n".join(lines)

    @_guard_tool
    async def ocverse_search_memory(self, event: AstrMessageEvent, keyword: str):
        """按关键词语义检索本群记忆(过去发生过什么)。

        Args:
            keyword(string): 检索关键词,如某人的名字/地点/事件
        """
        gid = self._need_gid(event)
        q = (keyword or "").strip()
        if not q:
            return "请给出检索关键词。"
        results = self.mem.related_by_keyword(gid, q, k=6)
        if not results:
            return f"没有找到与「{q}」相关的记忆。"
        return "\n".join([f"与「{q}」相关的记忆:"] + [f"· {r.get('text', '')}" for r in results])

    @_guard_tool
    async def ocverse_log(self, event: AstrMessageEvent, page: int = 1):
        """查看自己分身的人生日志(最近经历流水)。

        Args:
            page(number): 可选,页码,默认 1
        """
        gid = self._need_gid(event)
        ch = self._char_of(event)
        page_n = max(1, int(page or 1))
        limit = 20
        total = self.db.count_logs(gid, ch.uid)
        pages = max(1, (total + limit - 1) // limit)
        page_n = min(page_n, pages)
        entries = self.db.recent_logs(gid, ch.uid, limit=limit, offset=(page_n - 1) * limit)
        if not entries:
            return f"{ch.name} 还没有人生日志。"
        lines = [f"{ch.name} 的人生日志 · 第{page_n}/{pages}页:"]
        for e in entries[:limit]:
            ts = time.strftime("%m-%d %H:%M", time.localtime(float(e.get("created_at") or 0)))
            lines.append(f"[{ts}] {e.get('text', '')}")
        return "\n".join(lines)

    def _views_text(self, views: list[dict]) -> str:
        """多张 view 的文本合并(含附加场景卡)。"""
        out = []
        for v in views:
            tx = self._view_text(v)
            if tx:
                out.append(tx)
        return "\n---\n".join(out)

    # ══════════════════ 创建 / 修改 ══════════════════
    @_guard_tool
    async def ocverse_create_character(self, event: AstrMessageEvent, name: str, description: str = ""):
        """创建自己的 OC 分身(AI 自动整理人设并按设定分配初始属性;一人一个)。

        Args:
            name(string): 角色名字(1~12字)
            description(string): 必填,外貌/性格/背景等设定描述(供 AI 整理人设)
        """
        gid = self._need_gid(event)
        uid = self._uid(event)
        if self.db.get_char(gid, uid):
            return "你已经有一个分身了;想改人设可以说「修改分身」,想换人先删号。"
        name = (name or "").strip()[:12]
        if not name:
            return "名字不能为空。"
        desc = (description or "").strip()
        if not desc:
            return "设定描述不能为空:请提供外貌/性格/背景等描述,让 AI 整理成人设(如:「白发蓝瞳,性格温柔沉稳,擅长剑术」)。"
        r = await self.brain.parse_persona(desc)
        if r.ok:
            gender, tags, backstory, llm_attrs = r.data["gender"], r.data["tags"], r.data["backstory"], r.data.get("attrs")
        else:
            gender, tags, backstory, llm_attrs = "保密", [], desc[:4000], None
        async with self._glock(gid):
            ch = self.game.create_char(gid, uid, name, gender, tags, backstory, attrs=llm_attrs)
        return (
            f"分身「{ch.name}」创建成功!\n"
            + self._char_text(gid, uid)
            + "\n头像可发张图片让我设置;想改人设随时说。"
        )

    @_guard_tool
    async def ocverse_edit_character(self, event: AstrMessageEvent, text: str):
        """修改自己分身的人设(一句话描述想改什么,AI 自动处理)。

        Args:
            text(string): 修改描述,如「改成白发蓝瞳」「性格变得开朗大胆」
        """
        gid = self._need_gid(event)
        uid = self._uid(event)
        ch = self._char_of(event)
        content = (text or "").strip()
        if not content:
            return "请描述要改什么。"
        r = await self.brain.parse_persona_update(
            cur_name=ch.name, cur_gender=ch.gender, cur_tags=list(ch.tags or []),
            cur_backstory=ch.backstory or "", text=content)
        if not r.ok:
            return "没识别出要改的内容,请描述具体一点(如:性别改成…/性格是…/背景…)。"
        d = r.data
        changed = []
        async with self._glock(gid):
            if d.get("gender"):
                self.db.update_char(gid, uid, gender=d["gender"][:8])
                changed.append("性别")
            if d.get("tags"):
                self.db.update_char(gid, uid, tags=d["tags"][:6])
                changed.append("性格")
            if d.get("backstory"):
                self.db.update_char(gid, uid, backstory=d["backstory"][:4000])
                changed.append("背景设定")
        return f"已更新{ch.name}的:{'、'.join(changed)}" if changed else "没识别出要改的内容。"

    @_guard_tool
    async def ocverse_define_life_character(self, event: AstrMessageEvent, name: str, description: str = ""):
        """创造一位持久「生活角色」(不属于任何真人,像群友一样生活在世界里)。

        Args:
            name(string): 生活角色名字
            description(string): 必填,TA 的设定/性格/来历(供 AI/本地整理)
        """
        gid = self._need_gid(event)
        name = (name or "").strip()[:12]
        if not name:
            return "名字不能为空。"
        desc = (description or "").strip()
        if not desc:
            return "设定描述不能为空:请提供 TA 的性格/来历/特征等,让 AI 整理成档案(如:「住在雾码头的老婆婆,神秘而热心」)。"
        existing = self.db.get_char(gid, npc_uid(gid, name))
        async with self._glock(gid):
            ch = self.game.define_npc_char(gid, name, desc, self._uid(event))
        if existing:
            return f"已重设生活角色「{ch.name}」的设定(等级/关系保留)。"
        return f"生活角色「{ch.name}」融入了群世界!可以找TA聊天、发展关系,甚至成婚。"

    @_guard_tool
    async def ocverse_define_world(self, event: AstrMessageEvent, name: str, description: str):
        """把自己原创的世界写进世界书,等待某次世界变动时降临。

        Args:
            name(string): 世界名(1~16字)
            description(string): 世界观描述
        """
        gid = self._need_gid(event)
        name = (name or "").strip()[:16]
        desc = (description or "").strip()
        if not name or not desc:
            return "需要世界名和描述,例如:机械都市 / 一切由齿轮与蒸汽驱动,情感被禁止…"
        async with self._glock(gid):
            r = await self.game.define_world(gid, self._uid(event), name, desc)
        return f"《{r['name']}》已写进世界书,会在某次世界变动时降临——降临后即可自由穿越。"

    @_guard_tool
    async def ocverse_add_npc(self, event: AstrMessageEvent, name: str, description: str, world: str = ""):
        """给世界添加一位 NPC(AI 自动整理档案;可指定世界名,默认当前世界)。

        Args:
            name(string): NPC 名字
            description(string): 职业/性格/来历等描述
            world(string): 可选,所在世界名(留空为当前世界)
        """
        gid = self._need_gid(event)
        name = (name or "").strip()[:12]
        if not name:
            return "名字不能为空。"
        desc = (description or "").strip()[:400]
        world_ref = (world or "").strip()
        if desc:
            w = self.game._find_world(gid, world_ref)
            r = await self.brain.parse_npc(name, desc, world=w, npc_names=w.npc_names())
            role, persona, hook = (r.data["role"], r.data["persona"], r.data["hook"]) if r.ok else ("", "", desc[:40])
        else:
            role, persona, hook = "", "", ""
        async with self._glock(gid):
            wname, npc = await self.game.add_npc(gid, self._uid(event), name, role, persona, hook, world_ref)
        return f"《{wname}》新增NPC「{npc['name']}」:职业{npc['role']} | 性格{npc['persona']} | 钩子:{npc['hook']}"

    @_guard_tool
    async def ocverse_delete_npc(self, event: AstrMessageEvent, name: str):
        """从当前世界移除一位 NPC。

        Args:
            name(string): NPC 名字
        """
        gid = self._need_gid(event)
        name = (name or "").strip()
        if not name:
            return "请给出要删除的 NPC 名字。"
        async with self._glock(gid):
            wname, rm = self.game.del_npc(gid, self._uid(event), name, "")
        return f"《{wname}》的NPC「{rm}」已移除。"
# ══════════════════ 行动类 ══════════════════
    @_guard_tool
    async def ocverse_interact_with_friend(self, event: AstrMessageEvent, target_name: str, action: str = ""):
        """与某位群友的分身互动(打招呼/闲聊/一起做点什么,自由描述即可)。

        Args:
            target_name(string): 对方分身(玩家)的名字
            action(string): 可选,想怎么互动,留空默认打招呼
        """
        gid = self._need_gid(event)
        uid = self._uid(event)
        self._char_of(event)
        target = self._char_by_name(gid, target_name)
        if target is None:
            return f"找不到叫「{target_name}」的分身。"
        if target.uid == uid:
            return "不能和自己互动哦。"
        act = (action or "").strip()
        mode, detail = ("自由互动", act) if act else ("打招呼", self._default_mode_hint["打招呼"])
        async with self._glock(gid):
            v = await self.game.interact(gid, uid, target.uid, mode, detail)
        views = self._mix_view(v)
        return self._views_text(views)

    @_guard_tool
    async def ocverse_interact_with_life(self, event: AstrMessageEvent, name: str, action: str = ""):
        """与某位持久生活角色互动(可发展关系/成婚)。

        Args:
            name(string): 生活角色名字
            action(string): 可选,想怎么互动(聊天/请客/送东西/自由描述)
        """
        gid = self._need_gid(event)
        uid = self._uid(event)
        self._char_of(event)
        act = (action or "").strip()
        mode, detail = ("自由互动", act) if act else ("打招呼", self._default_mode_hint["打招呼"])
        async with self._glock(gid):
            v = await self.game.interact_life_char(gid, uid, (name or "").strip(), mode, detail)
        views = self._mix_view(v)
        return self._views_text(views)

    @_guard_tool
    async def ocverse_interact_with_npc(self, event: AstrMessageEvent, name: str, action: str):
        """与当前世界的 NPC 搭话/办事。

        Args:
            name(string): NPC 名字
            action(string): 想找 TA 做什么/问什么
        """
        gid = self._need_gid(event)
        self._char_of(event)
        async with self._glock(gid):
            v = await self.game.npc_interact(gid, self._uid(event), (name or "").strip()[:12], (action or "").strip()[:80])
        return self._view_text(v)

    @_guard_tool
    async def ocverse_visit_place(self, event: AstrMessageEvent, place: str, action: str = ""):
        """去某个社交/娱乐/约会设施消磨时光(产生小事件)。

        Args:
            place(string): 设施名,如茶馆/酒馆/花园
            action(string): 可选,想在那里做什么
        """
        gid = self._need_gid(event)
        self._char_of(event)
        act = (action or "").strip() or "随便转转"
        async with self._glock(gid):
            v = await self.game.visit_facility(gid, self._uid(event), (place or "").strip(), act)
        return self._view_text(v)

    @_guard_tool
    async def ocverse_do_action(self, event: AstrMessageEvent, kind: str, detail: str = ""):
        """主动行动一次:练习/健身/打怪/冒险(消耗体力,可能掉血也可能大丰收)。

        Args:
            kind(string): 练习 或 健身 或 打怪(讨伐) 或 冒险
            detail(string): 可选,想练什么/想做什么;打怪可点名危险区域或敌人(留空自动选区)
        """
        gid = self._need_gid(event)
        self._char_of(event)
        kind = (kind or "").strip()
        alias_map = {"练习": "练习", "训练": "练习", "健身": "健身", "锻炼": "健身",
                     "打怪": "打怪", "狩猎": "打怪", "讨伐": "打怪", "hunt": "打怪",
                     "冒险": "冒险", "探索": "冒险"}
        act_key = alias_map.get(kind)
        if not act_key:
            return "支持的行动:练习 / 健身 / 打怪 / 冒险。"
        async with self._glock(gid):
            v = await self.game.act(gid, self._uid(event), act_key, (detail or "").strip())
        return self._view_text(v)

    @_guard_tool
    async def ocverse_work_parttime(self, event: AstrMessageEvent):
        """在世界设施里上一班(约2小时后自动下班结算赚金币)。"""
        gid = self._need_gid(event)
        async with self._glock(gid):
            v = self.game.work_today(gid, self._uid(event))
        if v is None:
            return "现在没有适合你打工的地方。"
        return self._view_text(v)

    @_guard_tool
    async def ocverse_expedition(self, event: AstrMessageEvent, action: str = ""):
        """远征系统:查看今日远征委托并接下(接下后进入数小时~数天的远征,期间无法其他操作,
        每几小时播报剧情,归来有丰厚奖励;成功率取决于实力与补给)。

        Args:
            action(string): 可选,查看/接受/状态/放弃(留空=查看今日委托)
        """
        gid = self._need_gid(event)
        uid = self._uid(event)
        self._char_of(event)
        act = (action or "").strip()
        async with self._glock(gid):
            if any(k in act for k in ("接受", "接下", "出发")):
                v = await self.game.accept_expedition(gid, uid)
            elif any(k in act for k in ("放弃", "逃", "撤")):
                v = self.game.abort_expedition(gid, uid)
            elif any(k in act for k in ("状态", "进度")):
                return self.game.expedition_status(gid, uid)
            else:
                v = await self.game.ensure_expedition_offer(gid, uid)
        return self._view_text(v)

    @_guard_tool
    async def ocverse_expedition_invite(self, event: AstrMessageEvent, target_name: str):
        """邀请某位玩家分身/生活角色加入你的远征队伍(需今日远征委托,会自动生成;
        对方是否同意由 AI 按性格与交情判断)。"""
        gid = self._need_gid(event)
        uid = self._uid(event)
        self._char_of(event)
        t = self._char_by_name(gid, (target_name or "").strip())
        if t is None:
            return f"找不到叫「{target_name}」的角色。"
        async with self._glock(gid):
            v = await self.game.expedition_invite(gid, uid, t.uid)
        return self._view_text(v)

    @_guard_tool
    async def ocverse_claim_quest(self, event: AstrMessageEvent, number: int):
        """交付/完成今天的第 N 个委托任务。

        Args:
            number(number): 任务编号(从 1 开始)
        """
        gid = self._need_gid(event)
        self._char_of(event)
        idx = max(0, int(number or 1) - 1)
        async with self._glock(gid):
            v = await self.game.complete_quest(gid, self._uid(event), idx)
        return self._view_text(v)

    @_guard_tool
    async def ocverse_advance_mainline(self, event: AstrMessageEvent):
        """推进当前世界主线一步。"""
        gid = self._need_gid(event)
        self._char_of(event)
        async with self._glock(gid):
            v = await self.game.mainline_progress(gid, self._uid(event))
        return self._view_text(v)

    @_guard_tool
    async def ocverse_travel_world(self, event: AstrMessageEvent, target: str):
        """穿越到已经去过的世界(编号或名字见「世界书」)。

        Args:
            target(string): 目标世界编号或名字
        """
        gid = self._need_gid(event)
        self._char_of(event)
        async with self._glock(gid):
            v = await self.game.travel(gid, self._uid(event), (target or "").strip())
        return self._view_text(v)

    @_guard_tool
    async def ocverse_real_estate(self, event: AstrMessageEvent, action: str, target: str = ""):
        """房产操作:看房/买房/回家(查看、买第几号、回宅休整)。

        Args:
            action(string): 买/回家/查看 等
            target(string): 可选,买房时填编号(如 2)
        """
        gid = self._need_gid(event)
        uid = self._uid(event)
        act = (action or "").strip()
        if any(k in act for k in ("回家", "回宅", "休息")):
            self._char_of(event)
            async with self._glock(gid):
                hv = await self.game.my_home(gid, uid)
            return self._view_text(hv)
        if any(k in act for k in ("买", "购", "买楼")):
            self._char_of(event)
            nums = re.findall(r"\d+", (target or "") + act)
            if not nums:
                return "请给出要买的房产编号(如:买 2)。"
            async with self._glock(gid):
                w, p, chg = self.game.buy_plot(gid, uid, int(nums[0]) - 1)
            return f"购入《{w.name}》的「{p['name']}」({p['kind']})。\n" + "\n".join(f"· {c}" for c in chg)
        w = self.db.cur_world(gid)
        if not w:
            return "世界尚未初始化。"
        plots = self.db.plots(gid, w.id)
        if not plots:
            return f"《{w.name}》暂时没有可购置的房产。"
        ch = self.db.get_char(gid, uid)
        mine_pid = (ch.flags or {}).get("home_plot") if ch else None
        lines = [f"《{w.name}》房产:"]
        for i, p in enumerate(plots, 1):
            if p["id"] == mine_pid:
                own = "〔已购〕"
            elif p.get("owner_uid"):
                own = "〔已售〕"
            else:
                own = f"〔在售 {p.get('price', 0)} 金币〕"
            lines.append(f"{i}. {p.get('kind','')}·{p.get('name','')} {own} — {p.get('desc','')}")
        lines.append("(想买说「买第几号」,回家说「回家」)")
        return "\n".join(lines)

    @_guard_tool
    async def ocverse_propose_bond(self, event: AstrMessageEvent, target_name: str, label: str):
        """提议一个搞怪/生活向自定义关系(如认TA当爸爸/师父/女仆),AI 判断对方答不答应。

        Args:
            target_name(string): 对方角色名
            label(string): 想要的称谓,如 爸爸/师父/冤种弟弟
        """
        gid = self._need_gid(event)
        uid = self._uid(event)
        self._char_of(event)
        target = self._char_by_name(gid, target_name)
        if target is None:
            return f"找不到叫「{target_name}」的角色。"
        if target.uid == uid:
            return "不能和自己提关系。"
        label = (label or "").strip()
        if not label:
            return "请写明想要的称谓,如:爸爸。"
        async with self._glock(gid):
            v = await self.game.propose_bond(gid, uid, target.uid, label)
        return self._view_text(v)
# ══════════════════ 管理类(仅群管理员) ══════════════════
    @_guard_tool
    async def ocverse_init_world(self, event: AstrMessageEvent, description: str = ""):
        """初始化/重建当前群的世界(仅管理员;生成全新世界,保留角色)。

        Args:
            description(string): 可选,世界观描述/题材,不填则 AI 自由发挥
        """
        gid = self._need_gid(event)
        if not event.is_admin():
            return "只有群管理员能初始化世界。"
        async with self._glock(gid):
            v = await self.game.init_world(gid, (description or "").strip() or None, self._uid(event))
        return self._view_text(v)

    @_guard_tool
    async def ocverse_trigger_world_shift(self, event: AstrMessageEvent):
        """立即触发一次世界变动(全员可能穿越到新世界;仅管理员,有冷却)。"""
        gid = self._need_gid(event)
        if not event.is_admin():
            return "只有群管理员能触发世界变动。"
        async with self._glock(gid):
            v = await self.game.world_shift(gid, manual=True)
        return self._view_text(v)

    @_guard_tool
    async def ocverse_admin_setting(self, event: AstrMessageEvent, key: str, value: str):
        """调整世界参数(仅管理员):事件频率(每日几个事件)或世界变动概率(每日 %)。

        Args:
            key(string): 频率 或 概率
            value(string): 频率填「2 4」(最小 最大,0~12);概率填 0~80 的数字
        """
        gid = self._need_gid(event)
        if not event.is_admin():
            return "只有群管理员能调整。"
        k = (key or "").strip()
        v = (value or "").strip()
        self.db.ensure_group(gid, {})
        if "频" in k or "事件" in k:
            parts = re.findall(r"\d+", v)
            if len(parts) < 2:
                return "格式:频率 最小 最大(如:频率 2 4)"
            emin, emax = int(parts[0]), int(parts[1])
            if not (0 <= emin <= emax <= 12):
                return "范围需满足 0 ≤ 最小 ≤ 最大 ≤ 12。"
            self.db.update_group(gid, event_min=emin, event_max=emax)
            return f"每日事件数已设为 {emin}~{emax} 个。"
        if "概" in k or "变动" in k:
            parts = re.findall(r"\d+", v)
            if not parts:
                return "格式:概率 数字(0~80)"
            pct = int(parts[0])
            if not (0 <= pct <= 80):
                return "概率需在 0~80 之间。"
            self.db.update_group(gid, shift_percent=pct)
            return f"世界变动概率已设为 {pct}%/日。"
        return "支持调整:事件频率 / 世界变动概率。"

    @filter.command_group("分身", alias={"oc", "ocs"})
    async def oc(self):
        """分身的世界 · 指令组。发送「/分身」或「/分身 帮助」查看全部指令。"""

# ═══════════════════════════ 指令:引导/管理 ═══════════════════════════
    # ═══════════════════════════ 自然语言入口(「/分身 说」)═══════════════════════════
    @oc.command("说", alias={"自由", "自然语言", "nl", "do", "随我心意"})
    @_guard
    async def cmd_nl(self, event: AstrMessageEvent):
        """分身 说 <自然语言…> - 用大白话让 AI 操作世界(自动调用工具),无需背指令"""
        gid = self._need_gid(event)
        q = self._rest(event, "说", "自由", "自然语言", "nl", "do", "随我心意")
        if not q:
            yield event.plain_result(
                "格式:/分身 说 <你想做什么>\n"
                "例如:/分身 说 帮我看看现在是什么世界\n"
                "/分身 说 我想去茶馆喝杯茶\n"
                "/分身 说 和绫波婆婆打个招呼\n"
                "或直接在普通聊天里描述意图,我也会按需调用工具。"
            )
            return
        if not self._cfg("nl_agent_enabled", True):
            yield event.plain_result("自然语言模式已在插件配置中关闭,请改用基础指令(「/分身 帮助」)。")
            return
        pid = await self._chat_provider_id(event)
        if not pid:
            yield event.plain_result("❌ 没有可用的 LLM 提供商,自然语言模式暂不可用(可改用「/分身 帮助」的基础指令)。")
            return
        yield event.plain_result("⏳ 正在理解你的想法并行动,请稍候…")
        try:
            tools = self._build_tool_set()
            resp = await self.context.tool_loop_agent(
                event=event,
                chat_provider_id=pid,
                prompt=q,
                tools=tools,
                system_prompt=self._nl_system_prompt(gid, self._uid(event)),
                max_steps=self._cfgi("nl_max_steps", 6),
                tool_call_timeout=self._cfgi("nl_tool_timeout", 90),
            )
        except Exception as e:
            logger.exception(f"ocverse: 自然语言执行异常: {e}")
            yield event.plain_result(f"❌ 执行失败:{e}")
            return
        text = (getattr(resp, "completion_text", "") or "").strip()
        yield event.plain_result(text or "……似乎没有生成回复,换个说法再试试?")

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

    @filter.permission_type(PermissionType.ADMIN)
    @oc.command("重建区域", alias={"regen_zones", "重绘区域", "重建危险区域"})
    @_guard
    async def cmd_regen_zones(self, event: AstrMessageEvent):
        """分身 重建区域 - 管理员让 AI 按世界观重新生成当前世界的危险区域与治疗物品"""
        gid = self._need_gid(event)
        yield event.plain_result("⏳ 正在重绘世界舆图(可能联网搜索),请稍候…")
        async with self._glock(gid):
            msg, _zones, _heals = await self.game.regen_zones_heals(gid)
        yield event.plain_result(msg)

    @filter.permission_type(PermissionType.ADMIN)
    @oc.command("重建主线", alias={"regen_mainline", "重铸主线"})
    @_guard
    async def cmd_regen_mainline(self, event: AstrMessageEvent):
        """分身 重建主线 - 管理员让 AI 按世界观重新生成当前世界的主线剧情(3~6节,含阶段门槛)"""
        gid = self._need_gid(event)
        yield event.plain_result("⏳ 正在重铸世界主线(可能联网搜索),请稍候…")
        async with self._glock(gid):
            msg, _nodes = await self.game.regen_mainline(gid)
        yield event.plain_result(msg)

    @filter.permission_type(PermissionType.ADMIN)
    @oc.command("删除世界", alias={"del_world", "抹除世界"})
    @_guard
    async def cmd_del_world(self, event: AstrMessageEvent):
        """分身 删除世界 <编号/名称> - 管理员抹除一个世界及其全部世界数据
        (设施/危险区域/NPC/房产/世界声望;角色及其记忆物品关系保留),需二次确认"""
        gid = self._need_gid(event)
        ref = self._rest(event, "删除世界", "del_world", "抹除世界").strip()
        if not ref:
            yield event.plain_result(
                "格式:/分身 删除世界 <编号/名称>\n"
                "(编号/名称见「/分身 世界列表」;将抹除该世界的设施/危险区域/NPC/房产/世界声望,"
                "角色及其记忆物品关系保留;需二次确认)")
            return
        key = f"delw:{gid}:{self._uid(event)}"
        now = time.time()
        if now - self._confirm.get(key, 0) > 120 or self._confirm.get(key + ":ref") != ref:
            self._confirm[key] = now
            self._confirm[key + ":ref"] = ref
            yield event.plain_result(
                f"⚠ 将抹除世界「{ref}」及其全部世界数据(设施/危险区域/NPC/房产/世界声望;角色保留,不可恢复)。\n"
                f"确认请再发一次:「/分身 删除世界 {ref}」(2分钟内有效)")
            return
        self._confirm.pop(key, None)
        self._confirm.pop(key + ":ref", None)
        async with self._glock(gid):
            msg = self.game.delete_world(gid, ref, f"管理员{self._uid(event)}")
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
        if not desc:
            yield event.plain_result(
                "❌ 定义生活角色需要设定描述:请提供 TA 的性格/来历/特征,让 AI 整理成档案。\n"
                "例:/分身 定义角色 绫波 住在雾码头的老婆婆,神秘而热心")
            return
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
            # 自然语言:首词为名字,余下整段描述交给 AI 整理成人设(描述必填)
            toks = rest.split(None, 1)
            name = toks[0][:12]
            desc = (toks[1] if len(toks) > 1 else "").strip()
            if not desc:
                yield event.plain_result(
                    "❌ 创建分身需要设定描述:请带上外貌/性格/背景等描述,让 AI 整理成人设。\n"
                    "例:/分身 创建 森森 白发蓝瞳戴眼镜的帅哥,超级聪明的大天才,性格冷静缜密\n"
                    "也兼容竖线速写:/分身 创建 <名字> |性别|性格,性格|背景设定…")
                return
            yield event.plain_result("⏳ 正在整理人设,请稍候…")
            r = await self.brain.parse_persona(desc)
            if r.ok:
                gender, tags, backstory = r.data["gender"], r.data["tags"], r.data["backstory"]
                llm_attrs = r.data.get("attrs") or None
            else:
                gender, tags, backstory = "保密", [], desc[:4000]
                llm_attrs = None
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
        rels = self.db.list_rels_for(gid, uid, 10)
        name_map = {c.uid: c.name for c in self.db.list_chars(gid)}
        rel_named = [(name_map.get(u, u[:8]), s) for u, s in rels]
        rel_labels = {name_map.get(u, u[:8]): self.game.rel_stage_label(gid, uid, u)
                      for u, s in rels}
        mems = await self.mem.related(gid, f"{ch.name} 最近 经历", uid=uid, k=3)
        badges = [C.FLAG_TITLES[k] for k in ("traveler", "socialite", "survivor")
                  if (ch.flags or {}).get(k)]
        # 兼职时段徽章
        _wn = self.game._work_note(ch)
        if _wn:
            badges.append(f"⚒ 上班中·{_wn}")
        # 远征徽章(队长 / 随队生活角色)
        _exp = self.game._on_expedition(ch)
        if _exp:
            badges.append(f"⚔ 远征中·{_exp.get('title', '')}"
                          f"(目标「{_exp.get('zone', '')}」,约剩 {self.game._exp_left_h(_exp):.0f} 小时)")
        else:
            _comp = self.game._exp_companion_of(gid, uid)
            if _comp:
                _cexp, _leader = _comp
                badges.append(f"⚔ 随「{_leader.name}」远征·{_cexp.get('title', '')}"
                              f"(约剩 {self.game._exp_left_h(_cexp):.0f} 小时)")
        # 自定义搞怪关系(我是谁的X / 谁是我的X)
        for bd in self.game.bonds_of(gid, uid)[:3]:
            other = name_map.get(bd["target"] if bd["proposer"] == uid else bd["proposer"], "?")
            if bd["proposer"] == uid:
                badges.append(f"🤝 我是{other}的{bd['label']}")
            else:
                badges.append(f"🤝 {other}是我的{bd['label']}")
        from .ocverse.game import is_npc_uid
        rep = None
        if world is not None and not is_npc_uid(uid):
            _score = self.game.db.rep_get(gid, uid, world.id)
            rep = {"score": _score, "label": C.rep_level_label(_score)}
        items = self.db.items_list(gid, uid)
        return {"__profile__": True, "ch": ch, "world": world, "rels": rel_named,
                "rel_labels": rel_labels,
                "mems": mems, "badges": badges, "rep": rep, "items": items}

    def _render_profile(self, v: dict) -> list:
        """把 _profile_view 的 view 渲染成角色卡图片列表(统一渲染入口)。"""
        return profile_card(v["ch"], v["world"], v["rels"], v["mems"], self._card_cfg(),
                            rel_names=v.get("rel_labels"),
                            extra_badges=v.get("badges") or [],
                            rep=v.get("rep"), items=v.get("items"))

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
        async with self._glock(gid):
            w = self.game.ensure_world_content(gid) or w   # 旧世界补齐危险区域/治疗物品
            w = await self.game.ensure_world_mainline(gid) or w   # 主线空/缺失 → LLM 立即重生成
        # 直接取世界范畴(world/npc)近况:以原始记忆行还原标题+片段
        world_mem = await self.game.world_memory_panel(gid, w.name, k=5)
        imgs = world_card(w, self._card_cfg(), is_current=True, day=day, world_mem=world_mem)
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
        lines.append("")
        inter = [it.get('name','') for it in w.infra if C.infra_interactable(it)]
        lines.append("可光顾消遣(产生小事件):" + ("、".join(inter[:10]) if inter else "(暂时没有)"))
        med = [it.get('name','') for it in w.infra if C.is_medical_infra(it)]
        if med:
            lines.append("医疗设施(可治病/买药):" + "、".join(med[:6])
                         + " → 「/分身 治疗」或「/分身 去 " + med[0] + " 买治疗药」")
        lines.append("用「/分身 去 <设施名> [想做什么]」去社交/娱乐/约会场所消磨时光(每天每家1次)。")
        yield event.plain_result("\n".join(lines))

    @oc.command("去", alias={"光顾", "泡在", "逛", "溜达", "visit"})
    @_guard
    async def cmd_visit(self, event: AstrMessageEvent):
        """分身 去 <设施名> [想做什么] - 去社交/娱乐/约会设施消磨时光,产生小事件(每天每家1次)"""
        gid = self._need_gid(event)
        rest = self._rest(event, "去", "光顾", "泡在", "逛", "溜达", "visit").strip()
        if not rest:
            yield event.plain_result("格式:/分身 去 <设施名> [想做什么]\n例:/分身 去 清风茶楼 喝茶听八卦\n(可光顾的设施见「/分身 设施」)")
            return
        parts = rest.split(maxsplit=1)
        name = parts[0].strip()
        action = parts[1].strip() if len(parts) > 1 else "随便转转"
        yield event.plain_result("⏳ 正在前往…")
        async with self._glock(gid):
            v = await self.game.visit_facility(gid, self._uid(event), name, action)
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)
        else:
            yield event.plain_result(v.get("narration", "你去逛了一圈。"))

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
        q_done = int(self.db.kv_get(gid, "quests_done_total") or 0)
        d_done = int(self.db.kv_get(gid, "defeats_total") or 0)
        lines = [f"📜 《{w.name}》世界主线", ""]
        for i, m in enumerate(ml, 1):
            mark = "✅" if m.get("done") else "⬜"
            goal = m.get("goal_type") or ""
            goal_txt = ""
            if goal and not m.get("done"):
                gv = m.get("goal_value") or 0
                have = {"reputation": None, "quest": q_done, "defeat": d_done}.get(goal)
                cur_txt = ""
                if goal == "reputation":
                    cur_txt = f"(你的声望 {self.game.db.rep_get(gid, self._uid(event), w.id)}/{gv})"
                elif have is not None:
                    cur_txt = f"(进度 {have}/{gv})"
                goal_txt = f" ⚑ {m.get('goal_note', '')}{cur_txt}"
            lines.append(f"{mark} {i}. {m.get('stage','')}{goal_txt}")
            lines.append(f"　 └ {m.get('desc','')}")
        lines.append("")
        lines.append("用「/分身 主线 推进」推进当前一步:带 ⚑ 的小节需要先达成门槛"
                     "(声望/完成任务/讨伐),推进完会解锁下一环;全部完结后续写尾声新篇章。")
        yield event.plain_result("\n".join(lines))

    @oc.command("兼职", alias={"parttime", "打半天工"})
    @_guard
    async def cmd_workday(self, event: AstrMessageEvent):
        """分身 兼职 - 在世界基础设施里上一班,约2小时后自动下班结算(结算含NPC同事互动)"""
        gid = self._need_gid(event)
        async with self._glock(gid):
            v = self.game.work_today(gid, self._uid(event))
        if v is None:
            yield event.plain_result("(暂时没有适合你打工的地方)")
            return
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs)
        left_min = v.get("until_min", 120)
        if chain:
            yield event.chain_result(chain)
            yield event.plain_result(f"⚒ 在「{v['spot']}」的这班约 {left_min} 分钟,到点自动下班结算;上班期间没法自由行动/互动。")
        else:
            yield event.plain_result(
                f"⚒ 你在「{v['spot']}」上工了({v['occupation']}),约 {left_min} 分钟后自动下班结算;"
                "上班期间没法自由行动/互动,到点我会叫你。")

    @oc.command("背包", alias={"inventory", "物品", "行李"})
    @_guard
    async def cmd_inventory(self, event: AstrMessageEvent):
        """分身 背包 [丢弃 <物品名>] - 查看/丢弃随身物品(冒险/事件/委托/兼职都可能获得)"""
        gid = self._need_gid(event)
        uid = self._uid(event)
        rest = self._rest(event, "背包", "inventory", "物品", "行李").strip()
        if rest.startswith("丢弃") or rest.startswith("扔掉") or rest.startswith("丢"):
            name = re.sub(r"^(丢弃|扔掉|丢)\s*", "", rest, count=1).strip()
            if not name:
                yield event.plain_result("格式:/分身 背包 丢弃 <物品名>")
                return
            if self.db.item_remove(gid, uid, name, 1):
                yield event.plain_result(f"🎒 你扔掉了「{name}」。")
            else:
                yield event.plain_result(f"你没有「{name}」这东西。")
            return
        ch = self._char_of(event)
        items = self.db.items_list(gid, uid)
        if not items:
            yield event.plain_result(f"🎒 {ch.name} 的背包空空如也(冒险/事件/委托/兼职都可能得到物品)。")
            return
        lines = [f"🎒 {ch.name} 的背包({len(items)} 件)", ""]
        for i, it in enumerate(items, 1):
            note = f" — {it['note']}" if it.get("note") else ""
            lines.append(f"{i}. {it['name']} ×{it['count']}{note}")
        lines.append("")
        lines.append("丢弃:/分身 背包 丢弃 <物品名>")
        yield event.plain_result("\n".join(lines))

    @oc.command("治疗", alias={"heal", "疗伤", "看伤"})
    @_guard
    async def cmd_heal(self, event: AstrMessageEvent):
        """分身 治疗 [物品名] - 用背包治疗物品疗伤(留空自动);没药则去医院付费治疗"""
        gid = self._need_gid(event)
        uid = self._uid(event)
        self._char_of(event)
        rest = self._rest(event, "治疗", "heal", "疗伤", "看伤").strip()
        async with self._glock(gid):
            if rest:
                v = self.game.use_heal_item(gid, uid, rest)
            else:
                try:
                    v = self.game.use_heal_item(gid, uid)
                except GameError:
                    v = self.game.heal_at_hospital(gid, uid)
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)
        else:
            yield event.plain_result(v.get("narration", "处理完毕。"))

    @oc.command("购买", alias={"buy", "买药"})
    @_guard
    async def cmd_buy(self, event: AstrMessageEvent):
        """分身 购买 <物品名> [设施名] - 在店铺/药铺/诊所买治疗物品(声望有折扣)"""
        gid = self._need_gid(event)
        self._char_of(event)
        rest = self._rest(event, "购买", "buy", "买药").strip()
        parts = rest.split(None, 1)
        if not rest:
            w = self.db.cur_world(gid)
            items = self.game.heal_items_of(gid, w) if w else []
            names = "、".join(f"{h['name']}({h.get('price', '?')}金)" for h in items) or "无"
            yield event.plain_result(
                f"格式:/分身 购买 <物品名> [设施名]\n"
                f"这个世界能买到的治疗物品:{names}\n"
                f"也可以:/分身 去 <诊所/药铺/商店名> 买 <物品名>")
            return
        yield event.plain_result("⏳ 正在前往店铺…")
        async with self._glock(gid):
            v = self.game.buy_item(gid, self._uid(event), parts[0], parts[1].strip() if len(parts) > 1 else "")
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)
        else:
            yield event.plain_result(v.get("narration", "买好了。"))

    @oc.command("区域", alias={"zones", "危险区域"})
    @_guard
    async def cmd_zones(self, event: AstrMessageEvent):
        """分身 区域 - 查看当前世界的危险区域(敌人/素材,每日变动,与讨伐任务联动)"""
        gid = self._need_gid(event)
        zones = self.game.list_zones(gid)
        if not zones:
            yield event.plain_result("当前世界没有已探明的危险区域。")
            return
        lines = [f"⚔ 《{self.db.cur_world(gid).name}》的危险区域(每日不定时变动)", ""]
        for z in zones:
            stars = "★" * max(1, min(5, int(z.get("danger") or 1)))
            en = "、".join(e.get("name", "") for e in (z.get("enemies") or []) if isinstance(e, dict))
            loot = "、".join(z.get("loot") or [])
            lines.append(f"· {z.get('name')}({z.get('kind', '')}) 危险度{stars} — {z.get('desc', '')}")
            if en:
                lines.append(f"   出没:{en}" + (f" | 素材:{loot}" if loot else ""))
        lines.append("")
        lines.append("用「/分身 打怪 <目标/区域>」讨伐;击败敌人可能掉落素材与治疗物品;今日任务可能指向这些区域。")
        yield event.plain_result("\n".join(lines))

    @oc.command("声望", alias={"reputation", "声誉"})
    @_guard
    async def cmd_rep(self, event: AstrMessageEvent):
        """分身 声望 [名字] - 查看分身在各世界的声望(声望高:NPC友好/购物折扣/主线门槛)"""
        gid = self._need_gid(event)
        rest = self._rest(event, "声望", "reputation", "声誉").strip()
        uid = self._uid(event)
        if rest:
            t = self.db.get_char_by_name(gid, rest) or self.db.get_char(gid, npc_uid(gid, rest))
            if t is None:
                yield event.plain_result(f"找不到叫「{rest}」的角色。")
                return
            uid = t.uid
        else:
            self._char_of(event)
        p = self.game.rep_panel(gid, uid)
        lines = [f"⚜ {p['char_name']} 的世界声望", ""]
        if p.get("current"):
            c = p["current"]
            lines.append(f"当前世界《{c['world']}》:{c['score']}({c['label']})")
        for r in p.get("list", [])[:8]:
            lines.append(f"· 《{r['world']}》:{r['score']}({r['label']})")
        if not p.get("list") and not p.get("current"):
            lines.append("(还没有声望记录:完成委托/讨伐/善行会提升声望)")
        if p.get("top"):
            top = "、".join(f"{t['name']}({t['score']})" for t in p["top"])
            lines.append("")
            lines.append(f"当前世界声望榜:{top}")
        lines.append("")
        lines.append("声望越高:NPC 对你越友好、店铺/医院有折扣、主线推进的门槛也需要它。")
        yield event.plain_result("\n".join(lines))

    @oc.command("远征", alias={"expedition", "远征队"})
    @_guard
    async def cmd_expedition(self, event: AstrMessageEvent):
        """分身 远征 [接受|状态|放弃] - 查看/接下今日远征委托(公会/据点颁布,贴合世界观);
        远征持续数小时到数天,期间无法其他操作,每几小时播报剧情,归来丰厚结算"""
        gid = self._need_gid(event)
        uid = self._uid(event)
        rest = self._rest(event, "远征", "expedition", "远征队").strip()
        if any(k in rest for k in ("状态", "进度")):
            yield event.plain_result(self.game.expedition_status(gid, uid))
            return
        if rest and not any(k in rest for k in ("接受", "接下", "出发", "放弃", "逃", "撤", "查看", "布告", "委托")):
            yield event.plain_result(
                "用法:/分身 远征 — 查看今日远征委托\n"
                "　　　/分身 远征 接受 — 签下委托出发(期间无法其他操作)\n"
                "　　　/分身 远征 状态 — 查看远征进度\n"
                "　　　/分身 远征 放弃 — 中途撤离(声望重挫+违约金)")
            return
        if not rest:
            ch = self.db.get_char(gid, uid)
            if ch and self.game._on_expedition(ch):
                yield event.plain_result(self.game.expedition_status(gid, uid))
                return
        yield event.plain_result("⏳ 正在查看远征委托…" if not any(k in rest for k in ("放弃", "逃", "撤")) else "⏳ 正在办理撤离…")
        async with self._glock(gid):
            if any(k in rest for k in ("接受", "接下", "出发")):
                v = await self.game.accept_expedition(gid, uid)
            elif any(k in rest for k in ("放弃", "逃", "撤")):
                v = self.game.abort_expedition(gid, uid)
            else:
                v = await self.game.ensure_expedition_offer(gid, uid)
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)
        else:
            yield event.plain_result(v.get("narration", "……"))

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
        """分身 选择 <编号> - 回复(引用)事件卡后,对 TA 的遭遇做出抉择;选 4 可自定义行动"""
        gid = self._need_gid(event)
        uid = self._uid(event)
        raw = idx or self._rest(event, "选择", "choose")
        m = re.match(r"\s*(\d+)\s*(.*)", raw)
        n = m.group(1) if m else ""
        custom = (m.group(2) or "").strip() if m else ""
        if not n:
            yield event.plain_result("格式:回复要抉择的事件卡,发送「/分身 选择 <编号>」;选 4 可附自定义行动(≤30字)")
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
            try:
                v = await self.game.choose(gid, uid, int(n) - 1, ev=ev, custom=custom)
            except GameError as e:
                yield event.plain_result(f"❌ {e}")
                return
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    @oc.command("找", alias={"交往", "结识", "找TA", "talk", "与", "互动"})
    @_guard
    async def cmd_char_interact(self, event: AstrMessageEvent):
        """分身 找 <名字/@群友> [方式/自由描述…] - 与玩家分身或持久生活角色互动
        (@ 组件与角色名双兼容,QQ 官方接口无 At 组件时直接写名字即可;可发展关系/结婚)"""
        gid = self._need_gid(event)
        uid = self._uid(event)
        raw = self._rest(event, "找", "交往", "结识", "找TA", "talk", "与", "互动").strip()
        target, mode_text = self._resolve_interact_target(gid, event, raw)
        if not target:
            lives = "、".join(c.name for c in self.game._npc_chars(gid)) or "无"
            players = "、".join(c.name for c in self.db.list_chars(gid) if not is_npc_uid(c.uid)) or "无"
            yield event.plain_result(
                "格式:/分身 找 <名字 或 @群友> [互动方式]\n"
                "例:/分身 找 绫波 聊聊雾码头 / 分身 找 @某人 请客\n"
                "(玩家分身与生活角色通用;平台 @ 失效时直接写名字即可)\n"
                f"当前玩家分身:{players}\n当前生活角色:{lives}")
            return
        target_char = self.db.get_char(gid, target)
        if not target_char:
            yield event.plain_result("找不到这个目标(可能TA还没有分身)。")
            return
        if target_char.uid == uid:
            yield event.plain_result("不能和自己互动哦(对着镜子练吧)。")
            return
        # 远征邀约:接受前拉人同行(「找 阿澈 我要去远征你要不要跟我一起」)
        mode_text_l = (mode_text or "")
        if any(kw in mode_text_l for kw in ("远征", "组队", "同行")):
            yield event.plain_result("⏳ 正在向对方发出远征邀约…")
            async with self._glock(gid):
                v = await self.game.expedition_invite(gid, uid, target_char.uid)
            imgs = render_views([v], self._card_cfg())
            chain = self._chain(imgs)
            if chain:
                yield event.chain_result(chain)
            else:
                yield event.plain_result(v.get("narration", "……"))
            return
        mode, detail = "打招呼", self._default_mode_hint["打招呼"]
        if mode_text:
            m0 = mode_text.split()[0]
            custom = {i["name"]: i["descr"] for i in self.db.list_interactions(gid)}
            if m0 in self._default_mode_hint:
                mode, detail = m0, self._default_mode_hint[m0]
            elif m0 in custom:
                mode, detail = m0, custom[m0]
            else:
                mode, detail = "自由互动", mode_text
        yield event.plain_result("⏳ 正在演绎这段互动,请稍候…")
        async with self._glock(gid):
            v = await self.game.interact(gid, uid, target_char.uid, mode, detail)
        views = [v] + (v.pop("extra_views", []) or [])
        imgs = render_views(views, self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    @oc.command("关系", alias={"bond", "自定义关系"})
    @_guard
    async def cmd_bond(self, event: AstrMessageEvent):
        """分身 关系 <名字/@群友> <称谓> - 提议自定义搞怪关系(如想当TA爸爸),AI 判断对方答不答应"""
        gid = self._need_gid(event)
        raw = self._rest(event, "关系", "bond", "自定义关系").strip()
        target, label = self._resolve_interact_target(gid, event, raw)
        if not target:
            yield event.plain_result(
                "格式:/分身 关系 <名字 或 @群友> <称谓>\n"
                "例:/分身 关系 老徐 爸爸(你想当 TA 的爸爸)/女仆/主人/师父/冤种弟弟…\n"
                "AI 会以对方的性格与你们的交情判断答不答应;亲密关系(恋人/情侣/夫妻等)不可自定义"
            )
            return
        label = label.strip()
        if not label:
            yield event.plain_result("请写上想要的称谓,如:/分身 关系 老徐 爸爸")
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
        lines.append("用法:/分身 找 <名字 或 @群友> 互动方式(玩家分身/生活角色通用);也可以直接自由描述一段行动,由你们的性格与羁绊决定走向。")
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

    @oc.command("打怪", alias={"fight", "狩猎", "讨伐", "hunt"})
    @_guard
    async def cmd_fight(self, event: AstrMessageEvent):
        """分身 打怪 [区域/敌人] - 进危险区域讨伐:点名区域或敌人则锁定,
        不点名则自动选区(今日讨伐委托自动对齐);击败掉素材与治疗物品(风险行动)"""
        async for r in self._run_act(event, "打怪", "打怪", "狩猎", "讨伐", "fight", "hunt"):
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
                "· /分身 打怪 [区域或敌人] — 进危险区域讨伐:如「打怪 树精」或「打怪 低语森林」;"
                "不点名自动选区(今日讨伐委托会自动对齐),击败掉素材/治疗物品",
                "· /分身 区域 — 看当前世界有哪些危险区域(每日变动)",
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
        """分身 任务 - 领取/查看今天的委托(委托人+发布设施+多步骤目标)"""
        gid = self._need_gid(event)
        ch = self._char_of(event)
        yield event.plain_result("⏳ 正在领取今日委托,请稍候…")
        async with self._glock(gid):
            qs = await self.game.ensure_quests(gid, self._uid(event))
        open_qs = [q for q in qs if q["state"] == "open"]
        done = len(qs) - len(open_qs)
        if not open_qs:
            yield event.plain_result(f"🎉 今天给「{ch.name}」的委托都完成啦,明天再来。")
            return
        lines = [f"📌 {ch.name} 今天的委托({done}/{len(qs)} 已完成)", ""]
        for i, q in enumerate(open_qs, 1):
            lines.append(f"{i}. {q['text']}")
            giv = (q.get("giver") or "委托人").strip()
            plc = (q.get("place") or "").strip()
            lines.append(f"　 └ 📩 {giv}" + (f"(发布于「{plc}」)" if plc else ""))
            if q.get("hint"):
                lines.append(f"　 └ 💡 {q['hint']}")
            steps = q.get("steps") or []
            if isinstance(steps, str):
                import json as _json
                try:
                    steps = _json.loads(steps or "[]")
                except Exception:
                    steps = []
            for s in steps:
                if not isinstance(s, dict):
                    continue
                mk = "☑" if s.get("done") else "☐"
                lines.append(f"　   {mk} {s.get('desc','')}")
        lines.append("")
        lines.append("完成方式:按步骤做完(冒险/互动/兼职/获得物品)→「/分身 交任务 <编号>」向委托人交付")
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
