"""Workspace Integration Command、Stream 和 Permission HTTP 路由。"""

import json
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...integration.protocol import RuntimeCommand
from ...integration.runtime_adapter import RuntimeCommandAdapter


class RuntimeCommandRequest(BaseModel):
    commandId: str
    type: str
    runId: str
    conversationId: str
    issuedAt: str
    payload: dict = {}


class PermissionResolveRequest(BaseModel):
    responseId: str
    decision: str


router = APIRouter(prefix="/internal/v1/runtime")


def _adapter(request: Request) -> RuntimeCommandAdapter:
    return request.app.state.runtime_command_adapter


def _require_integration_token(request: Request) -> None:
    expected = os.environ.get("HEXGENT_WORKSPACE_INTEGRATION_TOKEN")
    authorization = request.headers.get("Authorization")
    if not expected or authorization != f"Bearer {expected}":
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="integration authentication required")


@router.post("/commands")
async def accept_command(body: RuntimeCommandRequest, request: Request) -> dict:
    """接收 WS Command，并返回 Transcript Decision 对应的 Ack。"""
    _require_integration_token(request)
    decision = await _adapter(request).handle(RuntimeCommand(
        body.commandId, body.type, body.runId, body.conversationId, body.issuedAt, body.payload,
    ))
    return {
        "commandId": decision.command_id,
        "runId": decision.run_id,
        "status": decision.status,
        "reasonCode": decision.reason_code,
        "message": decision.message,
    }


@router.get("/runs/{workspace_run_id}/state")
async def get_run_state(workspace_run_id: str, request: Request) -> dict:
    """从 durable Runtime facts 返回 Workspace Run 当前状态。"""
    _require_integration_token(request)
    state = _adapter(request).get_run_state(workspace_run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")
    return state


@router.get("/commands/{command_id}")
async def get_command_state(command_id: str, request: Request) -> dict:
    """查询 durable Command Decision 与因果 Effect。"""
    _require_integration_token(request)
    state = _adapter(request).get_command_state(command_id)
    if state is None:
        raise HTTPException(status_code=404, detail="COMMAND_NOT_FOUND")
    return state


@router.post("/commands/{command_id}/reconcile")
async def reconcile_command_effect(command_id: str, request: Request) -> dict:
    """触发 durable ACCEPTED command 的 effect 对账；不创建新 command。"""
    _require_integration_token(request)
    return await _adapter(request).reconcile_command_effect(command_id)


@router.get("/runs/{workspace_run_id}/lifecycle-events")
async def get_lifecycle_events(
    workspace_run_id: str,
    request: Request,
    afterSequence: int = 0,
) -> dict:
    """按 Workspace Run 和 sequence 回放确定性生命周期事件。"""
    _require_integration_token(request)
    return {
        "integrationRunId": workspace_run_id,
        "events": _adapter(request).get_lifecycle_events(
            workspace_run_id, after_sequence=afterSequence,
        ),
    }


@router.get("/runs/{workspace_run_id}/messages")
async def get_run_messages(workspace_run_id: str, request: Request) -> dict:
    """以 service token 向 Workspace 返回 durable Transcript 的最小消息投影。"""
    _require_integration_token(request)
    messages = _adapter(request).get_run_messages(workspace_run_id)
    if messages is None:
        raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")
    return {"integrationRunId": workspace_run_id, "messages": messages}


def _external_event(event: dict, integration_run_id: str) -> dict:
    """将 Web Adapter 事件转换为 Integration Stream envelope。"""
    payload = event.get("payload", {})
    if event["type"] == "tool_started":
        payload = {
            "callId": payload.get("call_id"),
            "toolName": payload.get("tool_name"),
        }
    elif event["type"] == "tool_finished":
        payload = {
            "callId": payload.get("call_id"),
            "toolName": payload.get("tool_name"),
            "status": payload.get("status", "failed").upper(),
        }
    elif event["type"] == "plan_updated":
        plan = payload.get("plan", {})
        payload = {"revision": plan.get("revision"), "steps": plan.get("steps", [])}
    elif event["type"] == "text_delta":
        payload = {
            "responseId": payload.get("responseId"),
            "delta": payload.get("delta", ""),
        }
    return {
        "eventId": event.get("event_id") or f"{event.get('stream_id')}:{event.get('sequence')}",
        "integrationRunId": integration_run_id,
        "streamId": event.get("stream_id"),
        "sequence": event.get("sequence"),
        "type": event["type"],
        "createdAt": event.get("created_at"),
        "payload": payload,
    }


@router.get("/runs/{workspace_run_id}/events")
async def stream_events(workspace_run_id: str, request: Request) -> StreamingResponse:
    """按 Workspace Run ID 提供非权威 Integration SSE。"""
    _require_integration_token(request)
    manager = request.app.state.run_manager
    handle = manager.find_run_by_workspace_id(workspace_run_id)

    async def generate():
        cursor = 0
        while True:
            events, terminal = await manager.wait_for_events(handle.run_id, cursor)
            if events:
                cursor += len(events)
                for event in events:
                    external = _external_event(event, workspace_run_id)
                    yield (
                        f"id: {external['sequence']}\n"
                        f"event: {external['type']}\n"
                        f"data: {json.dumps(external, ensure_ascii=False)}\n\n"
                    )
            elif terminal:
                return

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/runs/{workspace_run_id}/permissions/{request_id}")
async def resolve_permission(
    workspace_run_id: str,
    request_id: str,
    body: PermissionResolveRequest,
    request: Request,
) -> dict:
    """按 Workspace Run ID 转发一次 Permission 决定。"""
    _require_integration_token(request)
    if body.decision not in {"allow", "deny"}:
        return {"accepted": False, "reasonCode": "INVALID_DECISION"}
    result = await request.app.state.run_manager.resolve_permission_by_workspace_id(
        workspace_run_id,
        request_id,
        body.decision,
        body.responseId,
    )
    return {"runId": workspace_run_id, "accepted": True, "status": result.status}
