"""Workspace Capability HTTP 协议客户端。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class CapabilityResult:
    protocol_version: str
    run_id: str
    call_id: str
    status: str
    data: Any = None
    error_code: str | None = None
    error_message: str | None = None


class WorkspaceCapabilityClient:
    """只负责调用 WS Capability API，不参与 Workspace 授权判断。"""

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 timeout: float = 5.0) -> None:
        self.base_url = (base_url or os.environ.get(
            "HEXGENT_WORKSPACE_URL", "http://127.0.0.1:8098")).rstrip("/")
        self.token = token or os.environ.get("HEXGENT_WORKSPACE_CAPABILITY_TOKEN", "")
        self.timeout = timeout

    def invoke(self, run_id: str, call_id: str, capability: str,
               arguments: dict[str, Any]) -> CapabilityResult:
        payload = {
            "protocolVersion": "v1", "runId": run_id, "callId": call_id,
            "capability": capability, "arguments": arguments,
        }
        try:
            response = httpx.post(
                f"{self.base_url}/internal/agent/capabilities/invoke",
                json=payload,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
            )
            if response.status_code != 200:
                return CapabilityResult("v1", run_id, call_id, "FAILED",
                                        error_code="UPSTREAM_UNAVAILABLE",
                                        error_message=f"Workspace returned HTTP {response.status_code}")
            body = response.json()
            if (body.get("protocolVersion"), body.get("runId"), body.get("callId")) != (
                    "v1", run_id, call_id):
                return CapabilityResult("v1", run_id, call_id, "FAILED",
                                        error_code="PROTOCOL_ERROR",
                                        error_message="Capability response identity mismatch")
            if body.get("status") not in {"SUCCEEDED", "FAILED", "DENIED"}:
                return CapabilityResult("v1", run_id, call_id, "FAILED",
                                        error_code="PROTOCOL_ERROR",
                                        error_message="Unsupported Capability status")
            error = body.get("error") or {}
            return CapabilityResult("v1", run_id, call_id, body["status"], body.get("data"),
                                    error.get("code"), error.get("message"))
        except httpx.HTTPError as exc:
            return CapabilityResult("v1", run_id, call_id, "FAILED",
                                    error_code="UPSTREAM_UNAVAILABLE", error_message=str(exc))
