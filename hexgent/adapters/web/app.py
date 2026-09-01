"""Hexgent Web Adapter 的 FastAPI 应用工厂。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ...config import load_config
from ...workspace import find_workspace_root
from .errors import WebAdapterError
from .routes import router
from .run_manager import RunManager
from .settings import WebSettings


def create_app(
    *,
    config_path: Path | None = None,
    workdir: Path | None = None,
    run_manager: RunManager | None = None,
    settings: WebSettings | None = None,
) -> FastAPI:
    """创建只负责网络协议转换的 FastAPI 应用。"""
    # 1. 生产入口从工作区加载配置；测试可以显式注入隔离 RunManager。
    if run_manager is None:
        resolved_workdir = (workdir or find_workspace_root()).resolve()
        run_manager = RunManager(
            load_config(resolved_workdir, config_path),
            resolved_workdir,
        )

    # 2. Adapter 生命周期只负责收束自身启动的后台 Run。
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await run_manager.shutdown()

    active_settings = settings or WebSettings.from_environment()
    app = FastAPI(
        title="Hexgent Web Adapter",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_settings.cors_allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Accept", "Content-Type", "Last-Event-ID"],
    )
    app.state.run_manager = run_manager
    app.include_router(router)

    # 3. 业务状态冲突使用稳定错误协议，不向网络泄漏 Python 堆栈。
    @app.exception_handler(WebAdapterError)
    async def handle_adapter_error(
        _: Request,
        error: WebAdapterError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                }
            },
        )

    return app
