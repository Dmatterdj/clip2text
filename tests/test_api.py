"""API 端到端测试。

设计原则：测试不需要真实 ASR 模型也能跑（用假引擎），
否则 CI 上会下载 1.5GB 模型。真实引擎的测试标记为 slow。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clip2text.config import Settings
from clip2text.models import Segment, Transcript


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path / "data")
    s.ensure_dirs()
    return s


@pytest.fixture
def client(tmp_settings, monkeypatch):
    """用临时数据目录 + 假 ASR 引擎构造 app。"""
    from clip2text import asr as asr_mod
    from clip2text import state as state_mod

    class FakeEngine:
        name = "fake"
        supports_timestamps = True
        max_chunk_sec = None

        def available(self) -> bool:
            return True

        def unavailable_reason(self):
            return None

        def transcribe(self, audio, language=None):
            return Transcript(
                text="这是一个测试转写结果。",
                segments=[
                    Segment(start=0.0, end=1.0, text="这是一个"),
                    Segment(start=1.0, end=2.0, text="测试转写结果。"),
                ],
                language="zh",
            )

    monkeypatch.setattr(
        asr_mod, "build_engines", lambda settings: {"fake": FakeEngine()}
    )
    monkeypatch.setattr(
        asr_mod, "select_engine", lambda settings, engines=None: FakeEngine()
    )

    state_mod.reset_state()
    from clip2text.app import create_app

    app = create_app()
    with TestClient(app) as c:
        c.state = state_mod.init_state(tmp_settings)  # type: ignore[attr-defined]
        # 让 lifespan 用临时目录
        yield c
    state_mod.reset_state()


# ---------------- 基础 ----------------
def test_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_capabilities_shape(client):
    r = client.get("/api/v1/capabilities")
    assert r.status_code == 200
    data = r.json()
    for key in ("version", "ffmpeg", "asr", "ocr", "llm", "extractors", "exporters"):
        assert key in data
    assert data["exporters"] == ["txt", "markdown", "docx", "xlsx"]
    # 链接解析默认必须开启：这个工具的主场景就是「贴链接 → 出文字」
    assert data["extractors"]["enabled"] is True
    assert data["extractors"]["reason"] is None
    # 平台清单要真实可用，前端靠它渲染「支持哪些平台」
    catalog = data["extractors"]["catalog"]
    names = {item["name"] for item in catalog}
    assert {"bilibili", "xiaoyuzhou", "youtube"} <= names
    for item in catalog:
        assert set(item) >= {"name", "label", "domains", "available"}
        assert isinstance(item["domains"], list) and item["domains"]
    # 内置适配器（B 站 / 小宇宙 / RSS）不依赖 yt-dlp，必须始终可用
    builtin = {i["name"]: i for i in catalog if i["via"] == "builtin"}
    assert builtin["bilibili"]["available"] is True
    assert builtin["xiaoyuzhou"]["available"] is True


def test_capabilities_limits_expose_long_content_guards(client):
    data = client.get("/api/v1/capabilities").json()
    limits = data["limits"]
    assert limits["max_media_duration_min"] == 180
    assert limits["max_media_size_mb"] == 2048
    assert limits["chunk_retry_max"] == 2
    assert limits["resume_enabled"] is True


def test_unknown_route(client):
    assert client.get("/api/v1/tasks/nope").status_code == 404


# ---------------- 上传 ----------------
def test_upload_rejects_unsupported_type(client):
    r = client.post(
        "/api/v1/uploads", files={"file": ("a.txt", b"hello", "text/plain")}
    )
    assert r.status_code == 400
    assert "不支持" in r.json()["detail"]


def test_upload_empty_file(client):
    r = client.post("/api/v1/uploads", files={"file": ("a.wav", b"", "audio/wav")})
    assert r.status_code == 400


def test_upload_ok(client):
    r = client.post(
        "/api/v1/uploads", files={"file": ("a.wav", b"RIFFxxxx", "audio/wav")}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["size"] == 8
    assert data["work_type"] == "audio"
    assert Path(data["path"]).exists()


# ---------------- 任务 ----------------
def test_create_task_rejects_missing_source(client):
    r = client.post("/api/v1/tasks", json={"source_type": "upload", "source_value": ""})
    assert r.status_code == 400


def test_create_task_rejects_outside_uploads(client, tmp_path):
    outside = tmp_path / "evil.wav"
    outside.write_bytes(b"x")
    r = client.post(
        "/api/v1/tasks", json={"source_type": "upload", "source_value": str(outside)}
    )
    assert r.status_code == 400
    assert "uploads" in r.json()["detail"]


def test_create_task_url_allowed_by_default(client, monkeypatch):
    """默认配置下贴链接就能建任务 —— 这是产品的主入口。

    不真跑流水线：把 pipeline.start 换成记录器，避免测试里去连真实网络。
    """
    started: list[str] = []
    st = client.state  # type: ignore[attr-defined]
    monkeypatch.setattr(st.pipeline, "start", started.append)

    r = client.post(
        "/api/v1/tasks",
        json={"source_type": "url", "source_value": "https://www.bilibili.com/video/BV1xx411c7mD"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source_type"] == "url"
    assert body["status"] == "QUEUED"


def test_create_task_url_rejected_when_explicitly_disabled(client, monkeypatch):
    """合规兜底：显式关掉之后必须拒绝，并在报错里说清楚怎么恢复。"""
    st = client.state  # type: ignore[attr-defined]
    monkeypatch.setattr(st.settings, "extractors_enabled", False)

    r = client.post(
        "/api/v1/tasks",
        json={"source_type": "url", "source_value": "https://example.com/v/1"},
    )
    assert r.status_code == 400
    assert "显式关闭" in r.json()["detail"]


def test_capabilities_platforms_empty_when_disabled(client, monkeypatch):
    st = client.state  # type: ignore[attr-defined]
    monkeypatch.setattr(st.settings, "extractors_enabled", False)
    data = client.get("/api/v1/capabilities").json()
    assert data["extractors"]["catalog"] == []
    assert "显式关闭" in data["extractors"]["reason"]


def test_task_list_and_delete(client):
    up = client.post(
        "/api/v1/uploads", files={"file": ("a.wav", b"RIFFxxxx", "audio/wav")}
    ).json()
    t = client.post(
        "/api/v1/tasks",
        json={"source_type": "upload", "source_value": up["path"], "title": "T"},
    ).json()

    listed = client.get("/api/v1/tasks").json()
    assert listed["total"] >= 1
    assert any(i["id"] == t["id"] for i in listed["items"])

    assert client.delete(f"/api/v1/tasks/{t['id']}").status_code == 204
    assert client.get(f"/api/v1/tasks/{t['id']}").status_code == 404


# ---------------- 导出 ----------------
def test_export_all_formats(client):
    from clip2text.exporters import SUPPORTED

    up = client.post(
        "/api/v1/uploads", files={"file": ("a.wav", b"RIFFxxxx", "audio/wav")}
    ).json()
    task = client.post(
        "/api/v1/tasks",
        json={"source_type": "upload", "source_value": up["path"], "title": "导出测试"},
    ).json()

    # 直接往库里塞结果，跳过真实转写

    st = client.state  # type: ignore[attr-defined]
    st.db.update_fields(
        task["id"],
        status="SUCCESS",
        stage="done",
        progress=100,
        text_content="测试文本内容",
        segments='[{"start":0,"end":1,"text":"测试文本内容"}]',
        language="zh",
    )

    for fmt in SUPPORTED:
        r = client.post(f"/api/v1/tasks/{task['id']}/export", json={"format": fmt})
        assert r.status_code == 200, f"{fmt} 导出失败: {r.text}"
        assert len(r.content) > 0
        assert "attachment" in r.headers["content-disposition"]


def test_export_bad_format(client):
    up = client.post(
        "/api/v1/uploads", files={"file": ("a.wav", b"RIFFxxxx", "audio/wav")}
    ).json()
    task = client.post(
        "/api/v1/tasks", json={"source_type": "upload", "source_value": up["path"]}
    ).json()
    r = client.post(f"/api/v1/tasks/{task['id']}/export", json={"format": "pdf"})
    assert r.status_code == 400
