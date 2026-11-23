"""faster-whisper 引擎：CPU/GPU 通用，速度与体积平衡最好，默认首选。

模型大小影响首次下载体积（约）：
  tiny 75MB / base 145MB / small 500MB / medium 1.5GB / large-v3-turbo 1.6GB
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from ..models import Segment, Transcript
from .base import ASRError, BaseEngine

log = logging.getLogger(__name__)

# 进程内缓存已加载的模型，避免每次转写都重新加载（加载才是主要开销）
_MODEL_CACHE: dict[tuple[str, str, str], object] = {}
_CACHE_LOCK = threading.Lock()

# faster-whisper 的模型名对应到 HuggingFace 仓库，用于判断本地缓存与离线模式
_HF_REPO_NAMES = {
    "tiny": "Systran/faster-whisper-tiny",
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "base": "Systran/faster-whisper-base",
    "base.en": "Systran/faster-whisper-base.en",
    "small": "Systran/faster-whisper-small",
    "small.en": "Systran/faster-whisper-small.en",
    "medium": "Systran/faster-whisper-medium",
    "medium.en": "Systran/faster-whisper-medium.en",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
}


class FasterWhisperEngine(BaseEngine):
    name = "faster-whisper"
    supports_timestamps = True
    max_chunk_sec = None  # 本地无硬限制，但上层仍会按分片跑以显示进度

    def __init__(self, model: str = "large-v3-turbo", device: str | None = None) -> None:
        self.model_name = model
        self.device = device or ("cuda" if _cuda_available() else "cpu")
        self.compute_type = "float16" if self.device == "cuda" else "int8"
        self._import_error: str | None = None
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            self._import_error = f"未安装 faster-whisper：{exc}"

    def available(self) -> bool:
        return self._import_error is None

    def unavailable_reason(self) -> str | None:
        return self._import_error

    def _load(self):
        from faster_whisper import WhisperModel

        key = (self.model_name, self.device, self.compute_type)
        with _CACHE_LOCK:
            model = _MODEL_CACHE.get(key)
            if model is None:
                log.info("加载 faster-whisper 模型 %s (%s/%s)", *key)
                model = WhisperModel(
                    self.model_name, device=self.device, compute_type=self.compute_type
                )
                _MODEL_CACHE[key] = model
            return model

    def transcribe(self, audio: Path, language: str | None = None) -> Transcript:
        if not self.available():
            raise ASRError(self.unavailable_reason() or "faster-whisper 不可用")

        # 与 mlx 引擎同样的顺序要求：HF 环境变量必须在 import 前设好，
        # 否则已缓存的模型每次仍会联网校验 revision（国内网络下白等几十秒）。
        from .hf import apply_hf_endpoint, prepare_offline

        apply_hf_endpoint()
        if self.model_name in _HF_REPO_NAMES:
            prepare_offline(_HF_REPO_NAMES[self.model_name])

        model = self._load()
        try:
            seg_iter, info = model.transcribe(
                str(audio),
                language=language,
                vad_filter=True,
                beam_size=5,
            )
            segments: list[Segment] = []
            parts: list[str] = []
            for s in seg_iter:
                text = (s.text or "").strip()
                if not text:
                    continue
                segments.append(Segment(start=float(s.start), end=float(s.end), text=text))
                parts.append(text)
        except Exception as exc:  # noqa: BLE001 - 引擎内部异常类型不稳定
            raise ASRError(f"faster-whisper 转写失败: {exc}") from exc

        return Transcript(
            text="".join(parts) if _is_cjk(info.language) else " ".join(parts),
            segments=segments,
            language=getattr(info, "language", None),
        )


def _cuda_available() -> bool:
    if os.environ.get("C2T_FORCE_CPU"):
        return False
    try:
        import torch  # 可选依赖，没装就走 CPU
    except ImportError:
        return False
    return bool(torch.cuda.is_available())


def _is_cjk(lang: str | None) -> bool:
    return (lang or "").lower() in {"zh", "ja", "ko", "yue"}
