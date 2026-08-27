"""分身的世界 (OCverse) - 群聊 OC 养成与世界穿越插件核心包。

目录:
- models.py     数据模型
- db.py         SQLite 存储层
- embedder.py   轻量语义向量(零依赖哈希词向量 + 可选 API embedding)
- memory.py     记忆系统(时间线 + 语义检索 + 核心记忆压缩)
- llm_engine.py LLM 封装(世界生成/事件/判定/互动/NPC)
- game.py       玩法引擎(日程/事件/成长/世界变动/穿越)
- avatar_store.py 角色头像存取
- imcard/       IM 聊天卡片渲染引擎(纯 Pillow,无桌面/浏览器依赖)
"""

__version__ = "1.0.0"
