"""轻量语义向量。

后端(按配置选择):
- astrbot(默认): 使用 AstrBot 内置的 Embedding 提供商(WebUI 中配置),
  经 context.get_all_embedding_providers() 获取;失败自动回退哈希向量
- hash: 零依赖「字符 n-gram 哈希词向量」——纯 Python 计算,
  不加载任何模型,NAS 上开销可忽略

注意:不同后端维度不同,记忆条目按各自实际维度存库;
检索时仅对同维度向量计算余弦(换后端后旧记忆自然淡出,不影响运行)。
"""

from __future__ import annotations

import asyncio
import math
import zlib

DIM = 256


def _hash_ngram(token: str, salt: str) -> int:
    return zlib.crc32(f"{salt}|{token}".encode("utf-8")) % DIM


class HashEmbedder:
    """字符 bigram + 词语哈希特征向量。短文本(记忆条目)相似度足够好用。"""

    name = "hash"

    def _tokens(self, text: str) -> list[str]:
        t = (text or "").strip()
        if not t:
            return []
        toks: list[str] = []
        buf = ""
        for ch in t:
            if ch.isascii() and (ch.isalnum() or ch in "_-"):
                buf += ch
            else:
                if buf:
                    toks.append(buf.lower())
                    buf = ""
                if not ch.isspace():
                    toks.append(ch)
        if buf:
            toks.append(buf.lower())
        bigrams = [t[i] + t[i + 1] for i in range(len(t) - 1)] if len(t) > 1 else []
        return toks + bigrams

    def embed_sync(self, text: str) -> list[float]:
        vec = [0.0] * DIM
        toks = self._tokens(text)
        if not toks:
            return vec
        sw = {"的", "了", "是", "在", "我", "他", "她", "它", "和", "与", "也", "就", "都"}
        for tok in toks:
            if tok in sw:
                continue
            i1 = _hash_ngram(tok, "a")
            sign = 1.0 if (zlib.crc32(tok.encode()) >> 1) % 2 == 0 else -1.0
            vec[i1] += sign
            i2 = _hash_ngram(tok, "b")
            vec[i2] += sign * 0.5
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    async def embed(self, text: str) -> list[float]:
        return self.embed_sync(text)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0  # 维度不同(换了后端)的旧记忆视为不相关
    return sum(x * y for x, y in zip(a, b))


class AstrBotEmbedder:
    """包装 AstrBot 内置 Embedding 提供商(WebUI 中配置,自动选用第一个可用的)。"""

    name = "astrbot"

    def __init__(self, get_providers):
        """get_providers: () -> list[EmbeddingProvider](由 main 注入 context.get_all_embedding_providers)"""
        self._get_providers = get_providers
        self._provider = None
        self._tried = False
        self._broken = False  # 熔断:提供商超时/出错后不再重试,本会话直接走哈希

    def _provider_ok(self):
        if self._broken:
            return None
        if self._tried:
            return self._provider
        self._tried = True
        try:
            provs = self._get_providers() or []
            self._provider = provs[0] if provs else None
        except Exception:
            self._provider = None
        return self._provider

    async def embed(self, text: str) -> list[float]:
        prov = self._provider_ok()
        if prov is None:
            raise RuntimeError("无可用 Embedding 提供商")
        try:
            # 严格超时:提供商挂起(NAS 网络问题/配置错误)时快速回退,不卡住业务
            vec = await asyncio.wait_for(
                prov.get_embedding((text or "").strip()[:2000]), timeout=10.0
            )
        except Exception:
            self._broken = True  # 本会话熔断,后续直接走哈希
            raise
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


def make_embedder(cfg_get, get_providers=None):
    """按配置构造 embedding 后端,返回 (主 embedder, 哈希回退 embedder)。

    cfg_get("embedding_backend"):
      - "astrbot"(默认): 优先 AstrBot 内置提供商,不可用时回退哈希
      - "hash": 强制使用零依赖哈希词向量
    """
    fb = HashEmbedder()
    backend = str(cfg_get("embedding_backend", "astrbot") or "astrbot").lower()
    if backend != "hash" and get_providers is not None:
        emb = AstrBotEmbedder(get_providers)
        # 探活:构造期不 await,首次使用时才真正调用;失败由调用方回退
        return emb, fb
    return fb, fb
