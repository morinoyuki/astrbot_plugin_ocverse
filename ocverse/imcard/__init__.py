"""IM 聊天卡片渲染引擎(纯 Pillow,无桌面/无浏览器依赖)。

本模块基于作者自己的项目 astrbot_plugin_life_sim 的 im_render 引擎改造而来,
在本插件内独立维护:新增游戏卡片所需的行类型(属性条/选项卡/头像头部/标签胶囊等),
并适配 ocverse 的主题与字体环境变量(OCVERSE_FONT)。
"""

from .engine import ChatRenderer, Row, TooManyPages, render_narrative
from .cards import (
    event_card,
    fortune_card,
    help_card,
    interact_card,
    log_card,
    memory_card,
    morning_card,
    npc_card,
    profile_card,
    render_views,
    roster_card,
    world_card,
    world_list_card,
)

__all__ = [
    "ChatRenderer",
    "Row",
    "TooManyPages",
    "render_narrative",
    "render_views",
    "profile_card",
    "event_card",
    "world_card",
    "world_list_card",
    "log_card",
    "memory_card",
    "fortune_card",
    "morning_card",
    "interact_card",
    "npc_card",
    "help_card",
    "roster_card",
]
