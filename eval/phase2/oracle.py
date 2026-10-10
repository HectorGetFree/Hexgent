"""Phase2 Tool / Permission Contract Oracle。

所有检查只读取 RuntimeObservation 已暴露的实时事件和 Transcript，
不重新执行工具、不修改权限决策，也不推测未持久化副作用。
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from eval.phase1.oracle import ContractViolation

from .observation import ToolPermissionObservation


def _payload(record: dict[str, object]) -> dict[str, object]:
    """取得 Transcript record 的 payload，损坏记录按空对象处理。"""
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def _positions(
    observation: ToolPermissionObservation,
    record_type: str,
    call_id: str,
) -> list[int]:
    """返回某 call_id 的 durable record 位置。"""
    return [
        index
        for index, record in enumerate(observation.transcript_records)
        if record.get("type") == record_type
        and _payload(record).get("call_id") == call_id
    ]


def check_tool_event_pairing(
    observation: ToolPermissionObservation,
) -> tuple[ContractViolation, ...]:
    """每个 ToolStartedEvent 必须恰有一个同 call_id 的 ToolFinishedEvent。"""
    started = Counter(event.call_id for event in observation.tool_started_events)
    finished = Counter(event.call_id for event in observation.tool_finished_events)
    violations: list[ContractViolation] = []
    for call_id, count in started.items():
        if count != 1:
            violations.append(
                ContractViolation("tool_pairing", f"{call_id} 的 ToolStartedEvent 数量为 {count}")
            )
        if finished[call_id] != 1:
            violations.append(
                ContractViolation("tool_pairing", f"{call_id} 的 ToolFinishedEvent 数量为 {finished[call_id]}")
            )
    for call_id, count in finished.items():
        if call_id not in started:
            violations.append(
                ContractViolation("tool_pairing", f"{call_id} 出现无开始事件的 ToolFinishedEvent：{count} 个")
            )
    return tuple(violations)


def check_durable_tool_pairing(
    observation: ToolPermissionObservation,
) -> tuple[ContractViolation, ...]:
    """实时工具完成事件必须与唯一 durable finished fact 对应。"""
    violations: list[ContractViolation] = []
    finished_by_call = {
        event.call_id: event for event in observation.tool_finished_events
    }
    for call_id, event in finished_by_call.items():
        started = _positions(observation, "tool_execution_started", call_id)
        finished = _positions(observation, "tool_execution_finished", call_id)
        if len(started) != 1:
            violations.append(
                ContractViolation("durability", f"{call_id} 的 durable started 数量为 {len(started)}")
            )
        if len(finished) != 1:
            violations.append(
                ContractViolation("durability", f"{call_id} 的 durable finished 数量为 {len(finished)}")
            )
            continue
        payload = _payload(observation.transcript_records[finished[0]])
        if payload.get("status") != event.status:
            violations.append(
                ContractViolation(
                    "durability",
                    f"{call_id} 实时状态={event.status}，durable 状态={payload.get('status')}",
                )
            )
    return tuple(violations)


def check_permission_gates_dispatch(
    observation: ToolPermissionObservation,
) -> tuple[ContractViolation, ...]:
    """每个真实 dispatch 必须发生在同 call_id 的权限裁定之后。"""
    violations: list[ContractViolation] = []
    for call_id in observation.observed_call_ids:
        dispatched = _positions(observation, "tool_execution_dispatched", call_id)
        resolved = _positions(observation, "permission_resolved", call_id)
        if not dispatched:
            continue
        if len(dispatched) != 1:
            violations.append(
                ContractViolation("permission_gate", f"{call_id} 的 dispatch 数量为 {len(dispatched)}")
            )
        if len(resolved) != 1:
            violations.append(
                ContractViolation("permission_gate", f"{call_id} 的权限裁定数量为 {len(resolved)}")
            )
            continue
        payload = _payload(observation.transcript_records[resolved[0]])
        if payload.get("behavior") != "allow":
            violations.append(
                ContractViolation("permission_gate", f"{call_id} 在非 allow 裁定后被 dispatch")
            )
        if resolved[0] >= dispatched[0]:
            violations.append(
                ContractViolation("permission_gate", f"{call_id} 的 permission_resolved 未先于 dispatch")
            )
    return tuple(violations)


def check_denied_calls_are_not_dispatched(
    observation: ToolPermissionObservation,
) -> tuple[ContractViolation, ...]:
    """DENY 或 unavailable 权限裁定后不得有真实 handler dispatch。"""
    violations: list[ContractViolation] = []
    for record in observation.records("permission_resolved"):
        payload = _payload(record)
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or payload.get("behavior") != "deny":
            continue
        dispatched = _positions(observation, "tool_execution_dispatched", call_id)
        if dispatched:
            violations.append(
                ContractViolation("permission_gate", f"被拒绝调用 {call_id} 仍产生 dispatch")
            )
    return tuple(violations)


def check_function_output_pairing(
    observation: ToolPermissionObservation,
) -> tuple[ContractViolation, ...]:
    """每个结束工具调用必须向下一模型轮次提交且只提交一个配对输出。"""
    violations: list[ContractViolation] = []
    for call_id in observation.observed_call_ids:
        outputs = observation.function_call_outputs(call_id)
        if len(outputs) != 1:
            violations.append(
                ContractViolation("context_pairing", f"{call_id} 的 function_call_output 数量为 {len(outputs)}")
            )
    return tuple(violations)


def check_no_duplicate_dispatch(
    observation: ToolPermissionObservation,
) -> tuple[ContractViolation, ...]:
    """单次 Run 内每个 call_id 最多产生一次真实 handler dispatch。"""
    violations: list[ContractViolation] = []
    for call_id in observation.observed_call_ids:
        count = len(_positions(observation, "tool_execution_dispatched", call_id))
        if count > 1:
            violations.append(
                ContractViolation("side_effect", f"{call_id} 重复 dispatch：{count} 次")
            )
    return tuple(violations)


def check_all_contracts(
    observation: ToolPermissionObservation,
) -> tuple[ContractViolation, ...]:
    """运行 Phase2 所有通用 Tool / Permission 契约检查。"""
    checks: Iterable[tuple[ContractViolation, ...]] = (
        check_tool_event_pairing(observation),
        check_durable_tool_pairing(observation),
        check_permission_gates_dispatch(observation),
        check_denied_calls_are_not_dispatched(observation),
        check_function_output_pairing(observation),
        check_no_duplicate_dispatch(observation),
    )
    return tuple(violation for check in checks for violation in check)
