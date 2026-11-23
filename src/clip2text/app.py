"""FastAPI 应用装配。

关键点：
  · lifespan 里初始化 AppState（数据库 + 流水线），并清理上次中断的任务
  · 统一异常处理：业务错误统一转 {"detail": ...}，不泄露堆栈
  · 可选托管静态前端：apps/web 存在就挂到 /
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.routes import router
from .config import get_settings
from .pipeline import PipelineError
from .state import init_state, reset_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parents[2] / "apps" / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info("启动 clip2text %s，数据目录 %s", __version__, settings.data_dir.resolve())
    state = init_state(settings)
    st = state.db.count_by_status()
    if st:
        log.info("历史任务统计: %s", st)
    try:
        yield
    finally:
        reset_state()
        log.info("已关闭")


def create_app() -> FastAPI:
    app = FastAPI(
        title="clip2text",
        version=__version__,
        description="本地优先的音视频文案提取工具：上传文件、转写、导出。默认零 API key。",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # 本地工具，允许任意来源；对外部署请收紧
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)

    @app.exception_handler(PipelineError)
    async def _pipeline_error(_: Request, exc: PipelineError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("未处理异常")
        return JSONResponse(
            {"detail": f"服务器内部错误：{type(exc).__name__}"}, status_code=500
        )

    # 前端：apps/web 里有 index.html 就托管
    if (WEB_DIR / "index.html").exists():
        app.mount("/app", StaticFiles(directory=WEB_DIR, html=True), name="web")

        @app.get("/", include_in_schema=False)
        def root() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    else:

        @app.get("/", include_in_schema=False)
        def root_no_web() -> dict[str, str]:
            return {
                "service": "clip2text",
                "version": __version__,
                "docs": "/docs",
                "note": "未找到前端静态文件（apps/web/index.html）",
            }

    return app


app = create_app()
