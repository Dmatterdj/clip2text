"""音频分片。

为什么必须分片（不是性能优化，是硬约束）：
  - DeepSeek ASR 单次音频 ≤ 120 秒
  - 多数云 ASR 单段 60s–5min
  - 长音频一次性丢给本地 whisper 也会失去进度显示与断点续跑能力

策略：优先在静音处切（避免把一句话切成两半），
静音点不足时按固定时长硬切。
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .ffmpeg import FFmpegError, require_ffmpeg

log = logging.getLogger(__name__)

# ffmpeg silencedetect 的输出形如：
# [silencedetect @ ...] silence_start: 12.3456
# [silencedetect @ ...] silence_end: 13.789 | silence_duration: 1.4434
_SILENCE_START = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)")


@dataclass(frozen=True)
class Chunk:
    index: int
    start: float
    end: float
    path: Path

    @property
    def duration(self) -> float:
        return self.end - self.start


def _detect_silences(
    wav: Path, *, mode: str = "auto", noise_db: int = -35, min_silence: float = 0.35
) -> list[tuple[float, float]]:
    """返回静音区间 [(start, end), ...]，失败返回空列表。"""
    binary = require_ffmpeg(mode)
    cmd = [
        binary.ffmpeg, "-hide_banner", "-nostats",
        "-i", str(wav),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("静音检测失败，将按固定时长切分: %s", exc)
        return []
    out = (proc.stderr or "") + (proc.stdout or "")
    starts = [float(m) for m in _SILENCE_START.findall(out)]
    ends = [float(m) for m in _SILENCE_END.findall(out)]
    # 某个静音段没有 end（音频结尾就是静音）时，用一个大值占位
    pairs = list(zip(starts, ends + [float("inf")] * (len(starts) - len(ends))))
    return pairs


def _plan_boundaries(
    total: float, silences: list[tuple[float, float]], max_sec: float
) -> list[tuple[float, float]]:
    """在静音区间内选切点，保证每段 ≤ max_sec 且尽量落在句间。"""
    if total <= 0:
        return []
    if total <= max_sec:
        return [(0.0, total)]

    # 静音区间的中点作为候选切点
    candidates = sorted(
        (s + min(e, total)) / 2 for s, e in silences if s < total and (s + min(e, total)) > 0
    )

    boundaries: list[float] = [0.0]
    cursor = 0.0
    while total - cursor > max_sec:
        ideal = cursor + max_sec
        # 在 [cursor + max_sec*0.5, ideal] 里找最靠后的候选点
        window_lo = cursor + max_sec * 0.5
        usable = [c for c in candidates if window_lo <= c <= ideal and c > cursor]
        cut = max(usable) if usable else ideal
        boundaries.append(cut)
        cursor = cut
    boundaries.append(total)

    return [
        (boundaries[i], boundaries[i + 1])
        for i in range(len(boundaries) - 1)
        if boundaries[i + 1] - boundaries[i] > 0.05
    ]


def split_audio(
    wav: Path,
    out_dir: Path,
    *,
    total_duration: float,
    max_sec: float = 110.0,
    mode: str = "auto",
) -> list[Chunk]:
    """把 wav 切成若干 Chunk。总时长 ≤ max_sec 时返回单块（不复制文件）。"""
    out_dir.mkdir(parents=True, exist_ok=True)

    if total_duration <= max_sec:
        return [Chunk(index=0, start=0.0, end=total_duration, path=wav)]

    silences = _detect_silences(wav, mode=mode)
    plan = _plan_boundaries(total_duration, silences, max_sec)
    if not plan:
        raise FFmpegError("无法规划分片点")

    chunks: list[Chunk] = []
    for i, (start, end) in enumerate(plan):
        dst = out_dir / f"chunk_{i:04d}.wav"
        try:
            _slice(wav, dst, start=start, duration=end - start, mode=mode)
        except FFmpegError as exc:
            raise FFmpegError(f"切分第 {i} 段失败（{start:.1f}-{end:.1f}）: {exc}") from exc
        chunks.append(Chunk(index=i, start=start, end=end, path=dst))
    log.info("音频切分为 %d 段（总时长 %.1fs，单段上限 %.0fs）", len(chunks), total_duration, max_sec)
    return chunks


def _slice(src: Path, dst: Path, *, start: float, duration: float, mode: str = "auto") -> Path:
    binary = require_ffmpeg(mode)
    cmd = [
        binary.ffmpeg, "-y",
        "-ss", f"{start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(src),
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav",
        str(dst),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=True)
    except subprocess.CalledProcessError as exc:
        raise FFmpegError((exc.stderr or "")[-600:]) from exc
    return dst
