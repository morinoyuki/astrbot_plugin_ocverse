"""分身头像存取:avatars/group_<gid>/<分身名>.png。

线程安全、原子写、路径净化。渲染时按群 scope + 角色名取图。
(参考作者 life_sim 的 avatar_store 实现,裁剪为本插件所需的最小集。)
"""

from __future__ import annotations

import io
import os
import threading
from urllib.parse import quote, unquote

from PIL import Image

_ALLOWED_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


class AvatarStore:
    def __init__(self, data_dir: str, subdir: str = "avatars"):
        self.base = os.path.join(data_dir, subdir)
        os.makedirs(self.base, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, name: str) -> threading.Lock:
        with self._guard:
            if name not in self._locks:
                self._locks[name] = threading.Lock()
            return self._locks[name]

    @staticmethod
    def _sanitize_scope(scope: str) -> str:
        c = str(scope).replace("/", "_").replace("\\", "_").strip().rstrip(".:")
        if not c or c in (".", ".."):
            return ""
        return c[:64]

    @staticmethod
    def _sanitize_name(name: str) -> str:
        name = (name or "").strip().rstrip('\\/:*?"<>|')
        return name[:48] if name else ""

    def _scope_dir(self, scope: str) -> str:
        safe = self._sanitize_scope(scope)
        d = os.path.join(self.base, safe) if safe else self.base
        os.makedirs(d, exist_ok=True)
        return d

    def _path_for(self, scope: str, name: str) -> str:
        return os.path.join(self._scope_dir(scope), quote(name, safe="") + ".png")

    def save_avatar(self, scope: str, name: str, image_bytes: bytes) -> str | None:
        name = self._sanitize_name(name)
        if not name or not image_bytes:
            return None
        path = self._path_for(scope, name)
        with self._lock(scope + "::" + name):
            tmp = ""
            try:
                img = Image.open(io.BytesIO(image_bytes))
                img.load()
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
                tmp = path + f".{os.getpid()}.tmp"
                img.save(tmp, "PNG")
                os.replace(tmp, path)
                return path
            except Exception:
                try:
                    if tmp and os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                return None

    def save_from_pil(self, scope: str, name: str, image: Image.Image) -> str | None:
        buf = io.BytesIO()
        try:
            image.convert("RGBA").save(buf, "PNG")
        except Exception:
            return None
        return self.save_avatar(scope, name, buf.getvalue())

    def get_avatar(self, scope: str, name: str) -> str | None:
        name = self._sanitize_name(name)
        if not name:
            return None
        d = self._scope_dir(scope)
        stem = quote(name, safe="")
        try:
            # 只做精确文件名命中(标准落盘名 + 兼容历史后缀),不做前缀匹配,
            # 避免「凛」取到/删掉「凛子」的头像(前缀撞名)
            for cand in [stem] + [stem + ext for ext in _ALLOWED_EXT]:
                p = os.path.join(d, cand)
                if os.path.isfile(p):
                    return p
        except OSError:
            pass
        return None

    def delete(self, scope: str, name: str) -> bool:
        name = self._sanitize_name(name)
        if not name:
            return False
        p = self.get_avatar(scope, name)
        if not p:
            return False
        try:
            os.remove(p)
            return True
        except OSError:
            return False

    def list_names(self, scope: str) -> list[str]:
        d = self._scope_dir(scope)
        out = []
        try:
            for f in sorted(os.listdir(d)):
                if f.endswith(_ALLOWED_EXT):
                    try:
                        out.append(unquote(os.path.splitext(f)[0]))
                    except ValueError:
                        continue
        except OSError:
            pass
        return out
