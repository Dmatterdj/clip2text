"""mlx-whisper 引擎：Apple Silicon 专用，速度最快。

只在 arm64 macOS 上可用；模型走 HuggingFace 缓存（~/.cache/huggingface）。
"""

from __future__ import annotations

import logging
import platform
from pathlib import Path

from ..models import Segment, Transcript
from .base import ASRError, BaseEngine

log = logging.getLogger(__name__)

# mlx 的转录结果按段返回，段内已带时间戳
MODEL_ALIASES = {
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "tiny": "mlx-community/whisper-tiny-mlx",
}


class MLXWhisperEngine(BaseEngine):
    name = "mlx"
    supports_timestamps = True
    max_chunk_sec = None

    def __init__(self, model: str = "large-v3-turbo") -> None:
        self.model_name = MODEL_ALIASES.get(model, model)
        self._import_error: str | None = None
        try:
            import mlx_whisper  # noqa: F401
        except ImportError as exc:
            self._import_error = f"未安装 mlx-whisper：{exc}"

    def available(self) -> bool:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            return False
        if self._import_error is not None:
            return False
        return True

    def unavailable_reason(self) -> str | None:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            return "mlx-whisper 仅支持 Apple Silicon macOS"
        return self._import_error

    def transcribe(self, audio: Path, language: str | None = None) -> Transcript:
        if not self.available():
            raise ASRError(self.unavailable_reason() or "mlx-whisper 不可用")

        from ..media.ffmpeg import ensure_ffmpeg_on_path
        from .hf import apply_hf_endpoint, download_hint, snapshot_complete

        # 必须在 import mlx_whisper 之前设置，huggingface_hub 是在 import 时读的
        apply_hf_endpoint()
        import mlx_whisper

        # mlx_whisper 内部直接调 "ffmpeg" 读音频，不认绝对路径。
        # static-ffmpeg 的 bin 目录不在 PATH 里，必须先注入，否则报 FileNotFoundError。
        ensure_ffmpeg_on_path()

        # 先查缓存是否完整：残缺快照会让 mlx_whisper 抛难以理解的异常
        if not snapshot_complete(self.model_name):
            raise ASRError(download_hint(self.model_name))

        try:
            result = mlx_whisper.transcribe(
                str(audio),
                path_or_hf_repo=self.model_name,
                language=language,
                word_timestamps=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise ASRError(f"mlx-whisper 转写失败: {exc}") from exc

        segments = [
            Segment(
                start=float(s.get("start", 0.0)),
                end=float(s.get("end", 0.0)),
                text=(s.get("text") or "").strip(),
            )
            for s in result.get("segments", [])
            if (s.get("text") or "").strip()
        ]
        lang = result.get("language") or language
        text = "".join(s.text for s in segments) if _is_cjk(lang) else " ".join(s.text for s in segments)
        return Transcript(text=text, segments=segments, language=lang)


def _is_cjk(lang: str | None) -> bool:
    return (lang or "").lower() in {"zh", "ja", "ko", "yue"}
