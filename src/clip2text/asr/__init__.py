"""引擎注册与自动选择。

auto 的优先级（越靠前越优先）：
  mlx（Apple Silicon，最快）
  > faster-whisper（通用）
  > deepseek（云，需 key）

永远不会因为「装了某个包」就自动切到云；
云引擎只有在显式配置 key 且本机没有本地引擎时才会被选中。
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..models import EngineStatus
from .base import ASREngine, ASRError, BaseEngine
from .deepseek_engine import DeepSeekASREngine
from .faster_whisper_engine import FasterWhisperEngine
from .mlx_engine import MLXWhisperEngine

log = logging.getLogger(__name__)

__all__ = [
    "ASREngine",
    "ASRError",
    "BaseEngine",
    "DeepSeekASREngine",
    "FasterWhisperEngine",
    "MLXWhisperEngine",
    "build_engines",
    "select_engine",
    "engine_statuses",
]

LOCAL_ORDER = ("mlx", "faster-whisper")


def build_engines(settings: Settings) -> dict[str, ASREngine]:
    """构造所有引擎实例（不做实际加载，只探测可用性）。"""
    return {
        "mlx": MLXWhisperEngine(model=settings.asr_model),
        "faster-whisper": FasterWhisperEngine(model=settings.asr_model),
        "deepseek": DeepSeekASREngine(
            api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url
        ),
    }


def select_engine(settings: Settings, engines: dict[str, ASREngine] | None = None) -> ASREngine:
    """按配置与可用性挑一个引擎。挑不到抛 ASRError 并给出可操作的提示。"""
    engines = engines or build_engines(settings)
    wanted = settings.asr_engine.lower()

    if wanted != "auto":
        key = "faster-whisper" if wanted == "faster" else wanted
        engine = engines.get(key)
        if engine is None:
            raise ASRError(f"未知的 ASR 引擎: {settings.asr_engine}")
        if not engine.available():
            raise ASRError(
                f"指定的引擎 {key} 不可用：{engine.unavailable_reason() or '原因未知'}"
            )
        return engine

    for key in LOCAL_ORDER:
        eng = engines.get(key)
        if eng is not None and eng.available():
            log.info("自动选择本地 ASR 引擎: %s", key)
            return eng

    cloud = engines.get("deepseek")
    if cloud is not None and cloud.available():
        log.info("本地引擎均不可用，改用云 ASR: deepseek")
        return cloud

    reasons = {
        k: (e.unavailable_reason() or "不可用") for k, e in engines.items()
    }
    detail = "; ".join(f"{k}: {v}" for k, v in reasons.items())
    raise ASRError(
        "没有任何可用的 ASR 引擎。安装方式示例："
        "`pip install 'clip2text[faster]'` 或 `pip install 'clip2text[mlx]'`，"
        "或配置 DEEPSEEK_API_KEY。当前状态：" + detail
    )


def engine_statuses(settings: Settings, engines: dict[str, ASREngine] | None = None) -> list[EngineStatus]:
    engines = engines or build_engines(settings)
    out: list[EngineStatus] = []
    for key, eng in engines.items():
        available = eng.available()
        out.append(
            EngineStatus(
                name=key,
                available=available,
                reason=None if available else (eng.unavailable_reason() or "不可用"),
                timestamps=getattr(eng, "supports_timestamps", False),
                max_chunk_sec=getattr(eng, "max_chunk_sec", None),
            )
        )
    return out
