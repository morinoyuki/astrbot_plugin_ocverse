"""记忆系统。

三层:
1. timeline(时间线日志)— 结构化流水,人生日志卡片直接读这里
2. memories(语义记忆)— 每次重要事件/互动写入一条摘要,向量检索 top-k 注入 LLM 上下文
3. core(核心记忆)— 长期记忆超量后由 LLM 压缩出的稳定事实(永远优先注入)

scope: char(角色) / world(世界大事) / core(核心记忆) / npc(NPC 相关)
"""

from __future__ import annotations

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
