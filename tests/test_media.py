"""媒体层测试：ffmpeg 定位、抽音频、分片。

依赖真实 ffmpeg（auto 模式会自动下载 static 版）。
如果没有网络且系统无 ffmpeg，这些测试会跳过而不是失败。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clip2text.media import (
    FFmpegError,
    extract_audio,
    probe_duration,
    resolve_ffmpeg,
)
from clip2text.media.chunker import Chunk, _plan_boundaries, split_audio


@pytest.fixture(scope="module")
def ff():
    b = resolve_ffmpeg("auto")
    if b is None:
        pytest.skip("本机没有可用的 ffmpeg")
    return b


@pytest.fixture(scope="module")
def tone(ff, tmp_path_factory) -> Path:
    """生成 3 秒 440Hz 正弦波。"""
    d = tmp_path_factory.mktemp("audio")
    out = d / "tone.wav"
    subprocess.run(
        [ff.ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-ac", "1", "-ar", "16000", str(out)],
        capture_output=True, check=True,
    )
    return out


@pytest.fixture(scope="module")
def speech_like(ff, tmp_path_factory) -> Path:
    """生成 12 秒音频：3s 有声 + 2s 静音 + 3s 有声 + 2s 静音 + 2s 有声。

    用 concat 滤镜拼接 5 段，验证静音感知分片能落在静音区。
    """
    d = tmp_path_factory.mktemp("audio2")
    out = d / "gapped.wav"
    # 分别生成 5 个片再 concat 最稳，单条 filter_complex 串 anullsrc 容易踩格式坑
    parts = []
    plan = [(440, 3.0), (None, 2.0), (440, 3.0), (None, 2.0), (440, 2.0)]
    for i, (freq, dur) in enumerate(plan):
        p = d / f"p{i}.wav"
        src = (
            f"sine=frequency={freq}:duration={dur}"
            if freq
            else f"anullsrc=r=16000:cl=mono:d={dur}"
        )
        subprocess.run(
            [ff.ffmpeg, "-y", "-f", "lavfi", "-i", src,
             "-ac", "1", "-ar", "16000", "-t", str(dur), str(p)],
            capture_output=True, check=True,
        )
        parts.append(p)

    listfile = d / "list.txt"
    listfile.write_text("".join(f"file '{p.name}'\n" for p in parts))
    subprocess.run(
        [ff.ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-ac", "1", "-ar", "16000", str(out)],
        capture_output=True, check=True, cwd=d,
    )
    return out


# ---------------------------------------------------------------- ffmpeg
def test_resolve_ffmpeg(ff):
    assert Path(ff.ffmpeg).exists()
    assert ff.source in {"system", "static"}


def test_probe_duration(tone):
    dur = probe_duration(tone)
    assert dur == pytest.approx(3.0, abs=0.1)


def test_probe_duration_missing_file(tmp_path):
    assert probe_duration(tmp_path / "nope.wav") is None


def test_extract_audio(tone, tmp_path):
    out = extract_audio(tone, tmp_path / "out.wav")
    assert out.exists()
    # 16k 单声道 16bit，3 秒 ≈ 96KB
    assert 80_000 < out.stat().st_size < 120_000


def test_extract_audio_missing_source(tmp_path):
    with pytest.raises(FFmpegError, match="源文件不存在"):
        extract_audio(tmp_path / "nope.mp4", tmp_path / "o.wav")


# ---------------------------------------------------------------- 分片
def test_plan_boundaries_short_audio_no_split():
    assert _plan_boundaries(50.0, [], max_sec=110) == [(0.0, 50.0)]


def test_plan_boundaries_splits_when_too_long():
    plan = _plan_boundaries(300.0, [], max_sec=100)
    assert len(plan) >= 3
    for start, end in plan:
        assert end - start <= 100.01


def test_plan_boundaries_prefers_silence():
    # 120 秒音频、上限 60 秒；静音在 55-58 秒处，
    # 切点应落在静音内（约 56.5），而不是机械的 60.0
    plan = _plan_boundaries(120.0, [(55.0, 58.0)], max_sec=60)
    first_cut = plan[0][1]
    assert 55.0 <= first_cut <= 58.0


def test_plan_boundaries_zero_duration():
    assert _plan_boundaries(0, [], max_sec=110) == []


def test_split_short_audio_returns_single_chunk(tone, tmp_path):
    chunks = split_audio(tone, tmp_path / "c", total_duration=3.0, max_sec=110)
    assert len(chunks) == 1
    assert chunks[0].index == 0
    # 不超限时不应复制文件，直接复用原 wav
    assert chunks[0].path == tone


def test_split_long_audio_creates_files(speech_like, tmp_path):
    chunks = split_audio(speech_like, tmp_path / "c", total_duration=12.0, max_sec=5)
    assert len(chunks) >= 2
    for c in chunks:
        assert c.path.exists()
        assert c.path.stat().st_size > 1000
    # 索引连续、时间轴不重叠
    for i, c in enumerate(chunks):
        assert c.index == i
        if i:
            assert c.start >= chunks[i - 1].start


def test_chunk_duration_property():
    c = Chunk(index=0, start=2.0, end=7.5, path=Path("x.wav"))
    assert c.duration == 5.5
