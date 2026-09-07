"""根据 Transcript 工具事件恢复未闭合工具调用。"""

from dataclasses import dataclass
from typing import Any, Literal


ToolRecoveryAction = Literal[
    "not_started",  # 工具尚未进入真实执行，恢复时不得声称产生了副作用。
    "safe_to_retry",  # 已派发但声明为只读；后续模型可以重新调用。
    "verify_before_retry",  # 已派发且可能有副作用；必须先核验外部状态。
]


@dataclass(frozen=True)
class UnclosedToolCall:
    """一次崩溃后没有对应最终结果的工具调用。"""

    # 模型 Function Call 与补配对结果共用的稳定标识。
    call_id: str

    # 已知时保留原始工具名，便于恢复报告与模型诊断。
    tool_name: str | None

    # 基于持久化派发边界得出的保守恢复动作。
    recovery_action: ToolRecoveryAction


def find_unclosed_tool_calls(
    records: list[dict[str, Any]],
    call_ids: set[str],
) -> tuple[UnclosedToolCall, ...]:
    """找出活动 Context 中无配对结果的调用，不猜测未落盘的执行结果。"""
    started: dict[str, dict[str, Any]] = {}
    dispatched: dict[str, dict[str, Any]] = {}
    finished: set[str] = set()
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or call_id not in call_ids:
            continue
        if record.get("type") == "tool_execution_started":
            started[call_id] = payload
        elif record.get("type") == "tool_execution_dispatched":
            dispatched[call_id] = payload
        elif record.get("type") == "tool_execution_finished":
            finished.add(call_id)

    unresolved: list[UnclosedToolCall] = []
    for call_id in sorted(call_ids - finished):
        start = started.get(call_id, {})
        dispatch = dispatched.get(call_id)
        tool_name = start.get("tool_name")
        if not isinstance(tool_name, str):
            tool_name = None
        if dispatch is None:
            action: ToolRecoveryAction = "not_started"
        elif dispatch.get("recovery_action") == "safe_to_retry":
            action = "safe_to_retry"
        else:
            action = "verify_before_retry"
        unresolved.append(UnclosedToolCall(call_id, tool_name, action))
    return tuple(unresolved)


def build_recovery_tool_output(item: UnclosedToolCall) -> dict[str, str]:
    """把未闭合账本项转换为符合 Function Call 协议的保守结果。"""
    name = item.tool_name or "未知工具"
    if item.recovery_action == "not_started":
        message = f"工具 {name} 在进入真实执行前中断，本次调用未执行。"
    elif item.recovery_action == "safe_to_retry":
        message = (
            f"工具 {name} 已派发但未收到结果；该工具声明为只读，"
            "后续可以按需重新调用。系统没有自动重试。"
        )
    else:
        message = (
            f"工具 {name} 已派发但未收到结果，副作用状态未知；"
            "请先核验外部状态，再决定是否重新调用。系统没有自动重试。"
        )
    return {
        "type": "function_call_output",
        "call_id": item.call_id,
        "output": f"错误：{message}",
    }
