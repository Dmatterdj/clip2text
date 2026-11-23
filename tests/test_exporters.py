"""ASR 辅助模块与导出器测试。这些都不需要真实模型。"""

from __future__ import annotations

import os

import pytest

from clip2text.asr.hf import (
    WEIGHT_FILES,
    enable_offline_if_cached,
    repo_cache_dir,
    snapshot_complete,
)
from clip2text.exporters import (
    EXTENSIONS,
    MEDIA_TYPES,
    SUPPORTED,
    export,
    safe_filename,
    to_docx,
    to_markdown,
    to_txt,
    to_xlsx,
)
from clip2text.models import Segment, Task


def make_task(**kw) -> Task:
    base = dict(
        id="abc123",
        source_type="upload",
        source_value=".data/uploads/x/lecture.mp4",
        title="测试标题",
        text_content="这是转写出来的正文内容。",
        segments=[
            Segment(start=0.0, end=2.5, text="这是第一句"),
            Segment(start=2.5, end=5.0, text="这是第二句"),
        ],
        language="zh",
        duration=5.0,
        engine="mlx",
    )
    base.update(kw)
    return Task(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 导出器
def test_supported_formats():
    assert SUPPORTED == ("txt", "markdown", "docx", "xlsx")
    assert set(EXTENSIONS) == set(SUPPORTED)
    assert set(MEDIA_TYPES) == set(SUPPORTED)


@pytest.mark.parametrize("fmt", SUPPORTED)
def test_export_produces_bytes(fmt):
    data = export(make_task(), fmt)
    assert isinstance(data, bytes)
    assert len(data) > 0


def test_txt_contains_text_and_meta():
    out = to_txt(make_task()).decode()
    assert "这是转写出来的正文内容。" in out
    assert "测试标题" in out
    assert "mlx" in out


def test_markdown_has_heading_and_table():
    out = to_markdown(make_task()).decode()
    assert out.startswith("# 测试标题")
    assert "## 转写文本" in out
    assert "## 分句（带时间戳）" in out
    assert "| 00:00 |" in out


def test_docx_and_xlsx_are_zip_containers():
    # docx/xlsx 本质是 zip，用 magic number 校验比解析结构更稳
    assert to_docx(make_task())[:2] == b"PK"
    assert to_xlsx(make_task())[:2] == b"PK"


def test_export_unknown_format():
    with pytest.raises(ValueError, match="不支持的导出格式"):
        export(make_task(), "pdf")


def test_export_handles_task_without_segments():
    task = make_task(segments=[], text_content="只有一段文本")
    for fmt in SUPPORTED:
        assert len(export(task, fmt)) > 0


@pytest.mark.parametrize(
    "title,source,expected",
    [
        ("正常标题", ".data/uploads/x/a.mp4", "正常标题.txt"),
        ("含/斜杠\\和:冒号", ".data/uploads/x/a.mp4", "含_斜杠_和_冒号.txt"),
        # 标题为空时回退到 source_value（取 basename 前的清洗结果）
        (None, ".data/uploads/x/a.mp4", "data_uploads_x_a.mp4.txt"),
        (None, "", "abc123.txt"),
        ("x" * 200, ".data/uploads/x/a.mp4", "x" * 60 + ".txt"),
    ],
)
def test_safe_filename(title, source, expected):
    task = make_task(title=title, source_value=source)
    assert safe_filename(task, "txt") == expected


def test_safe_filename_uses_correct_extension():
    task = make_task(title="a")
    for fmt, ext in EXTENSIONS.items():
        assert safe_filename(task, fmt).endswith("." + ext)


# ---------------------------------------------------------------- HF 缓存
def test_weight_files_cover_all_engines():
    # mlx 用 npz/safetensors，faster-whisper 用 model.bin
    assert "weights.npz" in WEIGHT_FILES
    assert "model.bin" in WEIGHT_FILES


def test_repo_cache_dir_munges_slash():
    d = repo_cache_dir("mlx-community/whisper-small-mlx")
    assert d.name == "models--mlx-community--whisper-small-mlx"


def test_snapshot_complete_false_for_unknown_repo():
    assert snapshot_complete("definitely/not-a-real-repo-xyz") is False


def test_offline_respects_explicit_disable(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    # 用户显式要求联网时，即使缓存完整也不该改
    assert enable_offline_if_cached("Systran/faster-whisper-small") is False
    assert os.environ["HF_HUB_OFFLINE"] == "0"
