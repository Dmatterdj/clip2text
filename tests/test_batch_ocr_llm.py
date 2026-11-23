"""批量调度、OCR 与 LLM 改写的测试。

全部使用假实现，不联网、不下模型、不花钱。
"""

from __future__ import annotations

import pytest

from clip2text.batch import BatchRunner, gather_inputs, resolve_upload_paths
from clip2text.db import Database
from clip2text.llm import (
    DEFAULT_STYLE,
    STYLES,
    LLMConfig,
    LLMError,
    _build_messages,
    _extract_delta,
    style_list,
)
from clip2text.ocr import (
    OCRError,
    OCRResult,
    build_ocr_engines,
    ocr_statuses,
    recognize_images,
    select_ocr_engine,
)
from clip2text.ocr.base import OCRLine, normalize_box


# ================================================================ 批量输入
def test_gather_inputs_merges_items_and_text():
    items = ["https://a.com/1", "  ", "https://a.com/2"]
    text = "https://a.com/3\n\nhttps://a.com/4\n"
    out = gather_inputs(items, text)
    assert out == ["https://a.com/1", "https://a.com/2", "https://a.com/3", "https://a.com/4"]


def test_gather_inputs_empty():
    assert gather_inputs([], None) == []
    assert gather_inputs([], "") == []


def test_resolve_upload_paths_rejects_outside(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    evil = tmp_path / "evil.wav"
    evil.write_bytes(b"x")
    with pytest.raises(ValueError, match="不在 uploads 目录内"):
        resolve_upload_paths([str(evil)], uploads)


def test_resolve_upload_paths_missing(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    with pytest.raises(ValueError, match="文件不存在"):
        resolve_upload_paths([str(uploads / "nope.wav")], uploads)


def test_resolve_upload_paths_ok(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    f = uploads / "a.wav"
    f.write_bytes(b"x")
    assert resolve_upload_paths([str(f)], uploads) == [str(f)]


# ---------------------------------------------------------------- 去重
@pytest.fixture
def runner(tmp_path):
    from clip2text.config import Settings
    from clip2text.pipeline import Pipeline

    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    db = Database(settings.db_path)
    pipeline = Pipeline(settings, db)
    return BatchRunner(pipeline, settings, db), settings


def test_normalize_dedupes(runner):
    r, _ = runner
    items = [
        "https://a.com/1",
        "https://a.com/1",  # 完全重复
        "HTTPS://A.COM/1",  # 大小写不同也算重复
        "https://a.com/2",
    ]
    valid, dups, invalid = r.normalize(items, source_type="url")
    assert valid == ["https://a.com/1", "https://a.com/2"]
    assert len(dups) == 2


def test_normalize_strips_share_text(runner):
    r, _ = runner
    # 用户常直接粘贴整段分享文案
    valid, _, invalid = r.normalize(
        ["3.87 复制打开抖音 https://v.douyin.com/abc 看看"], source_type="url"
    )
    assert valid == ["https://v.douyin.com/abc"]
    assert invalid == []


def test_normalize_rejects_non_url(runner):
    r, _ = runner
    valid, _, invalid = r.normalize(["这不是链接", "https://a.com/1"], source_type="url")
    assert valid == ["https://a.com/1"]
    assert len(invalid) == 1


def test_normalize_respects_max_batch_size(runner, monkeypatch):
    r, settings = runner
    monkeypatch.setattr(settings, "max_batch_size", 3)
    with pytest.raises(ValueError, match="单次最多"):
        r.normalize([f"https://a.com/{i}" for i in range(10)], source_type="url")


def test_normalize_upload_does_not_need_url(runner):
    r, _ = runner
    valid, _, invalid = r.normalize(["/x/a.wav", "/x/b.mp4"], source_type="upload")
    assert valid == ["/x/a.wav", "/x/b.mp4"]
    assert invalid == []


def test_normalize_upload_dedupes_exactly(runner):
    r, _ = runner
    valid, dups, _ = r.normalize(["/x/a.wav", "/x/a.wav"], source_type="upload")
    assert valid == ["/x/a.wav"]
    assert dups == ["/x/a.wav"]


# ================================================================ OCR
def test_normalize_box_basic():
    assert normalize_box(0, 0, 100, 100, 200, 200) == [0, 0, 500, 500]


def test_normalize_box_clamps():
    assert normalize_box(-50, -50, 500, 500, 100, 100) == [0, 0, 1000, 1000]


def test_normalize_box_zero_size():
    assert normalize_box(0, 0, 10, 10, 0, 0) == []


def test_ocr_result_dedupe_blank():
    r = OCRResult(
        lines=[OCRLine(text="你好"), OCRLine(text="   "), OCRLine(text="世界")]
    ).dedupe_blank()
    assert [ln.text for ln in r.lines] == ["你好", "世界"]
    assert r.text == "你好\n世界"


class _DeadEngine:
    """永远不可用的引擎，用来测试失败路径，不受本机是否装了 rapidocr 影响。"""

    name = "dead"
    supports_bbox = False

    def available(self):
        return False

    def unavailable_reason(self):
        return "测试用：故意不可用"

    def recognize(self, image):
        raise OCRError("不应被调用")


def test_ocr_engines_status_shape():
    from clip2text.config import Settings

    settings = Settings()
    statuses = ocr_statuses(settings, build_ocr_engines(settings))
    names = {s.name for s in statuses}
    assert names == {"rapidocr", "deepseek"}
    for s in statuses:
        # 可用性取决于本机环境与 key，只校验二者一致
        assert s.available is (s.reason is None)


def test_ocr_statuses_reports_unavailable():
    from clip2text.config import Settings

    statuses = ocr_statuses(
        Settings(), {"dead": _DeadEngine(), "deepseek": _DeadEngine()}
    )
    assert all(not s.available for s in statuses)
    assert all(s.reason for s in statuses)


def test_select_ocr_engine_fails_clearly():
    from clip2text.config import Settings

    settings = Settings(ocr_engine="auto")
    with pytest.raises(OCRError, match="没有可用的 OCR 引擎"):
        select_ocr_engine(settings, {"dead": _DeadEngine()})


def test_select_ocr_engine_unknown_name():
    from clip2text.config import Settings

    settings = Settings(ocr_engine="magic")
    with pytest.raises(OCRError, match="未知的 OCR 引擎"):
        select_ocr_engine(settings, build_ocr_engines(settings))


def test_recognize_images_merges_pages(monkeypatch, tmp_path):
    from clip2text.config import Settings

    class FakeEngine:
        name = "fake"
        supports_bbox = False

        def available(self):
            return True

        def unavailable_reason(self):
            return None

        def recognize(self, image):
            return OCRResult(
                text=f"来自 {image.name}", lines=[OCRLine(text=image.name)], engine="fake"
            )

    settings = Settings(ocr_engine="auto")
    imgs = []
    for i in range(3):
        p = tmp_path / f"a{i}.jpg"
        p.write_bytes(b"x")
        imgs.append(p)

    seen: list[float] = []
    result = recognize_images(
        imgs,
        settings,
        engines={"fake": FakeEngine()},
        on_progress=seen.append,
    )
    assert result.page == 3
    assert "a0.jpg" in result.text and "a2.jpg" in result.text
    assert len(seen) == 3
    assert seen[-1] == pytest.approx(1.0)


def test_recognize_images_survives_single_failure(monkeypatch, tmp_path):
    from clip2text.config import Settings

    class FlakyEngine:
        name = "flaky"
        supports_bbox = False
        calls = 0

        def available(self):
            return True

        def unavailable_reason(self):
            return None

        def recognize(self, image):
            type(self).calls += 1
            if type(self).calls == 1:
                raise OCRError("模拟失败")
            return OCRResult(text="成功", lines=[OCRLine(text="成功")], engine="flaky")

    settings = Settings(ocr_engine="auto")
    imgs = []
    for i in range(2):
        p = tmp_path / f"b{i}.jpg"
        p.write_bytes(b"x")
        imgs.append(p)

    result = recognize_images(imgs, settings, engines={"flaky": FlakyEngine()})
    assert "模拟失败" in result.text  # 失败标注保留
    assert "成功" in result.text


def test_recognize_images_empty():
    from clip2text.config import Settings

    with pytest.raises(OCRError, match="没有可识别的图片"):
        recognize_images([], Settings())


# ================================================================ LLM
def test_style_list_shape():
    styles = style_list()
    assert len(styles) == len(STYLES)
    for s in styles:
        assert set(s) == {"key", "label", "description"}
    assert DEFAULT_STYLE in STYLES


def test_llm_config_endpoint_normalization():
    assert (
        LLMConfig("https://api.deepseek.com", "k", "m").endpoint()
        == "https://api.deepseek.com/v1/chat/completions"
    )
    assert (
        LLMConfig("http://localhost:11434/v1", "k", "m").endpoint()
        == "http://localhost:11434/v1/chat/completions"
    )


def test_llm_config_configured_flag():
    assert LLMConfig("http://x", "", "m").configured
    assert not LLMConfig("", "", "m").configured
    assert not LLMConfig("http://x", "", "").configured


def test_build_messages_unknown_style():
    with pytest.raises(LLMError, match="未知的改写风格"):
        _build_messages("文本", "nonexistent", None)


def test_build_messages_includes_style_prompt():
    msgs = _build_messages("原文", "summary", None)
    assert msgs[0]["role"] == "system"
    assert "要点" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "原文"}


def test_build_messages_extra_instruction():
    msgs = _build_messages("原文", "formal", "控制在 200 字")
    assert "控制在 200 字" in msgs[0]["content"]


def test_build_messages_rejects_too_long():
    from clip2text.llm import MAX_INPUT_CHARS

    with pytest.raises(LLMError, match="文本过长"):
        _build_messages("x" * (MAX_INPUT_CHARS + 1), "summary", None)


def test_extract_delta_openai_format():
    obj = {"choices": [{"delta": {"content": "你好"}}]}
    assert _extract_delta(obj) == "你好"


def test_extract_delta_message_format():
    obj = {"choices": [{"message": {"content": "兜底"}}]}
    assert _extract_delta(obj) == "兜底"


def test_extract_delta_empty():
    assert _extract_delta({}) == ""
    assert _extract_delta({"choices": []}) == ""
    assert _extract_delta({"choices": [{"delta": {}}]}) == ""


@pytest.mark.asyncio
async def test_stream_rewrite_requires_config():
    from clip2text.llm import stream_rewrite

    with pytest.raises(LLMError, match="未配置"):
        async for _ in stream_rewrite("文本", LLMConfig("", "", "")):
            pass


@pytest.mark.asyncio
async def test_stream_rewrite_rejects_empty_text():
    from clip2text.llm import stream_rewrite

    cfg = LLMConfig("http://localhost:1", "", "m")
    with pytest.raises(LLMError, match="没有可改写的内容"):
        async for _ in stream_rewrite("   ", cfg):
            pass
