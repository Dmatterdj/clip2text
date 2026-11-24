"""前端可配置 API Key + 用量统计的后端测试。

契约：docs/SETTINGS-CONTRACT.md（§10 的 12 条全部覆盖，外加几条
「契约没写但一定会被踩」的：热更新、时区日界、删任务不删账本）。

全部离线：不联网、不加载真实模型，用临时目录。
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clip2text.config import DEFAULT_PRICING, Settings, get_settings, reset_settings_cache
from clip2text.db import Database
from clip2text.models import UsageSummary
from clip2text.settings_store import (
    FULL_MASK,
    SETTINGS_FILENAME,
    apply_overrides,
    load_overrides,
    mask_key,
    save_overrides,
    settings_path,
    validate_base_url,
    validate_model,
)
from clip2text.usage import (
    build_summary,
    estimate_cost,
    local_offset_minutes,
    normalize_pricing,
    record,
)

# ---------------------------------------------------------------- fixture
LONG_KEY = "sk-4f2aABCDEFGHIJKLMNOPQRSTUV9c1b"  # 足够长，走「前 6 后 4」分支


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def tmp_settings(data_dir: Path) -> Settings:
    s = Settings(data_dir=data_dir)
    s.ensure_dirs()
    return s


@pytest.fixture
def client(tmp_settings: Settings, monkeypatch):
    """临时数据目录 + 干净环境变量的 app。

    `monkeypatch.delenv` 很重要：开发者本机的 .env 里可能真有 key，
    不清掉的话「GET 不返回原文」「DELETE 回落」这类断言会假阳性。
    """
    for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    reset_settings_cache()

    from clip2text import state as state_mod

    state_mod.reset_state()
    from clip2text.app import create_app

    app = create_app()
    with TestClient(app) as c:
        c.state = state_mod.init_state(tmp_settings)  # type: ignore[attr-defined]
        yield c
    state_mod.reset_state()
    reset_settings_cache()


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "usage.db")


# ================================================================ §10.1 权限
def test_saved_file_mode_is_0600(data_dir: Path):
    path = save_overrides(data_dir, {"llm": {"api_key": LONG_KEY}})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_overwriting_existing_0644_file_restores_0600(data_dir: Path):
    """已存在的老文件可能是 0644（手工创建/旧版本写的）。

    open() 的 mode 参数对已存在文件无效，所以必须写完 chmod；
    这条测试就是防止有人把 chmod 删掉。
    """
    path = settings_path(data_dir)
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o644)
    save_overrides(data_dir, {"llm": {"api_key": LONG_KEY}})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_save_creates_settings_json_in_data_dir(data_dir: Path):
    path = save_overrides(data_dir, {"llm": {"api_key": LONG_KEY}})
    assert path == data_dir / SETTINGS_FILENAME
    assert path.exists()


# ================================================================ §10.2 不泄露
def test_get_never_returns_raw_key(client: TestClient):
    r = client.put(
        "/api/v1/settings/llm",
        json={"base_url": "https://api.deepseek.com", "api_key": LONG_KEY, "model": "deepseek-chat"},
    )
    assert r.status_code == 200

    body = client.get("/api/v1/settings").text
    assert LONG_KEY not in body
    # 掩码里的那两段也不该完整出现
    data = json.loads(body)
    assert LONG_KEY not in data["llm"]["api_key_masked"]
    assert "api_key" not in data["llm"], "响应里不能有 api_key 原文字段"
    assert data["llm"]["api_key_set"] is True


def test_put_response_never_returns_raw_key(client: TestClient):
    r = client.put(
        "/api/v1/settings/llm",
        json={"base_url": "https://api.deepseek.com", "api_key": LONG_KEY, "model": "m"},
    )
    assert LONG_KEY not in r.text


def test_deepseek_get_never_returns_raw_key(client: TestClient):
    client.put("/api/v1/settings/deepseek", json={"api_key": LONG_KEY})
    body = client.get("/api/v1/settings").text
    assert LONG_KEY not in body


def test_capabilities_has_no_key(client: TestClient):
    client.put("/api/v1/settings/deepseek", json={"api_key": LONG_KEY})
    body = client.get("/api/v1/capabilities").text
    assert LONG_KEY not in body


# ================================================================ §10.3 掩码
def test_mask_long_key_keeps_6_and_4():
    masked = mask_key(LONG_KEY)
    assert masked == f"{LONG_KEY[:6]}{'·' * 8}{LONG_KEY[-4:]}"
    assert masked.startswith("sk-4f2")
    assert masked.endswith("9c1b")


def test_mask_short_key_is_fully_masked():
    for key in ("a", "short", "sk-12345678", "x" * 11):
        assert mask_key(key) == FULL_MASK
        # 短 key 里任何一个字符都不能出现在掩码里
        assert all(ch == "·" for ch in mask_key(key))


def test_mask_boundary_hides_enough():
    """阈值要同时满足两条：藏起来的中间段 ≥8 字符，且露出的不超过总长一半。

    之前的实现用「12 字符」当阈值，结果 12 字符的 key 会露出 10 个、只藏 2 个，
    掩码形同虚设 —— 注释里自己写了这个问题但代码没改。现在要求 len >= 20。
    """
    # 30 字符是下限：露出的 10 个字符不超过总长的 1/3
    assert mask_key("x" * 30) == f"{'x' * 6}{'·' * 8}{'x' * 4}"
    # 29 及以下全掩码：露 10 个就超过 1/3 了
    for n in (10, 11, 12, 17, 18, 19, 20, 29):
        assert mask_key("x" * n) == FULL_MASK, f"{n} 字符的 key 应该全掩码"


def test_mask_never_reveals_more_than_third():
    """性质检查：任何长度下，露出的字符都不超过总长的 1/3。

    这是比「固定阈值」更本质的约束 —— 只要露出的比例足够小，
    即使拿到掩码也无法重建 key。
    """
    for n in range(1, 120):
        key = "y" * n
        masked = mask_key(key)
        revealed = sum(1 for ch in masked if ch == "y")
        assert revealed <= n / 3, f"len={n} 露出 {revealed}，超过 1/3"


def test_mask_empty_key():
    assert mask_key("") == ""


# ================================================================ §10.4 空值不修改
def test_empty_api_key_keeps_existing(client: TestClient):
    client.put("/api/v1/settings/llm", json={"base_url": "https://a.com", "api_key": LONG_KEY, "model": "m"})
    before = client.get("/api/v1/settings").json()["llm"]

    r = client.put("/api/v1/settings/llm", json={"base_url": "https://b.com", "api_key": "", "model": "m2"})
    assert r.status_code == 200
    after = r.json()["llm"]
    assert after["base_url"] == "https://b.com"
    assert after["model"] == "m2"
    # key 没动
    assert after["api_key_masked"] == before["api_key_masked"]
    assert after["api_key_set"] is True


def test_missing_api_key_keeps_existing(client: TestClient):
    client.put("/api/v1/settings/llm", json={"api_key": LONG_KEY})
    before = client.get("/api/v1/settings").json()["llm"]["api_key_masked"]
    r = client.put("/api/v1/settings/llm", json={"model": "other"})
    assert r.json()["llm"]["api_key_masked"] == before


def test_null_api_key_keeps_existing(client: TestClient):
    client.put("/api/v1/settings/llm", json={"api_key": LONG_KEY})
    before = client.get("/api/v1/settings").json()["llm"]["api_key_masked"]
    r = client.put("/api/v1/settings/llm", json={"api_key": None, "model": "x"})
    assert r.json()["llm"]["api_key_masked"] == before


# ================================================================ §10.5 DELETE 回落
def test_delete_llm_falls_back_to_env(client: TestClient, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "env-key-1234567890abcdef")
    # 让 AppState 重新解析基线（env 变了）
    client.state.reload_settings()

    client.put("/api/v1/settings/llm", json={"base_url": "https://ui.com", "api_key": LONG_KEY, "model": "ui-model"})
    view = client.get("/api/v1/settings").json()
    assert view["llm"]["source"] == "settings"
    assert view["llm"]["model"] == "ui-model"

    r = client.delete("/api/v1/settings/llm")
    assert r.status_code == 200
    assert r.json()["llm"]["source"] == "env"
    assert r.json()["llm"]["api_key_set"] is True
    # 文件里已经没有 llm 分节
    assert "llm" not in load_overrides(client.state.settings.data_dir)


def test_delete_deepseek_falls_back_to_env(client: TestClient, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-env-key-1234567890")
    client.state.reload_settings()
    client.put("/api/v1/settings/deepseek", json={"api_key": LONG_KEY})
    assert client.get("/api/v1/settings").json()["deepseek"]["source"] == "settings"

    r = client.delete("/api/v1/settings/deepseek")
    assert r.json()["deepseek"]["source"] == "env"


def test_delete_without_file_is_idempotent(client: TestClient):
    r = client.delete("/api/v1/settings/llm")
    assert r.status_code == 200
    assert r.json()["llm"]["source"] == "none"


# ================================================================ §10.6 优先级
def test_settings_json_overrides_env(client: TestClient, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    monkeypatch.setenv("LLM_API_KEY", "env-key-aaaaaaaaaaaa")
    client.state.reload_settings()

    client.put("/api/v1/settings/llm", json={"base_url": "https://ui.example.com", "model": "ui-model"})
    view = client.get("/api/v1/settings").json()
    assert view["llm"]["base_url"] == "https://ui.example.com"
    assert view["llm"]["model"] == "ui-model"
    # 只覆盖了 url/model，key 仍来自 env → source 必须是 env
    assert view["llm"]["source"] == "env"

    client.put("/api/v1/settings/llm", json={"api_key": LONG_KEY})
    assert client.get("/api/v1/settings").json()["llm"]["source"] == "settings"


def test_overrides_are_field_wise_not_wholesale(data_dir: Path, monkeypatch):
    """纵深覆盖：JSON 只写 key 时，url/model 仍然来自 env。"""
    monkeypatch.setenv("LLM_BASE_URL", "https://env-only.com")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    reset_settings_cache()

    base = Settings(llm_base_url="https://env-only.com", llm_model="env-model")
    merged = apply_overrides(base, {"llm": {"api_key": LONG_KEY}})
    assert merged.llm_api_key == LONG_KEY
    assert merged.llm_base_url == "https://env-only.com"
    assert merged.llm_model == "env-model"


def test_apply_overrides_ignores_empty_values():
    base = Settings(llm_base_url="https://keep.com", llm_model="keep-model", llm_api_key="keep-key")
    merged = apply_overrides(base, {"llm": {"base_url": "", "model": "   ", "api_key": ""}})
    assert merged.llm_base_url == "https://keep.com"
    assert merged.llm_model == "keep-model"
    assert merged.llm_api_key == "keep-key"


def test_get_settings_applies_file_overrides(data_dir: Path, monkeypatch):
    """`get_settings()` 本身必须把 settings.json 覆盖进来（不只是 apply_overrides）。

    走真实的 env → 文件 → 生效值这条链路，防止有人只改了一半：
    apply_overrides 单测通过、但 get_settings 忘了调用它。
    """
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("LLM_MODEL", "from-env")
    reset_settings_cache()

    save_overrides(data_dir, {"llm": {"api_key": LONG_KEY}})
    effective = get_settings()
    assert effective.llm_model == "from-env"  # 文件没写 → env 生效
    assert effective.llm_api_key == LONG_KEY  # 文件写了 → 覆盖

    save_overrides(data_dir, {"llm": {"model": "from-file"}})
    # settings.json 每次都重读，不需要清缓存也能看到变化
    assert get_settings().llm_model == "from-file"
    reset_settings_cache()


def test_pricing_override_merges_with_defaults(data_dir: Path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    reset_settings_cache()
    save_overrides(data_dir, {"pricing": {"asr_per_hour": 3.5}})
    pricing = get_settings().pricing
    assert pricing["asr_per_hour"] == 3.5
    # 没写的单价不能被当成 0
    assert pricing["llm_output_per_mtok"] == DEFAULT_PRICING["llm_output_per_mtok"]
    reset_settings_cache()


# ================================================================ §10.7 立即生效
def test_saving_key_makes_analysis_available_without_restart(client: TestClient):
    """同一进程内：写盘 → 不重启 → capabilities.analysis.available 变 true。

    这是最容易被「lru_cache + 构造时快照」搞坏的一条。
    """
    before = client.get("/api/v1/capabilities").json()
    assert before["analysis"]["available"] is False

    r = client.put(
        "/api/v1/settings/llm",
        json={"base_url": "https://api.deepseek.com", "api_key": LONG_KEY, "model": "deepseek-chat"},
    )
    assert r.status_code == 200
    assert r.json()["analysis_ready"] is True

    after = client.get("/api/v1/capabilities").json()
    assert after["analysis"]["available"] is True
    assert after["analysis"]["source"] == "llm"
    assert after["rewrite"]["available"] is True


def test_saving_deepseek_only_enables_analysis(client: TestClient):
    """只配 DeepSeek 时分析可用（兜底路径），改写仍不可用。"""
    assert client.get("/api/v1/capabilities").json()["analysis"]["available"] is False
    client.put("/api/v1/settings/deepseek", json={"api_key": LONG_KEY})
    data = client.get("/api/v1/capabilities").json()
    assert data["analysis"]["available"] is True
    assert data["analysis"]["source"] == "deepseek"


def test_external_file_edit_is_picked_up(client: TestClient):
    """外部手工改 settings.json（比如用户 vim 编辑）也要生效。"""
    assert client.get("/api/v1/capabilities").json()["analysis"]["available"] is False
    save_overrides(
        client.state.settings.data_dir,
        {"llm": {"base_url": "https://x.com", "api_key": LONG_KEY, "model": "m"}},
    )
    # 不重启、也不调 reload：capabilities 自己会 sync
    assert client.get("/api/v1/capabilities").json()["analysis"]["available"] is True


def test_delete_llm_disables_analysis_again(client: TestClient):
    client.put("/api/v1/settings/llm", json={"base_url": "https://a.com", "api_key": LONG_KEY, "model": "m"})
    assert client.get("/api/v1/capabilities").json()["analysis"]["available"] is True
    client.delete("/api/v1/settings/llm")
    assert client.get("/api/v1/capabilities").json()["analysis"]["available"] is False


# ================================================================ §10.8 校验
@pytest.mark.parametrize(
    "base_url",
    ["ftp://x.com", "api.deepseek.com", "https://x .com", "http://a\tb.com", "javascript:alert(1)"],
)
def test_invalid_base_url_returns_400(client: TestClient, base_url: str):
    r = client.put("/api/v1/settings/llm", json={"base_url": base_url, "model": "m"})
    assert r.status_code == 400
    assert r.json()["detail"]


def test_whitespace_base_url_is_treated_as_unchanged(client: TestClient):
    """全空白 = 没填（不是「填了一个非法的值」）。

    这条与「非法 base_url 必须 400」不冲突：`"   "` 与 `""` 一样表示
    用户没动这个字段，此时另一个字段仍然可以正常保存。
    """
    r = client.put("/api/v1/settings/llm", json={"base_url": "   ", "model": "m"})
    assert r.status_code == 200
    # 不能把空白写进文件
    assert r.json()["llm"]["base_url"] == ""


def test_empty_model_only_returns_400(client: TestClient):
    """契约 §10.8「空 model → 400」：只提交空 model 等于什么都没提交。"""
    r = client.put("/api/v1/settings/llm", json={"model": ""})
    assert r.status_code == 400
    assert r.json()["detail"]


def test_empty_fields_mean_unchanged(client: TestClient):
    """契约 §9.1：留空保存 = 不修改（不只是 api_key，model 同理）。

    前端表单往往把三个字段一起提交，改一个时另外两个是空字符串；
    如果空 model 一律 400，用户就永远只能改一半。
    """
    client.put("/api/v1/settings/llm", json={"base_url": "https://a.com", "model": "keep-model"})
    r = client.put("/api/v1/settings/llm", json={"base_url": "", "model": "", "api_key": ""})
    # 已配置过时，三个都空是一次幂等的「什么都不改」，返回 200 且原值不丢
    assert r.status_code == 200
    view = client.get("/api/v1/settings").json()["llm"]
    assert view["base_url"] == "https://a.com"
    assert view["model"] == "keep-model"


def test_too_long_model_returns_400(client: TestClient):
    r = client.put("/api/v1/settings/llm", json={"model": "m" * 101})
    assert r.status_code == 400


def test_too_long_api_key_returns_400(client: TestClient):
    r = client.put("/api/v1/settings/llm", json={"api_key": "k" * 501})
    assert r.status_code == 400


def test_invalid_payload_does_not_write_file(client: TestClient):
    client.put("/api/v1/settings/llm", json={"base_url": "ftp://bad"})
    assert not settings_path(client.state.settings.data_dir).exists()


def test_validators_direct():
    assert validate_base_url("https://ok.com") == "https://ok.com"
    assert validate_base_url("  http://localhost:11434  ") == "http://localhost:11434"
    with pytest.raises(ValueError):
        validate_base_url("https://x.com/path with space")
    with pytest.raises(ValueError):
        validate_model("")
    assert validate_model("deepseek-chat") == "deepseek-chat"


# ================================================================ §10.9 埋点
def test_record_asr(db: Database):
    assert record(db, "asr", task_id="t1", model="whisper", audio_seconds=120.5) is True
    rows = db.list_usage_events()
    assert len(rows) == 1
    assert rows[0]["kind"] == "asr"
    assert rows[0]["task_id"] == "t1"
    assert rows[0]["model"] == "whisper"
    assert rows[0]["audio_seconds"] == 120.5
    assert rows[0]["images"] == 0


def test_record_ocr(db: Database):
    record(db, "ocr", task_id="t2", model="rapidocr", images=3)
    row = db.list_usage_events()[0]
    assert row["kind"] == "ocr"
    assert row["images"] == 3


def test_record_analysis(db: Database):
    record(db, "analysis", task_id="t3", model="deepseek-chat", prompt_tokens=1000, completion_tokens=200)
    row = db.list_usage_events()[0]
    assert row["kind"] == "analysis"
    assert row["prompt_tokens"] == 1000
    assert row["completion_tokens"] == 200


def test_record_rewrite(db: Database):
    record(db, "rewrite", task_id="t4", model="m", prompt_tokens=10, completion_tokens=5)
    assert db.list_usage_events()[0]["kind"] == "rewrite"


def test_pipeline_asr_records_usage(tmp_path: Path, monkeypatch):
    """真实的 pipeline.run()：转写成功 → 落一条 asr 用量。

    用假引擎 + 假的 ffmpeg 边界跑通全流程，不联网也不加载模型。
    注意要 patch `clip2text.pipeline.xxx`（模块里 import 进来的名字），
    不是 `clip2text.media.xxx` —— 后者改了 pipeline 看不到。
    """
    import asyncio

    from clip2text import asr as asr_mod
    from clip2text import pipeline as pipeline_mod
    from clip2text.models import Segment, Transcript
    from clip2text.pipeline import Pipeline

    class FakeEngine:
        name = "fake-asr"
        supports_timestamps = True
        max_chunk_sec = None

        def available(self) -> bool:
            return True

        def unavailable_reason(self):
            return None

        def transcribe(self, audio, language=None):
            return Transcript(
                text="测试文本",
                segments=[Segment(start=0.0, end=1.0, text="测试文本")],
                language="zh",
            )

    # pipeline.py 在 import 时就把 build_engines/select_engine 绑进了自己的
    # 命名空间，所以两个模块都要打补丁（_asr_mod 版本保持原样也无害）。
    monkeypatch.setattr(asr_mod, "build_engines", lambda settings: {"fake": FakeEngine()})
    monkeypatch.setattr(asr_mod, "select_engine", lambda settings, engines=None: FakeEngine())
    monkeypatch.setattr(pipeline_mod, "build_engines", lambda settings: {"fake": FakeEngine()})
    monkeypatch.setattr(pipeline_mod, "select_engine", lambda settings, engines=None: FakeEngine())

    settings = Settings(data_dir=tmp_path / "d")
    settings.ensure_dirs()
    pdb = Database(settings.db_path)
    pipeline = Pipeline(settings, pdb)

    src = tmp_path / "a.mp3"
    src.write_bytes(b"fake-audio")
    task = pipeline.create_task(source_type="upload", source_value=str(src))

    # 全部绕过 ffmpeg：探测给固定时长，抽音轨/切分/探测语言都返回桩
    monkeypatch.setattr(pipeline_mod, "probe_duration", lambda *a, **k: 90.0)
    monkeypatch.setattr(pipeline_mod, "extract_audio", lambda *a, **k: tmp_path / "audio.wav")
    monkeypatch.setattr(pipeline_mod, "split_audio", lambda *a, **k: [])
    monkeypatch.setattr(pipeline_mod, "slice_audio", lambda *a, **k: None)

    asyncio.run(pipeline.run(task.id))

    final = pdb.get(task.id)
    assert final is not None and final.status.value == "SUCCESS"
    events = [e for e in pdb.list_usage_events() if e["kind"] == "asr"]
    assert len(events) == 1
    assert events[0]["model"] == "fake-asr"
    assert events[0]["task_id"] == task.id
    assert events[0]["audio_seconds"] == pytest.approx(90.0)


def test_pipeline_ocr_records_usage(tmp_path: Path, monkeypatch):
    """OCR 分支：成功 → 落一条 ocr 用量，images=送去识别的张数。"""
    import asyncio

    from clip2text import ocr as ocr_mod
    from clip2text.ocr.base import OCRLine, OCRResult
    from clip2text.pipeline import Pipeline

    def fake_recognize(images, settings, on_progress=None):
        if on_progress:
            on_progress(1.0)
        return OCRResult(
            text="识别文本",
            lines=[OCRLine(text="识别文本")],
            engine="fakeocr",
        )

    monkeypatch.setattr(ocr_mod, "recognize_images", fake_recognize)

    settings = Settings(data_dir=tmp_path / "d2")
    settings.ensure_dirs()
    odb = Database(settings.db_path)
    pipeline = Pipeline(settings, odb)

    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG")
    task = pipeline.create_task(
        source_type="upload", source_value=str(img), work_type="image"
    )

    asyncio.run(pipeline.run(task.id))

    final = odb.get(task.id)
    assert final is not None and final.status.value == "SUCCESS"
    ocr_events = [e for e in odb.list_usage_events() if e["kind"] == "ocr"]
    assert len(ocr_events) == 1
    assert ocr_events[0]["images"] == 1
    assert ocr_events[0]["model"] == "fakeocr"


# ================================================================ §10.10 聚合
def _seed(db: Database) -> None:
    base = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    db.add_usage_event(
        kind="asr", task_id="a", model="whisper", audio_seconds=3600.0, created_at=base
    )
    db.add_usage_event(
        kind="asr", task_id="b", model="whisper", audio_seconds=1800.0,
        created_at=base - timedelta(days=1),
    )
    db.add_usage_event(
        kind="ocr", task_id="c", model="rapidocr", images=100, created_at=base
    )
    db.add_usage_event(
        kind="analysis", task_id="d", model="deepseek-chat",
        prompt_tokens=1_000_000, completion_tokens=1_000_000,
        created_at=base - timedelta(days=2),
    )


def test_usage_by_kind(db: Database):
    _seed(db)
    summary = build_summary(db, days=30, pricing=DEFAULT_PRICING, now=datetime(2026, 11, 24, 12, 0, tzinfo=UTC))
    kinds = {b.key: b for b in summary.by_kind}
    assert kinds["asr"].audio_seconds == 5400.0
    assert kinds["ocr"].images == 100
    assert kinds["analysis"].prompt_tokens == 1_000_000
    assert kinds["rewrite"].audio_seconds == 0  # 没数据也要在列表里
    assert [b.key for b in summary.by_kind] == ["asr", "ocr", "analysis", "rewrite"]


def test_usage_by_model(db: Database):
    _seed(db)
    summary = build_summary(db, days=30, pricing=DEFAULT_PRICING, now=datetime(2026, 11, 24, 12, 0, tzinfo=UTC))
    models = {b.key: b for b in summary.by_model}
    assert models["whisper"].audio_seconds == 5400.0
    assert models["rapidocr"].images == 100
    assert models["deepseek-chat"].prompt_tokens == 1_000_000
    # 按估算费用倒序：deepseek-chat（¥5）> whisper（¥1.17）> rapidocr（¥0.15）
    assert [b.key for b in summary.by_model][0] == "deepseek-chat"


def test_usage_total_and_event_count(db: Database):
    _seed(db)
    summary = build_summary(db, days=30, pricing=DEFAULT_PRICING, now=datetime(2026, 11, 24, 12, 0, tzinfo=UTC))
    assert summary.total.key == "total"
    assert summary.event_count == 4
    assert summary.total.audio_seconds == 5400.0
    assert summary.total.images == 100
    assert summary.currency == "CNY"


def test_usage_by_day_desc(db: Database):
    _seed(db)
    summary = build_summary(db, days=30, pricing=DEFAULT_PRICING, now=datetime(2026, 11, 24, 12, 0, tzinfo=UTC))
    days = [b.key for b in summary.by_day]
    assert days == sorted(days, reverse=True)
    assert "2026-11-24" in days
    assert "2026-11-23" in days
    assert "2026-11-22" in days


def test_usage_by_day_window():
    """最近 N 天窗口：7 天只回 7 天内的日期。"""
    db = Database(":memory:")
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    db.add_usage_event(kind="asr", audio_seconds=1, created_at=now - timedelta(days=2))
    db.add_usage_event(kind="asr", audio_seconds=2, created_at=now - timedelta(days=30))
    summary = build_summary(db, days=7, pricing=DEFAULT_PRICING, now=now, offset_minutes=0)
    assert len(summary.by_day) == 1
    # total 是全量，不受窗口限制
    assert summary.total.audio_seconds == 3


# ================================================================ §10.11 计价
def test_estimate_cost_formula():
    cost = estimate_cost(
        audio_seconds=3600,
        images=1000,
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        pricing={
            "asr_per_hour": 0.78,
            "ocr_per_image": 0.0015,
            "llm_input_per_mtok": 1.0,
            "llm_output_per_mtok": 4.0,
        },
    )
    # 0.78 + 1.5 + 1.0 + 4.0
    assert cost == pytest.approx(7.28)


def test_estimate_cost_zero():
    assert estimate_cost() == 0


def test_estimate_cost_uses_custom_pricing():
    cost = estimate_cost(audio_seconds=3600, pricing={"asr_per_hour": 2.0})
    assert cost == pytest.approx(2.0)


def test_summary_cost_matches_bucket(db: Database):
    _seed(db)
    summary = build_summary(db, days=30, pricing=DEFAULT_PRICING, now=datetime(2026, 11, 24, 12, 0, tzinfo=UTC))
    asr = next(b for b in summary.by_kind if b.key == "asr")
    assert asr.estimated_cost == pytest.approx(5400 / 3600 * 0.78, abs=1e-6)
    # 各项之和 = 总估算
    assert summary.total.estimated_cost == pytest.approx(
        sum(b.estimated_cost for b in summary.by_kind), abs=1e-5
    )


def test_normalize_pricing_fills_defaults():
    p = normalize_pricing({"asr_per_hour": 2.0})
    assert p["asr_per_hour"] == 2.0
    assert p["ocr_per_image"] == DEFAULT_PRICING["ocr_per_image"]
    assert set(p) == set(DEFAULT_PRICING)


def test_put_pricing_takes_effect(client: TestClient):
    r = client.put("/api/v1/settings/pricing", json={"asr_per_hour": 9.9})
    assert r.status_code == 200

    client.state.db.add_usage_event(kind="asr", audio_seconds=3600, model="x")
    usage = client.get("/api/v1/usage").json()
    assert usage["pricing"]["asr_per_hour"] == 9.9
    assert usage["total"]["estimated_cost"] == pytest.approx(9.9)


def test_put_pricing_rejects_negative(client: TestClient):
    r = client.put("/api/v1/settings/pricing", json={"asr_per_hour": -1})
    assert r.status_code == 400


# ================================================================ §10.12 埋点失败不影响
def test_record_swallows_db_error():
    class Boom:
        def add_usage_event(self, **kwargs):
            raise sqlite_error()

    def sqlite_error() -> Exception:
        import sqlite3

        return sqlite3.OperationalError("disk I/O error")

    assert record(Boom(), "asr", task_id="t", audio_seconds=1) is False


def test_usage_endpoint_still_works_when_recording_fails(client: TestClient, monkeypatch):
    def boom(*args, **kwargs):
        import sqlite3

        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(client.state.db, "add_usage_event", boom)
    # 埋点失败
    assert record(client.state.db, "asr", audio_seconds=10) is False
    # 读取接口不受影响
    r = client.get("/api/v1/usage")
    assert r.status_code == 200


def test_analysis_usage_failure_does_not_break_job(tmp_path, monkeypatch):
    """分析埋点失败，分析结果照样落库。"""
    from clip2text.analysis import build_job
    from clip2text.llm import LLMConfig
    from clip2text.models import Task

    db = Database(tmp_path / "x.db")
    task = Task(id="t1", source_type="upload", source_value="/x", text_content="正文" * 100)

    async def fake_run(*args, **kwargs):
        from clip2text.models import Analysis

        return Analysis(task_id="t1", model="m", state="SUCCESS", token_usage={"prompt": 10, "completion": 5})

    monkeypatch.setattr("clip2text.analysis.run_analysis", fake_run)
    monkeypatch.setattr(db, "add_usage_event", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    from clip2text.analysis import AnalysisRuns

    runs = AnalysisRuns()
    job = build_job(task=task, config=LLMConfig(base_url="https://x", api_key="k", model="m"), db=db, runs=runs)

    import asyncio

    asyncio.run(job())
    stored = db.get_analysis("t1")
    assert stored is not None
    assert stored.state == "SUCCESS"


# ================================================================ 删任务不删账本
def test_delete_task_keeps_usage(client: TestClient):
    db = client.state.db
    db.add_usage_event(kind="asr", task_id="keepme", audio_seconds=60)
    db.add_usage_event(kind="ocr", task_id="keepme", images=2)

    from clip2text.models import Task

    db.create(Task(id="keepme", source_type="upload", source_value="/x"))
    r = client.delete("/api/v1/tasks/keepme")
    assert r.status_code == 204
    assert db.get("keepme") is None

    events = db.list_usage_events()
    assert len(events) == 2
    assert {e["task_id"] for e in events} == {"keepme"}
    assert client.get("/api/v1/usage").json()["event_count"] == 2


def test_delete_all_usage(client: TestClient):
    client.state.db.add_usage_event(kind="asr", audio_seconds=60)
    r = client.delete("/api/v1/usage")
    assert r.status_code == 204
    assert client.get("/api/v1/usage").json()["event_count"] == 0


# ================================================================ 时区日界（契约外）
def test_local_offset_minutes_is_nonzero_for_asia_shanghai(monkeypatch):
    """东八区应为 +480 分钟。这条防止「直接写 UTC 日界」。"""
    import time

    if time.localtime().tm_gmtoff // 60 != 480:
        pytest.skip("当前时区不是东八区")
    assert local_offset_minutes() == 480


def test_by_day_uses_local_offset_not_utc():
    """UTC 23:59 的记录，东八区必须归到第二天。

    这条是 lead 实测出来的坑：SQLite 的 date() 会忽略时区偏移，
    直接 date('2026-11-23T23:59:00+00:00') 得到 11-23，
    而北京用户在本地时间看到的那一天是 11-24。
    """
    db = Database(":memory:")
    db.add_usage_event(
        kind="asr",
        audio_seconds=60,
        created_at=datetime(2026, 11, 23, 23, 59, tzinfo=UTC),
    )
    summary = build_summary(
        db, days=30, pricing=DEFAULT_PRICING,
        now=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
        offset_minutes=480,
    )
    assert [b.key for b in summary.by_day] == ["2026-11-24"]


def test_by_day_offset_zero_is_utc_day():
    """对照：offset=0 时同一条记录落在 11-23（证明偏移真的起作用）。"""
    db = Database(":memory:")
    db.add_usage_event(
        kind="asr",
        audio_seconds=60,
        created_at=datetime(2026, 11, 23, 23, 59, tzinfo=UTC),
    )
    summary = build_summary(
        db, days=30, pricing=DEFAULT_PRICING,
        now=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
        offset_minutes=0,
    )
    assert [b.key for b in summary.by_day] == ["2026-11-23"]


def test_by_day_half_hour_timezone():
    """印度 +05:30：20:00 UTC → 次日 01:30 本地，必须归到次日。

    用 hours 修饰符会把 -5.5 算错，所以实现里用的是 minutes。
    """
    db = Database(":memory:")
    db.add_usage_event(
        kind="asr",
        audio_seconds=1,
        created_at=datetime(2026, 11, 23, 20, 0, tzinfo=UTC),
    )
    summary = build_summary(
        db, days=30, pricing=DEFAULT_PRICING,
        now=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
        offset_minutes=330,
    )
    assert [b.key for b in summary.by_day] == ["2026-11-24"]


# ================================================================ 契约外：健壮性
def test_broken_settings_json_does_not_crash(data_dir: Path):
    (data_dir / SETTINGS_FILENAME).write_text("{not json", encoding="utf-8")
    assert load_overrides(data_dir) == {}


def test_settings_json_with_wrong_types_is_sanitized(data_dir: Path):
    (data_dir / SETTINGS_FILENAME).write_text(
        json.dumps({"llm": {"api_key": 123, "model": None}, "pricing": "nope"}),
        encoding="utf-8",
    )
    assert load_overrides(data_dir) == {}


def test_unknown_sections_are_ignored(data_dir: Path):
    (data_dir / SETTINGS_FILENAME).write_text(
        json.dumps({"evil": {"x": 1}, "llm": {"model": "m"}}), encoding="utf-8"
    )
    assert load_overrides(data_dir) == {"llm": {"model": "m"}}


def test_settings_view_shape(client: TestClient):
    data = client.get("/api/v1/settings").json()
    assert set(data) == {"llm", "deepseek", "analysis_ready", "config_path"}
    assert set(data["llm"]) == {"base_url", "api_key_masked", "api_key_set", "model", "source"}
    assert set(data["deepseek"]) == {"api_key_masked", "api_key_set", "base_url", "source"}
    assert data["config_path"].endswith(SETTINGS_FILENAME)


def test_usage_response_shape(client: TestClient):
    data = client.get("/api/v1/usage").json()
    assert set(data) == {
        "total", "by_day", "by_kind", "by_model", "event_count", "currency", "pricing",
    }
    assert data["currency"] == "CNY"
    assert data["total"]["key"] == "total"
    # 模型校验一遍，保证和契约字段名一致
    UsageSummary.model_validate(data)


def test_usage_days_query_validation(client: TestClient):
    assert client.get("/api/v1/usage?days=0").status_code == 422
    assert client.get("/api/v1/usage?days=99999").status_code == 422
    assert client.get("/api/v1/usage?days=7").status_code == 200


def test_settings_file_path_respects_data_dir(client: TestClient):
    assert client.get("/api/v1/settings").json()["config_path"] == str(
        client.state.settings.data_dir / SETTINGS_FILENAME
    )


def test_capabilities_exposes_settings_entry(client: TestClient):
    data = client.get("/api/v1/capabilities").json()
    assert data["settings"]["editable"] is True
    assert data["settings"]["hot_reload"] is True
    assert data["settings"]["config_path"].endswith(SETTINGS_FILENAME)
    assert data["usage"]["available"] is True


def test_test_connection_reports_not_configured(client: TestClient):
    r = client.post("/api/v1/settings/test", json={"target": "llm"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_test_connection_redacts_upstream_echo(client: TestClient, monkeypatch):
    """真机发现的坑：上游报错 body 可能原样回显 key。

    DeepSeek 实测返回过 `Your api key: ****9c1b`（已打码），但不能指望
    每家网关都打码。所以 /settings/test 在返回前必须自己再替换一次。
    """
    from clip2text.api import settings_routes as sr

    client.put(
        "/api/v1/settings/llm",
        json={"base_url": "https://api.deepseek.com", "api_key": LONG_KEY, "model": "m"},
    )

    async def echo_key(config, messages, *, temperature=0.2):
        raise RuntimeError(f"Authentication Fails, Your api key: {config.api_key} is invalid")

    monkeypatch.setattr("clip2text.analysis.chat_completion", echo_key)
    r = client.post("/api/v1/settings/test", json={"target": "llm"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert LONG_KEY not in r.text
    # 掩码仍能让人看出「是哪个 key 被拒了」
    assert "········" in r.json()["reason"]
    # 直接单测那个 helper，防止有人绕过路由
    assert LONG_KEY not in sr._redact_secret(f"bad key {LONG_KEY}", LONG_KEY)
    # 前端读的是 res.detail（与项目其它错误一致），所以两个字段都要有
    assert r.json()["detail"] == r.json()["reason"]


def test_logs_do_not_contain_key(client: TestClient, caplog):
    """契约 §2.2：任何 log 调用都不得含 key 原文。"""
    import logging

    caplog.set_level(logging.DEBUG)
    client.put(
        "/api/v1/settings/llm",
        json={"base_url": "https://api.deepseek.com", "api_key": LONG_KEY, "model": "deepseek-chat"},
    )
    client.put("/api/v1/settings/deepseek", json={"api_key": LONG_KEY})
    client.get("/api/v1/settings")
    assert LONG_KEY not in caplog.text


# ================================================================ 改写埋点
def test_rewrite_usage_recorded_from_stream(tmp_path, monkeypatch):
    """流式 usage 帧被采集并落到 rewrite 账上。"""
    import asyncio

    from clip2text import llm as llm_mod

    captured: list[dict] = []
    llm_mod.set_usage_sink(captured.append)
    try:
        sse = (
            'data: {"choices":[{"delta":{"content":"你好"}}]}\n'
            'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":7}}\n'
            "data: [DONE]\n"
        )

        class FakeStream:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def aread(self):
                return b""

            async def aiter_lines(self):
                for line in sse.splitlines():
                    yield line

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def stream(self, *a, **k):
                return FakeStream()

        monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeClient)

        cfg = llm_mod.LLMConfig(base_url="https://x", api_key="k", model="m")
        seen: dict = {}
        text = asyncio.run(llm_mod.rewrite_once("原文", cfg, task_id="t9", usage_out=seen))
        assert text == "你好"
        assert seen == {"prompt_tokens": 12, "completion_tokens": 7}
        assert captured and captured[0]["prompt_tokens"] == 12
        assert captured[0]["task_id"] == "t9"
    finally:
        llm_mod.set_usage_sink(None)


def test_rewrite_does_not_record_when_upstream_rejects(monkeypatch):
    """401 不该记一条全 0 的 rewrite（没产生 token，也没有可计费的输出）。"""
    import asyncio

    from clip2text import llm as llm_mod

    captured: list[dict] = []
    llm_mod.set_usage_sink(captured.append)
    try:

        class FakeStream:
            status_code = 401

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def aread(self):
                return b'{"error":"invalid key"}'

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def stream(self, *a, **k):
                return FakeStream()

        monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeClient)

        cfg = llm_mod.LLMConfig(base_url="https://x", api_key="k", model="m")
        with pytest.raises(llm_mod.LLMError):
            asyncio.run(llm_mod.rewrite_once("原文", cfg))
        assert captured == []
    finally:
        llm_mod.set_usage_sink(None)


def test_usage_sink_exception_is_swallowed(monkeypatch):
    """sink 自己炸了不能影响改写输出。"""
    import asyncio

    from clip2text import llm as llm_mod

    def boom(payload):
        raise RuntimeError("sink down")

    llm_mod.set_usage_sink(boom)
    try:
        sse = 'data: {"choices":[{"delta":{"content":"ok"}}]}\ndata: [DONE]\n'

        class FakeStream:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def aread(self):
                return b""

            async def aiter_lines(self):
                for line in sse.splitlines():
                    yield line

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def stream(self, *a, **k):
                return FakeStream()

        monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeClient)
        cfg = llm_mod.LLMConfig(base_url="https://x", api_key="k", model="m")
        assert asyncio.run(llm_mod.rewrite_once("原文", cfg)) == "ok"
    finally:
        llm_mod.set_usage_sink(None)
