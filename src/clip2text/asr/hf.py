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
