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
from ocverse.memory import MemoryStore  # noqa: E402

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
    "narration": "你在雾里撞上了一个支着旧灯的小摊。摊主看不清脸,只递来一枚还带温度的船票,雾便散了。",
    "effects": {"mood": 8, "exp": 14, "gold": 30, "attrs": {"luck": 2}},
    "memory": "在雾里收到过一张来路不明的暖船票。",
}

INTERACT_JSON = {
    "narration": "你们在齿轮区的面摊拼了桌,聊起雾码头的传闻。TA把最后一块炸鱼推给了你,自己嗦了口汤。",
    "a_effects": {"mood": 10, "gold": -25, "exp": 8},
    "b_effects": {"mood": 12},
    "rel_delta": 12,
    "memory": "和同伴在齿轮区面摊拼过桌。",
}

NPC_JSON = {
    "reply": "新来的?哈,我这儿的规矩——听故事,拿东西。你讲一个,我送你一盏旧灯。",
    "narration": "老铁擦了擦手上的铁屑,眯眼打量你。",
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
    "narration": "你咬着牙把这一套练完,酸痛里透着踏实。临下工,在旧排气管里摸出一枚发锈的齿轮币,权当彩头。",
    "effects": {"mood": 4, "exp": 12, "gold": 20, "attrs": {"force": 2}},
    "memory": "在齿轮区认真训练了一天,还顺手捞到一枚旧齿轮。",
}



def fake_llm(system: str, user: str) -> str:
    if "生成一个新世界" in user:
        return json.dumps(WORLD_JSON, ensure_ascii=False)
    if "生成一次突发遭遇" in user:
        return json.dumps(EVENT_JSON, ensure_ascii=False)
    if "请结算" in user:
        return json.dumps(RESOLVE_JSON, ensure_ascii=False)
    if "写出这段互动" in user:
        return json.dumps(INTERACT_JSON, ensure_ascii=False)
    if "以NPC的口吻" in user:
        return json.dumps(NPC_JSON, ensure_ascii=False)
    if "抵达播报" in user:
        return json.dumps(ARRIVE_JSON, ensure_ascii=False)
    if "晨报" in user:
        return json.dumps(MORNING_JSON, ensure_ascii=False)
    if "核心记忆" in user:
        return json.dumps({"cores": ["总在雾天收集奇怪的车票", "和老铁是换过故事的朋友"]}, ensure_ascii=False)
    if "执行行动" in user:
        return json.dumps(ACT_JSON, ensure_ascii=False)
    raise AssertionError("fake_llm 未覆盖的调用: " + user[:60])


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
    v = await game.fire_event("g1")
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

    # 5. 群友互动
    v = await game.interact("g1", "u1", "u2", "请客", "请对方吃一顿")
    assert v["rel"] > 0 and v["rel_label"]
    assert db.get_rel("g1", "u1", "u2") == v["rel"]
    ok += 1; print("✓ 群友互动(羁绊)")

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
        await game.act("g1", "u1", "打工")  # 体力不足
        raise AssertionError("体力不足未被拦截")
    except GameError:
        pass
    ok += 1; print("✓ 主动行动:每日上限 + 体力门槛")

    # 5.6 世界NPC自定义(添加/重名/列表/删除)
    wname, npc = await game.add_npc("g1", "u1", "豆包", "茶馆小二", "话痨,爱打听", "她知道码头每一桩八卦")
    assert npc["name"] == "豆包"
    cur = db.cur_world("g1")
    assert any(n["name"] == "豆包" for n in cur.npcs)
    try:
        await game.add_npc("g1", "u1", "豆包", "复读机", "重复", "重复")
        raise AssertionError("重名NPC未被拦截")
    except GameError:
        pass
    w, npcs = game.list_npcs("g1")
    assert any(n["name"] == "豆包" for n in npcs)
    _w, rm = game.del_npc("g1", "u1", "豆包")
    assert rm == "豆包" and not any(n["name"] == "豆包" for n in db.cur_world("g1").npcs)
    ok += 1; print("✓ 世界NPC:添加/重名拦截/列表/删除")

    # 6. NPC 互动
    v = await game.npc_interact("g1", "u2", "老铁", "想打听雾码头的规矩")
    assert "老铁" in v["npc"]["name"] and v["reply"]
    ok += 1; print("✓ NPC 互动")

    # 7. 定义自设世界 → 世界变动选中它 → 解锁
    await game.define_world("g1", "u2", "糖果星云", "由糖晶构成的星云,漂浮着糖果风暴与奶油行星。")
    pend = [w for w in db.list_worlds("g1") if not w.visited]
    assert len(pend) == 1 and pend[0].name == "糖果星云"
    # 把群的用户世界份额拉满,确保变动走自设世界分支
    db.update_group("g1", user_world_share=100)
    v = await game.world_shift("g1")
    assert v["type"] == "arrive" and db.cur_world("g1").name == "糖果星云", v
    assert any(w.visited and w.name == "糖果星云" for w in db.list_worlds("g1"))
    flags = db.get_char("g1", "u1").flags
    assert flags.get("traveler") == 1
    ok += 1; print("✓ 定义世界→世界变动(自设世界降临)→全员标记")

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
    ev_view = await game.fire_event("g1")
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
    asyncio.run(main())
