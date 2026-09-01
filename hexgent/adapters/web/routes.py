"""Hexgent Web Adapter 的 HTTP 与 SSE 路由。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from .errors import WebAdapterError
from .run_manager import RunManager
from .schemas import (
    ControlResponse,
    CreateSessionRequest,
    PermissionDecisionRequest,
    RunResponse,
    SessionResponse,
    StartRunRequest,
    SteeringRequest,
)


router = APIRouter(prefix="/api")


def _manager(request: Request) -> RunManager:
    """从应用生命周期中取得唯一 RunManager。"""
    return request.app.state.run_manager


@router.post("/sessions", response_model=SessionResponse, status_code=201)
async def create_session(
    body: CreateSessionRequest,
    request: Request,
) -> SessionResponse:
    """创建一个拥有独立 AgentEngine 的网络 Session。"""
    return await _manager(request).create_session(body.permission_mode)


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str, request: Request) -> SessionResponse:
    """查询 Session 及其当前活跃 Run。"""
    return await _manager(request).get_session(session_id)


@router.post("/sessions/{session_id}/runs", response_model=RunResponse, status_code=202)
async def start_run(
    session_id: str,
    body: StartRunRequest,
    request: Request,
) -> RunResponse:
    """启动一个后台 Agent Run。"""
    return await _manager(request).start_run(session_id, body.query)


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: str, request: Request) -> RunResponse:
    """查询 Agent Run 当前状态。"""
    return await _manager(request).get_run(run_id)


@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """按序回放并持续推送 Run 事件，直到 Run 形成终态。"""
    manager = _manager(request)

    # 1. 浏览器重连优先继承 Last-Event-ID，同时兼容显式查询参数。
    cursor = after_sequence
    if last_event_id is not None:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError as error:
            raise WebAdapterError(
                400,
                "invalid_last_event_id",
                "Last-Event-ID 必须是非负整数",
            ) from error
        if cursor < 0:
            raise WebAdapterError(
                400,
                "invalid_last_event_id",
                "Last-Event-ID 必须是非负整数",
            )

    # 2. 在返回响应头之前验证 Run，避免把 404 延迟到流内部。
    await manager.get_run(run_id)

    async def generate_events():
        """持续拉取内存事件，并生成标准 SSE 帧。"""
        nonlocal cursor
        while True:
            # 1. 等待增量事件；超时只发送心跳，不改变业务序列号。
            events, terminal = await manager.wait_for_events(run_id, cursor)
            if not events:
                if terminal or await request.is_disconnected():
                    return
                yield ": heartbeat\n\n"
                continue

            # 2. 每个 Core/Adapter 事件使用同一递增 sequence，支持断线去重。
            for event in events:
                cursor = event["sequence"]
                data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                yield f"id: {cursor}\nevent: {event['type']}\ndata: {data}\n\n"

            # 3. 终态事件已经发送完毕后关闭 SSE，而不是依赖客户端猜测完成。
            if terminal:
                return

    return StreamingResponse(
        generate_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runs/{run_id}/pause", response_model=ControlResponse, status_code=202)
async def pause_run(run_id: str, request: Request) -> ControlResponse:
    """请求当前 Run 在最近安全边界暂停。"""
    await _manager(request).pause(run_id)
    return ControlResponse(run_id=run_id, action="pause")


@router.post("/runs/{run_id}/cancel", response_model=ControlResponse, status_code=202)
async def cancel_run(run_id: str, request: Request) -> ControlResponse:
    """请求当前 Run 安全取消。"""
    await _manager(request).cancel(run_id)
    return ControlResponse(run_id=run_id, action="cancel")


@router.post("/runs/{run_id}/steer", response_model=ControlResponse, status_code=202)
async def steer_run(
    run_id: str,
    body: SteeringRequest,
    request: Request,
) -> ControlResponse:
    """按 FIFO 向当前 Run 提交一条 Steering Query。"""
    await _manager(request).steer(run_id, body.query)
    return ControlResponse(run_id=run_id, action="steer")


@router.post(
    "/runs/{run_id}/permissions/{request_id}",
    response_model=ControlResponse,
    status_code=202,
)
async def resolve_permission(
    run_id: str,
    request_id: str,
    body: PermissionDecisionRequest,
    request: Request,
) -> ControlResponse:
    """回应一个仍在等待的工具权限请求。"""
    await _manager(request).resolve_permission(run_id, request_id, body.decision)
    return ControlResponse(run_id=run_id, action="permission")
