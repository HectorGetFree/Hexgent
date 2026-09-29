"""Workspace Integration Command、Stream 和 Permission HTTP 路由。"""

import json
import os

from fastapi import APIRouter, Request
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
