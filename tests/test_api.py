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
    # 默认必须关闭链接解析
    assert data["extractors"]["enabled"] is False


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


def test_create_task_url_disabled_by_default(client):
    r = client.post(
        "/api/v1/tasks",
        json={"source_type": "url", "source_value": "https://example.com/v/1"},
    )
    assert r.status_code == 400
    assert "默认关闭" in r.json()["detail"]


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
