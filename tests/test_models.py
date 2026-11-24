"""模型探测（GET /v1/models）的测试。

全部用假 HTTP 响应，不联网。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from clip2text.llm import LLMConfig, LLMError, list_models


def fake_get(payload: object, status: int = 200, text: str | None = None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.text = text if text is not None else json.dumps(payload, ensure_ascii=False)
    return patch("clip2text.llm.httpx.get", return_value=resp)


# ---------------------------------------------------------------- URL 归一化
@pytest.mark.parametrize(
    "base",
    [
        "https://api.deepseek.com",
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com/v1/",
        "https://api.deepseek.com/",
    ],
)
def test_api_base_normalizes_all_variants(base):
    cfg = LLMConfig(base, "k", "m")
    assert cfg.api_base() == "https://api.deepseek.com/v1"
    assert cfg.models_endpoint() == "https://api.deepseek.com/v1/models"
    assert cfg.endpoint() == "https://api.deepseek.com/v1/chat/completions"


# ---------------------------------------------------------------- 返回格式兼容
def test_list_models_openai_standard():
    with fake_get(
        {
            "object": "list",
            "data": [
                {"id": "m-a", "object": "model", "owned_by": "x"},
                {"id": "m-b", "context_window": 65536, "max_output_tokens": 8192},
            ],
        }
    ):
        models = list_models("https://x.com", "k")
    ids = [m.id for m in models]
    assert ids == ["m-a", "m-b"]
    assert models[1].context_window == 65536
    assert models[1].max_output_tokens == 8192


def test_list_models_bare_array():
    """少数网关直接返回数组。"""
    with fake_get([{"id": "z"}, {"id": "a"}]):
        models = list_models("https://x.com", "k")
    # 会按 id 排序，保证下拉稳定
    assert [m.id for m in models] == ["a", "z"]


def test_list_models_ollama_shape():
    """Ollama 用 name 而不是 id。"""
    with fake_get({"models": [{"name": "qwen2.5:7b"}, {"name": "llama3:8b"}]}):
        models = list_models("http://localhost:11434", "k")
    assert {m.id for m in models} == {"qwen2.5:7b", "llama3:8b"}


def test_list_models_uses_display_name():
    with fake_get({"data": [{"id": "gpt-x", "name": "GPT X 展示名"}]}):
        models = list_models("https://x.com", "k")
    assert models[0].id == "gpt-x"
    assert models[0].name == "GPT X 展示名"


# ---------------------------------------------------------------- 非对话模型识别
@pytest.mark.parametrize(
    "model_id",
    ["text-embedding-3-small", "bge-reranker-v2", "nomic-embed-text", "whisper-1"],
)
def test_embedding_like_models_marked_non_chat(model_id):
    with fake_get({"data": [{"id": model_id}]}):
        models = list_models("https://x.com", "k")
    assert models[0].chat_capable is False


def test_output_modalities_marks_non_chat():
    with fake_get({"data": [{"id": "img-gen", "output_modalities": ["image"]}]}):
        models = list_models("https://x.com", "k")
    assert models[0].chat_capable is False


def test_chat_models_sorted_first():
    with fake_get(
        {"data": [{"id": "aaa-embed"}, {"id": "zzz-chat"}]}
    ):
        models = list_models("https://x.com", "k")
    # 可对话的排前面，即使 id 字母序靠后
    assert models[0].id == "zzz-chat"
    assert models[1].id == "aaa-embed"


# ---------------------------------------------------------------- 错误路径
def test_auth_failure_message_is_actionable():
    with fake_get({"error": "x"}, status=401):
        with pytest.raises(LLMError, match="鉴权失败"):
            list_models("https://x.com", "bad")


def test_missing_endpoint_suggests_manual_input():
    with fake_get({}, status=404):
        with pytest.raises(LLMError, match="请手工填写模型名"):
            list_models("https://x.com", "k")


def test_empty_list_is_an_error():
    with fake_get({"data": []}):
        with pytest.raises(LLMError, match="空模型列表"):
            list_models("https://x.com", "k")


def test_missing_base_url():
    with pytest.raises(LLMError, match="Base URL"):
        list_models("", "k")


def test_error_body_does_not_leak_key():
    """有些网关会在错误里回显 Authorization。"""
    secret = "sk-SECRET-should-never-appear"
    with fake_get({}, status=500, text=f"boom with {secret}"):
        with pytest.raises(LLMError) as ei:
            list_models("https://x.com", secret)
    assert secret not in str(ei.value)


# ---------------------------------------------------------------- 路由层
def test_models_route_requires_base_url(client: TestClient):
    r = client.post("/api/v1/settings/models", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "Base URL" in body["reason"]


def test_models_route_requires_key(client: TestClient):
    r = client.post(
        "/api/v1/settings/models", json={"base_url": "https://x.com", "api_key": ""}
    )
    body = r.json()
    assert body["ok"] is False
    assert "API Key" in body["reason"]


def test_models_route_success(client: TestClient):
    with fake_get({"data": [{"id": "m1"}, {"id": "m2"}]}):
        r = client.post(
            "/api/v1/settings/models",
            json={"base_url": "https://x.com", "api_key": "sk-test-key"},
        )
    body = r.json()
    assert body["ok"] is True
    assert [m["id"] for m in body["models"]] == ["m1", "m2"]


def test_models_route_failure_returns_ok_false_not_500(client: TestClient):
    """拉不到列表是正常业务结果，不是服务器错误。"""
    with fake_get({}, status=401):
        r = client.post(
            "/api/v1/settings/models",
            json={"base_url": "https://x.com", "api_key": "sk-bad"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "鉴权失败" in body["reason"]
