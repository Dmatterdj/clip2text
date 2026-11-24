"""代码新鲜度自检。

## 为什么需要这个

这个项目用 `pip install -e` 安装，意思是 Python 模块在**进程启动时**载入内存，
而 `web/` 下的静态文件每次请求都从磁盘读。

于是出现了一个反直觉的故障：

    后端代码是旧的（没重启）
    + 前端是新的（静态文件实时读盘）
    = 界面有按钮，点了报 405 Method Not Allowed

它已经发生过三次，每次用户都以为是功能坏了。诊断成本很高，因为
「按钮在、但点了 405」看起来完全不像「服务没重启」。

## 做法

启动时算一次源码指纹 = `loaded`；之后每次请求按需重算 `disk`。
两者不一致 = 内存里跑的是旧代码，需要重启。

界面据此显示一条明确的提示，把「玄学故障」变成「一行警告」。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class BuildInfo:
    loaded: str
    disk: str

    @property
    def restart_required(self) -> bool:
        return self.loaded != self.disk


# 启动时算一次的指纹（此时磁盘内容 == 已载入内容）
_LOADED: str | None = None

# 磁盘指纹缓存：(文件数, 总字节, 最新 mtime) -> 指纹
_disk_cache: tuple[tuple[int, int, float], str] | None = None


def _source_files() -> list[Path]:
    """所有 .py 文件。

    不递归进 web/（纯静态资源，改了不需要重启）和 __pycache__。
    """
    out: list[Path] = []
    for p in PACKAGE_DIR.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        out.append(p)
    return sorted(out)


def _fingerprint(files: list[Path]) -> str:
    h = hashlib.blake2b(digest_size=6)
    for path in files:
        try:
            h.update(path.relative_to(PACKAGE_DIR).as_posix().encode())
            h.update(path.read_bytes())
        except OSError:
            # 文件正在被写/删除时跳过，下次请求会重新算
            continue
    return h.hexdigest()


def mark_loaded() -> str:
    """在进程启动时调用，记录「当前载入内存的是哪一版代码」。"""
    global _LOADED
    _LOADED = _fingerprint(_source_files())
    log.debug("已载入代码指纹：%s", _LOADED)
    return _LOADED


def current() -> BuildInfo:
    """返回 loaded / disk 两个指纹。

    disk 用 (文件数, 总字节, 最新 mtime) 做缓存键 —— 只要文件没动就不重算，
    避免每个请求都读一遍全部源码。
    """
    global _disk_cache
    files = _source_files()

    total = 0
    newest = 0.0
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        total += st.st_size
        newest = max(newest, st.st_mtime)

    key = (len(files), total, newest)
    if _disk_cache is not None and _disk_cache[0] == key:
        disk = _disk_cache[1]
    else:
        disk = _fingerprint(files)
        _disk_cache = (key, disk)

    return BuildInfo(loaded=_LOADED or disk, disk=disk)
