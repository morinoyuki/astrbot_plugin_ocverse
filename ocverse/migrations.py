"""数据库结构统一管理:建表 + 版本化迁移,全部集中在本文件。

规则:
- BASE_SCHEMA 永远保持「最新完整形态」(新库直接建最新表);
- 结构变更时:同步更新 BASE_SCHEMA,并在 _MIGRATIONS 末尾追加一条
  (version, 描述, [sql...]);旧库按 PRAGMA user_version 依序平滑升级;
- 所有迁移语句必须幂等(ADD COLUMN 重复报错会被容忍 / CREATE 用 IF NOT EXISTS),
  因此新库跑迁移也是无害的。
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 6

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
  gid TEXT PRIMARY KEY,
  cur_world_id INTEGER,
  init_done INTEGER DEFAULT 0,
  event_min INTEGER DEFAULT 0,
  event_max INTEGER DEFAULT 0,
  shift_percent INTEGER DEFAULT 0,
  user_world_share INTEGER DEFAULT 0,
  travel_cooldown_h INTEGER DEFAULT 6,
  last_shift_at REAL DEFAULT 0,
  last_travel_at REAL DEFAULT 0,
  day_key TEXT DEFAULT '',
  created_at REAL
);
CREATE TABLE IF NOT EXISTS worlds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gid TEXT NOT NULL,
  name TEXT, genre TEXT, desc TEXT, atmosphere TEXT,
  rules TEXT, features TEXT, npcs TEXT, event_ideas TEXT,
  infra TEXT DEFAULT '[]', mainline TEXT DEFAULT '[]',
  zones TEXT DEFAULT '[]', heal_items TEXT DEFAULT '[]',
  source TEXT DEFAULT 'llm',
  visited INTEGER DEFAULT 0,
  created_by TEXT DEFAULT '',
  created_at REAL
);
CREATE TABLE IF NOT EXISTS chars (
  gid TEXT NOT NULL,
  uid TEXT NOT NULL,
  name TEXT, gender TEXT, tags TEXT, backstory TEXT, avatar TEXT,
  attrs TEXT, level INTEGER DEFAULT 1, exp INTEGER DEFAULT 0,
  gold INTEGER DEFAULT 100, mood INTEGER DEFAULT 70, stamina INTEGER DEFAULT 90,
  hp INTEGER DEFAULT 100,
  title TEXT DEFAULT '无名之辈', flags TEXT DEFAULT '{}',
  created_at REAL, updated_at REAL,
  PRIMARY KEY (gid, uid)
);
CREATE TABLE IF NOT EXISTS rels (
  gid TEXT NOT NULL,
  a TEXT NOT NULL,
  b TEXT NOT NULL,
  score INTEGER DEFAULT 0,
  note TEXT DEFAULT '',
  state TEXT DEFAULT '',
  crush_by TEXT DEFAULT '',
  PRIMARY KEY (gid, a, b)
);
CREATE TABLE IF NOT EXISTS plans (
  gid TEXT NOT NULL,
  day TEXT NOT NULL,
  items TEXT DEFAULT '[]',
  PRIMARY KEY (gid, day)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gid TEXT, uid TEXT, world_id INTEGER, kind TEXT,
  state TEXT DEFAULT 'pending',
  payload TEXT, chosen INTEGER DEFAULT -1, result TEXT DEFAULT '',
  effects TEXT DEFAULT '{}',
  sent INTEGER DEFAULT 0,
  created_at REAL, expires_at REAL
);
CREATE TABLE IF NOT EXISTS interactions (
  gid TEXT NOT NULL,
  name TEXT NOT NULL,
  descr TEXT DEFAULT '',
  by TEXT DEFAULT '',
  created_at REAL,
  PRIMARY KEY (gid, name)
);
CREATE TABLE IF NOT EXISTS bonds (
  gid TEXT NOT NULL,
  proposer TEXT NOT NULL,
  target TEXT NOT NULL,
  label TEXT NOT NULL,
  status TEXT DEFAULT 'agreed',
  created_at REAL,
  PRIMARY KEY (gid, proposer, target)
);
CREATE TABLE IF NOT EXISTS timeline (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gid TEXT, uid TEXT, ts REAL, kind TEXT, text TEXT, world_name TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_timeline_gu ON timeline (gid, uid, id);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gid TEXT, uid TEXT DEFAULT '', scope TEXT DEFAULT 'char',
  text TEXT, vec BLOB, ref TEXT DEFAULT '',
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_mem_g ON memories (gid, scope);
CREATE TABLE IF NOT EXISTS kv (
  gid TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT DEFAULT '',
  PRIMARY KEY (gid, key)
);
CREATE TABLE IF NOT EXISTS quests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gid TEXT NOT NULL,
  uid TEXT NOT NULL,
  day TEXT NOT NULL,
  text TEXT,
  hint TEXT DEFAULT '',
  state TEXT DEFAULT 'open',
  giver TEXT DEFAULT '',
  place TEXT DEFAULT '',
  steps TEXT DEFAULT '[]',
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_quests_gud ON quests (gid, uid, day);
CREATE TABLE IF NOT EXISTS kb (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gid TEXT NOT NULL,
  source TEXT DEFAULT '',
  theme TEXT DEFAULT '',
  kind TEXT DEFAULT 'work',
  content TEXT,
  vec BLOB,
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_kb_gid ON kb (gid);
CREATE TABLE IF NOT EXISTS reputations (
  gid TEXT NOT NULL,
  uid TEXT NOT NULL,
  world_id INTEGER NOT NULL,
  score INTEGER DEFAULT 0,
  note TEXT DEFAULT '',
  updated_at REAL,
  PRIMARY KEY (gid, uid, world_id)
);
CREATE INDEX IF NOT EXISTS idx_rep_w ON reputations (gid, world_id);
CREATE TABLE IF NOT EXISTS plots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gid TEXT NOT NULL,
  world_id INTEGER NOT NULL,
  bid INTEGER NOT NULL,           -- 地块编号
  kind TEXT DEFAULT '房',         -- 房/公寓/小屋/宅/铺
  name TEXT DEFAULT '',
  desc TEXT DEFAULT '',
  owner_uid TEXT DEFAULT '',      -- 空 = 待售
  price INTEGER DEFAULT 0,
  level INTEGER DEFAULT 0,        -- 建筑等级
  amenities TEXT DEFAULT '{}',
  built_at REAL,
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_plots_w ON plots (gid, world_id);
"""

# 版本化迁移:仅服务于「旧库升级」;新库 BASE_SCHEMA 已是最新,跑一遍也无害。
_MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (1, "关系阶段列(rels.state/crush_by:单恋/恋人/情侣/夫妻)", [
        "ALTER TABLE rels ADD COLUMN state TEXT DEFAULT ''",
        "ALTER TABLE rels ADD COLUMN crush_by TEXT DEFAULT ''",
    ]),
    (2, "世界基建与主线列(worlds.infra/mainline)", [
        "ALTER TABLE worlds ADD COLUMN infra TEXT DEFAULT '[]'",
        "ALTER TABLE worlds ADD COLUMN mainline TEXT DEFAULT '[]'",
    ]),
    (3, "事件投递标记(events.sent:只结算发送过的事件)", [
        "ALTER TABLE events ADD COLUMN sent INTEGER DEFAULT 0",
    ]),
    (4, "自定义搞怪关系表(bonds:爸爸/麻麻/主人/女仆…)", [
        """CREATE TABLE IF NOT EXISTS bonds (
          gid TEXT NOT NULL,
          proposer TEXT NOT NULL,
          target TEXT NOT NULL,
          label TEXT NOT NULL,
          status TEXT DEFAULT 'agreed',
          created_at REAL,
          PRIMARY KEY (gid, proposer, target)
        )""",
    ]),
    (5, "任务改造:委托人/发布设施/多步骤目标列(quests.giver/place/steps)", [
        "ALTER TABLE quests ADD COLUMN giver TEXT DEFAULT ''",
        "ALTER TABLE quests ADD COLUMN place TEXT DEFAULT ''",
        "ALTER TABLE quests ADD COLUMN steps TEXT DEFAULT '[]'",
        "ALTER TABLE quests ADD COLUMN giver TEXT DEFAULT ''",
        "ALTER TABLE quests ADD COLUMN place TEXT DEFAULT ''",
        """CREATE TABLE IF NOT EXISTS items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          gid TEXT NOT NULL,
          uid TEXT NOT NULL,
          name TEXT NOT NULL,
          count INTEGER DEFAULT 1,
          note TEXT DEFAULT '',
          created_at REAL,
          updated_at REAL,
          UNIQUE (gid, uid, name)
        )""",
    ]),
    (6, "生命值/危险区域/治疗物品/声望(chars.hp; worlds.zones/heal_items; reputations)", [
        "ALTER TABLE chars ADD COLUMN hp INTEGER DEFAULT 100",
        "ALTER TABLE worlds ADD COLUMN zones TEXT DEFAULT '[]'",
        "ALTER TABLE worlds ADD COLUMN heal_items TEXT DEFAULT '[]'",
        """CREATE TABLE IF NOT EXISTS reputations (
          gid TEXT NOT NULL,
          uid TEXT NOT NULL,
          world_id INTEGER NOT NULL,
          score INTEGER DEFAULT 0,
          note TEXT DEFAULT '',
          updated_at REAL,
          PRIMARY KEY (gid, uid, world_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_rep_w ON reputations (gid, world_id)",
    ]),
]


def _try_exec(conn: sqlite3.Connection, stmt: str) -> bool:
    """执行单条迁移语句;重复列/已存在的表视为已应用(幂等)。"""
    try:
        conn.execute(stmt)
        return True
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            return False
        raise


def apply_migrations(conn: sqlite3.Connection, log=None) -> list[tuple[int, str]]:
    """建表(最新形态)+ 按版本执行增量迁移。返回本次实际应用的迁移列表。"""
    conn.executescript(BASE_SCHEMA)
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    applied: list[tuple[int, str]] = []
    for version, desc, stmts in _MIGRATIONS:
        if version <= current:
            continue
        for stmt in stmts:
            _try_exec(conn, stmt)
        conn.execute(f"PRAGMA user_version = {int(version)}")
        applied.append((version, desc))
    conn.commit()
    if applied and log:
        for version, desc in applied:
            log(f"数据库迁移 v{version}: {desc}")
    return applied
