"""ffmpeg 定位与媒体操作（对外统一出口）。"""

from .chunker import Chunk, split_audio
from .ffmpeg import (
    FFmpegBin,
    FFmpegError,
    ensure_ffmpeg_on_path,
    extract_audio,
    get_resolve_error,
    probe_duration,
    require_ffmpeg,
    reset_ffmpeg_cache,
    resolve_ffmpeg,
    slice_audio,
    to_ogg_opus,
)

__all__ = [
    "Chunk",
    "FFmpegBin",
    "FFmpegError",
    "ensure_ffmpeg_on_path",
    "extract_audio",
    "get_resolve_error",
    "probe_duration",
    "require_ffmpeg",
    "reset_ffmpeg_cache",
    "resolve_ffmpeg",
    "slice_audio",
    "split_audio",
    "to_ogg_opus",
]
