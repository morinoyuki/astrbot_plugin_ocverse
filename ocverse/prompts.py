"""统一 Prompt 中心:所有 LLM 生成/判断的提示词都收敛在这里。

设计:
- STYLE_BASE 定义全局叙事风格(所有生成共享);
- 各 prompt 构建函数接收已渲染好的素材(world 文本/角色一行话/关系分数等),
  返回完整的 user 提示词;llm_engine 只负责拼装、重试与解析,不再内嵌提示词。
- 文风铁律:平实具体、情绪克制、不谜语人、有始有终(不留下悬空尾巴)。
"""

from __future__ import annotations

from .config import story_spec, story_weight_line  # noqa: F401 (剧情权重规格)

# ═══════════════════════════ 全局风格 ═══════════════════════════
STYLE_BASE = (
    "你是一个群聊文字游戏的叙事引擎,以轻小说的手法叙事。叙事铁律:"
    "一、平实具体:直接交代发生了什么,画面感来自细节描写,不靠夸张渲染,不一惊一乍;"
    "二、情绪克制:通过具体的动作、对话与事实呈现喜怒哀乐,不用感叹号轰炸,"
    "不用『惊!!』『恐怖如斯』一类夸张形容;"
    "三、直白不谜语:角色想说就说清楚,话里有话也要让读者看得懂,禁止故弄玄虚、"
    "用半句没头没尾的话留悬念;"
    "四、有始有终:每个场景都必须有明确的结果与落点——成功、失败、进展、付出什么代价都要交代清楚,"
    "结尾干脆收束,不许留下悬而未决的空洞尾巴;"
    "五、对话生动口语化、节奏明快,文风克制不堆砌、不水字数;"
    "六、剧情向前推进:新剧情不要围绕同一件旧事(吃过的东西、买过的物品、上一场遭遇)反复展开——"
    "除非玩家主动提起,旧事件只能作为背景偶尔被顺带提及,绝不能成为新剧情的核心钩子或主要场景;"
    "严格贴合指令给定的行动/互动主题展开,不跑题、不擅改场景、不凭空插入无关事件或陌生人物;"
    "不提及任何现实平台或AI身份。"
    "所有输出必须是严格的 JSON,不要 markdown 代码围栏,不要解释。"
)

# ═══════════════════════════ 演出脚本排版(所有含对白的剧情共用)═══════════════════════════
# 让 LLM 把叙述段、短状态胶囊与对白气泡写成交错排布的演出脚本,
# 不再「上面一整段叙述、下面才排对话」的生硬拼接。
SCRIPT_SPEC = (
    "\n【演出脚本排版法——务必遵守】禁止把叙述与对话生硬地分成上下两大块(先大段叙述、再统一排对话)。"
    "请把这一幕写成一条『演出脚本』,让叙述文字、短状态胶囊、对白气泡按剧情节奏自由穿插(顺序与次数任意):\n"
    "・场景/动作/心理/过渡的叙述:直接写普通文字(一句一行或一段皆可,拆成多段夹在对话之间更好);\n"
    "・短动作/短神态/短心理(一句话,尽量≤16字):单独一行写成胶囊 <c>内容</c>;\n"
    "・对白:单独一行写成气泡 <d name=角色本名>台词</d>;若发言者是主角(本幕做行动、被结算视角的人),"
    "加主角标记 <d name=主角本名 me>台词</d>;\n"
    "胶囊与对白必须各自独占一行(可夹在叙述段之间,建议用空行隔开);对白角色名用在场人物本名,不写代号;\n"
    "name 属性不要加引号,直接写名字即可。\n"
    "示例(仅演示排版,内容勿照抄;其中的「老板/主角」只是举例,实际必须用在场角色本名):\n"
    "<c>她放下茶杯,指尖在桌沿敲了两下。</c>\n"
    "<d name=老板>这单生意,你真要接?</d>\n"
    "<d name=主角 me>总得有人去做。</d>\n"
    "雨下大了,街上很快没了人。\n"
    "对白也可用 av 属性借用某张现有头像: <d name=发言者 av=头像名字>台词</d>(av 值从下方『本群头像名单』里选,一般不需要写)。\n"
)

# ═══ 头像名单注入:让 LLM 知道本群有哪些可用头像,供 <d> 标签 av 属性借用 ═══
def avatar_note(names: list[str] | None) -> str:
    """可用头像名单注入块(游戏层按当前群组装;空则返回空串)。"""
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not names:
        return ""
    return (
        "\n【本群头像名单】以下角色配了头像,<d> 标签的 av 属性可从中借用(av 值必须原样用名单里的名字):"
        + "、".join(names[:12])
        + "。名单外或这场戏没出场的人不要写 av;对白默认按 name 自动匹配头像,av 只在特殊需求时用。"
    )

# 世界生成 JSON 结构模板(gen_world / enrich_user_world 共用)
WORLD_SCHEMA = (
    '{"name":不超过8字,"genre":题材标签,"atmosphere":氛围一句话,'
    '"desc":世界描述80-160字,"rules":["规则1","规则2","规则3"],'
    '"features":["独特之处1","独特之处2","独特之处3"],'
    '"npcs":[{"name":"","role":"身份","persona":"性格一句话","hook":"可交互的钩子一句话","daily":"这名NPC每天一般会去做什么/在哪里","quirk":"一个鲜活的小怪癖/口头禅"}],'
    '"event_ideas":["该世界独有事件灵感",4-6条],'
    '"infra":[{"kind":"设施类型(店/馆/铺/坊/堂/楼/坛/站/所/市集/乐园/酒吧/剧场/温泉/观景等,尽量不重复)","name":"设施名(2~6字)","desc":"功能/氛围一句话(≤20字)","work":"在这里能打工赚钱的职业(无则不填)"}],'
    '"zones":[{"kind":"区域类型(野外/森林/沼泽/山脉/地下城/敌对阵营领地/禁区/超古代遗迹等)","name":"区域名(2~6字)","desc":"一句话","danger":1~5的整数,"enemies":[{"name":"敌人/势力名","desc":"一句话"}],"loot":["击败敌人可获得的素材名",1~3个]}],'
    '"heal_items":[{"name":"治疗物品名(2~6字,贴合世界观)","note":"功效一句话","price":售价金币,"heal":恢复的生命数}],'
    '"mainline":[{"stage":"主线小节名(≤10字)","desc":"这一步要做什么/线索(≤30字)","goal":{"type":"reputation|quest|defeat|空","value":数字,"note":"目标一句话"}}],'
    '"plots":[{"kind":"房|宅|小屋|公寓|铺面|庄园…","name":"可购住处名(2~6字)","desc":"一句话","price":按物价设定的金币价,600~8000区间}]}'
)

# 属性行(写进 action/event 结算 prompt):"力量=force、敏捷=agility …"
def attr_names_line() -> str:
    from .config import ATTRS  # noqa: E402
    return "、".join(f"{k}={v}" for k, v in ATTRS)


def recent_events_block(previous: list[str] | None) -> str:
    """近期已发生事件一览:新遭遇必须明显不同,禁止绕着旧事打转。"""
    if not previous:
        return ""
    return ("\n近期已经发生过的事件(本次遭遇必须在场景、题材与冲突上与它们明显不同——"
            "不要围绕这些旧事展开,不要让它们再次成为剧情核心;它们只能作为过去式偶尔被顺带提及):\n"
            + "\n".join(f"- {t[:70]}" for t in previous[:5]))


def previous_block(previous: list[str] | None) -> str:
    """把最近同类互动的旧叙述拼进 prompt,要求这次明显不同。"""
    if not previous:
        return ""
    return ("\n此前同类互动的旧叙述(这次必须在场景、话题、对话上明显不同,禁止重复旧梗):\n"
            + "\n".join(f"- {t[:90]}" for t in previous[:3]))


def _world_line(world) -> str:
    return (
        f"当前世界:《{world.name}》[{world.genre}] {world.desc}\n"
        f"氛围:{world.atmosphere}\n世界规则:{';'.join(world.rules or [])}"
        + _zone_line(world)
    )


def _zone_line(world) -> str:
    """危险区域/敌对阵营一览(打怪、讨伐、素材、任务联动用)。"""
    zones = [z for z in (getattr(world, "zones", None) or []) if isinstance(z, dict) and z.get("name")]
    if not zones:
        return ""
    parts = []
    for z in zones[:6]:
        en = "、".join(e.get("name", "") for e in (z.get("enemies") or []) if isinstance(e, dict))
        parts.append(f"{z.get('name')}({z.get('kind', '')},危险度{z.get('danger', '?')},敌人:{en or '不明'},可掉落:{'、'.join((z.get('loot') or [])[:3]) or '无'})")
    return ("\n危险区域(打怪/讨伐/冒险类场景请把地点放在其中之一,并点出区域与敌人;"
            "击败敌人可掉落该区域素材):" + ";".join(parts))


def _heal_line(world, inventory_note: str = "") -> str:
    """治疗物品提示:世界通名 + (可选)角色本人背包里的治疗物品。"""
    items = [h for h in (getattr(world, "heal_items", None) or []) if isinstance(h, dict) and h.get("name")]
    line = ""
    if items:
        line = "\n本世界治疗物品通名(掉落/售卖/使用物品时必须用这些名字):" + \
               "、".join(f"{h['name']}({h.get('note', '')})" for h in items[:4])
    if inventory_note:
        line += "\n" + inventory_note
    return line


ITEM_OWNERSHIP_RULE = (
    "\n【物品归属】items_gain/items_lose 结算的都是『角色本人』的随身物品:"
    "消耗必须写明是从角色自己的背包/行囊里取出,严禁写成从其他角色/NPC的背包里拿出"
    "(他们的物品不归系统结算);队友/NPC 的物品只能在他们自己的台词与动作里出现,"
    "不得计入角色的得失。"
)


def _material_tail(user: str, material: str) -> str:
    """拼接知识库素材(风补点缀)。"""
    m = (material or "").strip()
    if not m:
        return user
    return user + "\n" + m + "\n"

# ═══════════════════════════ 世界生成 ═══════════════════════════
def gen_world(desc: str | None, avoid_names: list[str] | None, theme_hint: str) -> str:
    """为新世界生成完整世界设定的 user prompt。"""
    if desc:
        ref = desc
    else:
        theme = (theme_hint or "").strip()
        ref = f"自由发挥。整体风格要求:{theme}" if theme else "自由发挥,题材新颖,避开烂大街的西幻冒险开局。"
    user = "为群聊文字游戏生成一个新世界。世界观设定参考:" + ref
    if avoid_names:
        user += "\n不要与这些已有世界重名:" + "、".join(avoid_names[:20])
    user += (
        "\nNPC 8~14个(名字不超过6字,融入世界,不要套模板名;身份/职业尽量多样,"
        "覆盖衣食住行玩各方面)。每个NPC都要有自己的"
        "日常行踪(daily:TA每天一般在哪/做什么)、鲜活小怪癖或口头禅(quirk),"
        "让这个世界住着活生生的人。\n"
        "然后为这个世界设计以下内容(必须全部给出,分别填入 infra / zones / heal_items / mainline / plots 五个数组):\n"
        "- infra: 20~28个贴合该世界题材与时代的基础设施,种类尽量丰富不重复,"
        "必须覆盖生存必要(补给/住宿/餐饮/医疗/据点),还要有不少社交娱乐约会场所"
        "(茶馆酒馆/戏台剧场/温泉澡堂/公园花园/观景看台/夜市市集/约会胜地等,越丰富越热闹),"
        "其中至少 2 个能打工赚钱(填 work 职业,符合世界观);\n"
        "  可从这些类型里选或自创:商店/集市/饭馆/小吃摊/茶馆/酒馆/咖啡馆/旅店/澡堂/书店/当铺/"
        "  花店/药铺/诊所/铁匠铺/工坊/裁缝铺/戏院/道场/学园/祭坛/神社/码头/驿站/车马行/据点/地标等,\n"
        "  kind/name/desc/work 必填;\n"
        "- zones: 5~10 个危险区域/敌对阵营(如野外险地/森林/沼泽/地下城/敌对势力领地/"
        "远远超越当前文明的超古代遗迹等,贴合世界观),每个区域含 danger(1~5 危险度)、"
        "1~2 种 enemies(敌人/怪物/敌对势力成员)与 loot(击败后可掉落的素材);\n"
        "- heal_items: 3 种贴合世界观的治疗物品(如丹药/药剂/医疗包,由低到高三档),"
        "price 为售价金币、heal 为可恢复的生命数(30/60/100 左右递增);\n"
        "- mainline: 3~6 节世界主线(stage/desc),是一段能推动这个世界的故事;"
        "每节可带 goal(阶段性门槛,层层推进):type 只能是 reputation(推进者在本世界的声望≥value)"
        "|quest(全群累计完成任务数≥value)|defeat(全群累计讨伐数≥value),value 为数字;"
        "开篇的 1~2 节不要设 goal,中后期的 2~3 节设 goal,让世界走向由玩家的积累决定;\n"
        "- plots: 3~5 处可供居民购置的住处(kind/name/desc/price),price 用整数金币。\n"
        "全部要贴合该世界的题材与时代,不要套用同一套现代模板。\n"
        f"严格输出 JSON,结构:{WORLD_SCHEMA}"
    )
    return user


# gen_world 重试纠正(漏掉 infra/mainline/plots 时)
WORLD_CORRECT = (
    "\n\n【纠正】你漏掉了基础设施或主线,请补全:infra 至少 20 个可去的场所"
    "(种类尽量丰富:含生存必要与社交娱乐约会场所、至少2个能打工的 work),"
    "mainline 至少 3 节主线,plots 至少 3 处可购住处。不要省略这些数组。"
)


def regen_infra(world) -> str:
    """管理员重新规划世界基础设施的 user prompt。"""
    existing = "、".join(
        str(i.get("name", "")) for i in (world.infra or []) if isinstance(i, dict) and i.get("name"))
    return (
        f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
        f"氛围:{world.atmosphere}\n世界规则:{';'.join(world.rules or [])}\n"
        f"现有设施(重新规划,不要照抄这些名字):{existing or '无'}\n\n"
        "请以世界规划者的身份,重新设计这个世界的基础设施。\n"
        "【硬性要求】\n"
        "1. 20~28 个,种类丰富不重复,全部必须贴合该世界的题材、时代与科技水平;\n"
        "2. 必须覆盖生存必要类型:补给(店铺/集市)、住宿(旅馆/酒店)、餐饮(饭馆/食堂)、"
        "医疗(医院/诊所)、据点(聚居/集会之所),缺一不可;\n"
        "3. 还要包含不少社交/娱乐/约会场所(茶馆酒馆、戏台剧场、温泉澡堂、公园花园、"
        "观景看台、夜市市集、约会胜地等),让世界热闹有烟火气;\n"
        "4. 其中至少 2 个能打工赚钱(填 work 职业,职业符合世界观);\n"
        "5. 名字 2~6 字,融入世界观,不要套用现代模板。\n"
        '严格输出 JSON:{"infra":[{"kind":"类型","name":"设施名",'
        '"desc":"功能/氛围一句话(≤20字)","work":"打工职业(无则不填)"}]}'
    )


INFRA_CORRECT = (
    "\n\n【纠正】设施太少或不合规,请补全:至少 20 个、种类丰富,"
    "覆盖补给/住宿/餐饮/医疗/据点五类并含社交娱乐约会场所,至少 2 个可打工。"
)


def regen_zones_heals(world) -> str:
    """管理员重绘危险区域与治疗物品的 user prompt(贴合世界观,避开旧名)。"""
    zones = [z for z in (world.zones or []) if isinstance(z, dict) and z.get("name")]
    heals = [h for h in (world.heal_items or []) if isinstance(h, dict) and h.get("name")]
    zone_line = "、".join(str(z.get("name")) for z in zones) or "无"
    heal_line = "、".join(str(h.get("name")) for h in heals) or "无"
    return (
        f"世界:《{world.name}》[{world.genre}] {world.desc}\n"
        f"氛围:{world.atmosphere}\n世界规则:{';'.join(world.rules or [])}\n"
        f"现有危险区域(重新规划,不要照抄这些名字):{zone_line}\n"
        f"现有治疗物品(重新规划,不要照抄这些名字):{heal_line}\n\n"
        "请以造世者的身份,重新设计这个世界的危险区域与治疗物品。\n"
        "【硬性要求】\n"
        "1. zones:5~10 个危险区域/敌对阵营,贴合该世界的题材、时代与文明水平"
        "(如野外险地/森林/沼泽/地下城/敌对势力领地/远远超越当代文明的超古代遗迹等);"
        "每个区域含 danger(1~5 危险度)、1~2 种 enemies(敌人/怪物/敌对势力成员,贴合世界观)"
        "与 1~3 种 loot(击败后可掉落的素材);\n"
        "2. heal_items:3 种贴合世界观的治疗物品,由低到高三档(如丹药/药剂/医疗包),"
        "price 为售价金币、heal 为可恢复的生命数(30/60/100 左右递增);\n"
        "3. 名字 2~6 字,融入世界观,不要套用模板。\n"
        '严格输出 JSON:{"zones":[{"kind":"类型","name":"区域名","desc":"一句话","danger":1~5,'
        '"enemies":[{"name":"敌人名","desc":"一句话"}],"loot":["素材1","素材2"]}],'
        '"heal_items":[{"name":"物品名","note":"功效一句话","price":售价,"heal":恢复量}]}'
    )


def regen_mainline(world) -> str:
    """管理员/自动补全:重新生成世界主线的 user prompt(贴合世界观,避开旧名)。"""
    nodes = [m for m in (world.mainline or []) if isinstance(m, dict) and m.get("stage")]
    old_line = "、".join(str(m.get("stage")) for m in nodes) or "无"
    return (
        f"{_world_line(world)}\n"
        f"现有主线小节(重新规划,不要照抄这些名字):{old_line}\n\n"
        "请以造世者的身份,重新设计这个世界的主线剧情:3~6 节(stage≤10字、desc≤30字),"
        "是一段能推动这个世界的故事,层层推进、可玩几个月;\n"
        "每节可带 goal(阶段门槛):type 只能是 reputation(推进者在本世界的声望≥value)"
        "|quest(全群累计完成任务数≥value)|defeat(全群累计讨伐数≥value),value 为数字;"
        "开篇的 1~2 节不要设 goal,中后期的 2~3 节设 goal,让世界走向由玩家的积累决定;\n"
        "全部贴合该世界的题材、时代与文明水平,不要套用模板。\n"
        '严格输出 JSON:{"mainline":[{"stage":"","desc":"",'
        '"goal":{"type":"reputation|quest|defeat|空","value":数字,"note":"目标一句话"}}]}'
    )


MAINLINE_CORRECT = (
    "\n\n【纠正】主线小节缺失/不足,请补全:至少 3 节(stage/desc 必填),"
    "中后期的小节要带 goal 门槛(reputation/quest/defeat 之一)。"
)


ZONES_HEALS_CORRECT = (
    "\n\n【纠正】危险区域或治疗物品缺失/不合规,请补全:"
    "zones 至少 5 片(各含 danger/enemies/loot),heal_items 恰好 3 档(低中高)。"
)


def enrich_user_world(name: str, desc: str) -> str:
    """玩家自设世界落地补全的 user prompt。"""
    return (
        f"玩家自设了一个世界。名称:{name}\n描述:{desc}\n"
        "请补全它的题材标签、氛围、规则、独特之处、6~10个NPC(身份多样,各有日常行踪与小怪癖)、"
        "独有事件灵感。并设计 20~28 个种类丰富的贴合设定的基础设施(含社交娱乐约会场所、至少2个能打工)、"
        "一段世界主线、可供购置的住处——贴合该世界设定,勿套模板。\n"
        f"严格输出 JSON,结构:{WORLD_SCHEMA}"
    )


# ═══════════════════════════ 事件生成/结算 ═══════════════════════════
def make_event(*, world, char=None, npc=None, memories: list[str] | None = None,
               ideas: list[str] | None = None, state_note: str = "",
               previous: list[str] | None = None) -> str:
    """生成一次突发遭遇的 user prompt。char=None 时为全员群事件。"""
    role = (
        "这是全员都会被卷入的群事件,主角是『群里的众人』。"
        if char is None
        else f"主角是 {char.persona_line()},背景:{char.backstory[:600] or '未详'}。"
             f"当前生命{char.hp}/100、体力{char.stamina}/心情{char.mood}/金币{char.gold}。"
    )
    if state_note:
        role += (
            f"\n【处境】该角色此刻正被「{state_note}」缠身,处于无法自由行动的特殊状态。"
            "本次遭遇应围绕TA的处境展开,抉择要给出一线脱困/化解的生机(可以成功脱困,也可以失败吃鳖或半困半脱)。"
        )
    npc_line = ""
    if npc:
        npc_line = f"\n事件需围绕NPC「{npc['name']}」({npc.get('role','')},{npc.get('persona','')})展开。"
    mem = ("\n".join(memories[:6]) if memories else "")
    idea = ""
    if ideas:
        idea = "\n可参考该世界的事件灵感(选或化用,不要照抄原文):" + " | ".join(ideas[:6])
    return (
        f"{_world_line(world)}\n"
        f"{role}{npc_line}{idea}\n"
        f"{mem}\n"
        + recent_events_block(previous) + "\n"
        "生成一次突发遭遇:结合世界设定与角色性格/状态,事件要具体、有钩子、能做出选择。\n"
        "大部分事件需要玩家抉择,生成 3 个选项:\n"
        '严格输出 JSON:{"title":"标题≤10字","scene":"场景描述70-120字",'
        '"options":[{"label":"选项≤8字","hint":"后果暗示≤14字"},{"label":"","hint":""},{"label":"","hint":""}]}'
        "\n恰好3个选项,风格各异(稳健/冒险/离谱皆可)。\n"
        "\n也可生成『即时生效』的通知事件(约占四分之一,适合:世界异象/天降馈赠/公众传闻/季节变化/"
        "NPC 的悄悄话/天气骤变等无需群友做主的状况):这种事件不设选项、直接定局,消息只是通知大家发生了什么。\n"
        '即时事件严格输出 JSON:{"title":"标题≤10字","scene":"场景描述40-80字","instant":true,'
        '"narration":"演出脚本(短:80~160字,叙述+胶囊+对白自由穿插,已发生或正在发生)",'
        '"effects":{"mood":±5~15,"stamina":±,"gold":±,"exp":0-20,"reputation":-10~10},'
        '"memory":"一句话存档"}。'
        "\n注意:即时事件 effects 数值克制(多数±5~15),即时事件不伤害到重伤/毁灭性程度。"
    )


def make_life_event(*, world, chars, rels: str = "",
                    memories: list[str] | None = None,
                    previous: list[str] | None = None) -> str:
    """群像生活事件(2~N 名玩家角色偶遇/结伴)的 user prompt。"""
    cast = []
    for i, c in enumerate(chars, 1):
        cast.append(
            f"角色{i}:{c.persona_line()},背景:{c.backstory[:600] or '未详'},"
            f"当前体力{c.stamina}/心情{c.mood}/金币{c.gold}"
        )
    cast_line = "\n".join(cast)
    mem = ("\n".join(memories[:5]) if memories else "")
    rel_line = ("\n" + rels) if rels else ""
    return (
        f"{_world_line(world)}\n"
        f"{cast_line}{rel_line}\n{mem}\n"
        + recent_events_block(previous) + "\n"
        "这群人是生活在同一个世界的真实人物。请让其中几人产生交集,生成一幕自然的生活日常："
        "偶遇寒暄、结伴逛街/吃饭、一起去某个地方、碰上同一个小麻烦、或某个人的心事被旁人撞见。"
        "要有画面感与生活气息,不搞超展开;事件要具体、能做出选择。\n"
        '严格输出 JSON:{"title":"标题≤12字","scene":"场景描述70-120字,至少点出2名上述角色",'
        '"options":[{"label":"选项≤10字","hint":"后果暗示≤16字"},{"label":"","hint":""},{"label":"","hint":""}]}'
        "\n恰好3个选项,各角色可能会有不同想法(稳健/随性/热心/各走各的皆可)。"
    )


def resolve_event(*, world, char=None, event: dict, choice_idx: int,
                  state_note: str = "", previous: list[str] | None = None,
                  heal_note: str = "", custom_action: str = "") -> str:
    """结算一次选择的 user prompt(含连贯性铁律与新文风要求)。
    custom_action: 选 4 自定义行动(≤30字),LLM 据此展开但保留随机性。"""
    who = char.persona_line() if char else "群里的众人"
    hp_line = (f"当前生命{char.hp}/100。" if char else "")
    opts = event.get("options") or []
    custom = (custom_action or "").strip()
    if custom:
        chosen_line = (
            f"TA没有按预设选项走,而是采取了自定义行动:「{custom}」(≤30字,一句动作)。\n"
            "【自定义行动纪律】玩家这句行动只是动作起点:LLM 负责续写出事件真正的展开——"
            "对方怎么反应、过程中出现什么意外、结果落在哪,全部由世界/命运决定,"
            "必须保留随机性与不可预知性。严禁把玩家没写的内容脑补成既定结局,"
            "也绝不允许玩家那句行动之外的意图决定一切;若自定义行动超出常理(如凭空获得宝物/"
            "直接杀死主角/操控 NPC 意志),可给出被现实打脸、或代价沉重的展开。"
        )
    else:
        pick = opts[choice_idx] if 0 <= choice_idx < len(opts) else {"label": "顺其自然", "hint": ""}
        chosen_line = f"TA选择了「{pick['label']}」({pick.get('hint','')})。"
    user = (
        f"世界:《{world.name}》。{who}遭遇了:「{event.get('title')}」——{event.get('scene')}\n"
        f"{chosen_line}{hp_line}\n"
        "【连贯性铁律】结算必须紧接上面这场遭遇续写:同一时间、同一地点、同一批在场人物,"
        "从做出选择之后的下一秒写起。严禁跳跃到新的时间/地点,严禁引入遭遇场景里没有的新人物;"
        "dialogues 的 speaker 必须使用遭遇中出现过的角色本名(禁止「少女」「神秘人」之类代称)。\n"
        "请结算:结果按『演出脚本』写成(轻小说式,合计120~220字:画面感+心理细节+结果交代清楚"
        "——这次选择带来了什么、落点在哪里都写明白,不许含糊收尾),并给出数值变化。"
        "属性键:" + attr_names_line() + "。\n"
        + SCRIPT_SPEC +
        "事件中至少 2 个不同说话人要开口回应(禁止独角戏),对白角色用遭遇中的本名。\n"
        "『dialogues』字段只是兜底:对白已写进脚本的,填空数组 [] 即可。\n"
        '严格输出 JSON:{"narration":"演出脚本","dialogues":[{"speaker":"","text":""}],"effects":{"stamina":±,"mood":±,"gold":±,"exp":0-25,'
        '"attrs":{"force":0},"reputation":-10~10}, "memory":"第三人称一句话记忆存档", "state":{"type":"囚禁|束缚|被困...","reason":"一句原因"}, "state_lift":true, '
        '"items_gain":[{"name":"获得物品≤12字","note":"来历≤20字"}],"items_lose":["失去/消耗的物品名"]}\n'
        "effects.reputation:此事传开后角色在本世界的声望变化(-10~10):善行/英勇/慷慨为正,恶行/怯懦/背信为负;寻常小事可不输出。\n"
        "effects.hp:生命值变化(-100~30):遭遇危险/受伤/激战为负,休息疗养/服用治疗物品为正;"
        "生命归零会重伤昏迷(小危险-10~-30,大危险-30~-60,不要轻易打到归零)。\n"
        "items_gain/items_lose 只在剧情自然涉及物品得失时输出(拾获/受赠/被拢/消耗/击败敌人拾获战利品),"
        "通常为空数组;物品名要贴合世界观(治疗物品用世界通名)。\n"
        "若角色受伤且背包有治疗物品,可让TA在剧情中自然使用(items_lose 该物品 + effects.hp 正值)。\n"
        + ITEM_OWNERSHIP_RULE + "\n"
        "数值克制:大部分±5~15,exp 5~20;负反馈不要毁灭性。memory 一句话,30字内。"
        "state 与 state_lift 只在处境发生变化时才输出(见上),否则两字段都不要出现。"
    )
    if state_note:
        user += (
            f"\n该角色正被「{state_note}」困住(无法自由行动)。本次抉择结果要明确交代处境:若这次成功挣脱,"
            "则输出 state_lift:true;若这次反而更被束缚或换一种束缚,则输出 state:{...}(type/reason自定);若只是推进未有果,则两者都不输出。"
        )
    user += _heal_line(world, heal_note)
    user += previous_block(previous)
    return user


def resolve_life_event(*, world, chars, event: dict, choice_idx: int,
                       rels: str = "", custom_action: str = "") -> str:
    """结算群像生活事件的 user prompt。custom_action: 选 4 自定义行动。"""
    opts = event.get("options") or []
    custom = (custom_action or "").strip()
    if custom:
        chosen_line = (
            f"大家没有按预设选项走,而是采取了自定义行动:「{custom}」(一句动作)。\n"
            "【自定义行动纪律】玩家这句行动只是动作起点:由世界/命运决定真实展开——"
            "各角色反应、意外与结果都要保留随机性,不得让玩家一句行动直接预定结局;"
            "若行动超常理(凭空获宝/操控他人等),可给出现实打脸或代价沉重的展开。"
        )
    else:
        pick = opts[choice_idx] if 0 <= choice_idx < len(opts) else {"label": "顺其自然", "hint": ""}
        chosen_line = f"大家共同选择了「{pick['label']}」({pick.get('hint','')})。"
    cast = "\n".join(
        f"角色{i}:{c.persona_line()},背景:{c.backstory[:600] or '未详'},体力{c.stamina}/心情{c.mood}/金币{c.gold}"
        for i, c in enumerate(chars, 1)
    )
    rel_line = ("\n" + rels) if rels else ""
    return (
        f"世界:《{world.name}》[{world.genre}] {world.desc}\n{cast}{rel_line}\n"
        f"他们正在这场交集里:「{event.get('title')}」——{event.get('scene')}\n"
        f"{chosen_line}\n"
        "【连贯性铁律】结算必须紧接这场交集续写:同一时间、同一地点、同一批在场角色,"
        "严禁跳到新的时间/地点或引入场景里没有的新人物;dialogues 的 speaker 用角色本名。\n"
        "请结算这段共同经历的结果:结果按『演出脚本』写成(轻小说式,合计110~200字:画面感+心理细节+结果交代清楚),"
        "并分别给出各角色的效果与彼此羁绊的变化。\n"
        + SCRIPT_SPEC +
        "这场交集至少 2 个不同说话人开口;『dialogues』只是兜底,对白已入脚本则填 []。\n"
        '"effects":每个参与角色的效果(体力/心情/金币/exp/attrs,克制:大部分±3~10),可逐个给不同角色不同起伏。\n'
        '"rel_delta":-10~15 整数(本次交集对彼此关系的整体影响)。\n'
        "memory:一句话存档(涉及哪几个人、发生了什么)。\n"
        '严格输出 JSON:{"narration":"演出脚本","dialogues":[{"speaker":"","text":""}],'
        '"effects":{[角色名或编号]: {"mood":±,"gold":±,"exp":0-12,"stamina":±,"attrs":{}}}, "rel_delta":0, "memory":"一句话"}'
        "(effects 的键用角色名即可,尽量给每人一个条目;数值克制,日常生活不必大起大落)"
    )


# ═══════════════════════════ 角色互动 ═══════════════════════════
def resolve_interaction(*, world, a, b=None, npc=None, mode: str, detail: str,
                        rel_score: int, rel_stage: str = "",
                        state_note: str = "", previous: list[str] | None = None,
                        rep_note: str = "") -> str:
    """A 与 B(玩家分身/NPC)一段互动的 user prompt(含切题铁律)。"""
    b_ps = ""
    if b:
        b_ps = (f"\nB资料:{b.persona_line()},背景:{b.backstory[:600] or '未详'},"
                f"体力{b.stamina}/心情{b.mood}")
    if npc:
        b_ps = f"\nB资料:{npc.get('name','?')}({npc.get('role','')}),{npc.get('persona','')}"
    rel_line = (f"两人当前关系:{rel_score}({rel_stage or ''})。" if not npc
                else "对方是本世界的NPC。")
    rep_line = (f"\n【声望】{rep_note}" if rep_note else "")
    state_line = ""
    if state_note:
        state_line = (
            f"\n【救援】B正被『{state_note}』困住,无法自由行动。A这次是来帮忙/营救/搭救B的。"
            "请在叙述里交代营救的经过与结果:若这次成功把B救出来(挣脱束缚),输出 state_lift:true;"
            "若救不动或反被卷入(换一种困局),输出 state:{...}(type/reason自定);若只是打照面没能救出,则两字段都不输出。"
        )
    return (
        f"{_world_line(world)}\n"
        f"A:{a.persona_line()},背景:{a.backstory[:600] or '未详'},体力{a.stamina}/心情{a.mood}/金币{a.gold}"
        f"{b_ps}\n{rel_line}{rep_line}\n"
        f"互动:「{mode}」" + (f"({detail[:60]})" if detail else "") + state_line + "\n"
        f"【切题铁律】这段叙述演的必须就是上面这场「{mode}」互动"
        + (f"({detail[:60]})" if detail else "") + "。"
        "A与B是仅有的主角,写两人相处的过程与氛围(如约会就是两人约会:地点/话题/氛围/情感流动);"
        "严禁跑题:不得凭空插入与互动无关的遭遇/战斗/陌生人物/超展开;"
        "知识库素材只作风味点缀,不得改变本次互动的主题、场景与人物关系。\n"
        "dialogues 的 speaker 必须用 A/B 的本名,禁止「少女」「神秘人」之类代称。\n"
        "写出这段互动的走向与结果:结果按『演出脚本』写成(轻小说式,合计120~220字:画面感+心理细节+结果交代清楚"
        "——聊了/做了/关系有何变化都明白写出来)。\n"
        + SCRIPT_SPEC +
        "A 与 B 都必须开口(禁止独角戏),对白用双方本名、口语化,可含自然的句间小注,要能看出性格碰撞;"
        "『dialogues』只是兜底,对白已入脚本则填 []。\n"
        "若是消费类互动(请客/送礼),务必扣 A 的金币并给 B 心情。\n"
        '严格输出 JSON:{"narration":"演出脚本","a_effects":{"mood":±,"gold":±,"exp":0-10,'
        '"stamina":±,"hp":±,"attrs":{}}, "b_effects":{"mood":±,"gold":±},'
        ' "rel_delta":-20~20整数, "reputation":-5~5整数, "memory":"一句话存档", "state":{...}, "state_lift":true}'
        "(state/state_lift 仅在救援场景、且B的处境发生变化时按上面的规则输出,否则不要出现)"
        "(reputation:A 在本世界的声望微调,仅在互动明显体现品性/义举/恶行时输出,一般可不输出)"
        "(hp:仅在互动中受伤/疗愈时输出,一般不要输出)"
    ) + previous_block(previous)


def propose_bond(*, world, a, b, label: str, rel_score: int, rel_stage: str = "") -> str:
    """自定义关系提案(A 想成为 B 的 label)的 user prompt。"""
    return (
        f"{_world_line(world)}\n"
        f"A(提案人):{a.persona_line()},背景:{a.backstory[:400] or '未详'}\n"
        f"B(被提案):{b.persona_line()},背景:{b.backstory[:400] or '未详'}\n"
        f"两人当前关系:{rel_score}分({rel_stage})\n"
        f"A 想成为 B 的「{label}」。\n"
        "【判定要求】以 B 的视角与性格判断是否接受(agree true/false),并写出这段提案交锋的场景。"
        "贴合两人亲疏:关系好或提案够好笑可以爽快答应,关系差或提案离谱就嫌弃地拒绝,理由要符合人设。"
        "这是搞怪/生活向关系,严禁发展出恋人/情侣/夫妻等亲密内容。\n"
        "【切题铁律】只写这场提案交锋,A与B是仅有主角;"
        "严禁凭空插入无关事件/战斗/陌生人物;知识库素材只作风味点缀。\n"
        "dialogues 的 speaker 必须用 A/B 的本名,禁止代称。\n"
        "请输出提案经过与结果(120~200字:画面感+心理细节+结果交代清楚——成没成、B怎么想都写明)。\n"
        '严格输出 JSON:{"agree":true,"narration":"经过与结果",'
        '"dialogues":[{"speaker":"A名","text":""},{"speaker":"B名","text":""}],'
        '"effects":{"mood":±,"exp":±},"memory":"第三人称一句话存档"}'
        "dialogues 2~4轮;effects 数值克制(±3~10,只给 mood/exp)。"
    )


def confess(*, world, a, b, score: int, outcome: str) -> str:
    """告白场景 prompt(重要剧情权重)。outcome: success(答应) | crush(婉拒留悬念) | reject(明确拒绝)。"""
    outcome_line = {
        "success": "告白成功,两人正式确立恋人关系(双向奔赴或水到渠成,写出动情与确定的一刻)",
        "crush": "告白被温柔地婉拒,但对方心动未泯、留下悬念(单相思的开始,克制而不绝情)",
        "reject": "告白被明确拒绝(写出局促、尴尬与体面收场,不要狗血)",
    }.get(outcome, "告白场景")
    return (
        f"{_world_line(world)}\n"
        f"告白者:{a.persona_line()},背景:{a.backstory[:600] or '未详'}\n"
        f"被告白者:{b.persona_line()},背景:{b.backstory[:600] or '未详'}\n"
        f"两人当前好感:{score}。\n"
        f"本次走向:{outcome_line}。\n"
        "写一段告白场景:叙述(300~600字,轻小说式:铺垫回忆→说出口→等待→回应→关系落点)"
        "+多轮对话(6~10轮,IM聊天体,"
        '每条"speaker"≤8字、"text"≤60字)。'
        "禁止独角戏:双方都必须开口。结果要按上面「本次走向」落到实处,不给模棱两可的暧昧结尾。\n"
        + story_weight_line("major") +
        '严格输出 JSON:{"narration":"告白场景叙述","dialogues":[{"speaker":"","text":""}]}'
    )


def propose(*, world, a, b, score: int) -> str:
    """求婚/缔结伴侣场景 prompt(重要剧情权重;条件已校验,必定成功)。"""
    return (
        f"{_world_line(world)}\n"
        f"求婚者:{a.persona_line()}\n"
        f"被求婚者:{b.persona_line()}\n"
        f"两人好感:{score},早已是彼此认定的恋人。\n"
        "写一段求婚场景:叙述(300~600字,轻小说式,从准备、时机、仪式到动情的完整一幕),"
        "+多轮对话(6~10轮,IM聊天体,每条\"speaker\"≤8字、\"text\"≤60字)。"
        "禁止独角戏:双方都必须开口。结尾要明确『答应与否』,不许停在欲言又止。\n"
        + story_weight_line("major") +
        '严格输出 JSON:{"narration":"求婚场景叙述","dialogues":[{"speaker":"","text":""}]}'
    )


# ═══════════════════════════ 主动行动 / 设施 / 家 / 兼职 / 主线 ═══════════════════════════
def resolve_action(*, world, char, action_name: str, detail: str, kind: str = "safe",
                   memories: list[str] | None = None, state_note: str = "",
                   heal_note: str = "", zone_note: str = "") -> str:
    """结算一次玩家主动行动(练习/健身/打怪/冒险)。kind: safe | risk。
    zone_note: 『打怪』的讨伐区域锁定说明(游戏层按玩家指定/委托对齐/等级适配预先选好)。"""
    risk_line = (
        "【风险型】结果起伏大:可能大丰收,也可能受伤/掉属性/破财。数值范围可以放得更宽。"
        if kind == "risk"
        else "【日常型】大体都往好的方向走,只是奖励丰俭有别;不要给毁灭性打击。"
    )
    if action_name == "打怪":
        risk_line += (
            "\n【讨伐规则】打怪必须把场景放进当前世界的危险区域之一,点出区域名与遭遇的敌人(用区域清单里的敌人名);"
            "战斗结果要明确(击败/击退/败退);若获胜,items_gain 可给 1~2 个该区域 loot 素材"
            "或世界治疗物品(用世界通名),声望也会因讨伐而上升;若败退,hp 负值要克制。"
        )
        if zone_note:
            risk_line += f"\n【本次讨伐已锁定】{zone_note}"
    if state_note:
        risk_line += (
            f"\n该角色正被「{state_note}」困住(无法自由行动)——这次行动是TA的脱困/求生尝试,"
            "成败由你判断并写进叙述。\n处境说明:若这次行动成功挣脱束缚/脱困/破局,则输出 state_lift:true;"
            "若反而陷入新的束缚或换一种困局,则输出 state:{...};若只是挣扎推进未有果,则两者都不输出。"
        )
    mem = "\n".join(memories[:4]) if memories else ""
    return (
        f"{_world_line(world)}\n"
        f"角色:{char.persona_line()},背景:{char.backstory[:600] or '未详'},"
        f"当前生命{char.hp}/100、体力{char.stamina}/心情{char.mood}/金币{char.gold}\n"
        f"今日于《{world.name}》执行行动:「{action_name}」{detail[:80]}\n{risk_line}\n{mem}\n"
        "请写出这次行动的经过与结果:结果按『演出脚本』写成(轻小说式,合计100~200字:画面感+心理细节+结果交代清楚"
        "——做成了什么/收获什么/付出什么代价,都写明白),结合世界设定与角色性格。\n"
        + SCRIPT_SPEC +
        "行动中至少 2 个不同说话人开口(禁止独角戏),场景人物必须回应;『dialogues』只是兜底,对白已入脚本则填 []。\n"
        "属性键:" + attr_names_line() + "。日常型行动要消耗的体力由系统扣除,效果表里不要写体力。\n"
        '严格输出 JSON:{"narration":"演出脚本",'
        '"effects":{"mood":±,"gold":±,"exp":0-25,"stamina":±(仅风险型可写),"hp":±,"attrs":{"force":0},"reputation":-10~15},"memory":"一句话存档", "state":{"type":"...","reason":"..."}, "state_lift":true, '
        '"items_gain":[{"name":"获得物品≤12字","note":"来历≤20字"}],"items_lose":["失去/消耗的物品名"]}\n'
        "effects.reputation:此事传开后在本世界的声望变化(-10~15):讨伐建功/义举为正,劫掠/怯逃为负;日常行动可不输出。\n"
        "effects.hp:生命值变化(-100~30):受伤为负、服用治疗物品/疗伤为正;归零会重伤昏迷,负值要克制。"
        "若角色受伤且背包有治疗物品,可让TA在行动中自然使用(items_lose 该物品 + effects.hp 正值)。"
        "items_gain/items_lose 只在行动自然涉及物品得失时输出(拾获/缴获/受赠/消耗/损坏/讨伐掉落),通常为空数组;物品名要贴合世界观。"
        + ITEM_OWNERSHIP_RULE +
        "数值克制:日常型大部分±5~15、exp 5~18、金币±0~40;风险型可到 exp 5~30、金币 0~80,失败时给负反馈但不要毁灭性打击。"
        "state 与 state_lift 只在处境变化时输出(见规则说明),否则两字段都不要出现。"
    ) + _heal_line(world, heal_note)


def facility_event(*, world, char, facility: dict, action: str,
                   memories: list[str] | None = None) -> str:
    """造访可交互设施时的小事件 prompt。"""
    mem = "\n".join(memories[:3]) if memories else ""
    fac_name = str(facility.get("name") or "某处")
    fac_kind = str(facility.get("kind") or "设施")
    fac_desc = str(facility.get("desc") or "")
    return (
        f"{_world_line(world)}\n"
        f"角色:{char.persona_line()},背景:{char.backstory[:600] or '未详'},体力{char.stamina}/心情{char.mood}/金币{char.gold}\n"
        f"角色去《{world.name}》的「{fac_name}」({fac_kind}——{fac_desc})"
        f"[想做的事:{action[:60]}],想在这里打发时光。\n{mem}\n"
        "写一段在这家场所里的经历:结果按『演出脚本』写成(轻小说式,合计100~200字:环境氛围+与场内人物的互动"
        "+一件小事或小插曲+结果交代清楚——这段时光过得怎样、有没有收获都写明)。\n"
        + SCRIPT_SPEC +
        "场内至少 2 个不同说话人开口(禁止独角戏);『dialogues』只是兜底,对白已入脚本则填 []。\n"
        "属性键:" + attr_names_line() + "。\n"
        '严格输出 JSON:{"narration":"演出脚本","effects":{"mood":±,"gold":±,"exp":0-10,"attrs":{"force":0}},"memory":"一句话存档", "items_gain":[{"name":"可选≤12字","note":"≤20字"}]}\n'
        "数值克制(大部分±3~12);items_gain 只在自然得到时才给(如买到的纪念品),通常为空;不要输出体力。"
    )


def home_event(*, world, char, plot_name: str, memories: list[str] | None = None) -> str:
    """回宅时家居小事件的 prompt。"""
    mem = "\n".join(memories[:3]) if memories else ""
    return (
        f"{_world_line(world)}\n"
        f"角色:{char.persona_line()}\n"
        f"角色回到《{world.name}》的「{plot_name}」家中,打算歇一歇。\n{mem}\n"
        "写一段回到家里的小剧情:结果按『演出脚本』写成(轻小说式,合计80~150字:归家的画面感+一件温馨的小事或小插曲"
        "+结果交代清楚——这趟回家歇好了没、发生了什么小事都写明)。\n"
        + SCRIPT_SPEC +
        "到家后至少 2 个不同说话人开口(禁止独角戏);『dialogues』只是兜底,对白已入脚本则填 []。\n"
        '严格输出 JSON:{"narration":"演出脚本","dialogues":[{"speaker":"","text":""}],"effects":{"mood":±,"exp":0~6},"memory":"一句话"}\n'
        "数值克制(mood/exp ±0~6),不要输出金币和体力。"
    )


def settle_work(*, world, char, spot: str, job: str, hours: float,
                colleague: str | None) -> str:
    """到点下班的结算 prompt(收工叙述 + 同事道别)。"""
    colleague_line = (
        f"今天的同班同事是「{colleague}」,收工时TA与角色有一段自然的道别/闲聊。"
        if colleague else "收工时独自一人,把工具归位后离开。")
    return (
        f"{_world_line(world)}\n"
        f"角色:{char.persona_line()}\n"
        f"角色刚结束了在「{spot}」的兼职(职业:{job}),干了约 {hours} 小时。\n"
        f"{colleague_line}\n"
        "写一段下班收工的叙述:结果按『演出脚本』写成(轻小说式,合计100~180字:劳动的画面感+一段小插曲或同事互动"
        "+下班时的结果交代清楚——拿了什么、累不累、明天还来不来都写明)。\n"
        + SCRIPT_SPEC +
        "在场者与角色要有道别对话(至少 2 个不同说话人);『dialogues』只是兜底,对白已入脚本则填 []。\n"
        '严格输出 JSON:{"narration":"演出脚本","dialogues":[{"speaker":"","text":""}],'
        '"effects":{"mood":±,"exp":0~8,"attrs":{}},'
        '"items_gain":[{"name":"可选:雇主塞的小谢礼≤12字","note":"≤20字"}]}'
        "数值克制(mood/exp ±0~8);items_gain 只在剧情自然时给一件,通常为空;不要输出金币(工钱另算)。"
    )


def resolve_mainline(*, world, char, stage: dict, goal_note: str = "",
                     weight: str = "major") -> str:
    """结算世界主线一小节(user prompt)。goal_note: 门槛达成说明;weight: 剧情权重。"""
    goal_line = (f"\n【阶段门槛已达成】{goal_note}" if goal_note else "")
    narr_spec, _ = story_spec(weight, "90~180", "1~3")
    return (
        f"{_world_line(world)}\n"
        f"主角:{char.persona_line()},{_rep_short(world, char)}\n"
        f"\n当前主线小节:{stage.get('stage','')} —— {stage.get('desc','')}{goal_line}\n"
        "角色主动去推进这段世界主线。请写出这一步的经过与结果:按『演出脚本』写成(轻小说式,"
        f"{narr_spec}字),"
        "扣住主线目标、有画面感、结果交代清楚(这一步达成与否、拿到的线索或代价都写明),并给出数值变化(克制:±3~10);"
        "主线推进有广泛影响,reputation 可在 -5~10 之间。\n"
        + SCRIPT_SPEC +
        "这段推进中至少 2 个不同说话人开口;『dialogues』只是兜底,对白已入脚本则填 []。\n"
        + story_weight_line(weight) +
        '严格输出 JSON:{"narration":"演出脚本","dialogues":[{"speaker":"","text":""}],'
        '"effects":{"mood":±,"gold":±,"exp":0-15,"hp":±,"attrs":{},"reputation":-5~10}, "memory":"一句话存档"}'
    )


def _rep_short(world, char) -> str:
    """角色当前世界声望的一句话(game 层预先挂在 char._rep_line 上;无则空)。"""
    return str(getattr(char, "_rep_line", "") or "")


def rep_note_block(rep_note: str) -> str:
    """声望注入块(game 层拼好的一句话;空则不占行)。"""
    rn = (rep_note or "").strip()
    return f"【声望】{rn}\n" if rn else ""


def mainline_echo(*, world, char, stage: dict, ctx: str) -> str:
    """主线回响:日常结算后小概率触发的主线伏笔/呼应叙述。"""
    return (
        f"{_world_line(world)}\n"
        f"角色:{char.persona_line()}\n"
        f"当前主线小节:{stage.get('stage','')} —— {stage.get('desc','')}\n"
        f"刚刚发生:{str(ctx)[:120]}\n"
        "这件事与世界主线隐隐呼应。请写一段『主线回响』(60~110字):从刚发生的事自然引出主线线索——"
        "一个征兆、一句传言、一件似曾相识的旧物、远处的一声号角、某位神秘人的注视等;"
        "只做伏笔与呼应,不要超展开、不要直接揭开谜底,也不要打断刚刚发生的事。"
        '严格输出 JSON:{"narration":"回响叙述"}'
    )


def gen_epilogue(*, world) -> str:
    """主线全篇完结后续写『尾声』新篇章(世界在结局之后仍在继续)。"""
    return (
        f"{_world_line(world)}\n"
        "这个世界的主线篇章已经完结(尘埃落定)。但世界仍在继续——请续写『尾声』新篇章:\n"
        '严格输出 JSON:{"narration":"篇章过渡叙述(80~140字:旧篇章落幕、余波与新暗流并起)",'
        '"stages":[{"stage":"小节名≤10字","desc":"≤30字","goal":{"type":"reputation|quest|defeat|空","value":数字}}]}\n'
        "stages 给 2~4 节:内容是主线结局之后的世界走向——余波、重建、新暗流、旧人物的归宿或新威胁的萌芽,"
        "层层可推进;中后期的 1~2 节可带 goal(与主线门槛同规则)。"
    )


# ═══════════════════════════ NPC 对话 ═══════════════════════════
def npc_chat(*, world, npc: dict, char, action: str,
             memories: list[str] | None = None,
             state_note: str = "", previous: list[str] | None = None,
             rep_note: str = "") -> str:
    """与当前世界 NPC 的多轮对话 prompt(切题铁律 + 直白不谜语)。"""
    state_line = ""
    if state_note:
        state_line = (
            f"\n{char.name}正被『{state_note}』困住,无法自由行动。"
            "请判断当前这位NPC是否能算是能帮助到TA的『特殊NPC』:能的话,自然演一段TA帮上忙的情节,"
            "并在成功挣脱/获救时输出 state_lift:true,或换一种困局时输出 state;"
            "若这位NPC帮不上忙,就如实演一段TA爱莫能助/婉拒的对话,不要强行放人,也不要输出 state/state_lift。"
        )
    return (
        f"世界:《{world.name}》。NPC「{npc['name']}」({npc.get('role','')},{npc.get('persona','')};"
        f"钩子:{npc.get('hook','')})"
        + (f"\nTA的日子:平时{npc.get('daily','')}" if npc.get("daily") else "")
        + (f";怪癖/口头禅:{npc.get('quirk','')}" if npc.get("quirk") else "")
        + f"\n角色:{char.persona_line()},{_rep_short(world, char)}\n角色行为:{action[:80]}\n"
        f"{("\n".join(memories[:4]) if memories else '')}\n{rep_note_block(rep_note)}{state_line}\n"
        "【声望与态度】NPC 对角色的第一态度应贴合其在本世界的声望:声望高则热络/尊敬/愿意帮忙,"
        "声望低则提防/冷淡/要价更高;态度是起点而非终点,互动本身可以让它变化。\n"
        f"【切题铁律】这段对话演的必须就是角色的「{action[:40]}」这个行为,只涉及角色与该NPC两人;"
        "严禁跑题:不得凭空插入与行为无关的遭遇/战斗/陌生人物/超展开;"
        "知识库素材只作风味点缀,不得改变本次互动的主题、场景与人物关系。\n"
        "与角色进行多轮对话(NPC 与角色都必须开口,禁止独角戏),再用旁白收尾(60~120字),可给微小奖励。\n"
        "让NPC像有自己生活的人:带上TA的行踪、习惯、语气与情绪,别念模板台词。\n"
        "对话/状态/旁白请按下面的排版交错展开:\n"
        + SCRIPT_SPEC +
        '严格输出 JSON:{"reply":"NPC最核心的一句台词","dialogues":[{"speaker":"","text":""}],'
        '"narration":"演出脚本",'
        '"effects":{"mood":±,"gold":±,"exp":0-8,"hp":±,"reputation":-5~5}, "memory":"一句话存档", "state":{...}, "state_lift":true}'
        "(reputation/hp 仅在剧情明显涉及时输出,一般可不输出)"
    ) + previous_block(previous)


# ═══════════════════════════ 抵达 / 晨报 / 记忆压缩 ═══════════════════════════
def compose_arrival(*, world, prev_name: str, via: str) -> str:
    via_line = {
        "shift": "世界在众人眼前剧烈扭曲、重组",
        "travel": "众人主动开启了一条穿越之门",
        "init": "群聊世界的帷幕第一次拉开",
    }.get(via, "时空泛起涟漪")
    return (
        f"众人从《{prev_name or '虚无'}》来到新世界:《{world.name}》[{world.genre}]。\n"
        f"世界描述:{world.desc}\n{via_line}。写一段抵达播报(80-140字,渲染初印象,点出1-2个独特之处)。\n"
        '严格输出 JSON:{"narration":"抵达播报","tips":["给新来者的一句忠告","一句忠告"]}'
    )


def morning_brief(*, world, chars, day_note: str) -> str:
    who = "、".join(c.persona_line() for c in chars[:8]) or "暂无居民"
    return (
        f"世界:《{world.name}》。今日({day_note})的晨报。居民:{who}\n"
        "写一段晨报(50-90字):天气/异象 + 今日氛围 + 点名一位居民该当心什么。\n"
        '严格输出 JSON:{"brief":"晨报正文","watch":"被点名者与原因(≤20字)"}'
    )


def summarize_core(uid_name: str, old_texts: list[str]) -> str:
    return (
        f"把角色「{uid_name}」的以下旧记忆压缩成 3-5 条稳定的『核心记忆』(第三人称,每条≤25字,"
        "只保留塑造性格/关系/重要经历的事实):\n- " + "\n- ".join(old_texts[:40])
        + '\n严格输出 JSON:{"cores":["..."]}'
    )


# ═══════════════════════════ 自由文本 → 结构化(创角/改角/加NPC)═══════════════════════════
def parse_persona(text: str) -> str:
    """口语化设定描述 → {gender, tags, backstory, attrs}。"""
    attr_line = "、".join(f"{k}({_nm})" for k, _nm in ATTRS_ZH)
    return (
        "群友在创建 OC 分身,给了一段口语化的设定描述。请整理成结构化人设,不要编造描述里没有的信息:\n"
        f"【设定描述】{text[:4000]}\n"
        "1. gender:性别,没提就填「保密」;\n"
        "2. tags:性格标签数组,2~6个,每个2~6字(如:腹黑/重情义/独来独往/生人勿近),从性格与行事风格中提炼;\n"
        "3. backstory:第三人称背景设定一段话(60~150字),把外观、穿着、身份、能力、经历等信息全部合并进去,语句通顺;\n"
        f"4. attrs:按设定强弱给六维分配初始属性(数值 18~60),与设定强相关的 1~2 项给 55~60 且为最高"
        f"(如「大天才」的 intellect 应最高),普通项 25~40,短板 18~25。键:{attr_line}\n"
        '严格输出 JSON:{"gender":"","tags":[""],"backstory":"","attrs":{"force":0,"agility":0,"intellect":0,"charm":0,"luck":0,"sanity":0}}'
    )


ATTRS_ZH = [
    ("force", "力量"), ("agility", "敏捷"), ("intellect", "智力"),
    ("charm", "魅力"), ("luck", "幸运"), ("sanity", "精神"),
]


def parse_persona_update(*, cur_name: str, cur_gender: str, cur_tags: list,
                         cur_backstory: str, text: str) -> str:
    """自由修改描述 → 需要更新的人设字段(只输出要改的)。"""
    return (
        f"角色「{cur_name}」当前人设:性别 {cur_gender};性格标签:{'、'.join(cur_tags) or '无'};"
        f"背景设定:{cur_backstory[:1000] or '无'}\n"
        f"玩家发出一段修改描述:{text[:400]}\n"
        "请判断要更新哪些字段,只输出需要修改的字段:\n"
        "- gender:仅当明确提及性别时输出;\n"
        "- tags:输出更新后的完整标签列表(2~6个,每个2~6字,保留仍然成立的旧标签,融合新描述);\n"
        "- backstory:输出合并后的完整背景设定(保留未被推翻的旧设定,融入新描述);\n"
        '严格输出 JSON:{"gender":"","tags":[""],"backstory":""}(不改的字段不要出现)'
    )


def parse_npc(name: str, text: str, world=None, npc_names: list[str] | None = None) -> str:
    """口语化描述 → NPC 档案 {role, persona, hook}。"""
    user = (
        f"群友要在世界里添加一位叫「{name}」的NPC,给了一段口语化描述。请整理成档案,不要编造描述里没有的信息:\n"
        f"【描述】{text[:400]}\n"
    )
    if world is not None:
        user += (
            f"【所在世界】《{world.name}》[{world.genre}] {world.desc}\n"
            f"氛围:{world.atmosphere};世界规则:{';'.join(world.rules or [])}\n"
            "档案(职业/性格/钩子)必须贴合该世界的题材、氛围与规则,不要出现与世界观冲突的设定。\n"
        )
    if npc_names:
        user += f"世界中已有NPC:{'、'.join(list(npc_names)[:10])}。不要与TA们重名,职业也不要雷同。\n"
    user += (
        "- role:职业/身份(2~10字,描述没提就结合世界背景推测一个最贴切的);\n"
        "- persona:性格一句话(≤30字);\n"
        "- hook:可交互的钩子一句话(≤30字,具体、有故事感、可直接对话推进,不玩虚的);\n"
        '严格输出 JSON:{"role":"","persona":"","hook":""}'
    )
    return user


# ═══════════════════════════ 每日小任务 ═══════════════════════════
def gen_quests(*, world, char, npc_names: list[str] | None = None,
               life_names: list[str] | None = None,
               facilities: list[dict] | None = None,
               memories: list[str] | None = None) -> str:
    """生成 3 个设施/委托人驱动的任务。zones: 危险区域(讨伐/素材类任务可与之联动)。"""
    mem = "\n".join(memories[:4]) if memories else ""
    npc_line = "、".join((npc_names or [])[:10]) or "暂无"
    life_line = "、".join((life_names or [])[:8]) or "暂无"
    facs = (facilities or [])[:10]
    fac_line = "\n".join(f"- {f.get('name')}({f.get('kind','')}){'·可打工:'+f['work'] if f.get('work') else ''}"
                         for f in facs) or "- 暂无"
    zones = [z for z in (getattr(world, "zones", None) or []) if isinstance(z, dict) and z.get("name")]
    zone_line = "\n".join(
        f"- {z.get('name')}({z.get('kind','')},危险度{z.get('danger','?')})敌人:"
        + "、".join(e.get("name", "") for e in (z.get("enemies") or []) if isinstance(e, dict))
        + "；素材:" + "、".join((z.get("loot") or [])[:3])
        for z in zones[:5]) or "- 暂无"
    return (
        f"{_world_line(world)}\n"
        f"角色:{char.persona_line()},背景:{char.backstory[:600] or '未详'}\n"
        f"世界知名NPC:{npc_line}\n生活角色:{life_line}\n"
        f"世界设施(任务的发布地点从这里选):\n{fac_line}\n"
        f"危险区域(讨伐/采集素材类任务应指向这些区域及其敌人/素材):\n{zone_line}\n"
        f"{mem}\n"
        "请给这个角色生成今天的 3 个委托任务,由设施的委托板/当值者发布。每个任务包含:\n"
        '1) "giver":委托人(2~6字)——优先用设施相关的组织(如冒险者公会/商会/茶馆掌柜)或上面的NPC,也可临时虚构一个贴合世界观的普通委托人;'
        '严禁把其他玩家/群友写成委托人\n'
        '2) "place":发布设施名(必须从上面设施清单里选)\n'
        '3) "text":任务名≤16字,"hint":完成提示≤20字\n'
        '4) "steps":1~3 个可验证步骤,type 只能从以下选:\n'
        '   {"type":"act","desc":"≤20字","keywords":["关键词2~4个"]} —— 需要玩家用「冒险/打怪」完成,keywords 要能出现在玩家的行动描述里(讨伐类任务的关键词直接用区域名或敌人名,如「打怪 低语森林 树精」)\n'
        '   {"type":"npc","desc":"≤20字","npc":"NPC名"} —— 与某位世界NPC互动;npc 必须用名单本名\n'
        '   {"type":"life","desc":"≤20字","npc":"生活角色名"} —— 与某位生活角色互动;必须用名单本名\n'
        '   {"type":"social","desc":"≤20字"} —— 与任意群友互动一次(不指定具体人)\n'
        '   {"type":"work","desc":"≤20字"} —— 完成一次兼职打工\n'
        '   {"type":"item","desc":"≤20字","item":"物品名≤12字"} —— 取得某件物品(讨伐掉落素材/冒险拾获/事件获得;素材名用上面区域 loot 名)\n'
        '   {"type":"facility","desc":"≤20字","facility":"设施名"} —— 前往某个设施办事(必须用设施清单本名;\n'
        '     非社交娱乐设施也可去——这是任务需要,系统会放行并生成一段在设施里的剧情)\n'
        "要求:3 个任务难度递进(至少1个单步日常,最多1个三步任务);"
        "至少 1 个任务与危险区域联动(讨伐敌人或采集素材);步骤必须与任务文本逻辑一致;"
        "结合世界观,生活气息或小冒险皆可。严禁把其他玩家/群友写成任务目标或委托人。\n"
        '严格输出 JSON:{"quests":[{"text":"","giver":"","place":"","hint":"",'
        '"steps":[{"type":"","desc":"","keywords":[],"npc":"","item":""}]}]}\n恰好 3 个。'
    )


def finish_quest(*, world, char, quest: str, giver: str = "", place: str = "",
                 steps_desc: list[str] | None = None,
                 memories: list[str] | None = None,
                 rep_note: str = "") -> str:
    """向委托人交付任务的 prompt(出场人物锁死)。"""
    mem = "\n".join(memories[:3]) if memories else ""
    giver = (giver or "委托人").strip()
    place = (place or "").strip()
    steps_line = "".join(f"\n- ✅ {s}" for s in (steps_desc or [])) or "\n- ✅(单步任务)"
    rep_line = (f",{_rep_short(world, char)}" if getattr(char, "_rep_line", "") else "")
    return (
        f"{_world_line(world)}\n"
        f"角色:{char.persona_line()}{rep_line},背景:{char.backstory[:600] or '未详'}\n"
        f"角色完成了委托任务:「{quest[:30]}」\n"
        f"委托人:{giver}(发布地点:{place or '不祥'})"
        "——若委托人是组织/设施,由当值代理人出面交付;若是个人委托人则以本名出场\n"
        f"已完成的步骤:{steps_line}\n"
        f"{mem}\n"
        "【出场铁律】这是交付场景:只允许角色与委托人两方出场,"
        "严禁出现其他群友/生活角色/无关NPC/凭空新人物,严禁把交付成果交给或送给别人。\n"
        "【声望】声望高的角色委托人更信任、酬劳更爽快;声望低则被防备、要价或扣除押金。交付叙述要体现这一点。\n"
        "写一段交付场景:按『演出脚本』写成(轻小说式,合计80~150字:委托人的验收反应+简短交代+结果说明——酬劳拿到没、"
        "事情如何收尾都写明),并给一点委托酬劳。\n"
        + SCRIPT_SPEC +
        f"角色与「{giver}」要有交割对话(至少 2 个不同说话人);『dialogues』只是兜底,对白已入脚本则填 []。\n"
        '严格输出 JSON:{"narration":"演出脚本","effects":{"exp":5~12,"gold":0~25,"mood":0~3,"reputation":0~8},'
        '"items_gain":[{"name":"可选:委托人额外送的谢礼≤12字","note":"≤20字(可为世界治疗物品)"}]}'
        "items_gain 只在委托人明确会给实物谢礼时才输出,通常为空;reputation 为完成任务后的声望上升(1~8)。"
    )


# ═══════════════════════════ 远征系统 ═══════════════════════════
WORLDVIEW_LAW = (
    "\n【世界观铁律】远征的一切措辞都必须贴合该世界的题材与时代:"
    "发布方与称谓(公会是奇幻,宗门/仙盟是修仙,据点议会是末世,株式会社/科考队是科幻近代……)、"
    "装备与交通、报酬形式、地名与术语,全部就地取材;严禁出现与世界观冲突的组织或名词。"
)


def expedition_offer(*, world, char, zone: dict, issuer: str, teammates: list[str],
                     duration_h: int, rate: int) -> str:
    """远征委托布告的 user prompt。issuer/队友名单由系统按世界观给定,布告以其名义撰写。"""
    team_line = "、".join(teammates) if teammates else "暂无,可补 1~2 名贴合世界观的临时同伴"
    return (
        f"{_world_line(world)}\n"
        f"角色:{char.persona_line()},{_rep_short(world, char)}\n"
        f"远征目标区域:「{zone.get('name')}」({zone.get('kind','')},危险度{zone.get('danger','?')}——{zone.get('desc','')})\n"
        f"出没敌人:{'、'.join(e.get('name','') for e in (zone.get('enemies') or []) if isinstance(e, dict)) or '不明'}\n"
        f"发布方(必须以此名义颁布):{issuer}\n"
        f"已确认同行的同伴:{team_line}\n"
        f"预计行程:约 {duration_h} 小时;以角色当前实力预估成功率约 {rate}%\n"
        "请以发布方口吻写一份远征委托布告:目标与意义、集合与行程、风险与告诫、报酬概要(报酬由系统结算,布告只需概述形式:"
        "贴合世界观的酬金/功勋/物资均可)。title 是这份布告的名字。"
        + WORLDVIEW_LAW +
        '严格输出 JSON:{"title":"远征委托名≤12字","briefing":"布告正文90~160字","teaser":"报酬一句话≤18字"}'
    )


def life_char_story(*, world, char, other=None, memories=None) -> str:
    """持久生活角色的日常小剧场:TA 自己的经历与变化(让群世界里的TA们活起来)。"""
    other_line = ""
    if other is not None:
        other_line = (f"\n在场者:{other.persona_line()},背景:{other.backstory[:300] or '未详'}"
                      "(TA恰好在场,可以互动、结伴,也可以只是擦肩)。")
    mem = "\n".join(memories[:3]) if memories else ""
    return (
        f"{_world_line(world)}\n"
        f"主角(持久生活角色):{char.name} —— {char.backstory[:400] or '一名生活在群世界里的角色'}\n"
        f"{other_line}{mem}\n"
        "写一段TA自己的日常小剧场(120~220字,轻小说式):贴合TA的人设与世界观,"
        "有画面、有小事件、有落点(做成了什么/心情如何/日子往前挪了一步);"
        "不超展开,像真实生活里的一帧。对话与叙述必须贴合该世界的题材与时代。\n"
        + SCRIPT_SPEC +
        "有在场者时至少2人开口,无在场者时可与路人交谈或自言自语;『dialogues』只是兜底,对白已入脚本则填 []。\n"
        "effects:TA自己的变化(exp 0~8,mood ±5,gold ±5,attrs 偶尔±1,克制)。\n"
        "rel_delta:与在场者的好感变化(-3~4;无在场者时输出0)。\n"
        "items_gain/items_lose:仅在自然涉及时输出一件(捡到/买了个小东西/用掉了什么),通常为空。\n"
        '严格输出 JSON:{"narration":"演出脚本","dialogues":[{"speaker":"","text":""}],'
        '"effects":{"exp":0-8,"mood":±,"gold":±,"attrs":{}},"rel_delta":0,'
        '"items_gain":[{"name":"","note":""}],"items_lose":[""],"memory":"一句话"}'
    )


def expedition_invite(*, world, char, target, offer: dict,
                      rel_score: int, rel_stage: str = "") -> str:
    """远征邀约:发起人邀请对方同行,由被邀请者的性格与两人关系判断是否同意。"""
    return (
        f"{_world_line(world)}\n"
        f"发起人:{char.persona_line()},{_rep_short(world, char)}\n"
        f"被邀请者:{target.persona_line()},背景:{target.backstory[:400] or '未详'}\n"
        f"两人关系:{rel_score}({rel_stage})\n"
        f"远征委托:「{offer.get('title')}」目标「{offer.get('zone_name')}」({offer.get('zone_kind','')})——{offer.get('briefing','')}\n"
        f"危险度★{offer.get('danger')},行程约 {offer.get('duration_h')} 小时,"
        f"预估成功率 {offer.get('rate')}%,发布方:{offer.get('issuer')};已有同行:{offer.get('team_note','暂无')}\n"
        "发起人诚邀被邀请者同行远征。请以被邀请者的性格、两人交情与远征风险判断是否同意(agree),"
        "并写出这段邀约交锋:对方可能爽快答应、提条件讨价还价、谨慎婉拒(危险/有要事/交情不够),"
        "结果要落到实处。\n"
        + SCRIPT_SPEC +
        "邀约对话与叙述交错展开(双方都开口,禁止独角戏);『dialogues』只是兜底,对白已入脚本则填 []。\n"
        + WORLDVIEW_LAW +
        '严格输出 JSON:{"agree":true,"narration":"演出脚本",'
        '"dialogues":[{"speaker":"","text":""}],"rel_delta":-3~5}'
    )


def expedition_report(*, world, char, exp: dict, phase: str, supplies_note: str) -> str:
    """远征途中的剧情片段(播报)。phase: 行军/遭遇战/险境/决战前夜。"""
    teammates = "、".join(exp.get("teammates") or []) or "队友们"
    return (
        f"{_world_line(world)}\n"
        f"角色:{char.persona_line()}\n"
        f"正在进行:「{exp.get('title','')}」远征,目标「{exp.get('zone','')}」(危险度{exp.get('danger','?')}),"
        f"同行:{teammates}。行程已过约 {exp.get('progress', 0)}%。\n"
        f"当前阶段:{phase}——写一段远征途中的剧情片段(120~220字):"
        "行军写风物与队伍氛围,遭遇战写一场短促交手,险境写困境与代价,决战前夜写肃杀与决心;"
        "片段要有画面、有进展、有落点,像连载中的一章。"
        "\n【物资归属铁律】背包物资清单按成员分列(角色本人与随行队友,各自随身);"
        "items_lose 每项必须写成『成员名:物品名』,表示从该成员自己的行囊取出——"
        "叙述与归属必须一致(谁出的就从谁的行囊拿出),严禁张冠李戴、严禁取用清单外的物品;"
        "未列出的队友/NPC 的私有物品不得混入。"
        + (f"\n{supplies_note}" if supplies_note else "")
        + SCRIPT_SPEC +
        "片段中队友/敌人的简短对话(1~3轮,IM聊天体,speaker用队友本名或敌人称谓,text≤50字,禁止独角戏);"
        + WORLDVIEW_LAW +
        '严格输出 JSON:{"narration":"演出脚本","dialogues":[{"speaker":"","text":""}],"items_lose":["本轮消耗的补给名"]}'
        "(items_lose 按上方物资指示填写;指示要求不要消耗时输出空数组)"
    )


def expedition_settle(*, world, char, exp: dict, outcome: str, reward_line: str) -> str:
    """远征结算:成功=高潮剧情(climax,600~1000字),失败=重要剧情(major,300~600字)。
    报酬/损失由系统结算(reward_line),叙述中自然带过即可,严禁自行发钱发物。"""
    weight = "climax" if outcome == "success" else "major"
    teammates = "、".join(exp.get("teammates") or []) or "队友们"
    outcome_line = (
        "远征大获全胜:写最终决战的多回合交锋(前哨→主力接战→危局→逆转→歼灭/夺标)→携战利品凯旋返程的完整一幕"
        if outcome == "success"
        else "远征折戟:写局势如何一步步失控(意外/强敌/天灾)、艰难撤离、带伤归来的狼狈与教训"
    )
    return (
        f"{_world_line(world)}\n"
        f"角色:{char.persona_line()},{_rep_short(world, char)}\n"
        f"「{exp.get('title','')}」远征({exp.get('zone','')},危险度{exp.get('danger','?')}),"
        f"同行:{teammates},历时约 {exp.get('duration_h', '?')} 小时。\n"
        f"结局走向:{outcome_line}。\n"
        f"系统结算结果(叙述中自然提及,不要改动):{reward_line}\n"
                + SCRIPT_SPEC +
        "最终一战/撤离途中队友与强敌的多轮对话(6~12轮,轮番开口、有呐喊有诀别或虚脱后的相视而笑)与叙述交错展开。"
        + story_weight_line(weight) + WORLDVIEW_LAW +
        '严格输出 JSON:{"narration":"演出脚本","dialogues":[{"speaker":"","text":""}],"memory":"一句话存档"}'
    )


# ═══════════════════════════ 纠错类 prompt(重试时用)═══════════════════════════
FRESH_CORRECT = (
    "\n\n【重要纠正】你这次的输出与之前发生过的情节几乎一模一样,这是敷衍的复读,不可接受。"
    "完全重写:换新的场景、新话题、新对话,让情节向前推进。"
)


def dialogue_correction(mono: bool, counterpart: str) -> str:
    """对话质量守卫的纠正 prompt(独角戏/对方没开口)。"""
    return (
        "你刚才的对话"
        + ("是独角戏(只有一个说话人)" if mono else f"没有让「{counterpart}」开口")
        + ",这不合要求。重写 dialogues:必须你来我往、至少 2 个不同的说话人"
        + (f",且「{counterpart}」必须以本名开口回应" if counterpart else "")
        + ";每条 speaker≤8字、text≤60字。"
    )
