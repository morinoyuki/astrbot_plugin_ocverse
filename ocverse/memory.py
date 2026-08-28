"""记忆系统。

三层:
1. timeline(时间线日志)— 结构化流水,人生日志卡片直接读这里
2. memories(语义记忆)— 每次重要事件/互动写入一条摘要,向量检索 top-k 注入 LLM 上下文
3. core(核心记忆)— 长期记忆超量后由 LLM 压缩出的稳定事实(永远优先注入)

scope: char(角色) / world(世界大事) / core(核心记忆) / npc(NPC 相关)
"""

from __future__ import annotations
import re

from .db import Database
from .embedder import HashEmbedder, cosine

SCOPES_ALL = ["char", "world", "core", "npc"]


class MemoryStore:
    def __init__(self, db: Database, embedder, fallback: HashEmbedder, top_k: int = 6):
        self.db = db
        self.embedder = embedder          # 主后端(可能 API)
        self.fallback = fallback          # 哈希回退
        self.top_k = max(1, top_k)

    # ── 写 ─────────────────────────────────────────────────────
    async def remember(self, gid: str, uid: str, scope: str, text: str, ref: str = ""):
        text = (text or "").strip()
        if not text:
            return
        try:
            vec = await self.embedder.embed(text)
        except Exception:
            vec = self.fallback.embed_sync(text)
        # 近重复去重
        try:
            rows = self.db.mem_rows(gid, scopes=[scope])
            for r in rows:
                if r.get("uid") == uid or not uid:
                    if cosine(vec, r["vec"]) > 0.965:
                        return
        except Exception:
            pass
        self.db.mem_add(gid, uid, scope, text, vec, ref)

    # ── 检索 ───────────────────────────────────────────────────
    async def related(self, gid: str, query: str, k: int | None = None,
                      uid: str | None = None, scopes: list[str] | None = None) -> list[str]:
        try:
            qv = await self.embedder.embed(query)
        except Exception:
            qv = self.fallback.embed_sync(query)
        rows = self.db.mem_rows(gid, scopes=scopes or SCOPES_ALL)
        scored: list[tuple[float, str]] = []
        for r in rows:
            if uid and r.get("uid") and r["uid"] != uid:
                continue
            s = cosine(qv, r["vec"])
            if s > 0.05:
                scored.append((s, r["text"]))
        scored.sort(key=lambda x: -x[0])
        return [t for _, t in scored[: k or self.top_k]]

    def related_by_keyword(self, gid: str, query: str, k: int = 6) -> list[dict]:
        """命令式检索(「回忆」指令):哈希向量 + 关键词命中加权。"""
        fb = self.fallback
        qv = fb.embed_sync(query)
        rows = self.db.mem_rows(gid)
        out: list[tuple[float, str, str]] = []
        qlow = query.lower()
        for r in rows:
            s = cosine(qv, r["vec"])
            t = r["text"]
            if qlow and qlow in t.lower():
                s += 0.35
            out.append((s, t, r.get("scope", "char")))
        out.sort(key=lambda x: -x[0])
        return [{"score": round(s, 3), "text": t, "scope": sc} for s, t, sc in out[:k] if s > 0.02]

    # ── 上下文注入 ─────────────────────────────────────────────
    async def context_block(self, gid: str, query: str, uid: str | None = None) -> str:
        lines = await self.related(gid, query, uid=uid)
        if not lines:
            return ""
        return "相关回忆(供参考,勿逐条复述):\n" + "\n".join(f"- {t}" for t in lines)

    # ── 核心记忆压缩 ───────────────────────────────────────────
    async def compress_if_needed(self, gid: str, uid: str, summarize_fn) -> bool:
        """长期记忆超阈值时,请 LLM 把最旧一批压缩为核心记忆。返回是否执行了压缩。"""  # noqa: ARG002
        # 阈值判断在 game 层做,直接调用 compress_now
        return False

    async def compress_now(self, gid: str, uid: str, keep: int, summarize_fn) -> bool:
        rows = self.db.mem_rows(gid, scopes=["char"])
        mine = [r for r in rows if r.get("uid") == uid]
        mine.sort(key=lambda r: r["created_at"])
        if len(mine) <= keep:
            return False
        old = mine[: len(mine) - keep]
        texts = [r["text"] for r in old]
        try:
            cores = await summarize_fn(uid, texts)
        except Exception:
            return False
        if not cores:
            return False
        for r in old:
            self.db.mem_delete_ids([r["id"]])
        for c in cores[:6]:
            await self.remember(gid, uid, "core", c)
        return True


class KnowledgeStore:
    """群级素材知识库:联网/LLM 采集的著作·轻小说等,供所有生成功能注入素材。

    与记忆(memories)区分:KB 长期保留、不压缩、不随角色删除,作为"创作素材库"。
    语义检索复用 embedder + 哈希回退。
    """

    def __init__(self, db, embedder, fallback, top_k: int = 3, max_items: int = 40):
        self.db = db
        self.embedder = embedder
        self.fallback = fallback
        self.top_k = max(1, top_k)
        self.max_items = max(1, max_items)

    async def _vec(self, text: str) -> list[float]:
        try:
            return await self.embedder.embed(text)
        except Exception:
            return self.fallback.embed_sync(text)

    async def add(self, gid: str, source: str, theme: str, kind: str, content: str):
        text = (content or "").strip()
        if not text:
            return None
        # 近重复去重
        try:
            v = await self._vec(text)
            for r in self.db.kb_rows(gid):
                if r.get("content") and _cosine_field(v, r["vec"]) > 0.92:
                    return None
        except Exception:
            return None
        nid = self.db.kb_add(gid, source, theme, kind, text, v)
        # 达到上限后淘汰最旧条目,给新素材腾位置(按 id 升序即入库先后)
        if self.db.kb_count(gid) > self.max_items:
            self.db.kb_trim(gid, keep=self.max_items)
        return nid

    async def related(self, gid: str, query: str, k: int | None = None) -> list[dict]:
        try:
            qv = await self._vec(query)
        except Exception:
            return []
        rows = self.db.kb_rows(gid)
        # 哈希向量稀疏:查询去空格后的单字+双字片段在正文出现即加权,补足召回
        q = re.sub(r"\s+", "", query or "")
        qs = (set(q) | {q[i:i + 2] for i in range(len(q) - 1)}) if len(q) > 1 else set(q)
        scored = []
        for r in rows:
            s = _cosine_field(qv, r["vec"])
            c = str(r.get("content") or "")
            hit = sum(1 for t in qs if t and t in c)
            if hit:
                s += 0.25 + 0.15 * min(hit, 3)
            if s > 0.02:
                scored.append((s, r))
        scored.sort(key=lambda x: -x[0])
        return scored[: (k or self.top_k)]

    async def context(self, gid: str, query: str, k: int = 3) -> str:
        """取相关素材拼成可直接注入 prompt 的文本块(空库返回空串)。"""
        items = await self.related(gid, query, k)
        if not items:
            return ""
        lines = []
        for _, r in items:
            tag = f"{r['theme']}·《{r['source']}》" if r.get("source") else (r.get("theme") or "")
            head = f"- {tag}: " if tag else "- "
            lines.append(head + str(r.get("content"))[:260])
        return ("\n\n【知识库素材】(可借鉴其氛围/手法/设定,但不要直接照搬人名或原剧情):\n"
                + "\n".join(lines))


def _cosine_field(a: list[float] | None, b: list[float] | None) -> float:
    """维度不一致视为不相关(换后端后旧向量自然淡出)。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b)) or 0.0
