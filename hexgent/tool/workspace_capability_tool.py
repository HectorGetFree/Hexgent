"""将现有 Tool 调用桥接到 Workspace Capability。"""

import json
from typing import Any

from ..integration.capability import WorkspaceCapabilityClient
from .tool_context import ToolExecutionContext
from .tool_result import ToolHandlerResult


def invoke_workspace_capability(
    capability: str,
    arguments: dict[str, Any],
    context: ToolExecutionContext,
) -> ToolHandlerResult:
    """复用现有 Tool 管线执行一次同步远程 Capability。"""
    run_id = context.integration_metadata.get("workspace_run_id")
    call_id = context.integration_metadata.get("tool_call_id")
    if not run_id or not call_id:
        return ToolHandlerResult.failed(
            "tool_internal_error", "缺少 Workspace Run 或 Tool Call 标识"
        )
    result = WorkspaceCapabilityClient().invoke(run_id, call_id, capability, arguments)
    if result.status == "SUCCEEDED":
        return ToolHandlerResult.success(
            json.dumps(result.data, ensure_ascii=False),
            result.data if isinstance(result.data, dict) else None,
        )
    if result.status == "DENIED":
        return ToolHandlerResult.failed("permission_denied", result.error_message or "Capability 被拒绝", phase="permission")
    return ToolHandlerResult.failed("external_dependency_failed", result.error_message or "Capability 执行失败", retryable=True)


def invoke_search_workspace(
    query: str,
    topK: int | None,
    context: ToolExecutionContext,
) -> ToolHandlerResult:
    """通过既有 Capability 管线检索当前授权 Workspace。"""
    arguments: dict[str, Any] = {"query": query}
    if topK is not None:
        arguments["topK"] = topK
    return invoke_workspace_capability("search_workspace", arguments, context)
