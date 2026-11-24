"""进程级单例容器。

FastAPI 的 lifespan 里初始化一次，路由通过依赖注入拿。
好处：测试时可以整体替换，不必碰全局状态。

热更新（契约 docs/SETTINGS-CONTRACT.md §3）
    用户在网页里保存 key 后必须**不重启**就生效。做法：
      · `sync_settings()` 比对 settings.json 的 mtime/size，变了就重算
      · 重算时「env/.env 基线 + settings.json 覆盖」逐字段覆盖到
        `self.settings` 的**热字段**上（路径/并发等字段不动，
        否则正在跑的任务会找不到自己的文件）
      · `analysis_config()` 每次请求都 sync + 重新解析
        （曾经的坑：读构造时的快照，清 lru_cache 也不生效）
"""

from __future__ import annotations

import logging
from typing import Any

from .analysis import AnalysisLLM, AnalysisRuns, resolve_analysis_llm
from .batch import BatchRunner
from .config import Settings, env_settings, get_settings, reset_settings_cache
from .db import Database
from .llm import build_llm_config, set_usage_sink
from .pipeline import Pipeline
from .settings_store import SETTINGS_FILENAME, apply_overrides, load_overrides
from .usage import record

log = logging.getLogger(__name__)

# 允许热更新的字段：只碰「配置项」，绝不碰 data_dir / 并发数这类
# 「改了就得重建整个进程」的东西。
HOT_FIELDS: tuple[str, ...] = (
    "llm_base_url",
    "llm_api_key",
    "llm_model",
    "deepseek_api_key",
    "deepseek_base_url",
    "pricing",
)


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
        # 改写走 llm.stream_rewrite()，那边拿不到 db，所以注册一个模块级
        # sink：改写结束时回调这里记账（失败只 warning，不影响输出）。
        set_usage_sink(self._rewrite_usage_sink)
        self._overrides_stamp = self._current_stamp()

    # ---------------- 热更新 ----------------
    @property
    def settings_file(self):
        return self.settings.data_dir / SETTINGS_FILENAME

    def _current_stamp(self) -> tuple[int, int] | None:
        """settings.json 的 (mtime_ns, size)。文件不存在 = None。"""
        try:
            st = self.settings_file.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def resolve_settings(self) -> Settings:
        """env/.env 基线 + settings.json 覆盖 = 当前应有的配置（新对象）。"""
        return apply_overrides(env_settings(), load_overrides(self.settings.data_dir))

    def reload_settings(self) -> Settings:
        """重读 settings.json 并应用到当前进程，**无需重启**。"""
        reset_settings_cache()  # 让其它 get_settings() 调用者也看到新值
        try:
            fresh = self.resolve_settings()
        except Exception:  # noqa: BLE001 - 配置坏了不该让正在跑的进程崩
            log.warning("应用 settings.json 覆盖失败，继续使用当前配置", exc_info=True)
            return self.settings

        # 只覆盖热字段：data_dir / 并发等由启动参数决定，改了会让正在跑的
        # 任务找不到自己的目录。DELETE 掉 json 后这里会回落到 env 值。
        for name in HOT_FIELDS:
            setattr(self.settings, name, getattr(fresh, name))

        # 引擎字典缓存了旧的 deepseek 配置，必须重置
        self.pipeline.settings = self.settings
        self.pipeline.refresh_engines()
        self.batches.settings = self.settings
        self.llm = build_llm_config(
            self.settings.llm_base_url, self.settings.llm_api_key, self.settings.llm_model
        )
        self.analysis_llm = resolve_analysis_llm(self.settings)
        self._overrides_stamp = self._current_stamp()
        log.info(
            "配置已热更新：llm=%s deepseek_key=%s analysis=%s",
            "set" if (self.settings.llm_base_url and self.settings.llm_model) else "none",
            "set" if self.settings.deepseek_api_key else "none",
            self.analysis_llm.source or "none",
        )
        return self.settings

    def sync_settings(self) -> Settings:
        """settings.json 变了就立刻跟上（外部手工编辑也能生效）。

        每个请求一次 stat：本地单用户工具，这点开销远小于「改了没生效」
        带来的困惑。
        """
        if self._current_stamp() != self._overrides_stamp:
            return self.reload_settings()
        return self.settings

    def analysis_config(self) -> AnalysisLLM:
        """按当前 settings 重新解析分析模型。

        必须先 sync_settings()，否则读到的是构造时的快照 —— 清 lru_cache
        也不会让快照变新，这正是最容易踩的坑。纯内存计算，无性能问题。
        """
        self.sync_settings()
        self.analysis_llm = resolve_analysis_llm(self.settings)
        return self.analysis_llm

    def _rewrite_usage_sink(self, usage: dict[str, Any]) -> None:
        record(
            self.db,
            "rewrite",
            task_id=usage.get("task_id"),
            model=str(usage.get("model") or ""),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

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
        set_usage_sink(None)
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
