"""FastAPI 应用工厂。"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .errors import install_error_handlers
from .routes.runs import router as runs_router
from .routes.sessions import router as sessions_router
from .run_manager import RunManager
from .settings import WebSettings


def create_app(
    *,
    workspace: Path | None = None,
    manager: RunManager | None = None,
    settings: WebSettings | None = None,
) -> FastAPI:
    """创建并装配 Hexgent Web API，测试可显式注入 RunManager。"""
    # 1. 生产环境根据启动目录构造 Manager；测试环境可注入假的 Manager。
    active_workspace = (workspace or Path.cwd()).resolve()
    active_manager = manager or RunManager(active_workspace)
    active_settings = settings or WebSettings.from_environment()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """在应用退出时请求活跃 Run 安全暂停并有限等待。"""
        # 2. yield 之前是启动阶段，之后是 FastAPI 的关闭阶段。
        yield
        await active_manager.shutdown()

    # 3. 注册应用级依赖、异常处理器和分组路由。
    app = FastAPI(title="Hexgent Web API", version="0.1.0", lifespan=lifespan)
    # 浏览器只允许从显式白名单来源访问 API；生产环境应配置实际前端域名。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_settings.cors_allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Accept", "Content-Type", "Last-Event-ID"],
    )
    app.state.run_manager = active_manager
    install_error_handlers(app)
    app.include_router(sessions_router)
    app.include_router(runs_router)
    return app
