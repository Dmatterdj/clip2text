"""共享测试夹具。

`client` 和 `tmp_settings` 原本写在 test_settings_usage.py 里，
但 test_models.py 也要用同一个「干净环境 + 临时数据目录」的 app，
所以提到 conftest 共用，避免两份实现漂移。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clip2text.config import Settings, reset_settings_cache


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
    for name in (
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
    ):
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
