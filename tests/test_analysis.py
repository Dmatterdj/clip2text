"""内容解析（长文本结构化总结）的测试。

全部用假的 chat 实现，不联网、不花钱（契约 §9 要求）。
覆盖思路：
  · 算法层直接调 run_analysis(fake chat)，用字典记录每次调用，
    既能断言调用次数，也能断言提示词里有没有给对时间范围
  · HTTP 层用 TestClient，但把 AppState 的 chat 注入点换成假的
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clip2text.analysis import (
    AnalysisError,
    AnalysisRuns,
    Window,
    build_job,
    format_timestamp,
    parse_json_object,
    plan_windows,
    resolve_analysis_llm,
    run_analysis,
)
from clip2text.config import Settings
from clip2text.db import Database
from clip2text.llm import LLMConfig
from clip2text.models import Analysis, Chapter, Quote, Segment, Task

# ---------------------------------------------------------------- 夹具


def make_task(
    *,
    segments: list[Segment] | None = None,
    text: str | None = None,
    duration: float | None = None,
    task_id: str = "task-1",
    language: str | None = "zh",
) -> Task:
    return Task(
        id=task_id,
        source_type="url",
        source_value="https://example.com/x",
        segments=segments or [],
        text_content=text,
        duration=duration,
        language=language,
    )


def long_segments(count: int, *, step: float = 30.0, chars: int = 100) -> list[Segment]:
    """造一段「够长」的分句：count 段，每段 chars 个字、step 秒。"""
    out = []
    body = "这是一段测试内容" * (chars // 7 + 1)
    for i in range(count):
        out.append(
            Segment(start=i * step, end=(i + 1) * step, text=f"{body[:chars]} 段{i}")
        )
    return out


class FakeChat:
    """可编程的假 LLM。

    · map 调用按窗口序号返回预设 JSON 或抛错
    · reduce 调用返回预设 JSON
    · 每次调用的提示词都会被记下来，供断言时间范围 / 窗口序号
    """

    def __init__(
        self,
        *,
        map_payloads: dict[int, str | Exception] | None = None,
        reduce_payload: str | Exception | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        self.map_payloads = map_payloads or {}
        self.reduce_payload = reduce_payload
        self.usage = usage
        self.map_calls: list[list[dict[str, str]]] = []
        self.reduce_calls: list[list[dict[str, str]]] = []

    @property
    def calls(self) -> list[list[dict[str, str]]]:
        return [*self.map_calls, *self.reduce_calls]

    async def __call__(self, config: LLMConfig, messages):
        system = messages[0]["content"]
        if "chapters" in system and "abstract" in system:
            self.reduce_calls.append(messages)
            payload = self.reduce_payload
            if payload is None:
                windows = self._window_indexes(messages[1]["content"])
                payload = json.dumps(
                    {
                        "abstract": "汇总出来的全文摘要。",
                        "chapters": [
                            {"window": w, "title": f"章节 {w}", "summary": f"第 {w} 窗小结"}
                            for w in windows
                        ],
                        "key_points": ["全局要点一", "全局要点二"],
                        "entities": ["实体A", "实体B"],
                    },
                    ensure_ascii=False,
                )
            if isinstance(payload, Exception):
                raise payload
            return payload, self.usage

        index = self._current_window(system)
        self.map_calls.append(messages)
        payload = self.map_payloads.get(index)
        if payload is None:
            payload = json.dumps(
                {
                    "title": f"第 {index + 1} 部分标题",
                    "summary": f"第 {index} 段讲了测试内容。",
                    "key_points": [f"要点 {index}"],
                    "quotes": [{"text": f"第 {index} 窗原话", "start": 5.0}],
                },
                ensure_ascii=False,
            )
        if isinstance(payload, Exception):
            raise payload
        return payload, self.usage

    @staticmethod
    def _current_window(system: str) -> int:
        import re

        match = re.search(r"窗口\s*(\d+)", system)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _window_indexes(user: str) -> list[int]:
        import re

        return sorted({int(m) for m in re.findall(r"窗口\s*(\d+)", user)})


CONFIG = LLMConfig(base_url="http://fake", api_key="k", model="fake-model")


# ---------------------------------------------------------------- 1. 单窗短路
async def test_single_window_skips_reduce():
    task = make_task(
        segments=[Segment(start=0, end=60, text="短内容，一段就够。")],
        duration=60,
    )
    chat = FakeChat()
    result = await run_analysis(task, CONFIG, chat=chat)

    assert result.state == "SUCCESS"
    assert result.windows == 1
    assert len(chat.map_calls) == 1
    assert chat.reduce_calls == []  # 关键：单窗不该调 reduce
    assert len(result.chapters) == 1
    assert result.chapters[0].start == 0
    assert result.chapters[0].end == 60


# ---------------------------------------------------------------- 2. 多窗 map/reduce
async def test_multi_window_maps_each_window_and_reduces_once():
    segments = long_segments(60, step=30.0, chars=120)
    task = make_task(segments=segments, duration=1800)
    chat = FakeChat()

    result = await run_analysis(
        task, CONFIG, chat=chat, window_sec=300, window_chars=3000
    )

    assert result.state == "SUCCESS"
    assert result.windows == len(chat.map_calls)
    assert result.windows > 1
    assert len(chat.reduce_calls) == 1
    assert result.model == "fake-model"


# ---------------------------------------------------------------- 3. 单窗失败跳过
async def test_one_bad_window_is_skipped_others_continue():
    segments = long_segments(60, step=30.0, chars=120)
    task = make_task(segments=segments, duration=1800)
    # 先算出会分几窗，再让第 0 窗返回垃圾
    windows = plan_windows(task, window_sec=300, window_chars=3000)
    assert len(windows) >= 3

    chat = FakeChat(map_payloads={0: "这不是 JSON，模型跑偏了"})
    result = await run_analysis(
        task, CONFIG, chat=chat, window_sec=300, window_chars=3000
    )

    assert result.state == "SUCCESS"
    assert len(chat.map_calls) == len(windows)  # 失败的窗口也真的被调过
    # 第 0 窗没进章节，但其它窗口都在
    assert all(ch.start != windows[0].start for ch in result.chapters)


# ---------------------------------------------------------------- 4. 过半失败 → FAILED
async def test_majority_window_failure_marks_analysis_failed():
    segments = long_segments(60, step=30.0, chars=120)
    task = make_task(segments=segments, duration=1800)
    windows = plan_windows(task, window_sec=300, window_chars=3000)
    bad = {w.index: "oops not json" for w in windows}
    chat = FakeChat(map_payloads=bad)

    result = await run_analysis(
        task, CONFIG, chat=chat, window_sec=300, window_chars=3000
    )

    assert result.state == "FAILED"
    assert result.error and "无法解析" in result.error
    # 整体失败也要如实告诉用户分了几窗、花了多少
    assert result.windows == len(windows)


# ---------------------------------------------------------------- 5. markdown 包裹
@pytest.mark.parametrize(
    "raw",
    [
        '```json\n{"title": "t", "summary": "s"}\n```',
        '好的，结果如下：\n```\n{"title": "t"}\n```\n希望有帮助。',
        '{"title": "t"} 后面还有一些话',
        '\ufeff{"title": "t"}',
    ],
)
def test_parse_json_object_tolerates_markdown_and_prose(raw):
    parsed = parse_json_object(raw)
    assert parsed is not None
    assert parsed["title"] == "t"


async def test_map_with_markdown_wrapped_json_still_parses():
    task = make_task(
        segments=[
            Segment(start=0, end=30, text="第一段内容。"),
            Segment(start=30, end=60, text="第二段内容。"),
        ],
        duration=60,
    )
    body = json.dumps(
        {
            "title": "被代码块包裹的标题",
            "summary": "内容正常。",
            "key_points": ["a"],
            "quotes": [],
        },
        ensure_ascii=False,
    )
    chat = FakeChat(map_payloads={0: f"```json\n{body}\n```"})
    result = await run_analysis(task, CONFIG, chat=chat)
    assert result.state == "SUCCESS"
    assert result.chapters[0].title == "被代码块包裹的标题"


# ---------------------------------------------------------------- 6. 时间戳回填
async def test_chapter_timestamps_backfilled_from_real_window_bounds():
    segments = long_segments(60, step=30.0, chars=120)
    task = make_task(segments=segments, duration=1800)
    chat = FakeChat()
    windows = plan_windows(task, window_sec=300, window_chars=3000)

    result = await run_analysis(
        task, CONFIG, chat=chat, window_sec=300, window_chars=3000
    )

    assert result.state == "SUCCESS"
    for chapter in result.chapters:
        match = [w for w in windows if w.start == chapter.start]
        assert match, f"章节 start={chapter.start} 不在任何窗口边界上"
        assert chapter.end == match[0].end


async def test_map_prompt_contains_window_time_range():
    segments = long_segments(60, step=30.0, chars=120)
    task = make_task(segments=segments, duration=1800)
    chat = FakeChat()
    await run_analysis(task, CONFIG, chat=chat, window_sec=300, window_chars=3000)

    first_system = chat.map_calls[0][0]["content"]
    # 提示词里必须给出本窗时间范围，模型才能自己算相对秒数
    assert "00:00" in first_system
    assert "05:00" in first_system  # 300 秒窗口


# ---------------------------------------------------------------- 7. quotes 只来自 map
async def test_quotes_come_from_map_and_ignore_reduce_quotes():
    segments = long_segments(60, step=30.0, chars=120)
    task = make_task(segments=segments, duration=1800)
    windows = plan_windows(task, window_sec=300, window_chars=3000)

    reduce_body = json.dumps(
        {
            "abstract": "整体摘要。",
            "chapters": [
                {"window": w.index, "title": f"章 {w.index}", "summary": "小结"}
                for w in windows
            ],
            "key_points": ["要点"],
            # reduce 自作主张加了 quotes：必须被忽略
            "quotes": [{"text": "reduce 编的金句，不该出现", "start": 0}],
            "entities": ["X"],
        },
        ensure_ascii=False,
    )
    chat = FakeChat(reduce_payload=reduce_body)
    result = await run_analysis(
        task, CONFIG, chat=chat, window_sec=300, window_chars=3000
    )

    assert result.state == "SUCCESS"
    texts = [q.text for q in result.quotes]
    assert texts, "map 阶段给了 quotes，结果不该为空"
    assert all("reduce" not in t for t in texts)
    # 均来自 map，且时间戳已回填成正数
    assert all(q.start >= 0 for q in result.quotes)


async def test_quote_start_is_offset_by_window_start():
    """模型给的是窗内相对秒数，回填后必须是绝对时间。"""
    segments = [
        Segment(start=0, end=600, text="第一窗内容" * 10),
        Segment(start=600, end=1200, text="第二窗内容" * 10),
    ]
    task = make_task(segments=segments, duration=1200)
    body0 = json.dumps(
        {
            "title": "第一窗",
            "summary": "s",
            "key_points": [],
            "quotes": [{"text": "第一窗原话", "start": 30.0}],
        },
        ensure_ascii=False,
    )
    body1 = json.dumps(
        {
            "title": "第二窗",
            "summary": "s",
            "key_points": [],
            "quotes": [{"text": "第二窗原话", "start": 30.0}],
        },
        ensure_ascii=False,
    )
    chat = FakeChat(map_payloads={0: body0, 1: body1})
    result = await run_analysis(
        task, CONFIG, chat=chat, window_sec=600, window_chars=10000
    )
    assert result.state == "SUCCESS"
    starts = {q.text: q.start for q in result.quotes}
    assert starts["第一窗原话"] == 30.0
    # 第二窗起点 600 + 相对 30
    assert starts["第二窗原话"] == 630.0


async def test_quotes_dedup_and_capped_at_six():
    segments = long_segments(120, step=30.0, chars=120)
    task = make_task(segments=segments, duration=3600)
    same = json.dumps(
        {
            "title": "t",
            "summary": "s",
            "key_points": [],
            "quotes": [
                {"text": "完全一样的一句原话", "start": 1.0},
                {"text": "另一句不同的原话", "start": 2.0},
            ],
        },
        ensure_ascii=False,
    )
    chat = FakeChat(map_payloads={i: same for i in range(20)})
    result = await run_analysis(
        task, CONFIG, chat=chat, window_sec=120, window_chars=800
    )
    assert len(result.quotes) <= 6
    texts = [q.text for q in result.quotes]
    assert len(texts) == len(set(texts)), "重复原话应被去重"


# ---------------------------------------------------------------- 8. 窗口上限
def test_window_count_respects_max_limit():
    segments = long_segments(400, step=30.0, chars=200)
    task = make_task(segments=segments, duration=12000)
    windows = plan_windows(task, window_sec=60, window_chars=500, max_windows=40)
    assert len(windows) <= 40
    # 窗口序号必须连续，reduce 才可能正确引用
    assert [w.index for w in windows] == list(range(len(windows)))
    # 时间轴必须覆盖全程，不能因为合并丢了尾巴
    assert windows[-1].end == max(s.end for s in segments)


def test_window_count_can_be_lowered():
    segments = long_segments(100, step=30.0, chars=100)
    task = make_task(segments=segments, duration=3000)
    windows = plan_windows(task, window_sec=60, window_chars=400, max_windows=5)
    assert len(windows) <= 5


# ---------------------------------------------------------------- 9. 存储
def test_analysis_storage_roundtrip_overwrite_and_cascade(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    task = make_task(task_id="store-1")
    db.create(task)

    first = Analysis(
        task_id=task.id,
        abstract="第一版摘要",
        chapters=[Chapter(start=0, end=60, title="章一", summary="小结")],
        key_points=["p1"],
        quotes=[Quote(start=1.0, text="原话")],
        entities=["E"],
        model="m1",
        windows=3,
        state="SUCCESS",
    )
    db.upsert_analysis(first)
    loaded = db.get_analysis(task.id)
    assert loaded is not None
    assert loaded.abstract == "第一版摘要"
    assert loaded.chapters[0].title == "章一"
    assert loaded.windows == 3

    second = first.model_copy(update={"abstract": "第二版摘要", "model": "m2"})
    db.upsert_analysis(second)
    loaded = db.get_analysis(task.id)
    assert loaded is not None
    assert loaded.abstract == "第二版摘要"
    assert loaded.model == "m2"

    # 删除任务要级联删除分析
    assert db.delete(task.id) is True
    assert db.get_analysis(task.id) is None
    db.close()


def test_get_analysis_missing_returns_none(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    assert db.get_analysis("nope") is None
    db.close()


def test_running_analyses_marked_failed_on_startup(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    task = make_task(task_id="run-1")
    db.create(task)
    db.upsert_analysis(Analysis(task_id=task.id, state="RUNNING", model="m"))

    count = db.fail_running_analyses()
    assert count == 1
    loaded = db.get_analysis(task.id)
    assert loaded is not None
    assert loaded.state == "FAILED"
    assert loaded.error
    db.close()


# ---------------------------------------------------------------- 10. 未配置 LLM
async def test_run_analysis_raises_when_llm_not_configured():
    task = make_task(segments=[Segment(start=0, end=10, text="内容")])
    broken = LLMConfig(base_url="", api_key="", model="")
    with pytest.raises(AnalysisError) as exc:
        await run_analysis(task, broken)
    assert "未配置" in str(exc.value)


# ---------------------------------------------------------------- 额外：DeepSeek 兜底
def test_resolve_analysis_llm_prefers_explicit_llm_config():
    settings = Settings(
        data_dir=Path("./.data-test"),
        llm_base_url="http://localhost:11434",
        llm_api_key="k",
        llm_model="qwen",
        deepseek_api_key="ds",
    )
    result = resolve_analysis_llm(settings)
    assert result.source == "llm"
    assert result.config is not None
    assert result.config.model == "qwen"
    assert result.reason is None


def test_resolve_analysis_llm_falls_back_to_deepseek_key():
    settings = Settings(
        data_dir=Path("./.data-test"),
        deepseek_api_key="ds-key",
    )
    result = resolve_analysis_llm(settings)
    assert result.configured
    assert result.source == "deepseek"
    assert result.config is not None
    assert result.config.model == "deepseek-chat"
    assert result.config.base_url == settings.deepseek_base_url
    assert result.config.api_key == "ds-key"
    # 必须说清来源，否则用户不知道自己在用哪个 key
    assert result.reason and "DEEPSEEK_API_KEY" in result.reason


def test_resolve_analysis_llm_unavailable_without_any_key():
    settings = Settings(data_dir=Path("./.data-test"))
    result = resolve_analysis_llm(settings)
    assert not result.configured
    assert result.source is None
    assert result.reason and "DEEPSEEK_API_KEY" in result.reason


async def test_analysis_works_with_deepseek_fallback_config():
    settings = Settings(data_dir=Path("./.data-test"), deepseek_api_key="ds-key")
    llm = resolve_analysis_llm(settings)
    assert llm.config is not None

    task = make_task(segments=[Segment(start=0, end=60, text="短内容")], duration=60)
    chat = FakeChat()
    result = await run_analysis(task, llm.config, chat=chat)
    assert result.state == "SUCCESS"
    # 实际使用的模型名要如实落进结果
    assert result.model == "deepseek-chat"


# ---------------------------------------------------------------- 额外：token_usage
async def test_token_usage_is_summed_across_calls():
    segments = long_segments(60, step=30.0, chars=120)
    task = make_task(segments=segments, duration=1800)
    chat = FakeChat(usage={"prompt": 100, "completion": 20})
    result = await run_analysis(
        task, CONFIG, chat=chat, window_sec=300, window_chars=3000
    )

    assert result.state == "SUCCESS"
    assert result.token_usage == {
        "prompt": 100 * len(chat.calls),
        "completion": 20 * len(chat.calls),
    }


async def test_token_usage_none_when_upstream_omits_usage():
    task = make_task(segments=[Segment(start=0, end=60, text="短内容")], duration=60)
    chat = FakeChat(usage=None)
    result = await run_analysis(task, CONFIG, chat=chat)
    # 上游没给就留 None，不要用估算值假装精确
    assert result.token_usage is None


# ---------------------------------------------------------------- 额外：无 segments 按字符切窗
async def test_empty_segments_split_by_text_content_chars():
    text = "字" * 5000
    task = make_task(segments=[], text=text)
    # 无时间戳时按字符预算切
    windows = plan_windows(task, window_chars=1000)
    assert len(windows) == 5
    assert all(w.start == 0.0 and w.end == 0.0 for w in windows)
    assert all(not w.has_timestamps for w in windows)
    assert "".join(w.text for w in windows) == text


async def test_no_segments_no_text_raises():
    task = make_task(segments=[], text=None)
    with pytest.raises(AnalysisError) as exc:
        await run_analysis(task, CONFIG)
    assert "没有可分析的文本" in str(exc.value)


async def test_no_timestamp_quotes_have_zero_start():
    task = make_task(segments=[], text="没有时间戳的 OCR 文本内容" * 20)
    chat = FakeChat()
    result = await run_analysis(task, CONFIG, chat=chat, window_chars=1000)
    assert result.state == "SUCCESS"
    assert all(q.start == 0.0 for q in result.quotes)


# ---------------------------------------------------------------- 额外：截断护栏
async def test_output_length_guards_truncate():
    task = make_task(segments=[Segment(start=0, end=60, text="内容" * 100)], duration=60)
    body = json.dumps(
        {
            "title": "标" * 200,
            "summary": "摘" * 900,
            "key_points": [f"要点{i}" for i in range(30)],
            "quotes": [{"text": f"原话{i}", "start": i} for i in range(20)],
        },
        ensure_ascii=False,
    )
    chat = FakeChat(map_payloads={0: body})
    result = await run_analysis(task, CONFIG, chat=chat)

    assert len(result.chapters[0].title) <= 40
    assert len(result.chapters[0].summary) <= 200
    assert len(result.key_points) <= 10
    assert len(result.quotes) <= 6


async def test_reduce_overlong_fields_truncated():
    segments = long_segments(60, step=30.0, chars=120)
    task = make_task(segments=segments, duration=1800)
    windows = plan_windows(task, window_sec=300, window_chars=3000)
    body = json.dumps(
        {
            "abstract": "摘" * 2000,
            "chapters": [
                {"window": w.index, "title": "题" * 300, "summary": "结" * 900}
                for w in windows
            ],
            "key_points": [f"要点{i}" for i in range(50)],
            "entities": [f"实体{i}" for i in range(50)],
        },
        ensure_ascii=False,
    )
    chat = FakeChat(reduce_payload=body)
    result = await run_analysis(
        task, CONFIG, chat=chat, window_sec=300, window_chars=3000
    )
    assert len(result.abstract) <= 600
    for chapter in result.chapters:
        assert len(chapter.title) <= 40
        assert len(chapter.summary) <= 200
    assert len(result.key_points) <= 10
    assert len(result.entities) <= 15


# ---------------------------------------------------------------- 额外：reduce 越界窗口
async def test_reduce_with_out_of_range_window_is_ignored():
    segments = long_segments(60, step=30.0, chars=120)
    task = make_task(segments=segments, duration=1800)
    windows = plan_windows(task, window_sec=300, window_chars=3000)
    body = json.dumps(
        {
            "abstract": "摘要",
            # 999 不存在，应被忽略而不是产生 start=0 的假章节
            "chapters": [
                {"window": 999, "title": "假章节", "summary": "不该出现"},
                {"window": windows[0].index, "title": "真章节", "summary": "保留"},
            ],
            "key_points": ["k"],
            "entities": [],
        },
        ensure_ascii=False,
    )
    chat = FakeChat(reduce_payload=body)
    result = await run_analysis(
        task, CONFIG, chat=chat, window_sec=300, window_chars=3000
    )
    titles = [c.title for c in result.chapters]
    assert "假章节" not in titles
    assert "真章节" in titles


# ---------------------------------------------------------------- 额外：时间格式
@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "00:00"),
        (59, "00:59"),
        (60, "01:00"),
        (3599, "59:59"),
        (3600, "1:00:00"),
        (7594.2, "2:06:34"),
    ],
)
def test_format_timestamp(seconds, expected):
    assert format_timestamp(seconds) == expected


# ---------------------------------------------------------------- 额外：Progress 事件
async def test_progress_reports_mapping_then_reducing():
    segments = long_segments(60, step=30.0, chars=120)
    task = make_task(segments=segments, duration=1800)
    chat = FakeChat()
    events: list[dict] = []

    async def progress(event):
        events.append(event)

    await run_analysis(
        task, CONFIG, progress=progress, chat=chat, window_sec=300, window_chars=3000
    )

    phases = [e["phase"] for e in events]
    assert "mapping" in phases
    assert "reducing" in phases
    assert phases.index("mapping") < phases.index("reducing")
    mapping = [e for e in events if e["phase"] == "mapping"]
    assert mapping[-1]["done"] == mapping[-1]["total"]


async def test_progress_callback_failure_does_not_break_analysis():
    task = make_task(segments=[Segment(start=0, end=60, text="短内容")], duration=60)
    chat = FakeChat()

    def broken_progress(_event):
        raise RuntimeError("连接断了")

    result = await run_analysis(task, CONFIG, progress=broken_progress, chat=chat)
    assert result.state == "SUCCESS"


# ---------------------------------------------------------------- 额外：窗口构造
def test_plan_windows_marks_timed_and_untimed():
    timed = plan_windows(make_task(segments=long_segments(4, step=30, chars=50)))
    assert all(w.has_timestamps for w in timed)
    untimed = plan_windows(make_task(text="纯文本" * 500), window_chars=1000)
    assert all(not w.has_timestamps for w in untimed)


def test_plan_windows_returns_empty_for_blank_task():
    assert plan_windows(make_task(segments=[], text="   ")) == []


def test_window_dataclass_defaults():
    window = Window(index=0, start=0.0, end=10.0, text="t", timed_text="t")
    assert window.has_timestamps is True


def test_parse_json_object_rejects_non_object():
    assert parse_json_object("[1,2,3]") is None
    assert parse_json_object("完全不是 JSON") is None
    assert parse_json_object("") is None


# ---------------------------------------------------------------- HTTP 层
@pytest.fixture
def app_client(tmp_path: Path, monkeypatch):
    """临时数据目录 + 假 chat，构造可用的 app。

    chat 注入点放在 analysis 模块的默认参数上不方便，
    所以这里直接替换 AppState.analyses 的 job 工厂：
    让 build_job 拿到 FakeChat。
    """
    from clip2text import state as state_mod

    state_mod.reset_state()
    from clip2text.app import create_app

    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()

    app = create_app()
    with TestClient(app) as client:
        client.state = state_mod.init_state(settings)  # type: ignore[attr-defined]
        yield client
    state_mod.reset_state()


def _seed_task(client, task_id: str = "http-task") -> str:
    st = client.state  # type: ignore[attr-defined]
    task = Task(
        id=task_id,
        source_type="url",
        source_value="https://example.com/a",
        status="SUCCESS",
        stage="done",
        progress=100,
        text_content="这是一段用于 HTTP 测试的文本。" * 5,
        segments=[
            Segment(start=0, end=30, text="第一句测试内容。"),
            Segment(start=30, end=60, text="第二句测试内容。"),
        ],
        language="zh",
    )
    st.db.create(task)
    return task_id


def _patch_chat(monkeypatch, fake: FakeChat) -> None:
    """让 HTTP 路径的分析走假 chat，不联网。"""
    import clip2text.analysis as analysis_mod

    real_run = analysis_mod.run_analysis

    async def run_with_fake(task, config, *, progress=None, chat=None, **kwargs):
        return await real_run(task, config, progress=progress, chat=fake, **kwargs)

    monkeypatch.setattr(analysis_mod, "run_analysis", run_with_fake)


def test_capabilities_includes_analysis_unavailable(app_client):
    data = app_client.get("/api/v1/capabilities").json()
    analysis = data["analysis"]
    assert analysis["available"] is False
    assert "DEEPSEEK_API_KEY" in analysis["reason"]
    assert analysis["source"] is None
    # 旧的 rewrite 字段不能被破坏
    assert "rewrite" in data


def test_capabilities_analysis_available_with_deepseek_fallback(app_client, monkeypatch):
    st = app_client.state  # type: ignore[attr-defined]
    monkeypatch.setattr(st.settings, "deepseek_api_key", "ds-key")

    analysis = app_client.get("/api/v1/capabilities").json()["analysis"]
    assert analysis["available"] is True
    assert analysis["source"] == "deepseek"
    assert analysis["model"] == "deepseek-chat"
    assert "DEEPSEEK_API_KEY" in analysis["reason"]


def test_capabilities_analysis_prefers_explicit_llm(app_client, monkeypatch):
    """配了 LLM_* 时不该悄悄走 DeepSeek，也不该有 explanation。"""
    st = app_client.state  # type: ignore[attr-defined]
    monkeypatch.setattr(st.settings, "llm_base_url", "http://localhost:11434")
    monkeypatch.setattr(st.settings, "llm_model", "qwen2.5")
    monkeypatch.setattr(st.settings, "deepseek_api_key", "ds-key")

    analysis = app_client.get("/api/v1/capabilities").json()["analysis"]
    assert analysis["available"] is True
    assert analysis["source"] == "llm"
    assert analysis["model"] == "qwen2.5"
    assert analysis["reason"] is None


def test_get_analysis_404_when_missing(app_client):
    task_id = _seed_task(app_client)
    r = app_client.get(f"/api/v1/tasks/{task_id}/analysis")
    assert r.status_code == 404
    assert "还没有分析结果" in r.json()["detail"]


def test_analyze_400_when_llm_not_configured(app_client):
    task_id = _seed_task(app_client)
    r = app_client.post(f"/api/v1/tasks/{task_id}/analyze", json={"force": False})
    assert r.status_code == 400
    assert "配置" in r.json()["detail"]


def test_analyze_400_for_unknown_task(app_client, monkeypatch):
    st = app_client.state  # type: ignore[attr-defined]
    monkeypatch.setattr(st.settings, "deepseek_api_key", "ds-key")
    r = app_client.post("/api/v1/tasks/nope/analyze", json={"force": False})
    assert r.status_code == 404


def test_analyze_400_when_task_has_no_text(app_client, monkeypatch):
    st = app_client.state  # type: ignore[attr-defined]
    monkeypatch.setattr(st.settings, "deepseek_api_key", "ds-key")
    st.db.create(
        Task(id="empty-task", source_type="url", source_value="https://x/y")
    )
    r = app_client.post("/api/v1/tasks/empty-task/analyze", json={"force": False})
    assert r.status_code == 400
    assert "没有可解析的文本" in r.json()["detail"]


def test_analyze_streams_progress_and_result(app_client, monkeypatch):
    st = app_client.state  # type: ignore[attr-defined]
    monkeypatch.setattr(st.settings, "deepseek_api_key", "ds-key")
    task_id = _seed_task(app_client)
    _patch_chat(monkeypatch, FakeChat())

    r = app_client.post(f"/api/v1/tasks/{task_id}/analyze", json={"force": False})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    body = r.text
    assert "event: progress" in body
    assert "event: result" in body

    # result 里必须是完整 Analysis
    payload = None
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        if any(line.strip() == "event: result" for line in lines):
            data_line = next(line for line in lines if line.startswith("data:"))
            payload = json.loads(data_line[5:].strip())
    assert payload is not None
    assert payload["task_id"] == task_id
    assert payload["state"] == "SUCCESS"
    assert payload["windows"] >= 1
    assert payload["chapters"]
    assert payload["token_usage"] == {"prompt": 0, "completion": 0} or payload[
        "token_usage"
    ] is None

    # 落库了，GET 能直接读到
    got = app_client.get(f"/api/v1/tasks/{task_id}/analysis")
    assert got.status_code == 200
    assert got.json()["task_id"] == task_id


def test_analyze_returns_cached_result_without_sse(app_client, monkeypatch):
    st = app_client.state  # type: ignore[attr-defined]
    monkeypatch.setattr(st.settings, "deepseek_api_key", "ds-key")
    task_id = _seed_task(app_client)
    st.db.upsert_analysis(
        Analysis(
            task_id=task_id,
            abstract="已有摘要",
            chapters=[Chapter(start=0, end=10, title="旧章节", summary="旧小结")],
            model="cached-model",
            windows=2,
            state="SUCCESS",
        )
    )
    _patch_chat(monkeypatch, FakeChat())

    r = app_client.post(f"/api/v1/tasks/{task_id}/analyze", json={"force": False})
    assert r.status_code == 200
    # 不走 SSE，直接 JSON：避免重复烧 token
    assert "text/event-stream" not in r.headers.get("content-type", "")
    data = r.json()
    assert data["abstract"] == "已有摘要"
    assert data["model"] == "cached-model"


def test_analyze_force_reruns_and_overwrites(app_client, monkeypatch):
    st = app_client.state  # type: ignore[attr-defined]
    monkeypatch.setattr(st.settings, "deepseek_api_key", "ds-key")
    task_id = _seed_task(app_client)
    st.db.upsert_analysis(
        Analysis(task_id=task_id, abstract="旧摘要", state="SUCCESS", model="old")
    )
    _patch_chat(monkeypatch, FakeChat())

    r = app_client.post(f"/api/v1/tasks/{task_id}/analyze", json={"force": True})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "event: result" in r.text

    stored = st.db.get_analysis(task_id)
    assert stored is not None
    assert stored.abstract != "旧摘要"


def test_analyze_emits_error_event_when_analysis_fails(app_client, monkeypatch):
    st = app_client.state  # type: ignore[attr-defined]
    monkeypatch.setattr(st.settings, "deepseek_api_key", "ds-key")
    task_id = _seed_task(app_client)
    # 所有窗口都返回坏 JSON → 整体 FAILED → 应推 error 事件
    fake = FakeChat(map_payloads={i: "坏 JSON" for i in range(10)})
    _patch_chat(monkeypatch, fake)

    r = app_client.post(f"/api/v1/tasks/{task_id}/analyze", json={"force": False})
    assert r.status_code == 200
    assert "event: error" in r.text
    stored = st.db.get_analysis(task_id)
    assert stored is not None
    assert stored.state == "FAILED"


def test_delete_task_cascades_analysis_via_api(app_client):
    st = app_client.state  # type: ignore[attr-defined]
    task_id = _seed_task(app_client)
    st.db.upsert_analysis(
        Analysis(task_id=task_id, abstract="会被级联删掉", state="SUCCESS")
    )
    assert app_client.delete(f"/api/v1/tasks/{task_id}").status_code == 204
    assert st.db.get_analysis(task_id) is None
    assert app_client.get(f"/api/v1/tasks/{task_id}/analysis").status_code == 404


# ---------------------------------------------------------------- 调度器
async def test_analysis_runs_does_not_start_twice():
    runs = AnalysisRuns()
    started: list[int] = []

    async def slow():
        started.append(1)
        import asyncio

        await asyncio.sleep(0.05)

    assert runs.ensure_started("t", slow) is True
    assert runs.ensure_started("t", slow) is False  # 重复点击不该再付一次钱
    import asyncio

    await asyncio.sleep(0.1)
    assert started == [1]


async def test_analysis_runs_publishes_to_subscribers():
    runs = AnalysisRuns()
    queue = runs.subscribe("t")
    await runs.publish("t", {"type": "progress", "data": {"phase": "mapping"}})
    event = queue.get_nowait()
    assert event["data"]["phase"] == "mapping"
    runs.unsubscribe("t", queue)


async def test_build_job_stores_failed_when_run_raises(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "t.db")
    task = make_task(task_id="job-1", segments=[Segment(start=0, end=10, text="x")])
    db.create(task)
    runs = AnalysisRuns()

    import clip2text.analysis as analysis_mod

    async def boom(*_args, **_kwargs):
        raise AnalysisError("模型整体崩了")

    monkeypatch.setattr(analysis_mod, "run_analysis", boom)
    job = build_job(task=task, config=CONFIG, db=db, runs=runs)
    await job()

    stored = db.get_analysis(task.id)
    assert stored is not None
    assert stored.state == "FAILED"
    assert "模型整体崩了" in (stored.error or "")
    db.close()


async def test_build_job_success_stores_success(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "t.db")
    task = make_task(task_id="job-2", segments=[Segment(start=0, end=10, text="x")])
    db.create(task)
    runs = AnalysisRuns()

    async def fake_run(task_arg, config_arg, *, progress=None, chat=None, **kwargs):
        return Analysis(
            task_id=task_arg.id,
            abstract="成功摘要",
            chapters=[Chapter(start=0, end=10, title="章", summary="小结")],
            model=config_arg.model,
            windows=1,
            state="SUCCESS",
        )

    import clip2text.analysis as analysis_mod

    monkeypatch.setattr(analysis_mod, "run_analysis", fake_run)
    job = build_job(task=task, config=CONFIG, db=db, runs=runs)
    await job()

    stored = db.get_analysis(task.id)
    assert stored is not None
    assert stored.state == "SUCCESS"
    assert stored.abstract == "成功摘要"
    db.close()
