"""Session 与 Run 创建接口。"""

from fastapi import APIRouter, Depends, status

from ..dependencies import get_run_manager
from ..run_manager import RunManager
from ..schemas import CreateRunRequest, CreateSessionRequest, RunResponse, SessionResponse

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(body: CreateSessionRequest, manager: RunManager = Depends(get_run_manager)):
    """创建一个独占 AgentEngine 的 Web Session。"""
    return manager.session_dict(await manager.create_session(body.workdir, body.permission_mode))


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str, manager: RunManager = Depends(get_run_manager)):
    """查询 Session 的权限模式、活跃 Run、最近 Run 和当前 Plan。"""
    return manager.session_dict(await manager.get_session(session_id))


@router.post("/{session_id}/runs", response_model=RunResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_run(session_id: str, body: CreateRunRequest, manager: RunManager = Depends(get_run_manager)):
    """为 Session 创建后台 Run，并立即返回而不等待 Agent 完成。"""
    return manager.run_dict(await manager.start_run(session_id, body.query))
