"""数据模型。所有字段都可 JSON/SQLite 序列化。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .config import ATTR_KEYS


def _loads(s, default):
    if s is None or s == "":
        return default
    if isinstance(s, (list, dict)):
        return s
    try:
        v = json.loads(s)
        return v if v is not None else default
    except Exception:
        return default


def _dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def default_attrs() -> dict:
    return {k: 0 for k in ATTR_KEYS}


@dataclass
class World:
    """一个世界/场景。source: llm | user | default;visited=0 表示「已定义但从未到达」(锁定)。"""

    gid: str = ""
    name: str = ""
    genre: str = ""
    desc: str = ""
    atmosphere: str = ""
    rules: list = field(default_factory=list)
    features: list = field(default_factory=list)
    npcs: list = field(default_factory=list)
    event_ideas: list = field(default_factory=list)
    infra: list = field(default_factory=list)      # 基础设施:商店/杂货铺/旅馆/工作/地标
    mainline: list = field(default_factory=list)   # 世界主线:{stage,title,desc,done}
    source: str = "llm"
    visited: int = 0
    created_by: str = ""
    created_at: float = field(default_factory=time.time)
    id: int = 0

    @classmethod
    def from_row(cls, row) -> "World":
        return cls(
            id=row["id"],
            gid=row["gid"],
            name=row["name"] or "",
            genre=row["genre"] or "",
            desc=row["desc"] or "",
            atmosphere=row["atmosphere"] or "",
            rules=_loads(row["rules"], []),
            features=_loads(row["features"], []),
            npcs=_loads(row["npcs"], []),
            event_ideas=_loads(row["event_ideas"], []),
            infra=_loads(row["infra"], []) if "infra" in row.keys() else [],
            mainline=_loads(row["mainline"], []) if "mainline" in row.keys() else [],
            source=row["source"] or "llm",
            visited=int(row["visited"] or 0),
            created_by=row["created_by"] or "",
            created_at=float(row["created_at"] or 0),
        )

    def npc_names(self) -> list[str]:
        return [n.get("name", "?") for n in self.npcs if isinstance(n, dict)]

    def brief(self) -> str:
        return f"《{self.name}》[{self.genre}] {self.desc}"


@dataclass
class Char:
    """群成员的 OC 分身。"""

    gid: str = ""
    uid: str = ""
    name: str = ""
    gender: str = "保密"
    tags: list = field(default_factory=list)       # 性格标签
    backstory: str = ""
    avatar: str = ""                                # 本地头像路径
    attrs: dict = field(default_factory=default_attrs)
    level: int = 1
    exp: int = 0
    gold: int = 100
    mood: int = 70
    stamina: int = 90
    title: str = "无名之辈"
    flags: dict = field(default_factory=dict)      # {traveler: 1, interactions: n, ...}
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_row(cls, row) -> "Char":
        attrs = _loads(row["attrs"], {})
        return cls(
            gid=row["gid"],
            uid=row["uid"],
            name=row["name"] or "?",
            gender=row["gender"] or "保密",
            tags=_loads(row["tags"], []),
            backstory=row["backstory"] or "",
            avatar=row["avatar"] or "",
            attrs={k: int(attrs.get(k, 20)) for k in ATTR_KEYS},
            level=int(row["level"] or 1),
            exp=int(row["exp"] or 0),
            gold=int(row["gold"] or 0),
            mood=int(row["mood"] or 70),
            stamina=int(row["stamina"] or 90),
            title=row["title"] or "无名之辈",
            flags=_loads(row["flags"], {}),
            created_at=float(row["created_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
        )

    def persona_line(self) -> str:
        tags = "/".join(self.tags) if self.tags else "性格未详"
        return f"{self.name}(Lv{self.level} {self.title},{self.gender},性格:{tags})"


@dataclass
class EventRow:
    """一次遭遇(事件)。kind: solo | group | npc;state: pending | resolved | expired。"""

    id: int = 0
    gid: str = ""
    uid: str = ""             # 目标角色;group 事件为空
    world_id: int = 0
    kind: str = "solo"
    state: str = "pending"
    payload: dict = field(default_factory=dict)   # {title, scene, options:[{label,hint}], npc?, ...}
    chosen: int = -1
    result: str = ""
    effects: dict = field(default_factory=dict)
    created_at: float = 0.0
    expires_at: float = 0.0

    @classmethod
    def from_row(cls, row) -> "EventRow":
        return cls(
            id=int(row["id"]),
            gid=row["gid"],
            uid=row["uid"] or "",
            world_id=int(row["world_id"] or 0),
            kind=row["kind"] or "solo",
            state=row["state"] or "pending",
            payload=_loads(row["payload"], {}),
            chosen=int(row["chosen"] if row["chosen"] is not None else -1),
            result=row["result"] or "",
            effects=_loads(row["effects"], {}),
            created_at=float(row["created_at"] or 0),
            expires_at=float(row["expires_at"] or 0),
        )
