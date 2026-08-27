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
from .ocverse.avatar_store import AvatarStore
from .ocverse.db import Database
from .ocverse.embedder import make_embedder
from .ocverse.game import Game, GameError
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
from .ocverse.memory import MemoryStore


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
        emb, fb = make_embedder(_cfg, lambda: self.context.get_all_embedding_providers())
        self.mem = MemoryStore(self.db, emb, fb, top_k=self._cfgi("memory_top_k", 6))
        self.brain = Brain(raw_call=self._llm_raw, style_extra=str(_cfg("style_prompt", "") or ""))
        self.game = Game(self.db, self.brain, self.mem, _cfg)
        self.avatars = AvatarStore(data_dir)

        self._task: asyncio.Task | None = None
        self._sem = asyncio.Semaphore(2)
        self._umo_map: dict[str, str] = {}      # gid -> unified_msg_origin(本会话内观察)
        self._pending: dict[str, list] = {}     # gid -> 主动卡片积压(无法主动发时)
        self._glocks: dict[str, asyncio.Lock] = {}  # 每群一把锁:LLM 调用期间锁定改数据的指令,防竞态
        self._confirm: dict[str, float] = {}    # 二次确认状态
        self._default_mode_hint = {m: d for m, d in C.DEFAULT_INTERACTIONS}

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
        for n in names:
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

    async def _broadcast(self, views: list[dict]):
        cfg = self._card_cfg()
        by_gid: dict[str, list] = {}
        for v in views:
            by_gid.setdefault(v["gid"], []).append(v)
        for gid, gviews in by_gid.items():
            umo = self._umo_map.get(gid) or (self.db.kv_get(gid, "umo") or "")
            imgs = render_views(gviews, cfg)
            chain = self._chain(imgs)
            if umo and await self._send_to(umo, chain):
                continue
            # 无法主动发送 → 积压,等群消息时补发
            pend = self._pending.setdefault(gid, [])
            pend.extend(gviews)
            del pend[3:]

    # ── 后台调度 ──────────────────────────────────────────────────
    async def initialize(self):
        self._task = asyncio.create_task(self._loop())
        logger.info("ocverse: 后台调度已启动")

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
            v = pend.pop(0)
            try:
                imgs = render_views([v], self._card_cfg())
                chain = self._chain(imgs)
                if chain:
                    yield event.chain_result(chain)
            except Exception as e:
                logger.warning(f"ocverse: 补发卡片失败: {e}")
            return
        # 被动事件:群里有动静,伏笔引爆(每次消息最多一个,自然限流)
        armed = self.game.armed_passives(gid)
        if not armed:
            return
        item = armed[0]
        try:
            async with self._glock(gid):
                # 二次确认(等待锁期间可能已被别的消息引爆)
                if not any(it.get("id") == item.get("id") for it in self.game.armed_passives(gid)):
                    return
                v = await self.game.fire_event(gid, char_uid=self._uid(event))
                self.game.mark_done(gid, item)
            if v:
                imgs = render_views([v], self._card_cfg())
                chain = self._chain(imgs)
                if chain:
                    yield event.chain_result(chain)
        except GameError as e:
            self.game.mark_done(gid, item)  # 无法触发(如无人建角色)也收掉伏笔
            logger.warning(f"ocverse: 被动事件引爆失败: {e}")

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
            raise GameError("你还没有创建分身,先「分身 创建 名字」")
        return ch

    # ═══════════════════════════ 指令:引导/管理 ═══════════════════════════
    @filter.command_group("分身", alias={"oc", "ocs"})
    async def oc(self):
        """分身的世界 · 指令组。发送「分身」或「分身 帮助」查看全部指令。"""

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
        async with self._glock(gid):
            v = await self.game.init_world(gid, desc or None, self._uid(event))
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs, "世界已就位!成员们用「分身 创建 名字」降生吧。")
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
            yield event.plain_result("⚠ 将为全群重新生成一个世界(角色与记忆保留)。确认请再发一次「分身 重开世界」")
            return
        self._confirm.pop(key, None)
        async with self._glock(gid):
            v = await self.game.init_world(gid, None, self._uid(event))
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs, "世界已重铸。")
        if chain:
            yield event.chain_result(chain)

    @oc.command("定义世界", alias={"add_world", "世界书"})
    @_guard
    async def cmd_define_world(self, event: AstrMessageEvent):
        """分身 定义世界 <名称> <描述…> - 把自设世界写进世界书(等待变动降临)"""
        gid = self._need_gid(event)
        rest = self._rest(event, "定义世界", "add_world", "世界书")
        parts = rest.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result("格式:分身 定义世界 <名称> <描述…>\n例如:分身 定义世界 机械都市 一切由齿轮与蒸汽驱动,情感被禁止…")
            return
        name, desc = parts[0][:16], parts[1]
        async with self._glock(gid):
            r = await self.game.define_world(gid, self._uid(event), name, desc)
        yield event.plain_result(f"📖 《{r['name']}》已写进世界书。它会在某次世界变动时降临——降临后即可自由穿越。")

    @filter.permission_type(PermissionType.ADMIN)
    @oc.command("触发变动", alias={"world_shift", "手动变动"})
    @_guard
    async def cmd_trigger_shift(self, event: AstrMessageEvent):
        """分身 触发变动 - 管理员立即触发一次世界变动(有冷却)"""
        gid = self._need_gid(event)
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
        """分身 创建 <名字> [|性别|性格,性格|背景故事…] - 创建你的 OC 分身(一人一个)"""
        gid = self._need_gid(event)
        rest = self._rest(event, "创建", "create", "创建角色")
        parts = [p.strip() for p in rest.split("|")]
        if not parts or not parts[0]:
            yield event.plain_result(
                "格式:分身 创建 <名字> [|性别|性格,性格|背景故事…]\n"
                "例如:分身 创建 凛 | 女 | 腹黑,重情义 | 曾在雾夜的海边捡到一枚会唱歌的贝壳…\n"
                "竖线后可省略任意段,之后用「分身 编辑」补全;头像用「分身 设置头像」+ 图片"
            )
            return
        name = parts[0][:12]
        gender = parts[1][:8] if len(parts) > 1 and parts[1] else "保密"
        tags = [t for t in re.split(r"[、,，/]", parts[2]) if t.strip()][:6] if len(parts) > 2 else []
        backstory = parts[3][:400] if len(parts) > 3 else ""
        async with self._glock(gid):
            ch = self.game.create_char(gid, self._uid(event), name, gender, tags, backstory)
            tip = "" if (tags and backstory) else "\n💡 建议用「分身 编辑 性格/背景 <内容>」补全人设,事件会更有戏"
            if await self._images_with_quoted(event):
                if await self._save_avatar_bytes(ch, (await self._images_with_quoted(event))[0]):
                    tip += "\n🖼️ 已顺手把随指令的图片设为头像"
            w = self.db.cur_world(gid)
            v = await self._profile_view(gid, self._uid(event))
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs, f"✨ {ch.name} 降临于《{w.name if w else '?'}》{tip}")
        if chain:
            yield event.chain_result(chain)

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
            yield event.plain_result(f"⚠ 将删除分身「{ch.name}」(角色卡与头像)。确认请再发一次「分身 删除角色」")
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
        ch = self._char_of(event)
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
        v = await self._profile_view(gid, self._uid(event))
        imgs_out = render_views([v], self._card_cfg())
        chain = self._chain(imgs_out, "🖼️ 头像已更新")
        if chain:
            yield event.chain_result(chain)

    @oc.command("编辑", alias={"edit"})
    @_guard
    async def cmd_edit(self, event: AstrMessageEvent):
        """分身 编辑 性别|性格|背景 <内容> - 修改人设"""
        ch = self._char_of(event)
        content = self._rest(event, "编辑", "edit")
        m = re.match(r"^(性别|性格|背景)(?:\s+|$)(.*)$", content, re.S)
        if not m:
            yield event.plain_result("格式:分身 编辑 性别|性格|背景 <内容>\n例如:分身 编辑 性格 高冷,毒舌,护短")
            return
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
            self.db.update_char(ch.gid, ch.uid, backstory=val[:400])
        yield event.plain_result(f"✅ 已更新「{ch.name}」的{f}")

    async def _profile_view(self, gid: str, uid: str) -> dict:
        ch = self.db.get_char(gid, uid)
        world = self.db.cur_world(gid)
        rels = self.db.list_rels_for(gid, uid, 4)
        name_map = {c.uid: c.name for c in self.db.list_chars(gid)}
        rel_named = [(name_map.get(u, u[:8]), s) for u, s in rels]
        mems = await self.mem.related(gid, f"{ch.name} 最近 经历", uid=uid, k=3)
        badges = [C.FLAG_TITLES[k] for k in ("traveler", "socialite", "survivor")
                  if (ch.flags or {}).get(k)]
        return {"__profile__": True, "ch": ch, "world": world, "rels": rel_named,
                "mems": mems, "badges": badges}

    async def _yield_profile(self, event, gid: str, uid: str, extra: str = ""):
        v = await self._profile_view(gid, uid)
        imgs = profile_card(v["ch"], v["world"], v["rels"], v["mems"], self._card_cfg(),
                            extra_badges=v["badges"])
        chain = self._chain(imgs, extra)
        if chain:
            yield event.chain_result(chain)

    @oc.command("我的卡片", alias={"card", "状态"})
    @_guard
    async def cmd_card(self, event: AstrMessageEvent):
        """分身 我的卡片 - 展示角色卡(属性/羁绊/记忆)"""
        gid = self._need_gid(event)
        self._char_of(event)
        async for r in self._yield_profile(event, gid, self._uid(event)):
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
            yield event.plain_result("世界尚未初始化。管理员:「分身 初始化世界 [世界观描述]」")
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
            yield event.plain_result("格式:分身 穿越世界 <编号/名称>(编号见「分身 世界列表」)")
            return
        async with self._glock(gid):
            v = await self.game.travel(gid, self._uid(event), target.strip())
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

    # ═══════════════════════════ 指令:事件/互动 ═══════════════════════════
    @oc.command("选择", alias={"choose"})
    @_guard
    async def cmd_choose(self, event: AstrMessageEvent, idx: str = ""):
        """分身 选择 <编号> - 对遭遇做出抉择"""
        gid = self._need_gid(event)
        n = re.sub(r"\D", "", idx or self._rest(event, "选择", "choose"))
        if not n:
            yield event.plain_result("格式:分身 选择 <编号>(事件卡片里的 1/2/3)")
            return
        async with self._glock(gid):
            v = await self.game.choose(gid, self._uid(event), int(n) - 1)
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
            yield event.plain_result("请 @ 对方:分身 与 @某人 [打招呼/闲聊/请客/切磋/吐槽/送礼/倾听/合影,或自由描述]")
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
        async with self._glock(gid):
            v = await self.game.interact(gid, self._uid(event), target, mode, detail)
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
        lines.append("用法:分身 与 @群友 互动方式;也可以直接自由描述一段行动,由你们的性格与羁绊决定走向。")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(PermissionType.ADMIN)
    @oc.command("添加互动", alias={"add_interaction"})
    @_guard
    async def cmd_add_inter(self, event: AstrMessageEvent, name: str = "", descr: str = ""):
        """分身 添加互动 <名称> <说明> - 添加群自定义互动(管理员)"""
        gid = self._need_gid(event)
        if not name or not descr:
            yield event.plain_result("格式:分身 添加互动 <名称> <说明>")
            return
        self.db.add_interaction(gid, name[:8], descr[:80], self._uid(event))
        yield event.plain_result(f"✅ 互动「{name}」已加入本群菜单")

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
            yield event.plain_result(f"格式:分身 npc <名字> <想做什么>\n当前世界NPC:{names}")
            return
        async with self._glock(gid):
            v = await self.game.npc_interact(gid, self._uid(event), parts[0][:12], parts[1][:80])
        imgs = render_views([v], self._card_cfg())
        chain = self._chain(imgs)
        if chain:
            yield event.chain_result(chain)

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
            yield event.plain_result("格式:分身 回忆 <关键词>,例如:分身 回忆 雾夜")
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
