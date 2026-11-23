"""ffmpeg 定位与封装。

三级策略（FFMPEG_MODE）：
  system  —— 只用系统 PATH 里的 ffmpeg，找不到就报错
  static  —— 只从 static-ffmpeg 下载/复用，不碰系统
  auto    —— 先试系统，再用 static-ffmpeg 兜底（默认）

这样用户机器上没有 ffmpeg 也能跑起来，不必先 `brew install`。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class FFmpegError(RuntimeError):
    pass


@dataclass(frozen=True)
class FFmpegBin:
    ffmpeg: str
    ffprobe: str
    source: str  # "system" | "static"

    def as_dict(self) -> dict[str, str]:
        return {"ffmpeg": self.ffmpeg, "ffprobe": self.ffprobe, "source": self.source}


_resolved: FFmpegBin | None = None
_resolve_error: str | None = None


def _try_system() -> FFmpegBin | None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return FFmpegBin(ffmpeg=ffmpeg, ffprobe=ffprobe, source="system")
    return None


def _try_static() -> FFmpegBin | None:
    try:
        from static_ffmpeg import run  # 延迟导入：不安装也能启动服务
    except ImportError:
        log.debug("static-ffmpeg 未安装")
        return None
    try:
        ffmpeg, ffprobe = run.get_or_fetch_platform_executables_else_raise()
        return FFmpegBin(ffmpeg=ffmpeg, ffprobe=ffprobe, source="static")
    except Exception as exc:  # noqa: BLE001 - 下载失败原因很多，统一降级
        log.warning("static-ffmpeg 获取失败: %s", exc)
        return None


def resolve_ffmpeg(mode: str = "auto") -> FFmpegBin | None:
    """解析 ffmpeg。结果缓存；失败原因记录在 get_resolve_error()。"""
    global _resolved, _resolve_error
    if _resolved is not None:
        return _resolved
    if _resolve_error is not None:
        return None

    candidates = {
        "system": [_try_system],
        "static": [_try_static],
        "auto": [_try_system, _try_static],
    }.get(mode.lower(), [_try_system, _try_static])

    for fn in candidates:
        got = fn()
        if got is not None:
            _resolved = got
            log.info("ffmpeg 就绪: source=%s path=%s", got.source, got.ffmpeg)
            return got

    _resolve_error = f"未找到 ffmpeg（mode={mode}）。请安装 ffmpeg 或 `pip install static-ffmpeg`"
    return None


def get_resolve_error() -> str | None:
    return _resolve_error


def reset_ffmpeg_cache() -> None:
    global _resolved, _resolve_error
    _resolved = None
    _resolve_error = None


def require_ffmpeg(mode: str = "auto") -> FFmpegBin:
    got = resolve_ffmpeg(mode)
    if got is None:
        raise FFmpegError(get_resolve_error() or "ffmpeg 不可用")
    return got


def ensure_ffmpeg_on_path(mode: str = "auto") -> FFmpegBin | None:
    """把已解析的 ffmpeg 目录加到 PATH 最前面。

    必要性：mlx-whisper 在内部直接 `subprocess.run(["ffmpeg", ...])` 读取音频，
    不认我们解析出的绝对路径。static-ffmpeg 下载的二进制不在 PATH 里，
    不注入就会报 `FileNotFoundError: 'ffmpeg'`。
    """
    got = resolve_ffmpeg(mode)
    if got is None:
        return None
    bin_dir = str(Path(got.ffmpeg).parent)
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    if bin_dir not in parts:
        os.environ["PATH"] = os.pathsep.join([bin_dir, *parts])
        log.debug("已把 %s 注入 PATH", bin_dir)
    return got


# ---------------- 媒体操作 ----------------


def probe_duration(media: Path, mode: str = "auto") -> float | None:
    """用 ffprobe 读取时长（秒）。失败返回 None，不抛异常。"""
    if not media.exists():
        return None
    binary = resolve_ffmpeg(mode)
    if binary is None:
        return None
    cmd = [
        binary.ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(media),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
        data = json.loads(out.stdout or "{}")
        dur = data.get("format", {}).get("duration")
        return float(dur) if dur is not None else None
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError) as exc:
        log.warning("probe_duration 失败: %s", exc)
        return None


def extract_audio(
    src: Path,
    dst: Path,
    *,
    mode: str = "auto",
    sample_rate: int = 16000,
    timeout: int = 1800,
) -> Path:
    """抽出 16k 单声道 wav —— ASR 的标准输入。

    -vn 去掉视频轨；-ac 1 单声道；-ar 16000 是 whisper / 多数云 ASR 的最佳采样率。
    """
    binary = require_ffmpeg(mode)
    if not src.exists():
        raise FFmpegError(f"源文件不存在: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        binary.ffmpeg,
        "-y",
        "-i", str(src),
        "-vn",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
        "-f", "wav",
        str(dst),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[-800:]
        raise FFmpegError(f"抽取音频失败: {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"抽取音频超时（{timeout}s）") from exc
    if not dst.exists() or dst.stat().st_size == 0:
        raise FFmpegError(f"抽取音频后文件为空: {dst}")
    return dst


def to_ogg_opus(src: Path, dst: Path, *, mode: str = "auto", timeout: int = 600) -> Path:
    """转成 Ogg Opus —— DeepSeek ASR 支持的另一种格式，体积远小于 wav。"""
    binary = require_ffmpeg(mode)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        binary.ffmpeg, "-y", "-i", str(src),
        "-vn", "-ac", "1", "-c:a", "libopus", "-b:a", "32k",
        str(dst),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
    except subprocess.CalledProcessError as exc:
        raise FFmpegError((exc.stderr or "")[-800:]) from exc
    return dst


def slice_audio(
    src: Path,
    dst: Path,
    *,
    start: float,
    duration: float,
    mode: str = "auto",
    timeout: int = 600,
) -> Path:
    """按时间区间切片，保持 wav 格式。"""
    binary = require_ffmpeg(mode)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        binary.ffmpeg, "-y",
        "-ss", f"{start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(src),
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        "-f", "wav",
        str(dst),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
    except subprocess.CalledProcessError as exc:
        raise FFmpegError((exc.stderr or "")[-800:]) from exc
    return dst
