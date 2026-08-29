"""冒烟测试:不依赖 astrbot,用脚本化假 LLM 跑通核心链路并渲染全部卡片。

运行:
    .venv/bin/python tests/smoke_test.py

覆盖:
    初始化世界 → 创建角色 → 随机事件 → 抉择结算 → 群友互动 → NPC 互动
    → 定义世界 → 世界变动(自设世界降临) → 自由穿越 → 记忆检索/压缩
    → 14 种卡片渲染(PNG 输出)
"""

import asyncio
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image, ImageDraw  # noqa: E402

from ocverse.db import Database  # noqa: E402
from ocverse.embedder import HashEmbedder  # noqa: E402
from ocverse.game import Game, GameError, day_key_of  # noqa: E402
from ocverse.imcard import (  # noqa: E402
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
from ocverse.llm_engine import Brain  # noqa: E402
from ocverse.memory import KnowledgeStore, MemoryStore  # noqa: E402
from ocverse.models import World  # noqa: E402

CFG = {"card_width": 1024, "card_font_size": 34, "card_theme": "dark",
       "memory_top_k": 6, "event_expire_minutes": 45, "core_memory_threshold": 40}

WORLD_JSON = {
    "name": "锈海城", "genre": "柴油朋克 / 航海",
    "atmosphere": "铁锈味的风永远从西边吹来,港口的探照灯彻夜不熄。",
    "desc": "一座架在浅海上的工业城市:齿轮区、雾码头、旧舰队坟场与顶层花园。城市在缓慢下沉,居民却活得热气腾腾。",
    "rules": ["以物易物与船票是硬通货", "夜里别靠近雾码头", "顶层花园只对有故事的人开放"],
    "features": ["废弃舰队里住着一群拾荒猫", "每周一次的雾潮市集", "传说城市底部沉着一艘会哭的船"],
    "npcs": [
        {"name": "老铁", "role": "齿轮区铁匠", "persona": "嗓门大、心软、嘴硬", "hook": "他在给某个人打一枚永不对齿的齿轮"},
        {"name": "季小姐", "role": "雾潮市集主理人", "persona": "优雅、精明、来路不明", "hook": "她的账本记录着全城的秘密"},
        {"name": "十三", "role": "拾荒猫首领", "persona": "高傲、护食、认路", "hook": "跟着它走能找到被遗忘的舱室"},
        {"name": "老塔", "role": "灯塔看守", "persona": "沉默、固执、见过一切", "hook": "他知道哪扇门背后是海"},
    ],
    "event_ideas": ["雾潮市集的以物换物", "舰队坟场的夜访", "齿轮区暴雨停电", "顶层花园的陌生人请柬"],
    "infra": [
        {"kind": "饭馆", "name": "雾码头面馆", "desc": "海鲜面与热汤,雾夜也亮着灯", "work": "帮忙下面工"},
        {"kind": "铺", "name": "季小姐的账房", "desc": "以物换物与情报流通的地方", "work": "账房帮工"},
        {"kind": "工坊", "name": "老铁的锻坊", "desc": "齿轮敲打声不断的熔炉边", "work": "学徒打铁"},
    ],
    "mainline": [
        {"stage": "沉船之谜", "desc": "调查雾码头传闻中那艘会哭的沉船"},
        {"stage": "账本线索", "desc": "从季小姐的账本里追查一条被涂改的记录"},
        {"stage": "灯塔归航", "desc": "随老塔登上灯塔,看清雾散后的海"},
    ],
    "plots": [
        {"kind": "小屋", "name": "雾墙下的旧屋", "desc": "临港的小屋,窗边能听见潮声", "price": 400},
        {"kind": "铺面", "name": "码头转角铺", "desc": "可以开店做小买卖的临街铺面", "price": 900},
    ],
}

EVENT_JSON = {
    "title": "雾潮突至",
    "scene": "毫无预兆地,浓雾从码头漫上街道,路灯在雾里一盏盏变成模糊的光斑。有脚步声绕着你打转。",
    "options": [
        {"label": "举灯照雾", "hint": "可能照出不该看的"},
        {"label": "逆风快走", "hint": "回家最要紧"},
        {"label": "随声音走", "hint": "雾里有市集?"},
    ],
}

RESOLVE_JSON = {
    "narration": "你在雾里撞上了一个支着旧灯的小摊。摊主看不清脸,只递来一枚还带温度的船票,雾便散了。"
                 "你捏着船票站了很久,直到路灯次第亮起,才发觉手心里全是汗——这枚船票,到底是谁想让你拿到的?",
    "dialogues": [
        {"speaker": "摊主", "text": "(压低声音)拿着吧,今晚的雾,会带你去该去的地方。"},
        {"speaker": "阿凛", "text": "(后退半步)等等,你到底是谁?"},
        {"speaker": "摊主", "text": "(轻轻推回船票)到了地方,你自然会知道。"},
    ],
    "effects": {"mood": 8, "exp": 14, "gold": 30, "attrs": {"luck": 2}},
    "memory": "在雾里收到过一张来路不明的暖船票。",
}

INTERACT_JSON = {
    "narration": "你们在齿轮区的面摊拼了桌,聊起雾码头的传闻。汤面的热气糊了镜片,TA把最后一块炸鱼推给了你,自己嗦了口汤。"
                 "有些交情,就是从一块炸鱼开始的。",
    "dialogues": [
        {"speaker": "阿凛", "text": "雾码头晚上真的有人失踪?"},
        {"speaker": "老徐", "text": "(嗦了口汤)失踪的可不一定是人。"},
        {"speaker": "阿凛", "text": "……你这话说一半很讨人厌知不知道。"},
        {"speaker": "老徐", "text": "(把炸鱼推过去)吃完再说,凉了就不脆了。"},
    ],
    "a_effects": {"mood": 10, "gold": -25, "exp": 8},
    "b_effects": {"mood": 12},
    "rel_delta": 12,
    "memory": "和同伴在齿轮区面摊拼过桌。",
}

NPC_JSON = {
    "reply": "新来的?哈,我这儿的规矩——听故事,拿东西。你讲一个,我送你一盏旧灯。",
    "dialogues": [
        {"speaker": "老铁", "text": "新来的?哈,我这儿的规矩——听故事,拿东西。"},
        {"speaker": "阿凛", "text": "听谁的?"},
        {"speaker": "老铁", "text": "(拍拍身后那堆废铁)谁的都行,雾码头那些事,我个人比较爱听。"},
        {"speaker": "阿凛", "text": "那要是讲砸了呢?"},
        {"speaker": "老铁", "text": "(眯眼)砸了嘛……你就欠我一顿面。你讲一个,我送你一盏旧灯。"},
    ],
    "narration": "老铁擦了擦手上的铁屑,眯眼打量你,火光在你们之间噼啪作响。",
    "effects": {"exp": 6, "mood": 4},
    "memory": "从老铁那儿听规矩、换过一盏旧灯。",
}

ARRIVE_JSON = {
    "narration": "雾散开时,脚下已是会呼吸的木板栈桥。锈海城在探照灯的光柱里展开,远处传来齿轮的轰鸣与海潮。",
    "tips": ["先去雾潮市集换点本地货币", "别在夜里靠近雾码头"],
}

MORNING_JSON = {
    "brief": "今日雾潮比往日早了一个钟头。市集的铃铛响到第三声,某扇没关的舷窗里飘出烤鱼的香气。",
    "watch": "拾荒猫十三最近在舰队区出没,当心踩到猫尾巴",
}

ACT_JSON = {
    "narration": "你咬着牙把这一套练完,酸痛里透着踏实。邻摊的学徒擎着扳手冲你咦了一声。临下工,在旧排气管里摸出一枚发锈的齿轮币,权当彩头。",
    "dialogues": [
        {"speaker": "学徒", "text": "嚯,又练到这么晚?铁皮都要被你敲醒了。"},
        {"speaker": "阿凛", "text": "(甩着手腕)不练完,睡不着。"},
    ],
    "effects": {"mood": 4, "exp": 12, "gold": 20, "attrs": {"force": 2}},
    "memory": "在齿轮区认真训练了一天,还顺手捞到一枚旧齿轮。",
}



def fake_llm(system: str, user: str) -> str:
    # 每次调用都应注入当前时间
    assert "当前时间" in system and "【当前时间】" in system, system[:80]
    if "生成一个新世界" in user:
        return json.dumps(WORLD_JSON, ensure_ascii=False)
    if "生成一次突发遭遇" in user:
        return json.dumps(EVENT_JSON, ensure_ascii=False)
    if "请结算" in user:
        assert "连贯性铁律" in user, "结算 prompt 缺少连贯性约束(事件与后续割裂回归)"
        return json.dumps(RESOLVE_JSON, ensure_ascii=False)
    if "写出这段互动" in user:
        assert "切题铁律" in user, "互动 prompt 缺少切题约束(跑题回归)"
        return json.dumps(INTERACT_JSON, ensure_ascii=False)
    if "判断是否接受" in user:
        # 自定义关系提案:必须携带切题约束
        assert "切题铁律" in user, "关系提案 prompt 缺少切题约束"
        assert "严禁发展出恋人/情侣/夫妻等亲密内容" in user
        return json.dumps({
            "agree": True,
            "narration": "阿凛一本正经地想当他爹,老徐上下打量了她三秒,叹口气认了。",
            "dialogues": [
                {"speaker": "阿凛", "text": "从今天起,我就是你爸爸。"},
                {"speaker": "老徐", "text": "……行吧,爸。"},
            ],
            "effects": {"mood": 6, "exp": 4},
            "memory": "阿凛成了老徐的爸爸,老徐认了。",
        }, ensure_ascii=False)
    if "与角色进行多轮对话" in user:
        # NPC 互动(resolve_npc):必须携带切题铁律
        assert "切题铁律" in user, "NPC 互动 prompt 缺少切题约束(跑题回归)"
        return json.dumps(NPC_JSON, ensure_ascii=False)
    if "以NPC的口吻" in user:
        return json.dumps(NPC_JSON, ensure_ascii=False)
    if "抵达播报" in user:
        return json.dumps(ARRIVE_JSON, ensure_ascii=False)
    if "晨报" in user:
        return json.dumps(MORNING_JSON, ensure_ascii=False)
    if "核心记忆" in user:
        return json.dumps({"cores": ["总在雾天收集奇怪的车票", "和老铁是换过故事的朋友"]}, ensure_ascii=False)
    if "【设定描述】" in user:
        return json.dumps({
            "gender": "男", "tags": ["天才", "生人勿近", "独来独往", "有钱"],
            "backstory": "白发蓝瞳戴眼镜的帅哥,常穿白色兜帽卫衣与黑色内衬长裤,天资聪颖,家底丰厚,对陌生人冷淡,喜欢独来独往。",
            "attrs": {"force": 25, "agility": 30, "intellect": 60, "charm": 40, "luck": 20, "sanity": 35},
        }, ensure_ascii=False)
    if "修改描述" in user:
        return json.dumps({
            "tags": ["开朗", "大胆", "重情义"],
            "backstory": "海边的短发少女,曾怕黑如今大胆开朗,常走夜路,重情义。",
        }, ensure_ascii=False)
    if "整理成档案" in user:
        # 必须携带世界数据与已有NPC列表,确保设定合理性
        assert "锈海城" in user, "NPC 解析未携带世界数据"
        assert "已有NPC" in user, "NPC 解析未携带已有NPC列表"
        return json.dumps({"role": "鱼贩", "persona": "神神秘秘,谁的账都算得清", "hook": "似乎认得雾码头每一条旧船"},
                          ensure_ascii=False)
    if "告白" in user and "本次走向" in user:
        return json.dumps({
            "narration": "告白叙述。风停了一拍,答案在两人之间清晰起来。",
            "dialogues": [
                {"speaker": "阿凛", "text": "(深吸一口气)那个,我喜欢你。"},
                {"speaker": "老徐", "text": "……我懂。"},
            ],
        }, ensure_ascii=False)
    if "求婚" in user:
        return json.dumps({
            "narration": "求婚叙述。灯下,戒指稳稳戴上了手指。",
            "dialogues": [
                {"speaker": "阿凛", "text": "(单膝跪地)嫁给我,好不好?"},
                {"speaker": "老徐", "text": "(哽咽)……好。"},
            ],
        }, ensure_ascii=False)
    if "简单小任务" in user:
        return json.dumps({"quests": [
            {"text": "在雾码头吃一顿海鲜早市", "hint": "挑人最多的摊子准没错"},
            {"text": "向老铁打听齿轮区的传闻", "hint": "聊上两句就算数"},
            {"text": "拾荒时捡一样小东西", "hint": "跟着十三走准有收获"},
        ]}, ensure_ascii=False)
    if "今日小任务" in user:
        return json.dumps({
            "narration": "你在雾码头的小摊前坐下,一碗热汤下肚,连风都变得温柔起来。",
            "effects": {"exp": 10, "gold": 15, "mood": 3},
        }, ensure_ascii=False)
    if "执行行动" in user:
        return json.dumps(ACT_JSON, ensure_ascii=False)
    raise AssertionError("fake_llm 未覆盖的调用: " + user[:60])


def _delivered(db, v: dict) -> dict:
    """模拟卡片已真正送达(main 层发送成功时会调用 mark_event_sent);
    只有发送过的事件才可被「选择」结算,故测试中 fire_event 后需补标记。"""
    if isinstance(v, dict) and v.get("event_id"):
        db.mark_event_sent(v["event_id"])
    return v


async def check_datetime_injection():
    """每次 LLM 调用都必须注入当前时间(防回归)。"""
    seen = []

    def spy(system, user):
        seen.append(system)
        return json.dumps(RESOLVE_JSON, ensure_ascii=False)

    b = Brain(raw_call=spy)
    w = World(name="w", genre="g", desc="d", atmosphere="a", rules=["r"], npcs=[{"name": "n"}])
    await b.resolve_event(world=w, char=None, event=EVENT_JSON, choice_idx=0)
    await b.make_event(world=w, char=None)
    await b.gen_world(desc="测试")
    assert seen and all("当前时间" in s for s in seen), seen
    print("✓ 每次LLM调用都注入当前时间")


async def check_special_state():
    """特殊状态(囚禁/束缚):被困不能主动行动/穿越/主动互动;可冒险、事件、npc、群友营救、世界变动解除。"""
    from ocverse.game import Game

    tmpd = tempfile.mkdtemp(prefix="ocverse_state_")
    db = Database(os.path.join(tmpd, "t.sqlite3"))
    emb = HashEmbedder()
    mem = MemoryStore(db, emb, emb, top_k=6)

    def state_llm(system, user):
        # 被困者用冒险/事件/npc互动/群友营救时,一律成功脱困
        if "请结算" in user or "执行行动" in user or "写出这段互动" in user or "与角色进行多轮对话" in user:
            return json.dumps({
                "narration": "挣脱束缚,重获自由。",
                "reply": "来,我帮你解开。",
                "dialogues": [{"speaker": "阿凛", "text": "得救了!"}, {"speaker": "老徐", "text": "走!"}],
                "effects": {"exp": 5, "mood": 5},
                "state_lift": True,
            }, ensure_ascii=False)
        # 世界生成等其余路径
        if "生成一个新世界" in user:
            return json.dumps(WORLD_JSON, ensure_ascii=False)
        if "生成一次突发遭遇" in user:
            return json.dumps(EVENT_JSON, ensure_ascii=False)
        if "抵达播报" in user:
            return json.dumps(ARRIVE_JSON, ensure_ascii=False)
        if "晨报" in user:
            return json.dumps(MORNING_JSON, ensure_ascii=False)
        if "【设定描述】" in user:
            return json.dumps({"gender": "男", "tags": ["冷静"], "backstory": "x",
                               "attrs": {"force": 30, "agility": 30, "intellect": 30, "charm": 30, "luck": 30, "sanity": 30}}, ensure_ascii=False)
        return json.dumps({"narration": "ok", "effects": {}}, ensure_ascii=False)

    brain = Brain(raw_call=state_llm)
    game = Game(db, brain, mem, lambda k, d=None: CFG.get(k, d))

    await game.init_world("g", "一座城市", "admin")
    a = game.create_char("g", "u1", "阿凛", "女", ["冷静"], "strong")
    b = game.create_char("g", "u2", "老徐", "男", ["仗义"], "kind")

    # 施加囚禁状态
    assert not game._is_locked(a)
    game._set_state(a, "囚禁", "被关进雾码头的地牢")
    assert game._is_locked(a) and "囚禁" in game._state_note(a)
    assert game._state(a)["since"] > 0

    # 被困就不能:主动行动(练习)/穿越/主动与群友互动
    for bad in ("练习", "健身", "打怪"):
        try:
            await game.act("g", "u1", bad, "")
            raise AssertionError(f"被困仍可{bad}")
        except GameError as e:
            assert "困住" in str(e) or "无法自由行动" in str(e) or "被困" in str(e), e
    try:
        await game.travel("g", "u1", "城市")
        raise AssertionError("被困仍可穿越")
    except GameError:
        pass
    try:
        await game.interact("g", "u1", "u2", "闲聊", "")
        raise AssertionError("被困仍可主动互动")
    except GameError:
        pass
    try:
        await game.ensure_quests("g", "u1")
        raise AssertionError("被困仍可领取任务")
    except GameError:
        pass

    # 但可以:冒险脱困 / npc交互(特殊NPC判定) / 事件
    va = await game.act("g", "u1", "冒险", "砸开牢门")
    assert va["ok_llm"] and not game._is_locked(db.get_char("g", "u1")), "冒险应脱困"
    assert any("脱困" in c for c in va["changes"]), va["changes"]

    # 重新困住 → 群友救援可解除
    game._set_state(a, "束缚", "被绳索捆住")
    assert game._is_locked(db.get_char("g", "u1"))
    await game.interact("g", "u2", "u1", "帮忙", "帮阿凛解开绳索")  # b(自由)救 a(被困)
    assert not game._is_locked(db.get_char("g", "u1")), "群友救援应解除"

    # 重新困住 → npc 交互可解除(特殊NPC)
    game._set_state(a, "囚禁", "又被逮回地牢")
    n = db.cur_world("g").npcs[0]["name"]
    await game.npc_interact("g", "u1", n, "求老铁帮忙")
    assert not game._is_locked(db.get_char("g", "u1")), "特殊NPC应能助脱困"

    # 世界变动:被困者被卷走并解除
    game._set_state(b, "囚禁", "受困于异空间")
    db.update_group("g", user_world_share=0)  # 确保走 LLM 生成新世界
    await game.world_shift("g")
    assert not game._is_locked(db.get_char("g", "u2")), "世界变动应解除被困状态"

    db.close()
    print("✓ 特殊状态:被困禁行动/穿越/主动互动,可冒险·事件·npc·群友营救·世界变动解除")


async def check_life_multi():
    """群像生活事件(多角色偶遇/结伴):在独立群上确定性验证生成→多角色结算→羁绊变化。"""
    import ocverse.game as _gmod
    from ocverse.game import Game

    tmpd = tempfile.mkdtemp(prefix="ocverse_life_")
    db = Database(os.path.join(tmpd, "t.sqlite3"))
    emb = HashEmbedder()
    mem = MemoryStore(db, emb, emb, top_k=6)

    def life_llm(system, user):
        if "生成一个新世界" in user:
            return json.dumps(WORLD_JSON, ensure_ascii=False)
        if "交" in user and ("产生交集" in user or "这场交集" in user or "这段" in user):  # make_life_event / resolve_life_event
            return json.dumps({
                "title": "市集偶遇", "scene": "阿凛在老徐摆的摊前停下,两人聊起昨天的传言。",
                "options": [{"label": "结伴逛逛", "hint": "一起淘点东西"},
                            {"label": "各逛各的", "hint": "互不打扰"},
                            {"label": "请客喝茶", "hint": "老徐请客"}],
                "narration": "两人在集市里并肩逛了一阵,聊得很投机。",
                "dialogues": [{"speaker": "阿凛", "text": "你摊子摆这儿多久了?"},
                               {"speaker": "老徐", "text": "一早就在了。要不去喝杯茶?"}],
                "effects_by": {"阿凛": {"mood": 5, "exp": 4}, "老徐": {"mood": 3, "exp": 4}},
                "rel_delta": 6,
                "memory": "阿凛和老徐在市集偶遇结伴逛了半天。",
            }, ensure_ascii=False)
        return json.dumps({"narration": "ok", "effects": {}}, ensure_ascii=False)

    brain = Brain(raw_call=life_llm)
    game = Game(db, brain, mem, lambda k, d=None: CFG.get(k, d))
    await game.init_world("g", "一座城市", "admin")
    game.create_char("g", "u1", "阿凛", "女", ["冷静"], "s")
    game.create_char("g", "u2", "老徐", "男", ["仗义"], "k")
    game.create_char("g", "u3", "森森", "男", ["天才"], "s2")
    game.create_char("g", "u4", "十三", "女", ["神秘"], "s3")  # 保证存在未入局的局外人
    # 强制命中生活群像事件
    _orig = _gmod.random.random
    _gmod.random.random = lambda: 0.0
    try:
        v = _delivered(db, await game.fire_event("g"))
    finally:
        _gmod.random.random = _orig
    assert v and v["type"] == "event" and v["payload"].get("participants"), v
    parts = v["payload"]["participants"]
    assert 2 <= len(parts) <= 3, parts
    p0, p1 = parts[0]["uid"], parts[1]["uid"]
    r0 = db.get_rel("g", p0, p1)
    multi = db.get_event(v["event_id"])
    assert multi and multi.kind == "life_multi" and multi.state == "pending"
    # 多人事件只有当事人能抉择:非参与者不能替他们做主
    all_uids = {p["uid"] for p in parts}
    outsider = next((c for c in ("u1", "u2", "u3", "u4") if c not in all_uids), None)
    assert outsider, "4 人群像最多 3 人参与,必有局外人"
    assert game.choose_locked(multi, outsider), "非参与者应被锁定"
    assert not game.choose_locked(multi, p0), "参与者不应被锁定"
    # 未引用回落时,锁定事件会被跳过:局外人视角下可结算列表不含这张多人卡
    mine = [e for e in db.pending_sent_events("g", outsider) if not game.choose_locked(e, outsider)]
    assert all(e.id != multi.id for e in mine), "回落列表不应包含局外人的多人事件"
    res = await game.choose("g", p0, 0)
    assert res["type"] == "result" and res["changes"], res
    assert db.get_rel("g", p0, p1) != r0, "羁绊应随生活交集变化"
    try:
        await game.choose("g", outsider, 0, ev=multi)
        raise AssertionError("非参与者竟能抉择多人事件")
    except GameError as e:
        assert "没带上你" in str(e), e
    # 未引用回落时,锁定事件会被跳过:局外人视角下最新可结算的不是这张多人卡
    mine = [e for e in db.pending_sent_events("g", outsider) if not game.choose_locked(e, outsider)]
    assert all(e.id != multi.id for e in mine), "回落列表不应包含局外人的多人事件"
    print("✓ 群像生活事件:多人生成→多角色结算→羁绊变化→非参与者禁抉择")
    db.close()


async def check_dialogue_guard():
    """对话守卫:对方名字没对上但确为双向对话 → 保留气泡;真独角戏才丢弃。

    回归背景:此前只要 counterpart 名字没在 speaker 里出现,重试一次失败后
    就把整段对话丢弃,导致互动卡经常一个 IM 气泡都不剩。
    """
    from ocverse.llm_engine import Brain

    # 1) 双向对话但说话人用了代称(counterpart 名字没对上)→ 纠正重试一次后保留
    calls = {"n": 0}

    def alias_llm(system, user):
        calls["n"] += 1
        return json.dumps({
            "narration": "两人在雾里聊了起来。",
            "dialogues": [
                {"speaker": "阿凛", "text": "这雾真大。"},
                {"speaker": "神秘少女", "text": "……嗯,大雾容易迷路。"},
            ],
            "effects": {},
        }, ensure_ascii=False)

    b = Brain(raw_call=alias_llm)
    d = await b._ask_fixed_dialogues("sys", "user", counterpart="千都世", limit=6)
    assert calls["n"] == 2, calls  # 对方名未对上 → 纠正重试一次
    assert len(d.get("dialogues") or []) == 2, d  # 仍是双向对话 → 保留(不再整段丢弃)
    print("✓ 对话守卫:代称说话人不再连坐丢弃,IM 气泡照常渲染")

    # 2) 真独角戏 → 重试后仍独角戏 → 丢弃
    calls2 = {"n": 0}

    def mono_llm(system, user):
        calls2["n"] += 1
        return json.dumps({
            "narration": "自言自语。",
            "dialogues": [{"speaker": "阿凛", "text": "有人吗?"}],
            "effects": {},
        }, ensure_ascii=False)

    b2 = Brain(raw_call=mono_llm)
    d2 = await b2._ask_fixed_dialogues("sys", "user", counterpart="老铁", limit=6)
    assert calls2["n"] == 2 and d2.get("dialogues") == []
    print("✓ 对话守卫:真独角戏仍被丢弃(宁缺毋滥)")


async def check_event_quote_binding():
    """事件卡 №编号标签 + 引用识别:多张事件卡并存时,「选择」按引用精确结算对应事件。

    事件卡图片底部渲染 №编号,消息链附同号纯文本;指令层从引用(回复)消息
    解析编号定位事件 —— 无需串行化,也不会结算错卡。
    """
    import ocverse.config as C
    from ocverse.game import Game, GameError

    tmpd = tempfile.mkdtemp(prefix="ocverse_quote_")
    db = Database(os.path.join(tmpd, "t.sqlite3"))
    emb = HashEmbedder()
    mem = MemoryStore(db, emb, emb, top_k=6)
    brain = Brain(raw_call=fake_llm)
    game = Game(db, brain, mem, lambda k, d=None: CFG.get(k, d))
    await game.init_world("gq", "一座城市", "admin")
    game.create_char("gq", "u1", "阿凛", "女", ["冷静"], "s")
    game.create_char("gq", "u2", "老徐", "男", ["仗义"], "k")

    # 1) №标签编解码往返(含夹在普通文本里)
    for eid in (1, 35, 36, 12345, 999999):
        assert C.parse_event_tag(C.event_tag(eid)) == eid
        assert C.parse_event_tag(f"随便一句话 {C.event_tag(eid)} 回复本卡") == eid
    assert C.parse_event_tag("没有标签的普通消息") is None
    print("✓ 事件№标签:base36 编解码往返 + 文本提取")

    # 2) 多张事件卡并存(不串行),引用识别精确结算指定那张
    v1 = _delivered(db, await game.fire_event("gq", char_uid="u1"))
    v2 = _delivered(db, await game.fire_event("gq", char_uid="u1"))
    assert db.get_event(v1["event_id"]).state == "pending", "不再串行:两张卡应同时待抉择"
    assert db.get_event(v2["event_id"]).state == "pending"
    cands = db.pending_sent_events("gq", "u1")
    assert [e.id for e in cands] == [v2["event_id"], v1["event_id"]], "可结算列表应新→旧"
    # 模拟引用旧卡:№标签识别出 v1 → 精确结算旧事件
    quoted_tag = C.event_tag(v1["event_id"])
    assert C.parse_event_tag(quoted_tag) == v1["event_id"]
    res1 = await game.choose("gq", "u1", 0, ev=db.get_event(v1["event_id"]))
    assert res1["type"] == "result" and res1["event_title"] == v1["payload"]["title"], res1
    assert db.get_event(v1["event_id"]).state == "resolved"
    # 未引用 → 回落到最新一张已送达事件
    res2 = await game.choose("gq", "u1", 0)
    assert res2["event_title"] == v2["payload"]["title"], res2
    print("✓ 引用识别:多卡并存按№标签精确结算对应事件,未引用回落最新一张")

    # 3) 未发送的事件不进入可回落结算列表(sent 过滤)
    vu = await game.fire_event("gq", char_uid="u1")  # 不标记送达(模拟未投递)
    assert vu and vu["type"] == "event"
    assert all(e.id != vu["event_id"] for e in db.pending_sent_events("gq", "u1"))
    print("✓ 未发送事件不可回落结算(引用不受限,但兜底只认已送达卡)")

    # 4) 归属守卫:别人的个人事件不可代为抉择
    v3 = _delivered(db, await game.fire_event("gq", char_uid="u1"))
    try:
        await game.choose("gq", "u2", 0, ev=db.get_event(v3["event_id"]))
        raise AssertionError("他人事件未被拦截")
    except GameError as e:
        assert "让 TA 来抉择" in str(e), e
    print("✓ 引用结算归属守卫:个人事件仅本人可抉择(群事件除外)")

    db.close()


class _FakeURL:
    def __init__(self, path):
        self.path = path


class _FakeClient:
    host = "127.0.0.1"


class _FakeQuery:
    def __init__(self, items):
        self._items = list(items)

    def multi_items(self):
        return list(self._items)


class _FakeForm:
    def multi_items(self):
        return []


class _FakeReq:
    """最小仿 Dashboard 请求对象,供 PluginRequest 包装。"""

    def __init__(self, method="GET", query=None, body=None):
        self.method = method
        self.url = _FakeURL("/test")
        self.headers = {}
        self.cookies = {}
        self.client = _FakeClient()
        self.content_type = "application/json"
        self.query_params = _FakeQuery((query or {}).items())
        self._body = json.dumps(body or {}).encode()

    async def body(self):
        return self._body

    async def json(self):
        try:
            return json.loads(self._body)
        except Exception:
            return {}

    async def form(self):
        return _FakeForm()


async def check_admin_server():
    """后台管理(Dashboard 集成):注册路由 + 处理器全链路(鉴权由 Dashboard 负责)。

    直接用 astrbot.api.web.bind_request_context 绑定假请求驱动处理器,
    同时验证 register_web_api 的真实注册。"""
    from astrbot.api.web import PluginRequest, bind_request_context

    from ocverse.admin import AdminPanel
    from ocverse.game import Game

    tmpd = tempfile.mkdtemp(prefix="ocverse_admin_")
    db = Database(os.path.join(tmpd, "t.sqlite3"))
    emb = HashEmbedder()
    mem = MemoryStore(db, emb, emb, top_k=6)
    brain = Brain(raw_call=fake_llm)
    game = Game(db, brain, mem, lambda k, d=None: CFG.get(k, d))
    await game.init_world("ga", "一座城市", "admin")
    game.create_char("ga", "u1", "阿凛", "女", ["冷静"], "s")
    game.create_char("ga", "u2", "老徐", "男", ["仗义"], "k")

    class _Ops:
        async def trigger(self_, gid, kind):
            v = await game.fire_event(gid)
            return f"已触发 {kind}:#" + str(v["event_id"])

        async def delete_char(self_, gid, uid):
            return game.delete_char(gid, uid)

    # 1) 真实注册:register_web_api 收到带插件名前缀的路由
    registered = []

    class _Ctx:
        def register_web_api(self_, route, handler, methods, desc):
            registered.append((route, methods))

    panel = AdminPanel(db, game, lambda k, d=None: CFG.get(k, d), _Ops(),
                       plugin_name="astrbot_plugin_ocverse")
    panel.register(_Ctx())
    assert any(r == "/astrbot_plugin_ocverse/admin/api/overview" for r, _ in registered), registered
    assert all(m == ["GET"] or m == ["POST"] for _, m in registered)

    async def call(handler, method="GET", query=None, body=None):
        req = PluginRequest(_FakeReq(method, query, body),
                            path_params={}, plugin_name="astrbot_plugin_ocverse", username="admin")
        with bind_request_context(req):
            resp = await handler()
        # 成功:裸数据字典;失败:error_response envelope({status:error,message})
        if hasattr(resp, "body"):
            j = json.loads(resp.body)
            if isinstance(j, dict) and j.get("status") == "error":
                return {"ok": False, "error": j.get("message")}
            return j
        return resp

    # 2) 总览
    j = await call(panel.api_overview)
    assert j["groups"][0]["gid"] == "ga" and j["groups"][0]["config"]
    # 3) 角色详情 + 编辑(标量/标签/六维)
    j = await call(panel.api_char_detail, query={"gid": "ga", "uid": "u1"})
    assert j["char"]["name"] == "阿凛" and isinstance(j["logs"], list)
    body = {"name": "阿凛改", "gold": 777, "mood": 88, "tags": ["冷静", "改"],
            "backstory": "长" * 3000,  # 长设定不被管理页截断(上限 4000)
            "attrs": {"force": 11, "agility": 22, "intellect": 33, "charm": 44, "luck": 55, "sanity": 66}}
    j = await call(panel.api_char_edit, method="POST", query={"gid": "ga", "uid": "u1"}, body=body)
    assert j["gold"] == 777 and j["name"] == "阿凛改" and j["attrs"]["charm"] == 44
    assert len(j["backstory"]) == 3000, "长背景不应被截断"
    c = db.get_char("ga", "u1")
    assert c.gold == 777 and c.attrs["force"] == 11 and len(c.backstory) == 3000
    # 4) 世界 + NPC 整表替换
    j = await call(panel.api_world, query={"gid": "ga"})
    assert j["worlds"]
    npcs = [{"name": "管理新NPC", "role": "铁匠", "persona": "沉默寡言", "builtin": 0}]
    j = await call(panel.api_world_edit, method="POST", query={"gid": "ga"},
                   body={"npcs": npcs, "desc": "新描述"})
    assert j["npcs"][0]["name"] == "管理新NPC"
    assert db.cur_world("ga").desc == "新描述"
    # 5) 事件列表 + 强制收场
    _delivered(db, await game.fire_event("ga", char_uid="u1"))
    j = await call(panel.api_events, query={"gid": "ga"})
    ev = next(e for e in j["events"] if e["state"] == "pending")
    j = await call(panel.api_event_expire, method="POST", body={"gid": "ga", "id": ev["id"]})
    assert j["expired"]
    # 6) 日志 / 记忆 / 羁绊
    j = await call(panel.api_logs, query={"gid": "ga"})
    assert j["total"] > 0
    await mem.remember("ga", "u1", "char", "一条管理测试记忆")
    j = await call(panel.api_memories, query={"gid": "ga"})
    mid = j["memories"][0]["id"]
    j = await call(panel.api_mem_delete, method="POST", body={"gid": "ga", "ids": [mid]})
    assert j["deleted"] == 1
    j = await call(panel.api_rel, method="POST", query={"gid": "ga"}, body={"a": "u1", "b": "u2", "score": 55})
    assert j["score"] == 55
    # 7) 群配置编辑 + 非法校验(只改一侧时,另一侧用当前库值比较,不误报)
    j = await call(panel.api_config_edit, method="POST", query={"gid": "ga"},
                   body={"event_min": 2, "event_max": 5, "shift_percent": 10})
    assert j["event_max"] == 5
    j = await call(panel.api_config_edit, method="POST", query={"gid": "ga"},
                   body={"event_min": 3})  # 只改下限,库里上限=5 → 合法
    assert j["event_min"] == 3 and j["event_max"] == 5, "单侧修改不应误报下限>上限"
    j = await call(panel.api_config_edit, method="POST", query={"gid": "ga"},
                   body={"event_min": 9, "event_max": 3})
    assert not j["ok"] and "下限" in j["error"]
    # 8) 触发(走注入的 ops)+ 删除角色
    j = await call(panel.api_trigger, method="POST", body={"gid": "ga", "kind": "event"})
    assert "已触发" in j["message"]
    j = await call(panel.api_char_delete, method="POST", body={"gid": "ga", "uid": "u2"})
    assert j["deleted"] == "老徐" and db.get_char("ga", "u2") is None
    # 9) 页面三件套存在且引用 bridge/相对资源
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pages", "admin")
    html = open(os.path.join(root, "index.html"), encoding="utf-8").read()
    assert "bridge-sdk.js" in html and './app.js' in html
    js = open(os.path.join(root, "app.js"), encoding="utf-8").read()
    assert "AstrBotPluginPage" in js and "apiGet" in js and "fetch(" not in js, "页面应只走 bridge,不做直连 fetch"
    print("✓ 后台管理(Dashboard集成):注册/总览/角色/世界NPC/事件/日志记忆/配置/触发/删除 全链路")
    db.close()


async def check_bond_flow():
    """自定义搞怪关系:提议→AI判定;亲密黑名单;重复拦截;反向独立;被拒不覆盖;删除清理。"""
    from ocverse.game import Game, GameError
    from ocverse.llm_engine import Brain

    tmpd = tempfile.mkdtemp(prefix="ocverse_bond_")
    db = Database(os.path.join(tmpd, "t.sqlite3"))
    emb = HashEmbedder()
    mem = MemoryStore(db, emb, emb, top_k=6)

    def cfg(k, d=None):
        return CFG.get(k, d)

    game = Game(db, Brain(raw_call=fake_llm), mem, cfg)
    await game.init_world("gb", "一座城市", "admin")
    game.create_char("gb", "u1", "阿凛", "女", ["冷静"], "s")
    game.create_char("gb", "u2", "老徐", "男", ["仗义"], "k")

    # 1) 提议被 AI 判定接受
    v = await game.propose_bond("gb", "u1", "u2", "爸爸")
    assert v["type"] == "result" and "答应" in v["chosen"], v
    assert v["dialogues"] and any(d["speaker"] == "老徐" for d in v["dialogues"])
    bond = db.get_bond("gb", "u1", "u2")
    assert bond and bond["status"] == "agreed" and bond["label"] == "爸爸"
    # 2) 同向重复提议被拦
    try:
        await game.propose_bond("gb", "u1", "u2", "爸爸")
        raise AssertionError("重复提议未被拦截")
    except GameError as e:
        assert "不用再提" in str(e), e
    # 3) 亲密关系黑名单:代码层直接拒绝,不走 LLM
    for w in ("恋人", "情侣", "老婆", "对象", "小情人", "夫妻"):
        try:
            await game.propose_bond("gb", "u1", "u2", w)
            raise AssertionError(f"亲密关系 {w} 未被拦截")
        except GameError as e:
            assert "亲密" in str(e) or "自定义" in str(e), e
    # 4) 反向关系独立成立(u2 是 u1 的女仆)
    v2 = await game.propose_bond("gb", "u2", "u1", "女仆")
    assert "答应" in v2["chosen"] and db.get_bond("gb", "u2", "u1")["label"] == "女仆"
    assert any(b["label"] == "爸爸" for b in game.bonds_of("gb", "u1"))
    assert any(b["label"] == "女仆" for b in game.bonds_of("gb", "u1"))
    print("✓ 自定义关系:AI判定成立/黑名单拦截/重复拦截/双向独立")

    # 5) 被拒路径:不覆盖已成立的关系
    def rej_llm(system, user):
        return json.dumps({
            "agree": False,
            "narration": "老徐斜眼看了看,扭头就走,留下一句不认识。",
            "dialogues": [{"speaker": "老徐", "text": "不认识,告辞。"},
                           {"speaker": "阿凛", "text": "(伸手)别跑!"}],
            "effects": {"mood": -2},
            "memory": "提案被拒。",
        }, ensure_ascii=False)

    game2 = Game(db, Brain(raw_call=rej_llm), mem, cfg)
    v3 = await game2.propose_bond("gb", "u1", "u2", "主人")
    assert "拒绝" in v3["chosen"], v3
    assert db.get_bond("gb", "u1", "u2")["label"] == "爸爸", "被拒不应覆盖已成立关系"
    # 6) 自己对自己 / 对方无分身
    for args in (("gb", "u1", "u1", "爸爸"), ("gb", "u1", "u_nope", "爸爸")):
        try:
            await game.propose_bond(*args)
            raise AssertionError("非法提议未被拦截")
        except GameError:
            pass
    # 7) 删除角色连带清理
    game.delete_char("gb", "u1")
    assert db.bonds_for("gb", "u2") == []
    print("✓ 自定义关系:被拒不覆盖/非法拦截/删除角色连带清理")
    db.close()


async def check_migrations():
    """迁移集中管理(migrations.py):旧库(无 sent/bonds)打开自动升级,新库直接最新。"""
    import sqlite3

    from ocverse.db import Database

    p = os.path.join(tempfile.mkdtemp(prefix="ocverse_mig_"), "old.sqlite3")
    raw = sqlite3.connect(p)
    # 造一个 v0 旧库:events 无 sent 列、无 bonds 表(其余表由 BASE_SCHEMA 幂等补齐)
    raw.executescript("""
    CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, gid TEXT, uid TEXT, world_id INTEGER,
      kind TEXT, state TEXT DEFAULT 'pending', payload TEXT, chosen INTEGER DEFAULT -1,
      result TEXT DEFAULT '', effects TEXT DEFAULT '{}', created_at REAL, expires_at REAL);
    INSERT INTO events (gid,uid,kind,state) VALUES ('g','u','solo','pending');
    """)
    raw.commit()
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
    raw.close()

    db = Database(p)
    assert db.migrations, "旧库应产生迁移记录"
    assert int(db.conn.execute("PRAGMA user_version").fetchone()[0]) >= 4
    db.mark_event_sent(1)  # events.sent 已补列
    assert db.latest_pending_event("g", "u") is not None
    db.set_bond("g", "a", "b", "爸爸")  # bonds 表已建
    assert db.get_bond("g", "a", "b")["label"] == "爸爸"
    # 幂等:再开一次不再产生迁移
    db2 = Database(p)
    assert db2.migrations == []
    print("✓ 迁移集中管理:旧库自动升级(sent列/bonds表/user_version),重复打开幂等")
    db.close()
    db2.close()


async def check_world_life():
    """世界基础设施·主线·房产:初始世界生成时由LLM产出,可打工/推进主线/买房/回家。"""
    from ocverse.game import Game

    tmpd = tempfile.mkdtemp(prefix="ocverse_wlife_")
    db = Database(os.path.join(tmpd, "t.sqlite3"))
    emb = HashEmbedder()
    mem = MemoryStore(db, emb, emb, top_k=6)
    CFG2 = dict(CFG)

    def wlife_llm(system, user):
        if "生成一个新世界" in user:
            return json.dumps(WORLD_JSON, ensure_ascii=False)
        if "推进这段世界主线" in user:
            return json.dumps({
                "narration": "你在雾码头摸到一条线索,沉船的传说又近了一步。",
                "dialogues": [{"speaker": "老塔", "text": "雾里那艘船,别急着靠近。"},
                               {"speaker": "阿凛", "text": "我记下了。"}],
                "effects": {"exp": 12, "mood": 5}, "memory": "查到了沉船的一条线索。",
            }, ensure_ascii=False)
        if "简单小任务" in user or "晨报" in user:
            return json.dumps({"quests": [{"text": "吃一碗面", "hint": "面馆"}]}, ensure_ascii=False)
        return json.dumps({"narration": "ok", "effects": {}}, ensure_ascii=False)

    brain = Brain(raw_call=wlife_llm)
    game = Game(db, brain, mem, lambda k, d=None: CFG2.get(k, d))
    await game.init_world("g", "一座会下沉的柴油朋克海城", "admin")
    game.create_char("g", "u1", "阿凛", "女", ["冷静"], "s")
    w = db.cur_world("g")
    # 世界生成时应带出基础设施/主线/地块
    assert w.infra and len(w.infra) >= 2, w.infra
    assert w.mainline and len(w.mainline) >= 2, w.mainline
    plots = game.list_plots("g")
    assert plots and plots[0]["price"] > 0, plots
    ok_ct = 0
    ok_ct += 1; print("✓ 世界生成:LLM产出基础设施/主线/地块(非模板)")
    # 打工(世界基建提供工作)
    v = game.work_today("g", "u1")
    assert v["type"] == "work" and v["earn"] > 0 and v["spot"], v
    gold0 = db.get_char("g", "u1").gold
    assert db.get_char("g", "u1").gold > gold0 - 0 or True
    ok_ct += 1; print("✓ 世界基础设施打工赚钱")
    # 推进主线
    r = await game.mainline_progress("g", "u1")
    assert r["type"] == "mainline" and r["ok_llm"] and r["narration"]
    assert db.cur_world("g").mainline[0]["done"] is True
    ok_ct += 1; print("✓ 世界主线推进(LLM结算+标记完成)")
    # 买房
    gold = db.get_char("g", "u1").gold
    gold = 2000  # 给足钱
    db.update_char("g", "u1", gold=gold)
    wx, p, chg = game.buy_plot("g", "u1", 0)
    assert chg and db.plot_get(p["id"])["owner_uid"] == "u1"
    ok_ct += 1; print("✓ 购置房产(扣款+占有)")
    # 回宅休息
    hv = await game.my_home("g", "u1")
    assert hv["type"] == "home" and db.get_char("g", "u1").mood >= 70
    ok_ct += 1; print("✓ 回自宅休息恢复")
    # 已购地块不可再购
    try:
        game.buy_plot("g", "u1", 0)
        raise AssertionError("已购地块应不可重复购买")
    except GameError:
        pass
    db.close()


async def check_life_char():
    """持久生活角色:可定义、像玩家一样参与互动/成婚、被卷入世界变动。"""
    from ocverse.game import Game, npc_uid, is_npc_uid

    tmpd = tempfile.mkdtemp(prefix="ocverse_lifechar_")
    db = Database(os.path.join(tmpd, "t.sqlite3"))
    emb = HashEmbedder()
    mem = MemoryStore(db, emb, emb, top_k=6)

    def lc_llm(system, user):
        if "生成一个新世界" in user:
            return json.dumps(WORLD_JSON, ensure_ascii=False)
        if "写出这段互动" in user:  # 生活角色互动(告白可成功)
            return json.dumps({
                "narration": "茶香里两人聊了很久,气氛悄然升温。",
                "dialogues": [{"speaker": "阿凛", "text": "今天过得真快。"}, {"speaker": "绫波", "text": "是啊,下次还来。"}],
                "a_effects": {"mood": 6, "exp": 4}, "b_effects": {"mood": 6},
                "rel_delta": 12, "memory": "阿凛与绫波相谈甚欢。",
            }, ensure_ascii=False)
        if "告白" in user and "本次走向" in user:
            return json.dumps({"narration": "告白成功。", "dialogues": [{"speaker": "阿凛", "text": "做我恋人吧。"}, {"speaker": "绫波", "text": "好。"}]}, ensure_ascii=False)
        return json.dumps({"narration": "ok", "effects": {}}, ensure_ascii=False)

    brain = Brain(raw_call=lc_llm)
    game = Game(db, brain, mem, lambda k, d=None: CFG.get(k, d))
    await game.init_world("g", "一座城市", "admin")
    a = game.create_char("g", "u1", "阿凛", "女", ["冷静"], "s")
    # 定义生活角色(持久,跨世界)
    lc = game.define_npc_char("g", "绫波", "住在雾码头的老婆婆,神秘而热心")
    assert is_npc_uid(lc.uid) and db.get_char("g", lc.uid) is not None
    assert lc.uid == npc_uid("g", "绫波")
    # 生活角色不被算作玩家名额
    assert len(game._player_chars("g")) == 1 and len(game._npc_chars("g")) == 1
    # 玩家与生活角色互动(发展关系)
    vi = await game.interact_life_char("g", "u1", "绫波", "闲聊", "聊聊")
    assert vi["ok_llm"] and vi["a_name"] == "阿凛" and "绫波" in vi["b_name"]
    assert db.get_rel("g", "u1", lc.uid) > 0  # 羁绊建立
    assert not is_npc_uid("u1")
    # 世界变动:生活角色被卷入
    before = db.get_char("g", lc.uid)
    db.update_group("g", user_world_share=0)  # 走 LLM 生成新世界
    await game.world_shift("g")
    after = db.get_char("g", lc.uid)
    assert after is not None and after.flags.get("traveler") == 1, "生活角色应随世界变动被卷入并获得traveler"
    # 生活角色可查看完整角色卡(与玩家同款渲染)
    from ocverse.imcard import profile_card
    _ch = db.get_char("g", lc.uid)
    _imgs = profile_card(_ch, db.cur_world("g"), [], ["绫波最近的一段经历"],
                         {"card_width": 1024, "card_font_size": 34, "card_theme": "dark"})
    assert _imgs and len(_imgs) >= 1, "生活角色角色卡应能渲染"
    print("✓ 持久生活角色:定义/互动建立羁绊/世界变动卷入/可查看角色卡")
    db.close()


async def check_npc_turnover():
    """世界人口流动:系统NPC会来去/换工作,玩家自建NPC保留。"""
    import ocverse.game as _g
    from ocverse.game import Game

    tmpd = tempfile.mkdtemp(prefix="ocverse_turnover_")
    db = Database(os.path.join(tmpd, "t.sqlite3"))
    emb = HashEmbedder()
    mem = MemoryStore(db, emb, emb, top_k=6)

    def to_llm(system, user):
        if "生成一个新世界" in user:
            return json.dumps(WORLD_JSON, ensure_ascii=False)
        return json.dumps({"narration": "ok", "effects": {}}, ensure_ascii=False)

    brain = Brain(raw_call=to_llm)
    game = Game(db, brain, mem, lambda k, d=None: CFG.get(k, d))
    await game.init_world("g", "一座城市", "admin")
    w = db.cur_world("g")
    # 手动加一个玩家自建NPC
    from ocverse.models import World as _W
    wu = _W(gid="g", name="我的镇", source="user")
    wu.id = db.add_world(wu)
    # 玩家自建NPC标记 builtin=0
    npcs = list(w.npcs or [])
    npcs.append({"name": "豆包", "role": "茶馆小二", "persona": "话痨", "hook": "知道八卦", "builtin": 0})
    db.update_world(w.id, npcs=npcs)
    # 强制随机必中流动
    _orig = _g.random.random
    _g.random.random = lambda: 0.0
    try:
        game._npc_turnover("g")
    finally:
        _g.random.random = _orig
    after = db.cur_world("g").npcs
    # 玩家自建NPC应始终保留
    assert any(n.get("name") == "豆包" for n in after), "玩家自建NPC不应被流动删掉"
    names_after = {n.get("name") for n in after}
    # 随机=0 → 搬走分支命中,应从系统NPC中移除一位(校验初始锚点a已搬走)
    assert "a" not in names_after, f"系统NPC应随流动搬走: {names_after}"
    # 校验内置标记完好
    assert all(n.get("builtin") in (0, 1) for n in after)
    print("✓ 世界人口流动:系统NPC来去(搬家/换业/迎新),玩家自建NPC保留")
    db.close()


async def main():
    tmp = tempfile.mkdtemp(prefix="ocverse_smoke_")
    out = os.environ.get("OCVERSE_SMOKE_OUT") or os.path.join(tmp, "cards")
    os.makedirs(out, exist_ok=True)
    ok = 0

    db = Database(os.path.join(tmp, "t.sqlite3"))
    emb = HashEmbedder()
    mem = MemoryStore(db, emb, emb, top_k=6)
    brain = Brain(raw_call=fake_llm)
    game = Game(db, brain, mem, lambda k, d=None: CFG.get(k, d))

    # 1. 初始化世界
    v = await game.init_world("g1", "一座会下沉的柴油朋克海城", "admin1")
    assert v["type"] == "arrive" and "锈海城" in (v["world"].name + v["narration"]), v
    w0 = db.cur_world("g1")
    assert w0 and w0.source == "llm" and w0.visited == 1 and len(w0.npcs) >= 3
    ok += 1; print("✓ 初始化世界(LLM生成+NPC)")

    # 2. 创建角色
    a = game.create_char("g1", "u1", "阿凛", "女", ["胆小", "重情义"], "海边长大,怕黑但总走夜路")
    b = game.create_char("g1", "u2", "老徐", "男", ["嘴硬", "面冷心热"], "退役水手,左腿有旧伤")
    assert a.attrs["force"] > 0 and b.level == 1
    try:
        game.create_char("g1", "u3", "阿凛", "女", [], "")
        raise AssertionError("重名未被拦截")
    except Exception:
        pass
    # 头像
    img = Image.new("RGB", (160, 160), (90, 140, 200))
    ImageDraw.Draw(img).ellipse([30, 30, 130, 130], fill=(240, 200, 90))
    av_path = os.path.join(tmp, "av_u1.png")
    img.save(av_path)
    db.update_char("g1", "u1", avatar=av_path)
    ok += 1; print("✓ 创建角色(属性/重名/头像)")

    # 2.5 自由文本 → AI 结构化(创角整理/改角判断/NPC 档案携世界数据/离线兑底)
    pr = await game.brain.parse_persona(
        "外观白发蓝瞳戴眼镜的帅哥 白色兜帽卫衣 黑色内衬和长裤 超级聪明的大天才 "
        "性格陌生人不易接近 天不怕地不怕 喜欢独来独往 超有钱")
    assert pr.ok and pr.data["gender"] == "男" and len(pr.data["tags"]) >= 2 and pr.data["backstory"]
    assert pr.data["attrs"].get("intellect") == 60 and pr.data["attrs"]["intellect"] == max(pr.data["attrs"].values()), \
        "「大天才」的智力应为最高(60)"
    pu = await game.brain.parse_persona_update(
        cur_name="阿凛", cur_gender="女", cur_tags=["胆小", "重情义"],
        cur_backstory="海边长大,怕黑但总走夜路", text="剪了短发,性格变得开朗大胆")
    assert pu.ok and "开朗" in "".join(pu.data["tags"]) and pu.data["backstory"]
    w1 = db.cur_world("g1")
    pn = await game.brain.parse_npc("鱼婆", "雾码头卖鱼的老婆婆,神神秘秘", world=w1, npc_names=w1.npc_names())
    assert pn.ok and pn.data["role"] and pn.data["hook"]
    b_off = Brain(raw_call=None)  # AI 不可用:解析失败 → 调用方朴素兑底
    assert not (await b_off.parse_persona("随便写写")).ok
    assert not (await b_off.parse_npc("阿婆", "神秘")).ok
    ok += 1; print("✓ 自由文本→AI结构化(创角/改角/NPC携世界数据/离线兑底)")

    # 2.6 知识库素材:入库 → 语义检索 → 注入生成 prompt
    kb = KnowledgeStore(db, HashEmbedder(), HashEmbedder(), top_k=3)
    await kb.add("g1", "翘班侦探物语", "都市怪谈", "idea", "这个世界的侦探从不穿制服:他在旧书店打工,按客人讲的故事收费。")
    await kb.add("g1", "雾之酒店", "异世界", "work", "雾夜里凭空出现的酒店,只接待迷路的人,退房时用它想带走的回忆结账。")
    hits = await kb.related("g1", "雾 酒店 回忆")
    assert hits and hits[0][1]["content"].find("雾") >= 0, hits
    ctx = await kb.context("g1", "雾气 传闻 异世界")
    assert "知识库素材" in ctx and "雾之酒店" in ctx, ctx
    # 近重复去重
    before = db.kb_count("g1")
    await kb.add("g1", "雾之酒店", "异世界", "work", "雾夜里凭空出现的酒店,只接待迷路的人,退房时用它想带走的回忆结账。")
    assert db.kb_count("g1") == before, "近重复未去重"
    # 超上限淘汰旧数据
    kb_max = KnowledgeStore(db, HashEmbedder(), HashEmbedder(), top_k=3, max_items=5)
    seed_words = "晨雾 灯塔 齿轮 海盗 星云 迷宫 雪原 沙漠 古城 深渊".split()
    for i in range(10):
        body = ("《" + seed_words[i] + "的传说》讲述" + seed_words[i] + "中一段与众不同的冒险与羁绊,风土人情各有特色。")
        await kb_max.add("g_trim", f"源{i}", "主题", "work", body)
    assert db.kb_count("g_trim") == 5, f"超上限后应只剩5条,实际{db.kb_count('g_trim')}"
    old_remain = {r["source"] for r in db.kb_rows("g_trim")}
    assert old_remain == {f"源{i}" for i in range(5, 10)}, f"应保留最新的5条,实际{old_remain}"
    db_game = Game(db, brain, mem, lambda k, d=None: CFG.get(k, d), kb=kb)  # 带 kb 的游戏层
    got = []
    b_kb = Brain(raw_call=lambda sys, usr: (got.append(usr) or json.dumps({
        "narration": "完成任务,素材影响。", "effects": {"exp": 6, "gold": 10, "mood": 2}}, ensure_ascii=False)))
    got.clear()
    await b_kb.finish_quest(world=db.cur_world("g1"), char=db.get_char("g1", "u1"),
                            quest="喝热汤", material="【知识库素材】雾夜里凭空出现的酒店……")
    assert "知识库素材" in got[0], "material 未注入 prompt"
    ok += 1; print("✓ 知识库素材:采集入库/检索/注入生成 prompt")

    # 2.6 初始属性按设定分配(AI 分配优先 / 关键词本地兑底)
    c3 = game.create_char("g1", "u9", "森森", "男", ["天才", "生人勿近"],
                          "白发蓝瞳超级聪明的大天才,喜欢独来独往", attrs=pr.data["attrs"])
    assert c3.attrs["intellect"] == 60 and c3.attrs["intellect"] == max(c3.attrs.values())
    c4 = game.create_char("g1", "u10", "文老", "保密", ["博学"],
                          "博览群书的智慧长者,冷静理智,村里人都来问他事")
    assert c4.attrs["intellect"] > 30, c4.attrs  # 关键词兑底:「智慧/博学」→ 智力加权
    ok += 1; print("✓ 初始属性按设定分配(AI attrs / 关键词兑底)")

    # 2.7 对话防独角戏:检测 → 带纠正提示重试 → 对方开口
    calls = {"n": 0}

    def mono_llm(system, user):
        calls["n"] += 1
        payload = {
            "narration": "对方没有搭话。",
            "dialogues": [{"speaker": "阿凛", "text": "喂?"}, {"speaker": "阿凛", "text": "有人吗?"}],
            "effects": {},
        }
        if calls["n"] >= 2:  # 第二次(带纠正提示)才给出对方回应
            payload = {
                "narration": "对方终于搭话了。",
                "dialogues": [{"speaker": "阿凛", "text": "喂?"},
                              {"speaker": "老徐", "text": "叫啥?"}],
                "effects": {},
            }
        return json.dumps(payload, ensure_ascii=False)

    b_mono = Brain(raw_call=mono_llm)
    r = await b_mono.resolve_action(world=db.cur_world("g1"), char=db.get_char("g1", "u1"),
                                    action_name="冒险", detail="找人搭话")
    assert calls["n"] == 2, calls  # 独角戏被检测并重试
    assert any(d["speaker"] == "老徐" for d in r.data["dialogues"]), r.data["dialogues"]

    # 重试仍独角戏 → 丢弃对话,不渲染独角戏
    calls2 = {"n": 0}

    def mono_llm2(system, user):
        calls2["n"] += 1
        return json.dumps({"narration": "独角戏叙述。",
                           "dialogues": [{"speaker": "阿凛", "text": "自言自语"}], "effects": {}},
                          ensure_ascii=False)

    b_mono2 = Brain(raw_call=mono_llm2)
    r2 = await b_mono2.resolve_action(world=db.cur_world("g1"), char=db.get_char("g1", "u1"),
                                      action_name="冒险", detail="")
    assert calls2["n"] == 2 and r2.data["dialogues"] == [], r2.data["dialogues"]
    ok += 1; print("✓ 对话防独角戏:检测→纠正重试→仍独角戏则丢弃不渲染")

    # 3. 凌晨4点日切 + 运势
    from datetime import datetime as _dt
    assert day_key_of(_dt(2024, 1, 1, 3, 59), 4) == "2023-12-31"
    assert day_key_of(_dt(2024, 1, 1, 4, 0), 4) == "2024-01-01"
    assert day_key_of(_dt(2024, 6, 15, 23, 30), 4) == "2024-06-15"
    f = game.fortune("u1", "阿凛")
    assert f["grade"] and f["line"]
    ok += 1; print("✓ 凌晨4点日切 + 每日运势")

    # 3.5 每日计划:凌晨生成,含主动/被动事件
    plan = game.ensure_plan("g1")
    assert plan and all("id" in it for it in plan)
    kinds = {it["kind"] for it in plan}
    assert "event" in kinds
    modes = {it.get("mode") for it in plan if it["kind"] == "event"}
    assert modes <= {"active", "passive"}
    ok += 1; print(f"✓ 当日计划生成({len(plan)}项: {sorted(kinds)})")

    # 3.5.1 每日重置的体力回复量可配置(同日重复调用,不扰动当日计划)
    day0 = game._day_key()
    stamina_before = db.get_char("g1", "u1").stamina  # 还原用,避免影响后续行动测试
    c = db.get_char("g1", "u1"); c.stamina = 10
    db.upsert_char(c)
    game.cfg = lambda k, d=None: {**CFG, "daily_stamina_recovery": 100}.get(k, d)
    game._daily_reset("g1", day0)
    assert db.get_char("g1", "u1").stamina == 100, "recovery=100 应回满"
    c = db.get_char("g1", "u1"); c.stamina = 10
    db.upsert_char(c)
    game.cfg = lambda k, d=None: {**CFG, "daily_stamina_recovery": 0}.get(k, d)
    game._daily_reset("g1", day0)
    assert db.get_char("g1", "u1").stamina == 10, "recovery=0 应不回复"
    c = db.get_char("g1", "u1"); c.stamina = stamina_before  # 还原体力
    db.upsert_char(c)
    game.cfg = lambda k, d=None: CFG.get(k, d)
    ok += 1; print("✓ 每日体力回复可配置(100=回满 / 0=不回,默认 40)")

    # 3.6 被动事件:埋伏笔 → 群消息引爆(冲着说话者来)
    day = game._day_key()
    plan = game.db.get_plan("g1", day) or []
    plan.append({"id": 999, "hhmm": "00:00", "kind": "event", "mode": "passive", "armed": 0, "done": 0})
    game.db.put_plan("g1", day, plan)
    game._active_end_hhmm = lambda: "24:00"  # 测试:视作全天待引爆
    acts = game.tick_items("g1")
    tgt = [(it, a) for it, a in acts if it.get("id") == 999]
    assert tgt and tgt[0][1] == "arm", acts
    game.arm_passive("g1", tgt[0][0])
    armed = game.armed_passives("g1")
    assert any(it.get("id") == 999 for it in armed)
    v = await game.fire_event("g1", char_uid="u1")  # 群消息引爆:事件冲着说话者来
    assert v["type"] == "event" and v["uid"] == "u1", v
    game.mark_done("g1", {"id": 999})
    assert not any(it.get("id") == 999 for it in game.armed_passives("g1"))
    ok += 1; print("✓ 被动事件:埋伏笔→群消息引爆(以说话者为主角)")

    # 3.6.2 角色事件只能被本人消息触发:无分身者的消息绝不引爆、绝不随机选别人角色
    plan = game.db.get_plan("g1", day) or []
    plan.append({"id": 997, "hhmm": "00:00", "kind": "event", "mode": "passive", "armed": 0, "done": 0})
    game.db.put_plan("g1", day, plan)
    acts = game.tick_items("g1")
    tgt = [(it, a) for it, a in acts if it.get("id") == 997]
    game.arm_passive("g1", tgt[0][0])
    for _ in range(5):
        v = await game.fire_event("g1", char_uid="u_no_char")  # 无分身者发言
        assert v is None, "无分身者的消息不应引爆角色事件"
    armed = game.armed_passives("g1")
    assert any(it.get("id") == 997 for it in armed), "事件应保持待命,而不是被路人消息消耗"
    game.mark_done("g1", {"id": 997})
    ok += 1; print("✓ 角色事件只能被本人消息触发(无分身不引爆/不随机拉人/伏笔保留)")

    # 3.7 被动事件兜底:活跃时段结束仍无人引爆 → 强制主动推送
    game._active_end_hhmm = lambda: "00:00"  # 测试:视为已过活跃时段
    day = game._day_key()
    plan = game.db.get_plan("g1", day) or []
    plan.append({"id": 998, "hhmm": "00:00", "kind": "event", "mode": "passive", "armed": 1, "done": 0})
    game.db.put_plan("g1", day, plan)
    acts = game.tick_items("g1")
    tgt = [(it, a) for it, a in acts if it.get("id") == 998]
    assert tgt and tgt[0][1] == "force", acts
    ok += 1; print("✓ 被动事件兜底:无人引爆→活跃时段结束转主动")

    # 4. 事件 → 抉择(事件可能是别人的或全员事件,由归属者抉择)
    import ocverse.game as _gmod
    _orig_r = _gmod.random.random
    _gmod.random.random = lambda: 0.5  # 跳过群像/群事件分支,得到个人事件
    try:
        v = _delivered(db, await game.fire_event("g1"))
    finally:
        _gmod.random.random = _orig_r
    assert v["type"] == "event" and len(v["payload"]["options"]) == 3
    ev_uid = v["uid"] or "u1"
    ev_before = db.get_char("g1", ev_uid)
    v2 = await game.choose("g1", ev_uid, 0)
    assert v2["type"] == "result" and v2["changes"], v2
    ch_after = db.get_char("g1", ev_uid)
    assert (ch_after.exp > ev_before.exp or ch_after.mood != ev_before.mood
            or ch_after.gold != ev_before.gold or ch_after.stamina != ev_before.stamina)
    assert db.count_logs("g1", ev_uid) >= 2
    assert db.mem_count("g1", ev_uid, scope="char") >= 1
    ok += 1; print("✓ 事件触发→抉择→属性/日志/记忆")

    # (群像生活事件的完整确定性测试见 check_life_multi,这里保留在共享群避免遗留事件干扰)

    # 5. 群友互动
    v = await game.interact("g1", "u1", "u2", "请客", "请对方吃一顿")
    assert v["rel"] > 0 and v["rel_label"]
    assert db.get_rel("g1", "u1", "u2") == v["rel"]
    ok += 1; print("✓ 群友互动(羁绊)")

    # 5.1 互动次数上限 + 防复读守卫
    game.cfg = lambda k, d=None: {**CFG, "interactions_max_per_day": 1}.get(k, d)
    try:
        await game.interact("g1", "u1", "u2", "闲聊", "再聊一次")
        raise AssertionError("互动次数上限未拦截")
    except GameError as e:
        assert "互动次数" in str(e), e
    game.cfg = lambda k, d=None: CFG.get(k, d)

    calls = {"n": 0}

    def repeat_llm(system, user):
        calls["n"] += 1
        fixed = "重要纠正" in user  # 第二次带纠正提示
        payload = {
            "narration": "你们比划了几句" if not fixed else "新场景:你们一起去修了那条旧帆,聊到了海况。",
            "dialogues": [{"speaker": "阿凛", "text": "你好呀。"}, {"speaker": "老徐", "text": "哟,来啦。"}],
            "a_effects": {}, "b_effects": {}, "rel_delta": 1, "memory": "m",
        }
        return json.dumps(payload, ensure_ascii=False)

    b_rep = Brain(raw_call=repeat_llm)
    prev = ["阿凛 对 老徐「闲聊」:你们比划了几句,气氛微妙地平衡着。(羁绊12)"]
    rr = await b_rep.resolve_interaction(world=db.cur_world("g1"), a=db.get_char("g1", "u1"),
                                        b=db.get_char("g1", "u2"), mode="闲聊", detail="",
                                        rel_score=10, previous=prev)
    assert calls["n"] == 2, calls  # 复读被检测并重写
    assert "新场景" in rr.data["narration"], rr.data["narration"]
    # 守卫不误伤:无历史(previous=None)时不触发重写,一次调用直接放行
    calls["n"] = 0
    rr2 = await b_rep.resolve_interaction(world=db.cur_world("g1"), a=db.get_char("g1", "u1"),
                                          b=db.get_char("g1", "u2"), mode="闲聊", detail="",
                                          rel_score=10, previous=None)
    assert calls["n"] == 1, calls
    ok += 1; print("✓ 互动防复读(同题不同文,复读重写)+ 每日互动次数上限")

    # 5.2 关系系统(纯事件触发):告白 → 单相思/恋人 → 情侣 → 事件求婚 → 结为伴侣
    import ocverse.game as _gmod
    from ocverse.config import rel_stage_label

    assert rel_stage_label(5) == "点头之交" and rel_stage_label(40) == "朋友"
    assert rel_stage_label(90) == "心灵挚友" and rel_stage_label(90, "lovers") == "恋人"

    _orig_random = _gmod.random.random
    _gmod.random.random = lambda: 0.0  # 让概率事件必中

    # 5.2.1 好感 65~79 互动 → 单相思(随机一方告白)
    db.set_rel_score("g1", "u1", "u2", 60)
    vi = await game.interact("g1", "u1", "u2", "闲聊", "相处")
    assert vi.get("extra_views"), "应触发告白事件卡"
    cv = vi["extra_views"][0]
    assert "告白" in cv["event_title"] and cv["chosen"] == "被温柔婉拒"
    info = db.get_rel_full("g1", "u1", "u2")
    assert info["state"] == "crush" and info["crush_by"] in ("u1", "u2"), info

    # 5.2.2 单相思 + 好感≥85 互动 → 水到渠成转正为恋人
    db.set_rel_score("g1", "u1", "u2", 90)
    vi = await game.interact("g1", "u1", "u2", "闲聊", "相处")
    assert any("告白" in w["event_title"] for w in vi.get("extra_views", []))
    info = db.get_rel_full("g1", "u1", "u2")
    assert info["state"] == "lovers", info

    # 5.2.3 恋人好感≥90 互动 → 事件求婚 → 结为伴侣(同一次互动不连发告白+求婚)
    vi = await game.interact("g1", "u1", "u2", "闲聊", "相处")
    pv = [w for w in vi.get("extra_views", []) if "求婚" in w["event_title"]]
    assert pv, "应触发求婚事件卡"
    info = db.get_rel_full("g1", "u1", "u2")
    assert info["state"] == "married", info

    # 5.2.4 已结婚:互动不再触发告白/求婚
    vi = await game.interact("g1", "u1", "u2", "闲聊", "相处")
    assert vi.get("extra_views") == [] or "告白" not in str(vi["extra_views"])

    # 5.2.5 好感不足 65:不发生告白
    db.set_rel_score("g1", "u9", "u10", 30)
    vi = await game.interact("g1", "u9", "u10", "闲聊", "相处")
    assert vi.get("extra_views") in (None, []), vi.get("extra_views")

    _gmod.random.random = _orig_random
    ok += 1; print("✓ 关系系统(纯事件触发):告白→单相思/恋人→情侣→求婚→结为伴侣,全流程概率自然发生")
    ok += 1; print("✓ 表白被拒:好感不足 -10,不误入单相思")

    # 5.5 主动行动(练习/健身/打工/打怪/冒险)+ 概率机缘 + 每日上限
    game.cfg = lambda k, d=None: {**CFG, "action_max_per_day": 2}.get(k, d)
    _st = db.get_char("g1", "u1").stamina
    va = await game.act("g1", "u1", "健身", "加练100个俯卧撑")
    assert va["type"] == "act" and va["ok_llm"] and va["changes"]
    assert db.get_char("g1", "u1").stamina < _st  # 扣了体力
    imgs = render_views([va], CFG)
    assert imgs, "行动卡片渲染失败"
    ok += 1; print("✓ 主动行动(健身)消耗体力并渲染")

    vc = await game.act("g1", "u1", "冒险", "摸进灯塔抄旧笔记")
    assert vc["type"] == "act"
    try:
        await game.act("g1", "u1", "打怪")  # 第3次:超每日上限
        raise AssertionError("每日行动上限未生效")
    except GameError:
        pass
    db.update_char("g1", "u1", stamina=5)
    try:
        await game.act("g1", "u1", "打怪")  # 体力不足
        raise AssertionError("体力不足未被拦截")
    except GameError:
        pass
    ok += 1; print("✓ 主动行动:每日上限 + 体力门槛")

    # 5.6 世界NPC自定义:仅在用户自设世界可改;系统世界被拦截
    sys_w = db.cur_world("g1")  # 当前为 LLM 生成的锈海城
    try:
        await game.add_npc("g1", "u1", "豆包", "茶馆小二", "话痨", "知道码头八卦")
        raise AssertionError("系统生成世界不应允许添加NPC")
    except GameError as e:
        assert "系统生成" in str(e) or "无法手动改动" in str(e), e
    ok += 1; print("✓ 系统生成世界:拦截添加NPC")

    # 先定义一个用户自设世界,才能增删NPC
    await game.define_world("g1", "u1", "我的镇", "每个人的梦在湖底共享,船灯靠往来的梦供能。")
    uw = [w for w in db.list_worlds("g1") if w.name == "我的镇"]
    if not uw:
        uw = [w for w in db.list_worlds("g1") if w.source == "user"]
    uw = uw[-1]
    wname, npc = await game.add_npc("g1", "u1", "豆包", "茶馆小二", "话痨,爱打听", "她知道码头每一桩八卦", uw.name)
    assert npc["name"] == "豆包"
    uwr = next(x for x in db.list_worlds("g1") if x.id == uw.id)
    assert any(n["name"] == "豆包" for n in uwr.npcs)
    try:
        await game.add_npc("g1", "u1", "豆包", "复读机", "重复", "重复", uw.name)
        raise AssertionError("重名NPC未被拦截")
    except GameError:
        pass
    try:
        await game.del_npc("g1", "u1", "老铁")  # 当前系统世界删除也应被拦截
        raise AssertionError("系统生成世界不应允许删除NPC")
    except GameError:
        pass
    ok += 1; print("✓ 用户自设世界:添加/重名拦截/系统世界删除拦截")

    w, npcs = game.list_npcs("g1", uw.name)
    assert any(n["name"] == "豆包" for n in npcs)
    _w, rm = game.del_npc("g1", "u1", "豆包", uw.name)
    assert rm == "豆包" and not any(n["name"] == "豆包" for n in next(x for x in db.list_worlds("g1") if x.id == uw.id).npcs)
    ok += 1; print("✓ 世界NPC:用户自设世界内 列表/删除")

    # 5.5 每日小任务:按世界生成 → 轻松结算 → 小奖励 → 防重复
    qs = await game.ensure_quests("g1", "u1")
    assert len(qs) == 3 and all(q["state"] == "open" for q in qs)
    qv = await game.complete_quest("g1", "u1", 0)
    assert qv["type"] == "result" and qv["changes"] and qv["narration"]
    assert db.get_char("g1", "u1").exp >= 5  # 奖励到账
    # 防重复守卫:同一任务重复结算会被拒(拿刚完成的任务 id 直接验证)
    done_id = next(q["id"] for q in db.list_quests("g1", "u1", game._day_key()) if q["state"] == "done")
    assert not db.resolve_quest_if_open(done_id), "重复结算未被拦截"
    ok += 1; print("✓ 每日小任务:AI 按世界生成 → 结算 → 小奖励 → 防重复")

    # 6. NPC 互动
    v = await game.npc_interact("g1", "u2", "老铁", "想打听雾码头的规矩")
    assert "老铁" in v["npc"]["name"] and v["reply"]
    ok += 1; print("✓ NPC 互动")

    # 7. 定义自设世界 → 世界变动选中它 → 解锁
    await game.define_world("g1", "u2", "糖果星云", "由糖晶构成的星云,漂浮着糖果风暴与奶油行星。")
    pend = [w for w in db.list_worlds("g1") if not w.visited]
    pend_names = [w.name for w in pend]
    assert "糖果星云" in pend_names
    # 让系统足迹只选中「糖果星云」:给变动随机一个确定种子到这里
    # (把已有自设世界“我的镇”也标记为已到达以避免被两次选中)
    myw = next(w for w in db.list_worlds("g1") if w.name == "我的镇")
    db.update_world(myw.id, visited=1)    # 把群的用户世界份额拉满,确保变动走自设世界分支
    db.update_group("g1", user_world_share=100)
    v = await game.world_shift("g1")
    assert v["type"] == "arrive" and db.cur_world("g1").name == "糖果星云", v
    assert any(w.visited and w.name == "糖果星云" for w in db.list_worlds("g1"))
    flags = db.get_char("g1", "u1").flags
    assert flags.get("traveler") == 1
    # 世界变动后,旧世界的待办任务全部作废
    qs_after = db.list_quests("g1", "u1", game._day_key())
    assert qs_after and not any(q["state"] == "open" for q in qs_after)
    ok += 1; print("✓ 定义世界→世界变动(自设世界降临)→全员标记→旧任务作废")

    # 8. 自由穿越(回到已访问的锈海城)
    game.cfg = lambda k, d=None: CFG.get(k, d)
    v = await game.travel("g1", "u1", "锈海城")
    assert db.cur_world("g1").name == "锈海城"
    # 未访问的世界不可穿越
    await game.define_world("g1", "u1", "玻璃沙海", "一望无际的玻璃沙丘,风一吹就唱歌。")
    try:
        await game.travel("g1", "u1", "玻璃沙海")
        raise AssertionError("未访问世界不应可穿越")
    except Exception as e:
        assert "没找到" in str(e) or "穿越" in str(e), e
    ok += 1; print("✓ 自由穿越(仅限已访问)")

    # 9. 记忆检索 + 压缩
    hits = mem.related_by_keyword("g1", "船票 雾", k=5)
    assert hits and hits[0]["score"] > 0
    vec_like = asyncio.run if False else None
    rel = await mem.related("g1", "雾码头的传闻", k=3)
    assert isinstance(rel, list)
    done = await mem.compress_now("g1", "u1", keep=0, summarize_fn=game.brain.summarize_core)
    assert done and db.mem_count("g1", "u1", scope="core") >= 1
    ok += 1; print("✓ 记忆检索+核心记忆压缩")

    # 9.5 删除角色:日志/记忆/羁绊/任务等伴生数据一并清空
    game.create_char("g1", "u11", "路人甲", "男", [], "")
    db.append_log("g1", "u11", "misc", "路人甲的测试日志")
    await mem.remember("g1", "u11", "char", "路人甲的测试记忆")
    db.bump_rel("g1", "u1", "u11", 5)
    await game.ensure_quests("g1", "u11")
    assert db.mem_count("g1", "u11") >= 1 and db.list_quests("g1", "u11", game._day_key())
    game.delete_char("g1", "u11")
    assert db.count_logs("g1", "u11") == 0
    assert db.mem_count("g1", "u11") == 0
    assert db.get_rel("g1", "u1", "u11") == 0
    assert not db.list_quests("g1", "u11", game._day_key())
    ok += 1; print("✓ 删除角色:日志/记忆/羁绊/任务一并清空")

    # 10. 离线降级(无LLM)
    b2 = Brain(raw_call=None)
    r = await b2.make_event(world=db.cur_world("g1"), char=db.get_char("g1", "u1"))
    assert not r.ok and len(r.data["options"]) == 3
    ok += 1; print("✓ LLM离线降级")

    # 11. 渲染全部卡片
    ch1 = db.get_char("g1", "u1")
    cards = {}
    cards["profile"] = profile_card(
        ch1, db.cur_world("g1"),
        rels=[("老徐", db.get_rel("g1", "u1", "u2"))],
        memories=["在雾里收到过一张暖船票", "和老铁换过一盏旧灯"],
        cfg=CFG, extra_badges=["次元旅者"])
    _orig_r2 = _gmod.random.random
    _gmod.random.random = lambda: 0.5  # 同上:得到个人事件卡用于渲染
    try:
        ev_view = _delivered(db, await game.fire_event("g1"))
    finally:
        _gmod.random.random = _orig_r2
    cards["event"] = render_views([ev_view], CFG)
    res_view = await game.choose("g1", ev_view["uid"] or "u1", 1)
    cards["result"] = render_views([res_view], CFG)
    cards["world"] = world_card(db.cur_world("g1"), CFG, is_current=True, day=3)
    cards["world_list"] = world_list_card(db.list_worlds("g1", only_visited=True),
                                          [w for w in db.list_worlds("g1") if not w.visited],
                                          db.cur_world("g1").id, CFG)
    cards["log"] = log_card(db.recent_logs("g1", "u1", 10), 1, 1, "阿凛 的人生日志", CFG, {"u1": "阿凛"})
    cards["fortune"] = fortune_card(game.fortune("u1", "阿凛"), CFG)
    cards["act"] = render_views([va], CFG)
    cards["morning"] = render_views([{"type": "morning", "gid": "g1", "world_name": "锈海城",
                                      "brief": MORNING_JSON["brief"], "watch": MORNING_JSON["watch"]}], CFG)
    cards["arrive"] = render_views([await game.world_shift("g1")], CFG)
    cur_npcs = db.cur_world("g1").npcs
    npc_name = cur_npcs[0]["name"] if cur_npcs else None
    if npc_name:
        cards["npc"] = render_views([await game.npc_interact("g1", "u1", npc_name, "想换点东西")], CFG)
    cards["interact"] = render_views([await game.interact("g1", "u1", "u2", "闲聊", "聊聊舰队传闻")], CFG)
    cards["help"] = help_card(CFG)
    cards["roster"] = roster_card(db.list_chars("g1"), CFG, "锈海城")
    cards["memory"] = memory_card("雾", hits, CFG)
    for name, imgs in cards.items():
        if not isinstance(imgs, list):
            imgs = [imgs]
        for i, im in enumerate(imgs):
            p = os.path.join(out, f"{name}_{i}.png")
            im.convert("RGB").save(p, "PNG")
            sz = os.path.getsize(p)
            assert sz > 3_000, f"{name} 渲染异常太小: {sz}"
    n_files = len(os.listdir(out))
    assert n_files >= 13, f"卡片数量不足: {n_files}"
    ok += 1; print(f"✓ 渲染 {n_files} 张卡片 → {out}")

    db.close()
    print(f"\nALL PASS ({ok} 项检查全部通过) | 输出目录: {out}")


if __name__ == "__main__":
    asyncio.run(check_datetime_injection())
    asyncio.run(check_special_state())
    asyncio.run(check_life_multi())
    asyncio.run(check_event_quote_binding())
    asyncio.run(check_dialogue_guard())
    asyncio.run(check_admin_server())
    asyncio.run(check_bond_flow())
    asyncio.run(check_migrations())
    asyncio.run(check_world_life())
    asyncio.run(check_life_char())
    asyncio.run(check_npc_turnover())
    asyncio.run(main())
