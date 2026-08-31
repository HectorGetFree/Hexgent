"""FastAPI 依赖解析入口。"""

from fastapi import Request

from .run_manager import RunManager


def get_run_manager(request: Request) -> RunManager:
    """从当前 FastAPI 应用状态中取得进程级 RunManager。"""
    return request.app.state.run_manager
