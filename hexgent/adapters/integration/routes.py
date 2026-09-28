"""WS Runtime Command HTTP 路由。"""

from fastapi import APIRouter, Request
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


router = APIRouter(prefix="/internal/v1/runtime")


def _adapter(request: Request) -> RuntimeCommandAdapter:
    return request.app.state.runtime_command_adapter


@router.post("/commands")
async def accept_command(body: RuntimeCommandRequest, request: Request) -> dict:
    """接收 WS Command，并返回 Transcript Decision 对应的 Ack。"""
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
