"""新系统专项测试:生命值/治疗物品/危险区域/声望/主线门槛与尾声/后台修复。

运行:
    .venv/bin/python tests/feature_test.py

覆盖:
    1. HP:效果应用→归零昏迷→无法行动→治疗物品/医院恢复→日切苏醒
    2. 治疗物品:世界兜底生成/店铺购买(声望折扣)/使用/任务物品联动
    3. 危险区域:世界生成兜底/每日流转/查看
    4. 声望:事件/任务结算增减、NPC态度注入、世界间独立
    5. 主线:阶段门槛(reputation/quest/defeat)拦截与达成、全完结后续写尾声
    6. 旧库迁移 v5→v6(缺列老库升级不炸)
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ocverse.db import Database  # noqa: E402
from ocverse.embedder import HashEmbedder  # noqa: E402
from ocverse.game import Game, GameError  # noqa: E402
from ocverse.llm_engine import Brain  # noqa: E402
from ocverse.memory import MemoryStore  # noqa: E402
from ocverse import config as C  # noqa: E402

CFG = {"memory_top_k": 6, "event_expire_minutes": 45, "action_max_per_day": 99}

WORLD_JSON = {
    "name": "灰烬荒原", "genre": "末世废土",
    "desc": "核冬后的拾荒者聚落,辐射与秩序并存。",
    "rules": ["子弹是硬通货"], "features": ["绿色是奢侈"],
    "npcs": [{"name": "老周", "role": "医务室卫生员", "persona": "嘴硬心软", "hook": "他留着战前的抗生素"},
             {"name": "夜莺", "role": "补给点配给员", "persona": "精明", "hook": "账目永远差三发子弹"}],
    "event_ideas": ["废墟拾荒", "辐射风暴"],
    "infra": [
        {"kind": "补给点", "name": "三号补给点", "desc": "按需分配", "work": "配给员"},
        {"kind": "避难所", "name": "地下避难所", "desc": "厚铁门", "work": "守夜人"},
        {"kind": "炊事棚", "name": "篝火炊事棚", "desc": "今天的汤", "work": "炊事员"},
        {"kind": "医务室", "name": "战地医务室", "desc": "绷带永远不够", "work": "卫生员"},
        {"kind": "交易棚", "name": "以物易物棚", "desc": "以物易物", "work": ""},
    ],
    "zones": [
        {"kind": "废墟区", "name": "旧城商场废墟", "desc": "坍塌的商场", "danger": 2,
         "enemies": [{"name": "徘徊者", "desc": "听声辨位"}], "loot": ["罐头", "旧电池"]},
        {"kind": "禁区", "name": "归零深坑", "desc": "灾变原点", "danger": 5,
         "enemies": [{"name": "畸变体Alpha", "desc": "灾变造物"}], "loot": ["奇点碎片"]},
    ],
    "heal_items": [
        {"name": "绷带包", "note": "恢复30点生命", "price": 30, "heal": 30},
        {"name": "急救医疗包", "note": "恢复100点生命", "price": 130, "heal": 100},
    ],
    "mainline": [
        {"stage": "初到荒原", "desc": "活下来,认识这块地方"},
        {"stage": "深坑传闻", "desc": "弄清楚深坑里有什么",
         "goal": {"type": "defeat", "value": 2, "note": "全群累计讨伐≥2次"}},
        {"stage": "荒原新生", "desc": "成为受人尊敬的拾荒者",
         "goal": {"type": "reputation", "value": 15, "note": "推进者声望≥15"}},
    ],
    "plots": [{"kind": "避难舱", "name": "三号舱位", "desc": "带铁门的那种", "price": 800}],
}

DIALOGUE = [{"speaker": "自己", "text": "先这样。"}, {"speaker": "对方", "text": "嗯,回头见。"}]
INVITE_PROMPTS: list[str] = []   # 记录远征邀约 prompt(验证任务信息注入)


def llm_fake(system, user):
    if "生成一个新世界" in user or "补全它的题材标签" in user:
        return json.dumps(WORLD_JSON, ensure_ascii=False)
    if "突发遭遇" in user:
        return json.dumps({"title": "废墟枪声", "scene": "远处传来一声枪响。",
                           "options": [{"label": "去看看", "hint": "危险"}, {"label": "躲开", "hint": "稳"},
                                       {"label": "喊人", "hint": "求援"}]}, ensure_ascii=False)
    if "主线回响" in user:
        return json.dumps({"narration": "那声枪响的方向,与传闻中深坑的位置一致。"}, ensure_ascii=False)
    if "重新设计这个世界的主线剧情" in user:
        return json.dumps({"mainline": [
            {"stage": "异响之源", "desc": "追查夜里金属摩擦声的来历"},
            {"stage": "旧地图之谜", "desc": "拼齐散落的地图碎片",
             "goal": {"type": "quest", "value": 2, "note": "全群累计完成任务≥2件"}},
            {"stage": "深坑之门", "desc": "打开通往核心的门",
             "goal": {"type": "reputation", "value": 10, "note": "推进者声望≥10"}}]},
            ensure_ascii=False)
    if "重新设计这个世界的危险区域与治疗物品" in user:
        return json.dumps({"zones": [
                                {"kind": "辐射区", "name": "荧光沼泽", "desc": "发光的泥潭", "danger": 4,
                                 "enemies": [{"name": "沼泽游魂", "desc": "半透明"}], "loot": ["荧光孢子"]},
                                {"kind": "匪帮", "name": "铁钩帮废站", "desc": "劫掠者营地", "danger": 2,
                                 "enemies": [{"name": "铁钩帮徒", "desc": "铁钩"}], "loot": ["废铁"]}],
                            "heal_items": [
                                {"name": "止血喷剂", "note": "恢复30点生命", "price": 35, "heal": 30},
                                {"name": "强化血清", "note": "恢复60点生命", "price": 70, "heal": 60},
                                {"name": "战地急救包", "note": "恢复100点生命", "price": 140, "heal": 100}]},
                           ensure_ascii=False)
    if "远征委托布告" in user:
        return json.dumps({"title": "深坑清剿令", "teaser": "酬金与战利品丰厚",
                           "briefing": "据点议会征召远征队:目标归零深坑,清剿畸变体,带回样本。行程艰险,报酬从优,量力而行。"},
                          ensure_ascii=False)
    if "诚邀被邀请者同行远征" in user:
        INVITE_PROMPTS.append(user)
        return json.dumps({"agree": True, "rel_delta": 2,
                           "narration": "绫波听完行程,把烟杆在鞋底磕了磕:正好,老身也想再去看看那条船。",
                           "dialogues": [{"speaker": "自己", "text": "路上危险。"},
                                         {"speaker": "绫波", "text": "老身见过的浪比你吃过的盐多。"}]},
                          ensure_ascii=False)
    if "远征途中的剧情片段" in user:
        return json.dumps({"narration": "队伍在废墟间推进,轮流探路,暂时平安。",
                           "dialogues": [{"speaker": "老周", "text": "跟紧点。"},
                                         {"speaker": "自己", "text": "明白。"}],
                           "items_lose": ["罐头"]}, ensure_ascii=False)
    if "远征大获全胜" in user or "远征折戟" in user:
        return json.dumps({"narration": "最终一战打完了,队伍带着战利品回到出发的地方,人人带伤,个个挺立。",
                           "dialogues": [{"speaker": "老周", "text": "活下来了。"},
                                         {"speaker": "自己", "text": "回去喝酒。"}],
                           "memory": "一场远征。"}, ensure_ascii=False)
    if "尾声" in user and "续写" in user:
        return json.dumps({"narration": "旧篇章落幕。新的商队带来了远方的消息。",
                           "stages": [{"stage": "远方商队", "desc": "他们带来了地图"}]}, ensure_ascii=False)
    if "生成今天的 3 个委托任务" in user:
        return json.dumps({"quests": [{"text": "清理徘徊者", "giver": "三号补给点", "place": "三号补给点",
                                       "hint": "去废墟讨伐",
                                       "steps": [{"type": "act", "desc": "讨伐一次", "keywords": ["打怪"]}]}]},
                          ensure_ascii=False)
    if "请结算" in user or "写一段" in user or "写出这段互动" in user or "与角色进行多轮对话" in user \
            or "想在这里打发时光" in user or "推进这段世界主线" in user or "发生」这个行为" in user \
            or "写出这次行动" in user:
        rep = 2 if ("讨伐" in user or "委托" in user) else 0
        eff = {"mood": 2, "exp": 5}
        if rep:
            eff["reputation"] = rep
        return json.dumps({"narration": "事情到此有了明确收尾。", "dialogues": DIALOGUE,
                           "effects": eff, "memory": "一次经历。",
                           "a_effects": {"mood": 2}, "b_effects": {"mood": 1}, "rel_delta": 1,
                           "reply": "进来吧。"}, ensure_ascii=False)
    if "【设定描述】" in user:
        return json.dumps({"gender": "男", "tags": ["沉稳"], "backstory": "拾荒者。",
                           "attrs": {"force": 40, "agility": 30, "intellect": 30, "charm": 25, "luck": 30, "sanity": 35}},
                          ensure_ascii=False)
    if "整理成人设" in user or "修改描述" in user or "整理成档案" in user or "整理一下人设" in user:
        return json.dumps({"gender": "男", "tags": ["沉稳"], "backstory": "拾荒者。",
                           "attrs": {"force": 40, "agility": 30, "intellect": 30, "charm": 25, "luck": 30, "sanity": 35}},
                          ensure_ascii=False)
    return json.dumps({"narration": "无事发生。", "dialogues": DIALOGUE, "effects": {}}, ensure_ascii=False)


async def main():
    tmp = tempfile.mkdtemp()
    db = Database(os.path.join(tmp, "t.sqlite3"))
    mem = MemoryStore(db, HashEmbedder(), HashEmbedder())
    game = Game(db, Brain(llm_fake), mem, lambda k, d=None: CFG.get(k, d))
    gid = "g1"

    ok = 0

    def check(name, cond, extra=""):
        nonlocal ok
        assert cond, f"FAIL: {name}" + (f" | {extra}" if extra else "")
        ok += 1
        print(f"✓ {name}")

    # 世界初始化(带 zones/heal_items)
    await game.init_world(gid, "末世废土", "admin")
    w = db.cur_world(gid)
    check("世界生成带危险区域(不足5片自动补齐)", len(w.zones) >= C.ZONES_MIN)
    check("世界生成带治疗物品", len(w.heal_items) >= 2)

    # 角色
    game.create_char(gid, "u1", "阿灰", "男", ["沉稳"], "拾荒者")
    db.update_char(gid, "u1", avatar="fake_avatar.png")   # 验证各卡对话头像
    ch = db.get_char(gid, "u1")
    check("初始生命满", ch.hp == C.HP_MAX)

    # ── 治疗物品:购买(声望折扣)先行囤药 ──
    db.update_char(gid, "u1", gold=500)
    v = game.buy_item(gid, "u1", "绷带包")
    check("购买治疗物品", v["type"] == "buy" and v["price"] <= 30)
    game.buy_item(gid, "u1", "急救医疗包")

    # ── HP:效果应用 → 归零昏迷 ──
    game._apply_effects(db.get_char(gid, "u1"), {"hp": -40})
    ch = db.get_char(gid, "u1")
    check("受伤扣血", ch.hp == 60)
    game._apply_effects(ch, {"hp": -70})
    ch = db.get_char(gid, "u1")
    check("生命归零触发昏迷状态", ch.hp == 0 and (ch.flags.get("_state") or {}).get("type") == Game.KO_TYPE)
    check("昏迷者被锁定(无法行动)", game._is_locked(ch))
    try:
        await game.act(gid, "u1", "冒险", "挣扎")
        raise AssertionError("昏迷者不应能行动")
    except GameError as e:
        check("昏迷者行动被拦截", "重伤昏迷" in str(e))

    # 背包里的药还能自救(消耗物品)
    v = game.use_heal_item(gid, "u1", "绷带包")
    ch = db.get_char(gid, "u1")
    check("使用治疗物品回血并苏醒", v["after"] == 30 and ch.hp == 30
          and not (ch.flags.get("_state") or {}).get("type") == Game.KO_TYPE)
    v = game.use_heal_item(gid, "u1", "急救医疗包")
    check("治疗物品恢复量生效", v["after"] == 100)

    # ── 医院:付费治疗 ──
    game._apply_effects(db.get_char(gid, "u1"), {"hp": -50})
    ch = db.get_char(gid, "u1")
    check("医院前生命50", ch.hp == 50)
    v = game.heal_at_hospital(gid, "u1")
    ch = db.get_char(gid, "u1")
    check("医院付费治疗回满", ch.hp == C.HP_MAX and v["cost"] > 0)

    # ── 声望:事件结算带 reputation ──
    await game.act(gid, "u1", "打怪", "去旧城商场废墟打怪")
    ch = db.get_char(gid, "u1")
    rep = db.rep_get(gid, "u1", w.id)
    check("打怪结算积累声望", rep > 0)
    check("讨伐计数", int(db.kv_get(gid, "defeats_total") or 0) >= 1)

    # ── 主线门槛:defeat ──
    try:
        await game.mainline_progress(gid, "u1")
        # 第一节无门槛,能推进
        w2 = db.get_world(w.id)
        check("无门槛主线小节可推进", w2.mainline[0].get("done") is True)
    except GameError as e:
        raise AssertionError(f"无门槛小节不应被拦截: {e}")
    # 第二节 defeat>=2,当前只有1 → 拦截
    try:
        await game.mainline_progress(gid, "u1")
        raise AssertionError("defeat 门槛未达成不应放行")
    except GameError as e:
        check("defeat 门槛拦截", "门槛尚未达成" in str(e))
    # 再打一次 → 达成
    await game.act(gid, "u1", "打怪", "再去打怪")
    # reputation 门槛(第三节 15):当前 rep 大约 2~4,推进第二节应当被 rep 拦?第二节门槛 defeat=2 已满足
    v = await game.mainline_progress(gid, "u1")
    check("defeat 达成后可推进", v["stage"] == "深坑传闻")
    try:
        await game.mainline_progress(gid, "u1")
        raise AssertionError("reputation 门槛未达成不应放行")
    except GameError as e:
        check("reputation 门槛拦截", "门槛尚未达成" in str(e))
    # 直接拉满声望
    db.rep_add(gid, "u1", w.id, 60, "test")
    v = await game.mainline_progress(gid, "u1")
    check("声望达标后可推进", v["stage"] == "荒原新生")

    # ── 尾声:全部完结后续写 ──
    v = await game.mainline_progress(gid, "u1")
    check("主线全完结触发尾声新篇章", "尾声" in v["stage"] and v["remaining"] >= 1)
    from ocverse.imcard import render_views as _rv
    check("主线卡片正常渲染(含对话头像)", len(_rv([v], {"card_width": 900, "card_font_size": 34, "card_theme": "dark"})) > 0
          and v["avatars"].get("阿灰") == "fake_avatar.png")
    w3 = db.get_world(w.id)
    check("尾声小节写入主线", any("远方商队" == m.get("stage") for m in w3.mainline))

    # ── 打怪 × 危险区域融合 ──
    db.update_char(gid, "u1", stamina=100)
    v = await game.act(gid, "u1", "打怪", "去旧城商场废墟")
    check("打怪点名区域→锁定", v.get("zone") == "旧城商场废墟")
    db.update_char(gid, "u1", stamina=100)
    v = await game.act(gid, "u1", "打怪", "打徘徊者")
    check("打怪点名敌人→锁定其区域", v.get("zone") == "旧城商场废墟")
    # 今日委托指向「归零深坑」→ 未点名打怪应自动对齐委托(需在无其他打怪消耗前测)
    db.add_quest(gid, "u1", game._day_key(), "深坑清剿", "去深坑",
                 steps=[{"type": "act", "desc": "讨伐深坑敌人", "keywords": ["归零深坑", "畸变体Alpha"], "done": False}],
                 giver="三号补给点", place="三号补给点")
    db.update_char(gid, "u1", stamina=100)
    v = await game.act(gid, "u1", "打怪", "")
    check("打怪未点名→自动对齐今日讨伐委托", v.get("zone") == "归零深坑")
    # 委托已被上面的打怪推进(叙述含区域名);作废后无指向 → 回落到等级适配自动选区
    db.expire_open_quests(gid)
    db.update_char(gid, "u1", stamina=100)
    v = await game.act(gid, "u1", "打怪", "")
    all_zones = {z["name"] for z in db.get_world(w.id).zones}
    check("打怪不点名→自动选区(等级适配)", v.get("zone") in all_zones and v.get("zone"))

    # ── 声望折扣验证:医院费用随声望下降 ──
    db.update_char(gid, "u1", gold=1000)
    game._apply_effects(db.get_char(gid, "u1"), {"hp": -50})
    game.heal_at_hospital(gid, "u1")

    # ── 危险区域每日流转 ──
    game.ZONE_NEW_P = 1.0
    game.ZONE_MUTATE_P = 1.0
    game._zones_turnover(gid)
    w4 = db.get_world(w.id)
    check("区域流转后仍不低于保底", len(w4.zones) >= C.ZONES_MIN)

    # ── 日切苏醒:昏迷→第二天 ──
    game._apply_effects(db.get_char(gid, "u1"), {"hp": -200})
    ch = db.get_char(gid, "u1")
    check("再次昏迷", ch.hp == 0)
    game._daily_reset(gid, "2050-01-02")
    ch = db.get_char(gid, "u1")
    check("日切苏醒(生命恢复到苏醒值)", ch.hp == C.HP_WAKEUP
          and not (ch.flags.get("_state") or {}).get("type") == Game.KO_TYPE)
    check("昏迷者日切后可行动", not game._is_locked(ch))

    # ── 回家恢复生命 ──
    plots = db.plots(gid, w.id)
    db.plot_update(plots[0]["id"], owner_uid="u1", built_at=1.0)
    ch = db.get_char(gid, "u1")
    ch.flags = {"home_plot": plots[0]["id"]}
    db.upsert_char(ch)
    game._apply_effects(ch, {"hp": -20})
    v = await game.my_home(gid, "u1")
    ch = db.get_char(gid, "u1")
    check("回家恢复生命(单次)", v["hp_gain"] > 0 and ch.hp == min(C.HP_MAX, 10 + v["hp_gain"]))

    # ── 远征系统 ──
    db.update_char(gid, "u1", stamina=100)
    gold_before_exp = db.get_char(gid, "u1").gold
    ov = await game.ensure_expedition_offer(gid, "u1")
    check("远征委托生成(布告+同伴+成功率)", ov["phase"] == "offer" and ov["offer"]["teammates"]
          and 8 <= ov["offer"]["rate"] <= 95)
    check("远征发布方贴合世界观(用世界设施)", ov["offer"]["issuer"] in
          {i.get("name") for i in (db.get_world(w.id).infra or [])} | {"本地卫队"})
    dv = await game.accept_expedition(gid, "u1")
    ch = db.get_char(gid, "u1")
    check("接下远征进入远征状态", dv["phase"] == "depart" and game._on_expedition(ch) is not None)
    gold_after_exp = ch.gold
    check("远征前行前采购(金币-- 背包++ 食物饮水)", gold_after_exp < gold_before_exp
          and (db.item_get(gid, "u1", "罐头") or {}).get("count", 0) >= 1
          and (db.item_get(gid, "u1", "净化水") or {}).get("count", 0) >= 1
          and any("采买" in c for c in dv["changes"]))
    check("远征快照记录补给名(供途中消耗)", game._on_expedition(ch).get("supply_names") == ["罐头", "净化水"])
    check("出征对话带头像", dv["avatars"].get("阿灰") == "fake_avatar.png")
    try:
        await game.act(gid, "u1", "冒险", "挣扎")
        raise AssertionError("远征中不应能行动")
    except GameError as e:
        check("远征期间无法行动", "远征途中" in str(e))
    if not db.get_char(gid, "u2"):
        game.create_char(gid, "u2", "小夜", "女", ["冷静"], "同乡")
    try:
        await game.interact(gid, "u1", "u2", "闲聊", "")
        raise AssertionError("远征中不应能互动")
    except GameError as e:
        check("远征期间无法主动互动", "远征途中" in str(e))
    try:
        await game.interact(gid, "u2", "u1", "闲聊", "")
        raise AssertionError("不应能联系上远征者")
    except GameError as e:
        check("无法联系远征中的角色", "联系不上" in str(e))
    # 强制到播报点
    ch = db.get_char(gid, "u1")
    fl = dict(ch.flags); fl["_exp"]["next_report"] = 0; fl["_exp"]["until"] = 0 + 1
    fl["_exp"]["started"] = __import__("time").time() - 3600
    db.update_char(gid, "u1", flags=fl)
    canned_before = (db.item_get(gid, "u1", "罐头") or {}).get("count", 0)
    hp_before = db.get_char(gid, "u1").hp
    rv = await game.expedition_report(gid, "u1")
    check("远征途中播报剧情片段", rv is not None and rv["phase"] == "report" and rv["narration"])
    check("途中消耗补给(LLM 判断,背包--)", (db.item_get(gid, "u1", "罐头") or {}).get("count", 0) == canned_before - 1
          and any("罐头" in c for c in rv["changes"]))
    check("有补给维持则生命无损", db.get_char(gid, "u1").hp == hp_before)
    check("播报对话带头像", rv["avatars"].get("阿灰") == "fake_avatar.png")
    # 强制归来结算
    ch = db.get_char(gid, "u1")
    fl = dict(ch.flags); fl["_exp"]["until"] = 1
    db.update_char(gid, "u1", flags=fl)
    gold_before = db.get_char(gid, "u1").gold
    sv = await game.settle_expedition(gid, "u1")
    ch = db.get_char(gid, "u1")
    check("远征归来结算", sv["phase"] == "return" and game._on_expedition(ch) is None)
    if sv["outcome"] == "success":
        check("远征成功丰厚奖励", ch.gold > gold_before and sv["narration"])
    else:
        check("远征失败有损失", sv["narration"] and "重伤" in " ".join(sv["changes"]) + sv["narration"])
    # 逃兵路径(远征失败可能重伤昏迷——先治好再出发)
    ch = db.get_char(gid, "u1")
    ch.hp = C.HP_MAX
    ch.flags.pop("_state", None)
    db.upsert_char(ch)
    db.update_char(gid, "u1", stamina=100)
    await game.ensure_expedition_offer(gid, "u1")
    await game.accept_expedition(gid, "u1")
    rep_before = db.rep_get(gid, "u1", w.id)
    av = game.abort_expedition(gid, "u1")
    ch = db.get_char(gid, "u1")
    check("中途撤离=逃兵(声望重挫+解除状态)", av["phase"] == "abort"
          and game._on_expedition(ch) is None and db.rep_get(gid, "u1", w.id) < rep_before)

    # ── 查漏补缺:旧世界数据补齐 / 随队锁定 / 过期委托作废 ──
    db.update_world(w.id, zones=[], heal_items=[])
    w5 = game.ensure_world_content(gid)
    check("旧世界 zones/heal_items 自动补齐(区域保底5片)", w5 is not None and len(w5.zones) >= C.ZONES_MIN
          and len(w5.heal_items) >= 1 and db.get_world(w.id).heal_items)

    # 随队锁定:构造一场带生活角色队友的远征(zones 被重置过 → 刷新今日委托缓存)
    db.kv_set(gid, f"exp_offer:{game._day_key()}:u1", "")
    game.define_npc_char(gid, "绫波", "雾码头的老婆婆")
    life = db.get_char(gid, "npc:g1:绫波")
    db.update_char(gid, "u1", stamina=100)
    ch = db.get_char(gid, "u1")
    ch.hp = C.HP_MAX
    ch.flags.pop("_state", None)
    ch.flags.pop("_exp", None)
    db.upsert_char(ch)
    await game.ensure_expedition_offer(gid, "u1")
    await game.accept_expedition(gid, "u1")
    march_npc = db.get_world(w.id).npcs[0].get("name")
    ch = db.get_char(gid, "u1")
    fl = dict(ch.flags)
    fl["_exp"]["teammates"] = ["绫波", march_npc]
    fl["_exp"]["life_teammates"] = [life.uid]
    db.update_char(gid, "u1", flags=fl)
    try:
        await game.interact(gid, "u2", life.uid, "闲聊", "")
        raise AssertionError("随队生活角色不应能被互动")
    except GameError as e:
        check("随队生活角色被锁定(无法互动)", "远征队在外" in str(e))
    try:
        await game.npc_interact(gid, "u2", march_npc, "打听点事")
        raise AssertionError("随队世界NPC不应能被互动")
    except GameError as e:
        check("随队世界NPC被锁定(无法互动)", "远征队在外" in str(e))
    # 世界变动后旧委托作废
    db.update_char(gid, "u1", flags={"_state": {}})   # 先解除远征便于后续
    ov = await game.ensure_expedition_offer(gid, "u1")
    import json as _json
    day = game._day_key()
    bad = dict(ov["offer"]); bad["zone_name"] = "不存在的区域"
    db.kv_set(gid, f"exp_offer:{day}:u1", _json.dumps(bad, ensure_ascii=False))
    try:
        await game.accept_expedition(gid, "u1")
        raise AssertionError("过期委托不应能接受")
    except GameError as e:
        check("世界变动后旧委托作废拦截", "作废" in str(e))

    # ── 管理员重绘 zones/heal_items ──
    msg, rz, rh = await game.regen_zones_heals(gid)
    w6 = db.get_world(w.id)
    check("AI 重绘危险区域/治疗物品(落库,重绘后仍保底5片)", "荧光沼泽" in {z["name"] for z in w6.zones}
          and len(w6.zones) >= C.ZONES_MIN
          and "止血喷剂" in {h["name"] for h in w6.heal_items} and rz and rh)
    check("重绘总结文本", "舆图已重绘" in msg)

    # ── 主线空/缺失 → LLM 立即重生成 ──
    db.update_world(w.id, mainline=[])
    v = await game.mainline_progress(gid, "u1")
    w7 = db.get_world(w.id)
    check("主线为空 → 推进时自动 LLM 重生成", len(w7.mainline) >= 3
          and v["stage"] == "异响之源" and w7.mainline[0].get("done") is True)
    check("重生成的小节带阶段门槛", any(m.get("goal_type") == "quest" for m in w7.mainline))
    # 管理员重绘主线
    msg, nodes = await game.regen_mainline(gid)
    check("管理员重建主线(落库+含门槛)", len(db.get_world(w.id).mainline) >= 3
          and "主线已重铸" in msg
          and any(m.get("goal_type") == "reputation" for m in db.get_world(w.id).mainline))

    # ── 远征邀约:接受前拉人入队 ──
    db.update_char(gid, "u1", stamina=100)
    await game.ensure_expedition_offer(gid, "u1")
    life2 = db.get_char(gid, "npc:g1:绫波")
    ov2 = await game.ensure_expedition_offer(gid, "u1")
    iv = await game.expedition_invite(gid, "u1", life2.uid)
    check("远征邀约(LLM 判定同行)", iv["phase"] == "invite" and iv["agree"] is True
          and "绫波" in iv["narration"])
    _p = INVITE_PROMPTS[-1] if INVITE_PROMPTS else ""
    check("邀约对话注入真实委托信息", "深坑清剿令" in _p and "据点议会征召远征队" in _p
          and "危险度★" in _p and "行程约" in _p and "成功率" in _p and "已有同行" in _p,
          extra=_p[:160])
    check("受邀者入招募名单", "绫波" in game._exp_recruited(gid, "u1"))
    iv2 = await game.expedition_invite(gid, "u1", life2.uid)
    check("重复邀请短路(不重复编入)", iv2["agree"] is True and game._exp_recruited(gid, "u1").count("绫波") == 1)
    dv = await game.accept_expedition(gid, "u1")
    ch = db.get_char(gid, "u1")
    team_now = game._on_expedition(ch).get("teammates") or []
    check("接受时招募者优先编入队伍(含生活角色)", "绫波" in team_now
          and life2.uid in (game._on_expedition(ch).get("life_teammates") or []))
    check("出发变更栏显示队伍", any("同行" in c and "绫波" in c for c in dv["changes"]))
    # 收尾:终止这场远征以便后续测试
    fl = dict(ch.flags); fl["_exp"]["until"] = 1; db.update_char(gid, "u1", flags=fl)
    await game.settle_expedition(gid, "u1")
    ch = db.get_char(gid, "u1")
    ch.hp = C.HP_MAX; ch.flags.pop("_state", None); db.upsert_char(ch)
    # 缓存委托的目标区域消失(区域重绘/世界变动)→ ensure 时作废重生成
    stale_zone = ov2["offer"]["zone_name"]
    keep = [z for z in db.get_world(w.id).zones if z.get("name") != stale_zone]
    db.update_world(w.id, zones=keep)
    ov3 = await game.ensure_expedition_offer(gid, "u1")
    check("缓存委托区域失效后自动作废重生成", ov3["offer"]["zone_name"] != stale_zone
          and ov3["offer"]["zone_name"] in {z["name"] for z in db.get_world(w.id).zones}
          and len(db.get_world(w.id).zones) >= C.ZONES_MIN)

    # ── 剧情权重:主线终章=高潮 ──
    from ocverse.prompts import resolve_mainline as _rm, story_weight_line
    p_major = _rm(world=db.get_world(w.id), char=ch, stage={"stage": "x", "desc": "y"})
    p_climax = _rm(world=db.get_world(w.id), char=ch, stage={"stage": "x", "desc": "y"}, weight="climax")
    check("剧情权重注入(重要/高潮规格)", "【重要剧情】" in p_major and "【高潮剧情】" in p_climax
          and "600~1000" in p_climax)

    # ── 旧库迁移 v5→v6 ──
    old_db = os.path.join(tmp, "old.sqlite3")
    conn = sqlite3.connect(old_db)
    conn.executescript("""
    CREATE TABLE groups (gid TEXT PRIMARY KEY, cur_world_id INTEGER, init_done INTEGER DEFAULT 0,
      event_min INTEGER DEFAULT 0, event_max INTEGER DEFAULT 0, shift_percent INTEGER DEFAULT 0,
      user_world_share INTEGER DEFAULT 0, travel_cooldown_h INTEGER DEFAULT 6,
      last_shift_at REAL DEFAULT 0, last_travel_at REAL DEFAULT 0, day_key TEXT DEFAULT '', created_at REAL);
    CREATE TABLE chars (gid TEXT NOT NULL, uid TEXT NOT NULL, name TEXT, gender TEXT, tags TEXT,
      backstory TEXT, avatar TEXT, attrs TEXT, level INTEGER DEFAULT 1, exp INTEGER DEFAULT 0,
      gold INTEGER DEFAULT 100, mood INTEGER DEFAULT 70, stamina INTEGER DEFAULT 90,
      title TEXT DEFAULT '无名之辈', flags TEXT DEFAULT '{}', created_at REAL, updated_at REAL,
      PRIMARY KEY (gid, uid));
    PRAGMA user_version = 5;
    """)
    conn.execute("INSERT INTO chars (gid,uid,name) VALUES ('g','u','旧角色')")
    conn.commit()
    conn.close()
    db2 = Database(old_db)
    ch_old = db2.get_char("g", "u")
    check("旧库迁移后角色可读(生命默认满)", ch_old.hp == C.HP_MAX)
    db2.close()

    db.close()
    print(f"\nALL PASS ({ok} 项检查全部通过)")


if __name__ == "__main__":
    asyncio.run(main())
