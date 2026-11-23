"""长内容流水线测试：分片重试、真实进度、断点续跑、长内容护栏。

全部用假引擎 + 假 ffmpeg 层，不下载 whisper 模型、不调用真实 ffmpeg。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from clip2text import pipeline as pipeline_mod
from clip2text.config import Settings
from clip2text.db import Database
from clip2text.media.chunker import Chunk
from clip2text.models import Segment, Stage, TaskStatus, Transcript
from clip2text.pipeline import (
    CHECKPOINT_VERSION,
    Checkpoint,
    ChunkResult,
    Pipeline,
    PipelineError,
)

# ---------------------------------------------------------------- 假件


class FakeEngine:
    name = "fake"
    supports_timestamps = True
    max_chunk_sec = None

    def __init__(self, fail_indexes: set[int] | None = None, fail_times: int = 99):
        self.calls: list[tuple[int, int]] = []  # (chunk 序号, 第几次调用)
        self._fail_indexes = fail_indexes or set()
        self._fail_times = fail_times
        self._seen: dict[int, int] = {}

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> None:
        return None

    def transcribe(self, audio: Path, language: str | None = None) -> Transcript:
        idx = int(Path(audio).stem.split("_")[-1])
        attempt = self._seen.get(idx, 0) + 1
        self._seen[idx] = attempt
        self.calls.append((idx, attempt))

        # 前 fail_times 次调用对该分片抛错
        if idx in self._fail_indexes and attempt <= self._fail_times:
            from clip2text.asr import ASRError

            raise ASRError(f"模拟第 {idx} 段失败")

        return Transcript(
            text=f"第{idx}段文字",
            segments=[Segment(start=0.0, end=1.0, text=f"第{idx}段")],
            language="zh",
        )


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", chunk_retry_backoff_sec=0.0)
    settings.ensure_dirs()
    db = Database(settings.db_path)
    return settings, db


def make_pipeline(env, engine=None) -> Pipeline:
    settings, db = env
    pipe = Pipeline(settings, db)
    pipe._engines = {"fake": engine or FakeEngine()}
    return pipe


def _fake_chunks(work_dir: Path, n: int, *, start_each: float = 100.0) -> list[Chunk]:
    """造 n 个真实存在的小分片文件（内容无所谓，引擎是假的）。"""
    out = work_dir / "chunks"
    out.mkdir(parents=True, exist_ok=True)
    chunks = []
    for i in range(n):
        path = out / f"chunk_{i:04d}.wav"
        path.write_bytes(b"x" * 128)
        chunks.append(
            Chunk(index=i, start=i * start_each, end=(i + 1) * start_each, path=path)
        )
    return chunks


def patch_media(monkeypatch, tmp_path, chunks: list[Chunk], duration: float):
    """把 ffmpeg 相关步骤全换成假的。"""

    def fake_probe(path, mode="auto"):
        return duration

    def fake_extract(src, dst, *, mode="auto", **kwargs):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"RIFF" + b"\0" * 64)
        return Path(dst)

    def fake_split(wav, out_dir, *, total_duration, max_sec=110.0, mode="auto"):
        return chunks

    monkeypatch.setattr(pipeline_mod, "probe_duration", fake_probe)
    monkeypatch.setattr(pipeline_mod, "extract_audio", fake_extract)
    monkeypatch.setattr(pipeline_mod, "split_audio", fake_split)


def make_task(pipe: Pipeline, tmp_path: Path):
    src = tmp_path / "input.m4a"
    src.write_bytes(b"audio")
    return pipe.create_task(source_type="upload", source_value=str(src))


# ---------------------------------------------------------------- 长内容护栏
def test_guard_duration_rejects_too_long(env):
    pipe = make_pipeline(env)
    with pytest.raises(PipelineError, match="超过上限"):
        pipe._guard_duration(4 * 3600)  # 4 小时 > 默认 180 分钟


def test_guard_duration_allows_within_limit(env):
    pipe = make_pipeline(env)
    pipe._guard_duration(179 * 60)
    pipe._guard_duration(None)  # 时长未知时放行，由后续步骤决定


def test_guard_duration_can_be_disabled(env):
    settings, db = env
    settings.max_media_duration_min = 0
    pipe = Pipeline(settings, db)
    pipe._guard_duration(10 * 3600)  # 不限制


def test_guard_runs_before_transcode(env, tmp_path, monkeypatch):
    """护栏必须在抽音轨之前触发，否则等于白跑一遍 ffmpeg。"""
    pipe = make_pipeline(env)
    task = make_task(pipe, tmp_path)

    called = {"extract": False}

    def fake_probe(path, mode="auto"):
        return 4 * 3600

    def fake_extract(*args, **kwargs):
        called["extract"] = True
        raise AssertionError("不该走到抽音轨这一步")

    monkeypatch.setattr(pipeline_mod, "probe_duration", fake_probe)
    monkeypatch.setattr(pipeline_mod, "extract_audio", fake_extract)
    monkeypatch.setattr(pipeline_mod, "select_engine", lambda s, e=None: FakeEngine())

    result = asyncio.run(pipe.run(task.id))
    assert result.status == TaskStatus.FAILED
    assert "超过上限" in (result.error_message or "")
    assert called["extract"] is False


# ---------------------------------------------------------------- 分片重试
def test_transcribe_chunk_retries_then_succeeds(env, tmp_path, monkeypatch):
    settings, db = env
    settings.chunk_retry_max = 2
    pipe = Pipeline(settings, db)
    engine = FakeEngine(fail_indexes={3}, fail_times=2)

    chunk = Chunk(index=3, start=100.0, end=200.0, path=tmp_path / "chunk_0003.wav")
    chunk.path.write_bytes(b"x")
    task = make_task(pipe, tmp_path)

    result = asyncio.run(pipe._transcribe_chunk(task, engine, chunk, index=3, total=70))

    # 前两次失败、第三次成功
    assert engine.calls == [(3, 1), (3, 2), (3, 3)]
    assert result.index == 3
    assert result.text == "第3段文字"


def test_transcribe_chunk_timestamps_are_shifted(env, tmp_path):
    """片内时间戳必须平移到整段音频的绝对时间。"""
    pipe = make_pipeline(env)
    chunk = Chunk(index=2, start=220.0, end=330.0, path=tmp_path / "chunk_0002.wav")
    chunk.path.write_bytes(b"x")
    task = make_task(pipe, tmp_path)

    result = asyncio.run(
        pipe._transcribe_chunk(task, FakeEngine(), chunk, index=2, total=10)
    )
    assert result.segments[0].start == 220.0
    assert result.segments[0].end == 221.0


def test_transcribe_chunk_gives_up_after_max_retries(env, tmp_path):
    """重试次数用尽后必须明确失败，并告诉用户已完成片段还在。"""
    from clip2text.asr import ASRError

    settings, db = env
    settings.chunk_retry_max = 2
    pipe = Pipeline(settings, db)
    engine = FakeEngine(fail_indexes={5}, fail_times=99)

    chunk = Chunk(index=5, start=0.0, end=100.0, path=tmp_path / "chunk_0005.wav")
    chunk.path.write_bytes(b"x")
    task = make_task(pipe, tmp_path)

    with pytest.raises(ASRError) as exc:
        asyncio.run(pipe._transcribe_chunk(task, engine, chunk, index=5, total=70))

    # 1 次初始 + 2 次重试
    assert len(engine.calls) == 3
    message = str(exc.value)
    assert "第 6/70 段" in message
    assert "已重试 2 次" in message
    assert "已保存" in message


def test_transcribe_chunk_zero_retries_fails_immediately(env, tmp_path):
    from clip2text.asr import ASRError

    settings, db = env
    settings.chunk_retry_max = 0
    pipe = Pipeline(settings, db)
    engine = FakeEngine(fail_indexes={0}, fail_times=99)

    chunk = Chunk(index=0, start=0.0, end=100.0, path=tmp_path / "chunk_0000.wav")
    chunk.path.write_bytes(b"x")
    task = make_task(pipe, tmp_path)

    with pytest.raises(ASRError):
        asyncio.run(pipe._transcribe_chunk(task, engine, chunk, index=0, total=1))
    assert len(engine.calls) == 1


def test_pipeline_survives_one_flaky_chunk(env, tmp_path, monkeypatch):
    """端到端：70 段里有一段抖了两次，任务仍然成功。"""
    settings, db = env
    settings.chunk_retry_max = 2
    settings.resume_enabled = False
    settings.chunk_max_sec = 110
    pipe = Pipeline(settings, db)
    engine = FakeEngine(fail_indexes={6}, fail_times=2)
    pipe._engines = {"fake": engine}

    chunks = _fake_chunks(tmp_path, 8)
    patch_media(monkeypatch, tmp_path, chunks, duration=800.0)
    monkeypatch.setattr(pipeline_mod, "select_engine", lambda s, e=None: engine)

    task = make_task(pipe, tmp_path)
    result = asyncio.run(pipe.run(task.id))

    assert result.status == TaskStatus.SUCCESS, result.error_message
    assert "第6段文字" in (result.text_content or "")
    assert result.stage == Stage.DONE


# ---------------------------------------------------------------- 进度
def test_progress_is_monotonic_and_reaches_100(env, tmp_path, monkeypatch):
    """进度只能往前走，且必须到 100 —— 长任务最怕卡在一个数字上。"""
    settings, db = env
    settings.resume_enabled = False
    settings.chunk_retry_max = 0
    pipe = Pipeline(settings, db)

    chunks = _fake_chunks(tmp_path, 6)
    patch_media(monkeypatch, tmp_path, chunks, duration=600.0)
    engine = FakeEngine()
    monkeypatch.setattr(pipeline_mod, "select_engine", lambda s, e=None: engine)

    seen: list[int] = []
    pipe.on_progress(lambda task: seen.append(task.progress))

    task = make_task(pipe, tmp_path)
    result = asyncio.run(pipe.run(task.id))

    assert result.status == TaskStatus.SUCCESS
    assert seen == sorted(seen), f"进度回退: {seen}"
    assert max(seen) == 100
    # 至少要有下载/转码/转写这些阶段的多个百分点，而不是只有首尾两个值
    assert len({p for p in seen if 0 < p < 100}) >= 3


def test_progress_reports_intermediate_stages(env, tmp_path, monkeypatch):
    """分阶段进度：fetching / transcoding / transcribing / packaging 都要有。"""
    settings, db = env
    settings.resume_enabled = False
    pipe = Pipeline(settings, db)
    chunks = _fake_chunks(tmp_path, 4)
    patch_media(monkeypatch, tmp_path, chunks, duration=400.0)
    monkeypatch.setattr(
        pipeline_mod, "select_engine", lambda s, e=None: FakeEngine()
    )

    stages: list[Stage] = []
    pipe.on_progress(
        lambda task: stages.append(task.stage) if not stages or stages[-1] != task.stage else None
    )

    task = make_task(pipe, tmp_path)
    asyncio.run(pipe.run(task.id))

    assert Stage.FETCHING in stages
    assert Stage.TRANSCODING in stages
    assert Stage.TRANSCRIBING in stages
    assert Stage.PACKAGING in stages
    assert stages[-1] == Stage.DONE


# ---------------------------------------------------------------- 断点续跑
def test_checkpoint_roundtrip(tmp_path):
    path = tmp_path / "checkpoint.json"
    fp = {"duration": 600.0, "max_chunk_sec": 110.0, "engine": "fake"}
    cp = Checkpoint(path, fp)
    cp.record(ChunkResult(index=0, text="a", segments=[], language="zh"))
    cp.record(ChunkResult(index=1, text="b", segments=[], language="zh"))

    again = Checkpoint(path, fp)
    assert again.load() is True
    assert again.done_indexes == {0, 1}
    assert again.get(1).text == "b"


def test_checkpoint_rejects_changed_fingerprint(tmp_path):
    """换了引擎/分片长度后，按下标复用会把时间轴接错，必须丢弃。"""
    path = tmp_path / "checkpoint.json"
    cp = Checkpoint(path, {"duration": 600.0, "max_chunk_sec": 110.0, "engine": "fake"})
    cp.record(ChunkResult(index=0, text="a"))

    other = Checkpoint(path, {"duration": 600.0, "max_chunk_sec": 60.0, "engine": "fake"})
    assert other.load() is False
    assert other.done_indexes == set()


def test_checkpoint_rejects_corrupt_file(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    assert Checkpoint(path, {}).load() is False


def test_checkpoint_rejects_wrong_version(tmp_path):
    import json

    path = tmp_path / "checkpoint.json"
    path.write_text(
        json.dumps({"version": CHECKPOINT_VERSION + 99, "chunks": []}), encoding="utf-8"
    )
    assert Checkpoint(path, {}).load() is False


def test_checkpoint_clear(tmp_path):
    path = tmp_path / "checkpoint.json"
    cp = Checkpoint(path, {})
    cp.record(ChunkResult(index=0, text="a"))
    assert path.exists()
    cp.clear()
    assert not path.exists()
    assert cp.done_indexes == set()


def test_pipeline_resumes_from_checkpoint(env, tmp_path, monkeypatch):
    """核心验收点：重启后跳过已完成分片，不再重复喂给 ASR。"""
    settings, db = env
    settings.resume_enabled = True
    settings.chunk_retry_max = 0
    pipe = Pipeline(settings, db)
    chunks = _fake_chunks(tmp_path, 6)
    patch_media(monkeypatch, tmp_path, chunks, duration=600.0)
    engine = FakeEngine()
    monkeypatch.setattr(pipeline_mod, "select_engine", lambda s, e=None: engine)

    task = make_task(pipe, tmp_path)
    work_dir = settings.work_dir / task.id

    # 手工造一份「前 4 段已完成」的检查点，模拟进程在此时被杀
    fingerprint = {"duration": 600.0, "max_chunk_sec": 110.0, "engine": "fake"}
    cp = Checkpoint(work_dir / "checkpoint.json", fingerprint)
    for i in range(4):
        cp.record(
            ChunkResult(
                index=i,
                text=f"第{i}段文字",
                segments=[Segment(start=i * 100.0, end=i * 100.0 + 1, text=f"第{i}段")],
                language="zh",
            )
        )

    result = asyncio.run(pipe.run(task.id))

    assert result.status == TaskStatus.SUCCESS, result.error_message
    # 只应转写第 4、5 段（索引）
    assert sorted(idx for idx, _ in engine.calls) == [4, 5]
    joined = result.text_content or ""
    for i in range(6):
        assert f"第{i}段文字" in joined
    # 成功后检查点应被清掉
    assert not (work_dir / "checkpoint.json").exists()


def test_pipeline_skips_resume_when_disabled(env, tmp_path, monkeypatch):
    settings, db = env
    settings.resume_enabled = False
    settings.chunk_retry_max = 0
    pipe = Pipeline(settings, db)
    chunks = _fake_chunks(tmp_path, 3)
    patch_media(monkeypatch, tmp_path, chunks, duration=300.0)
    engine = FakeEngine()
    monkeypatch.setattr(pipeline_mod, "select_engine", lambda s, e=None: engine)

    task = make_task(pipe, tmp_path)
    work_dir = settings.work_dir / task.id
    fingerprint = {"duration": 300.0, "max_chunk_sec": 110.0, "engine": "fake"}
    cp = Checkpoint(work_dir / "checkpoint.json", fingerprint)
    cp.record(ChunkResult(index=0, text="旧的"))

    result = asyncio.run(pipe.run(task.id))
    assert result.status == TaskStatus.SUCCESS
    # 关掉续跑就该全量重跑
    assert sorted(idx for idx, _ in engine.calls) == [0, 1, 2]


def test_recover_orphans_mentions_resume_when_checkpoint_exists(env, tmp_path):
    """重启后：有检查点的任务，错误信息要告诉用户「可以续跑」。"""
    settings, db = env
    pipe = Pipeline(settings, db)

    with_cp = pipe.create_task(source_type="upload", source_value=str(tmp_path / "a.m4a"))
    without_cp = pipe.create_task(source_type="upload", source_value=str(tmp_path / "b.m4a"))
    for t in (with_cp, without_cp):
        db.update_fields(t.id, status=TaskStatus.RUNNING.value)

    cp_dir = settings.work_dir / with_cp.id
    cp_dir.mkdir(parents=True, exist_ok=True)
    (cp_dir / "checkpoint.json").write_text('{"version":1}', encoding="utf-8")

    count = pipe.recover_orphans()
    assert count == 2

    a = db.get(with_cp.id)
    b = db.get(without_cp.id)
    assert a.status == TaskStatus.FAILED
    assert "续跑" in (a.error_message or "")
    assert "重新提交" in (b.error_message or "")
    assert "续跑" not in (b.error_message or "")


def test_recover_orphans_does_not_auto_rerun(env, tmp_path):
    """有意不自动续跑：重启就偷偷重跑会重复烧算力，用户还不知道。"""
    settings, db = env
    pipe = make_pipeline(env)
    task = pipe.create_task(source_type="upload", source_value=str(tmp_path / "a.m4a"))
    db.update_fields(task.id, status=TaskStatus.RUNNING.value)

    pipe.recover_orphans()

    stored = db.get(task.id)
    assert stored.status == TaskStatus.FAILED
    assert stored.status != TaskStatus.RUNNING


# ---------------------------------------------------------------- 续跑快路
def test_resume_fast_path_skips_download_and_transcode(env, tmp_path, monkeypatch):
    """续跑时不应重新下载/重新转码 —— 那正是长内容最耗时的部分。

    不变式：检查点只在「下载+转码+切分」全部成功后才写盘，
    所以它存在就说明前置产物完整。
    """
    settings, db = env
    settings.resume_enabled = True
    settings.chunk_retry_max = 0
    pipe = Pipeline(settings, db)
    engine = FakeEngine()
    pipe._engines = {"fake": engine}
    monkeypatch.setattr(pipeline_mod, "select_engine", lambda s, e=None: engine)

    # 链接任务：如果走完整流程就会真的发网络请求，这里必须避免
    task = pipe.create_task(source_type="url", source_value="https://example.com/v/1")
    work_dir = settings.work_dir / task.id
    work_dir.mkdir(parents=True, exist_ok=True)

    # 造出「上一轮已完成下载+转码+切分」的产物
    wav = work_dir / "audio.wav"
    wav.write_bytes(b"RIFF" + b"\0" * 200_000)
    db.update_fields(task.id, duration=400.0)

    chunks = _fake_chunks(work_dir, 4)
    for c in chunks:
        c.path.write_bytes(b"x")
    fingerprint = {"duration": 400.0, "max_chunk_sec": 110.0, "engine": "fake"}
    cp = Checkpoint(work_dir / "checkpoint.json", fingerprint)
    for i in range(2):
        cp.record(ChunkResult(index=i, text=f"第{i}段文字"))

    # 任何网络/ffmpeg 调用都判失败
    def boom(*a, **k):
        raise AssertionError("续跑快路不应重新下载或转码")

    monkeypatch.setattr(pipeline_mod, "extract_audio", boom)
    monkeypatch.setattr(pipeline_mod, "split_audio", lambda *a, **k: chunks)

    result = asyncio.run(pipe.run(task.id))

    assert result.status == TaskStatus.SUCCESS, result.error_message
    # 只转写了 2、3 两段，0、1 来自检查点
    assert sorted(idx for idx, _ in engine.calls) == [2, 3]
    assert "第0段文字" in (result.text_content or "")


def test_no_fast_path_when_wav_missing(env, tmp_path, monkeypatch):
    """检查点还在但 wav 丢了（用户清了中间产物）→ 必须老实重跑转码。

    注意：重转码后 wav 与上一轮内容一致，分片切点不变，所以检查点里的
    **转写结果仍然有效、应该继续复用**。要钉住的是「重新抽了音轨」，
    而不是「重新转写了」——后者反而说明实现浪费了已有的转写成果。
    """
    settings, db = env
    settings.resume_enabled = True
    settings.chunk_retry_max = 0
    pipe = Pipeline(settings, db)
    engine = FakeEngine()
    pipe._engines = {"fake": engine}
    monkeypatch.setattr(pipeline_mod, "select_engine", lambda s, e=None: engine)

    src = tmp_path / "in.m4a"
    src.write_bytes(b"audio")
    task = pipe.create_task(source_type="upload", source_value=str(src))
    work_dir = settings.work_dir / task.id
    work_dir.mkdir(parents=True, exist_ok=True)

    # 只有检查点，没有 audio.wav
    Checkpoint(
        work_dir / "checkpoint.json",
        {"duration": 300.0, "max_chunk_sec": 110.0, "engine": "fake"},
    ).record(ChunkResult(index=0, text="旧结果"))

    chunks = _fake_chunks(tmp_path, 3)
    patch_media(monkeypatch, tmp_path, chunks, duration=300.0)

    transcoded: list[Path] = []
    real_extract = pipeline_mod.extract_audio

    def spy_extract(src_arg, dst_arg, **kwargs):
        transcoded.append(Path(dst_arg))
        return real_extract(src_arg, dst_arg, **kwargs)

    monkeypatch.setattr(pipeline_mod, "extract_audio", spy_extract)

    result = asyncio.run(pipe.run(task.id))
    assert result.status == TaskStatus.SUCCESS
    assert transcoded, "wav 丢了就必须重新抽音轨"
    # 第 0 段的转写成果被复用了，只重跑第 1、2 段
    assert sorted(idx for idx, _ in engine.calls) == [1, 2]
    assert "旧结果" in (result.text_content or "")


def test_no_fast_path_when_fingerprint_changed(env, tmp_path, monkeypatch):
    """换了引擎（指纹不匹配）必须重跑，不能按旧下标拼接。"""
    settings, db = env
    settings.resume_enabled = True
    pipe = Pipeline(settings, db)

    class OtherEngine(FakeEngine):
        name = "other"

    engine = OtherEngine()
    pipe._engines = {"other": engine}
    monkeypatch.setattr(pipeline_mod, "select_engine", lambda s, e=None: engine)

    src = tmp_path / "in.m4a"
    src.write_bytes(b"audio")
    task = pipe.create_task(source_type="upload", source_value=str(src))
    work_dir = settings.work_dir / task.id
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "audio.wav").write_bytes(b"RIFF" + b"\0" * 1000)
    db.update_fields(task.id, duration=300.0)

    # 指纹里记的是 "fake"，现在引擎是 "other"
    Checkpoint(
        work_dir / "checkpoint.json",
        {"duration": 300.0, "max_chunk_sec": 110.0, "engine": "fake"},
    ).record(ChunkResult(index=0, text="旧结果"))

    patch_media(monkeypatch, tmp_path, _fake_chunks(tmp_path, 3), duration=300.0)
    result = asyncio.run(pipe.run(task.id))
    assert result.status == TaskStatus.SUCCESS
    assert sorted(idx for idx, _ in engine.calls) == [0, 1, 2]


# ---------------------------------------------------------------- ChunkResult
def test_chunk_result_serialization_roundtrip():
    original = ChunkResult(
        index=7,
        text="内容",
        segments=[Segment(start=1.0, end=2.0, text="内容")],
        language="zh",
    )
    restored = ChunkResult.from_dict(original.to_dict())
    assert restored.index == 7
    assert restored.text == "内容"
    assert restored.segments[0].start == 1.0
    assert restored.language == "zh"


# ================================================================ 语言探测
def test_language_probe_feeds_initial_prompt(tmp_path, monkeypatch):
    """没配 ASR_LANGUAGE 时必须先探测语言。

    为什么重要：各引擎的中文标点提示词依赖明确的 language，
    传 None 时提示词被跳过，输出是 "- 我是森森" 这种无标点文本。
    """
    from clip2text.config import Settings
    from clip2text.db import Database
    from clip2text.pipeline import Pipeline

    settings = Settings(data_dir=tmp_path / "d", asr_language="")
    settings.ensure_dirs()
    db = Database(settings.db_path)
    p = Pipeline(settings, db)

    class FakeEngine:
        name = "fake"
        calls: list = []

        def transcribe(self, audio, language=None):
            type(self).calls.append(language)
            from clip2text.models import Transcript

            return Transcript(text="测试", segments=[], language="zh")

    import asyncio

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    eng = FakeEngine()

    # 让切片函数直接产出一个假文件，不依赖 ffmpeg
    import clip2text.pipeline as mod

    monkeypatch.setattr(
        mod, "slice_audio", lambda *a, **k: (tmp_path / "probe.wav").write_bytes(b"x")
    )

    lang = asyncio.run(p._probe_language(eng, wav, tmp_path))
    assert lang == "zh"
    # 探测时不应预设语言，让引擎自己判断
    assert eng.calls == [None]


def test_configured_language_skips_probe(tmp_path):
    """配了 ASR_LANGUAGE 就不该再探测，省一次转写。"""
    from clip2text.config import Settings

    s = Settings(asr_language="zh")
    assert s.asr_language_or_none == "zh"

    s2 = Settings(asr_language="")
    assert s2.asr_language_or_none is None
