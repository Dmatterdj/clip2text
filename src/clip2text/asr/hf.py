"""HuggingFace 镜像支持。

国内网络直连 huggingface.co 经常 SSL 中断，导致模型下载失败。
官方提供 HF_ENDPOINT 环境变量切换镜像，这里做两件事：
  1. 在 import mlx_whisper / faster_whisper 之前把 HF_ENDPOINT 注入环境
  2. 用户没配时，检查模型是否已完整缓存；不完整就给出可操作的提示

镜像用法（写进 .env 或 shell）：
  HF_ENDPOINT=https://hf-mirror.com
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_MIRROR = "https://hf-mirror.com"


def apply_hf_endpoint() -> str | None:
    """把 HF_ENDPOINT 应用到当前进程。返回生效的地址（未设置返回 None）。

    注意：huggingface_hub 在 import 时读取该变量，
    所以必须在 import 引擎库之前调用。
    """
    endpoint = os.environ.get("HF_ENDPOINT", "").strip()
    if endpoint:
        log.debug("HF_ENDPOINT = %s", endpoint)
        return endpoint
    return None


def enable_offline_if_cached(repo_id: str) -> bool:
    """模型已完整缓存时切到离线模式——这是一个 25 倍的性能修复。

    实测（M4，29.7s 中文音频，turbo 模型）：
        联网校验 revision（被墙）  84.5s
        离线模式                    3.4s

    原因：即便权重已在本地，huggingface_hub 每次仍会访问 hub 校验
    refs/main 指向的 revision。网络不通时它会做多轮带退避的重试，
    然后才回落到本地快照——用户看到的就是"第一次转写卡了 80 秒"。

    ⚠️ 调用时机极其重要
        huggingface_hub 在**模块级**就把 HF_HUB_OFFLINE 读成了常量：
            HF_HUB_OFFLINE = _is_true(os.environ.get("HF_HUB_OFFLINE"))
            def is_offline_mode(): return HF_HUB_OFFLINE
        所以必须在本函数之后再 import huggingface_hub / mlx_whisper /
        faster_whisper，否则设环境变量不生效（实测仍然 84s）。
        已经 import 过的情况由 _patch_loaded_constants() 兜底。

    需要强制检查更新时，用户显式设 HF_HUB_OFFLINE=0 即可覆盖。
    """
    if os.environ.get("HF_HUB_OFFLINE") == "0":
        return False  # 用户明确要求联网
    if snapshot_complete(repo_id):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        log.debug("模型 %s 已在本地缓存，启用离线模式", repo_id)
        return True
    return False


def _patch_loaded_constants() -> None:
    """兜底：如果 huggingface_hub 已经被 import，直接改模块常量。

    只在 enable_offline_if_cached() 返回 True 且 hub 已加载时调用。
    """
    import sys

    mod = sys.modules.get("huggingface_hub.constants")
    if mod is not None:
        mod.HF_HUB_OFFLINE = True
    hub = sys.modules.get("huggingface_hub")
    if hub is not None and hasattr(hub, "constants"):
        hub.constants.HF_HUB_OFFLINE = True


def prepare_offline(repo_id: str) -> bool:
    """一步到位：检查缓存 → 设环境变量 → 打补丁。

    必须在 import 任何 hf 相关库之前调用。
    """
    ok = enable_offline_if_cached(repo_id)
    if ok:
        _patch_loaded_constants()
    return ok


def enable_mirror() -> None:
    """显式启用国内镜像（供 CLI --mirror 或环境变量触发）。"""
    os.environ.setdefault("HF_ENDPOINT", DEFAULT_MIRROR)
    log.info("已启用 HuggingFace 镜像: %s", DEFAULT_MIRROR)


def hub_cache_dir() -> Path:
    if env := os.environ.get("HF_HOME"):
        return Path(env) / "hub"
    if env := os.environ.get("HUGGINGFACE_HUB_CACHE"):
        return Path(env)
    return Path.home() / ".cache" / "huggingface" / "hub"


def repo_cache_dir(repo_id: str) -> Path:
    return hub_cache_dir() / ("models--" + repo_id.replace("/", "--"))


# 不同引擎的权重文件名不同：
#   mlx-whisper      -> weights.npz / weights.safetensors
#   faster-whisper   -> model.bin
#   transformers     -> model.safetensors
WEIGHT_FILES = ("weights.npz", "weights.safetensors", "model.bin", "model.safetensors")


def snapshot_complete(repo_id: str) -> bool:
    """判断模型权重是否已完整下载（忽略只下了一半的 .incomplete 文件）。"""
    base = repo_cache_dir(repo_id)
    snapshots = base / "snapshots"
    if not snapshots.is_dir():
        return False
    for snap in snapshots.iterdir():
        if not snap.is_dir():
            continue
        if any((snap / name).exists() for name in WEIGHT_FILES):
            return True
    return False


def download_hint(repo_id: str) -> str:
    """给用户一段可以直接复制的修复命令。"""
    return (
        f"模型 {repo_id} 未完整下载（常见原因：huggingface.co 无法直连）。\n"
        f"  解决办法任选其一：\n"
        f"    1) 使用镜像后重试：export HF_ENDPOINT={DEFAULT_MIRROR}\n"
        f"    2) 换个更小的模型：在 .env 里设 ASR_MODEL=small 或 base\n"
        f"    3) 手动预下载：huggingface-cli download {repo_id} --local-dir-use-symlinks False"
    )
