"""进程级单例容器。

FastAPI 的 lifespan 里初始化一次，路由通过依赖注入拿。
好处：测试时可以整体替换，不必碰全局状态。
"""

from __future__ import annotations

import logging

from .analysis import AnalysisLLM, AnalysisRuns, resolve_analysis_llm
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
        # 分析专用的模型解析结果：没配 LLM_* 时会兜底复用 DEEPSEEK_API_KEY，
        # 所以不能直接复用上面那个 self.llm（那意味着「分析不可用」）。
        self.analysis_llm = resolve_analysis_llm(self.settings)
        # 正在进行的分析（脱离请求生命周期，前端断开也不影响结果落库）
        self.analyses = AnalysisRuns()

    def analysis_config(self) -> AnalysisLLM:
        """按当前 settings 重新解析分析模型。

        每次请求都算一次：测试里改配置能立刻生效，运维热改 .env 也不用等重启。
        纯内存计算，没有性能问题。
        """
        self.analysis_llm = resolve_analysis_llm(self.settings)
        return self.analysis_llm

    def startup(self) -> None:
        # 上次进程留下的 RUNNING 任务不会自己完成，标记失败避免前端永远转圈
        self.pipeline.recover_orphans()
        # 批量同理
        for batch in self.db.list_batches(limit=1000)[0]:
            if not batch.status.terminal:
                self.db.update_batch(batch.id, status="FAILED")
        # 分析同理：进程没了，SSE 也断了，不可能再有结果
        self.db.fail_running_analyses()

    def shutdown(self) -> None:
        # 先取消在跑的分析，尽量别让它们往关闭中的连接写数据
        self.analyses.cancel_all()
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
