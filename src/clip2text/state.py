"""进程级单例容器。

FastAPI 的 lifespan 里初始化一次，路由通过依赖注入拿。
好处：测试时可以整体替换，不必碰全局状态。
"""

from __future__ import annotations

import logging

from .batch import BatchRunner
from .config import Settings, get_settings
from .db import Database
from .llm import build_llm_config
from .pipeline import Pipeline

log = logging.getLogger(__name__)


class AppState:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self.db = Database(self.settings.db_path)
        self.pipeline = Pipeline(self.settings, self.db)
        self.batches = BatchRunner(self.pipeline, self.settings, self.db)
        self.llm = build_llm_config(
            self.settings.llm_base_url, self.settings.llm_api_key, self.settings.llm_model
        )

    def startup(self) -> None:
        # 上次进程留下的 RUNNING 任务不会自己完成，标记失败避免前端永远转圈
        self.pipeline.recover_orphans()
        # 批量同理
        for batch in self.db.list_batches(limit=1000)[0]:
            if not batch.status.terminal:
                self.db.update_batch(batch.id, status="FAILED")

    def shutdown(self) -> None:
        self.db.close()


_state: AppState | None = None


def init_state(settings: Settings | None = None) -> AppState:
    global _state
    _state = AppState(settings)
    _state.startup()
    return _state


def get_state() -> AppState:
    if _state is None:
        raise RuntimeError("AppState 未初始化，应在 lifespan 中调用 init_state()")
    return _state


def reset_state() -> None:
    global _state
    if _state is not None:
        _state.shutdown()
    _state = None
