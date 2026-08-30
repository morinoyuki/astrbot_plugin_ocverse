"""工具层功能测试:在不启动 astrbot 的情况下,把 OcversePlugin 属主对象补全成
一个最小可用实例,直接驱动 ocverse_* 工具方法(验证自然语言工具链路)。"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARENT = os.path.dirname(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, PARENT)

def llm_fake(system, user):
    if "生成一个新世界" in user:
        return json.dumps(WORLD_JSON, ensure_ascii=False)
    if "生成一次突发遭遇" in user:
        return json.dumps({"title": "小插曲", "scene": "茶馆里雾气腾腾。", "options": [
            {"label": "喝茶", "hint": "舒服"}, {"label": "打听", "hint": "有收获"}, {"label": "离开", "hint": "无妨"}]}, ensure_ascii=False)
    if "【设定描述】" in user:
        return json.dumps({"gender": "男", "tags": ["冷静", "天才"], "backstory": "白发蓝瞳的男孩子。",
                           "attrs": {"force": 30, "agility": 30, "intellect": 60, "charm": 40, "luck": 30, "sanity": 35}}, ensure_ascii=False)
    if "修改描述" in user:
        return json.dumps({"tags": ["开朗"], "backstory": "白发蓝瞳,性格开朗。"}, ensure_ascii=False)
    if "整理成档案" in user:
        return json.dumps({"role": "掌柜", "persona": "精明和气", "hook": "知道每桌客人的故事"}, ensure_ascii=False)
    if "整理成人设" in user:
        return json.dumps({"gender": "男", "tags": ["热情"], "backstory": "茶楼主人。",
                           "attrs": {"force": 20, "agility": 25, "intellect": 45, "charm": 45, "luck": 30, "sanity": 40}}, ensure_ascii=False)
    if "恰同" in user:
        pass
    if "生成今天的 3 个委托任务" in user:
        return json.dumps({"quests": [
            {"text": "给雾婆婆送信", "giver": "驿站长", "place": "驿站", "hint": "送到灯塔",
             "steps": [{"type": "npc", "desc": "找灯叔聊聊", "npc": "灯叔"}]}]}, ensure_ascii=False)
    if "请结算" in user or "写一段" in user or "写出这段互动" in user or "与角色进行多轮对话" in user \
            or "执行行动" in user or "想在这里打发时光" in user or "推进叙述" in user:
        return json.dumps({"narration": "事情到此有了一个明确的收尾。",
                           "dialogues": [{"speaker": "自己", "text": "那今天就这样。"},
                                         {"speaker": "对方", "text": "嗯,改日再会。"}],
                           "effects": {"mood": 3, "exp": 5}}, ensure_ascii=False)
    return json.dumps({"narration": "ok", "effects": {}}, ensure_ascii=False)

WORLD_JSON = {
    "name": "雾镇", "genre": "低魔日常",
    "atmosphere": "薄雾与旧灯塔。", "desc": "一座临海小镇。",
    "rules": ["夜里雾大"], "features": ["灯塔看得见全城"],
    "npcs": [{"name": "灯叔", "role": "灯塔看守", "persona": "寡言", "hook": "知道旧事", "daily": "在灯塔", "quirk": "擦怀表", "builtin": 1}],
    "event_ideas": ["雾夜集市"],
    "infra": [{"kind": "茶馆", "name": "清风茶楼", "desc": "喝茶听八卦", "work": "茶博士"},
              {"kind": "驿站", "name": "雾边驿站", "desc": "收发信件", "work": "驿员"}],
    "mainline": [{"stage": "雾夜来客", "desc": "镇口来了陌生人"}],
    "plots": [{"kind": "小屋", "name": "转角小屋", "desc": "安静", "price": 300}],
}

class FakeEvent:
    def __init__(self, uid="u1", admin=False):
        self._uid = uid
        self._admin = admin
        self.unified_msg_origin = "aiocqhttp:GroupMessage:g1"
    def get_group_id(self):
        return "g1"
    def get_sender_id(self):
        return self._uid
    def is_admin(self):
        return self._admin
    def plain_result(self, text):
        return text
    def chain_result(self, chain):
        return ("chain", chain)

def make_plugin():
    from ocverse.db import Database
    from ocverse.embedder import HashEmbedder
    from ocverse.memory import MemoryStore, KnowledgeStore
    from ocverse.llm_engine import Brain
    from ocverse.game import Game
    from astrbot_plugin_ocverse.main import OcversePlugin

    tmpd = tempfile.mkdtemp(prefix="ocverse_tool_")
    cfg = {"memory_top_k": 6, "event_expire_minutes": 45, "card_width": 900,
           "card_font_size": 34, "card_theme": "dark", "knowledge_base_max": 40}
    pl = OcversePlugin.__new__(OcversePlugin)
    pl._cfg = lambda k, d=None: cfg.get(k, d)
    pl._cfgi = lambda k, d: int(cfg.get(k, d))
    pl.db = Database(os.path.join(tmpd, "t.sqlite3"))
    emb, fb = HashEmbedder(), HashEmbedder()
    pl.mem = MemoryStore(pl.db, emb, fb, top_k=6)
    pl.kb = KnowledgeStore(pl.db, emb, fb, top_k=3, max_items=40)
    pl.brain = Brain(raw_call=llm_fake)
    pl.game = Game(pl.db, pl.brain, pl.mem, pl._cfg, kb=pl.kb)
    pl._glocks = {}
    pl._umo_map = {}
    pl._pending = {}
    pl._confirm = {}
    pl.data_dir = tmpd
    pl._default_mode_hint = {}
    from ocverse.config import DEFAULT_INTERACTIONS
    pl._default_mode_hint = {m: d for m, d in DEFAULT_INTERACTIONS}
    pl._glock = lambda gid: pl._glocks.setdefault(gid, asyncio.Lock())
    return pl

async def main():
    pl = make_plugin()
    ev = FakeEvent("u1")
    # 1) 创建分身
    r = await pl.ocverse_create_character(ev, "凛", "白发蓝瞳的温柔少年")
    print("创建:", r.splitlines()[0], "|", "属性" in r)
    # 2) 再创建会拦截
    r2 = await pl.ocverse_create_character(ev, "凛2", "x")
    print("重复创建:", r2)
    # 3) 角色卡
    print("卡片:\n" + (await pl.ocverse_show_character(ev, "")))
    # 4) 修改人设
    print("修改:", await pl.ocverse_edit_character(ev, "性格改成开朗"))
    # 5) 初始化世界(非管理员被拦;管理员可建)
    print("未授权:", await pl.ocverse_init_world(ev, "蒸汽城"))
    admin = FakeEvent("u1", admin=True)
    print("初始化世界:", (await pl.ocverse_init_world(admin, "雾镇,低魔日常,临海"))[:60])
    print("事件频率(非管理员):", await pl.ocverse_admin_setting(ev, "频率", "2 4"))
    print("权限工具:", await pl.ocverse_trigger_world_shift(ev))
    # 6) 定义生活角色 + 互动
    print("生活角色:", await pl.ocverse_define_life_character(ev, "绫波", "雾码头的婆婆"))
    ev2 = FakeEvent("u2")
    await pl.ocverse_create_character(ev2, "阿澈", "话多")
    print("朋友互动:\n" + (await pl.ocverse_interact_with_friend(ev, "阿澈", "一起喝茶")))
    print("觅角色互动:\n" + (await pl.ocverse_interact_with_life(ev, "绫波", "打招呼")))
    # 7) 设施/行动/任务
    print("去茶楼:\n" + (await pl.ocverse_visit_place(ev, "清风茶楼", "喝茶")))
    print("行动:", (await pl.ocverse_do_action(ev, "练习", "剑术")).splitlines()[0])
    print("打工:", (await pl.ocverse_work_parttime(ev)).splitlines()[0])
    # 8) 世界与设施查看
    print("设施数:", len((await pl.ocverse_show_facilities(ev)).splitlines()))
    # 9) 工具集构建
    ts = pl._build_tool_set()
    names = sorted(ts.names())
    print("ToolSet 大小:", len(names))
    required = {
        "ocverse_help", "ocverse_show_character", "ocverse_roster", "ocverse_show_world",
        "ocverse_show_worlds", "ocverse_show_facilities", "ocverse_show_quests",
        "ocverse_inventory", "ocverse_search_memory", "ocverse_log",
        "ocverse_create_character", "ocverse_edit_character", "ocverse_define_life_character",
        "ocverse_define_world", "ocverse_add_npc", "ocverse_delete_npc",
        "ocverse_interact_with_friend", "ocverse_interact_with_life", "ocverse_interact_with_npc",
        "ocverse_visit_place", "ocverse_do_action", "ocverse_work_parttime",
        "ocverse_claim_quest", "ocverse_advance_mainline", "ocverse_travel_world",
        "ocverse_real_estate", "ocverse_propose_bond", "ocverse_init_world",
        "ocverse_trigger_world_shift", "ocverse_admin_setting",
        # 生命值 / 治疗物品 / 危险区域 / 声望
        "ocverse_heal", "ocverse_buy_item", "ocverse_show_zones", "ocverse_show_reputation",
    }
    missing = required - set(names)
    assert not missing, f"缺少工具: {missing}"
    for n in names:
        tool = ts.get_tool(n)
        assert tool.description, f"{n} 缺描述"
    print("✓ 工具集完整且每个都有描述")

    # 10) 知识库自动采集(此前 KnowledgeStore 缺 count 方法 → AttributeError 被吞,永不采集)
    calls = {"kb": 0}

    async def no_web(s, u):
        return None

    async def kb_llm(s, u):
        calls["kb"] += 1
        return json.dumps({"source": "《雾中信号》", "theme": "都市怪谈", "kind": "work",
                           "content": "一艘只在雾夜出现信号的小船,船员们用摩斯电码交换关于逝者的故事,"
                                      "直到有人收到自己发出的那条旧消息——整座小镇开始失眠。"}
                          , ensure_ascii=False)

    pl._llm_raw_enriched = no_web
    pl._llm_raw = kb_llm
    await pl._kb_maintenance()
    assert pl.kb.count("g1") == 1, f"空库首次应立即采集,实际 {pl.kb.count('g1')}"
    kb_total_before = pl.kb.count("g1")
    await pl._kb_maintenance()
    assert pl.kb.count("g1") == kb_total_before, "当日第二次应跳过(已标记 kb_last2)"
    print("✓ 知识库自动采集:空库即采 + 当日去重跳过")

    # 失败重试:LLM 连续失败 → 计数,3 次后当日放弃(不再打 LLM)
    async def kb_none(s, u):
        calls["kb"] += 1
        return None

    pl._llm_raw = kb_none
    await pl._kb_maintenance()   # fail 1
    await pl._kb_maintenance()   # fail 2
    await pl._kb_maintenance()   # fail 3 → 放弃
    fails3 = calls["kb"]
    await pl._kb_maintenance()   # 应直接跳过,不再调用
    assert calls["kb"] == fails3, "放弃后不应再调用 LLM"
    print("✓ 知识库采集失败:重试 3 次/天上限,防风暴")

    # 11) 互动目标解析:@ 组件 → 文本 @名字 → 角色名(QQ 官方接口 At 兼容)
    from astrbot.core.message.components import At

    class AtEvent(FakeEvent):
        def __init__(self, uid="u1", at=None):
            super().__init__(uid)
            self._at = at

        def get_messages(self):
            return [At(qq=self._at)] if self._at else []

    ev2 = FakeEvent("u2")   # u2 的分身「阿澈」在前文互动工具测试中已创建
    # ① At 组件(平台原生;官方 openid 非数字同样接受 —— 该 uid 需有分身)
    from ocverse.models import Char
    pl.db.upsert_char(Char(gid="g1", uid="OPENID_ABC", name="官方君"))
    t, rest = pl._resolve_interact_target("g1", AtEvent("u1", at="u2"), "@阿澈 打个招呼")
    assert t == "u2" and rest == "打个招呼", (t, rest)
    t, _ = pl._resolve_interact_target("g1", AtEvent("u1", at="OPENID_ABC"), "闲聊")
    assert t == "OPENID_ABC", t
    t, rest = pl._resolve_interact_target("g1", AtEvent("u1", at="NOCHAR_999"), "阿澈 闲聊")
    assert t == "u2" and rest == "闲聊", (t, rest)
    # ② 无 At 组件:文本「@名字」(官方接口常见形态)
    t, rest = pl._resolve_interact_target("g1", AtEvent("u1"), "@阿澈 请客")
    assert t == "u2" and rest == "请客", (t, rest)
    # ③ 直接写角色名
    t, rest = pl._resolve_interact_target("g1", AtEvent("u1"), "阿澈 闲聊")
    assert t == "u2" and rest == "闲聊", (t, rest)
    # ④ 陌生名字 → None(由上层给出名单帮助)
    t, _ = pl._resolve_interact_target("g1", AtEvent("u1"), "不存在的人 闲聊")
    assert t is None
    # ⑤ 官方接口唤醒:消息自带 At(机器人自身)/At(无分身者)/At(全体) → 全部回落名字解析
    bot_id = "BOT_SELF"
    ev_bot = AtEvent("u1", at=bot_id)
    ev_bot.get_self_id = lambda: bot_id
    t, rest = pl._resolve_interact_target("g1", ev_bot, "阿澈 打个招呼")
    assert t == "u2" and rest == "打个招呼", (t, rest)
    t, rest = pl._resolve_interact_target("g1", AtEvent("u1", at="all"), "@阿澈 请客")
    assert t == "u2" and rest == "请客", (t, rest)
    t, rest = pl._resolve_interact_target("g1", AtEvent("u1", at="NOCHAR_999"), "阿澈 闲聊")
    assert t == "u2" and rest == "闲聊", (t, rest)
    print("✓ 互动目标解析:@组件 → 文本@名字 → 角色名,官方接口兼容(唤醒At/全体/无分身均回落名字)")

    # 12) 非个人剧情(晨报/主动事件/远征等)一律走 _send_to 主动通道,不引用群友消息
    pl._remember_umo(ev)
    assert pl.db.kv_get("g1", "umo") == "aiocqhttp:GroupMessage:g1", "umo 应持久化"
    sent_umos = []

    async def ok_send(umo, chain):
        sent_umos.append(umo)
        return True

    pl._send_to = ok_send
    pl._pending["g1"] = [{"type": "morning", "gid": "g1", "world_name": "雾镇",
                          "brief": "雾更浓了。", "watch": "", "ok_llm": False}]
    async for _ in pl.on_group_msg(ev):
        pass
    assert sent_umos == ["aiocqhttp:GroupMessage:g1"], sent_umos
    assert not pl._pending.get("g1"), "发送成功应清空积压"
    print("✓ 积压补发走 _send_to 主动通道(不引用群友消息)")

    async def bad_send(umo, chain):
        return False

    pl._send_to = bad_send
    pl._pending["g1"] = [{"type": "morning", "gid": "g1", "brief": "x", "ok_llm": False}]
    async for _ in pl.on_group_msg(ev):
        pass
    assert len(pl._pending["g1"]) == 1, "主动通道失败应重新排队,卡片不丢"
    print("✓ 主动通道失败 → 卡片重新排队等待下次,不落消息引用")

asyncio.run(main())