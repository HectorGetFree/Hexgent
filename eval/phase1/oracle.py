"""Phase1 的通用 Event Contract Oracle。

Oracle 只检查可观察事实，不重新实现 Agent Loop，也不要求事件列表完全相等。
每个检查同时提供返回违规列表的 ``check_*`` 和发现违规即失败的 ``assert_*``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from hexgent.events import AgentEvent

from .observation import RuntimeObservation


@dataclass(frozen=True)
class ContractViolation:
    """一条可报告的 Runtime Contract 违规。"""

    category: str
    message: str


def _raise(violations: Iterable[ContractViolation]) -> None:
    """把结构化违规转换为测试框架可读的 AssertionError。"""
    items = tuple(violations)
    if items:
        raise AssertionError("\n".join(f"[{item.category}] {item.message}" for item in items))


def _event_positions(observation: RuntimeObservation, event_name: str) -> list[int]:
    """按实时事件类型名称返回所有位置。"""
    return [
        index
        for index, event in enumerate(observation.realtime_events)
        if type(event).__name__ == event_name
    ]


def check_required_events(
    observation: RuntimeObservation,
    required_events: Iterable[str],
) -> tuple[ContractViolation, ...]:
    """检查实时事件流是否包含所有必需事件，不限制中间事件。"""
    available = set(observation.event_types)
    return tuple(
        ContractViolation("required_events", f"缺少必需事件：{event_name}")
        for event_name in required_events
        if event_name not in available
    )


def assert_required_events(
    observation: RuntimeObservation,
    required_events: Iterable[str],
) -> None:
    """断言必需事件存在。"""
    _raise(check_required_events(observation, required_events))


def check_happens_before(
    observation: RuntimeObservation,
    ordering: Iterable[tuple[str, str]],
) -> tuple[ContractViolation, ...]:
    """检查事件类型之间的 happens-before 约束。"""
    violations: list[ContractViolation] = []
    for before, after in ordering:
        before_positions = _event_positions(observation, before)
        after_positions = _event_positions(observation, after)
        if not before_positions:
            violations.append(
                ContractViolation("ordering", f"顺序前置事件不存在：{before}")
            )
        if not after_positions:
            violations.append(
                ContractViolation("ordering", f"顺序后置事件不存在：{after}")
            )
        if before_positions and after_positions and min(before_positions) >= max(after_positions):
            violations.append(
                ContractViolation("ordering", f"未满足 {before} < {after}")
            )
    return tuple(violations)


def assert_happens_before(
    observation: RuntimeObservation,
    ordering: Iterable[tuple[str, str]],
) -> None:
    """断言指定事件满足 happens-before 约束。"""
    _raise(check_happens_before(observation, ordering))


def check_terminal(
    observation: RuntimeObservation,
    expected_status: str | None = None,
    *,
    expected_stop_reason: str | None = None,
) -> tuple[ContractViolation, ...]:
    """检查唯一终态及其可选 status / stop reason。"""
    violations: list[ContractViolation] = []
    if len(observation.terminal_events) != 1:
        violations.append(
            ContractViolation(
                "terminal",
                f"期望恰好一个 RunFinishedEvent，实际为 {len(observation.terminal_events)} 个",
            )
        )
        return tuple(violations)
    if expected_status is not None and observation.terminal_status != expected_status:
        violations.append(
            ContractViolation(
                "terminal",
                f"期望 terminal status={expected_status}，实际为 {observation.terminal_status}",
            )
        )
    if (
        expected_stop_reason is not None
        and observation.terminal_stop_reason != expected_stop_reason
    ):
        violations.append(
            ContractViolation(
                "terminal",
                f"期望 stop_reason={expected_stop_reason}，实际为 {observation.terminal_stop_reason}",
            )
        )
    return tuple(violations)


def assert_terminal(
    observation: RuntimeObservation,
    expected_status: str | None = None,
    *,
    expected_stop_reason: str | None = None,
) -> None:
    """断言终态唯一且符合期望。"""
    _raise(check_terminal(observation, expected_status, expected_stop_reason=expected_stop_reason))


def check_no_duplicate_terminal(
    observation: RuntimeObservation,
) -> tuple[ContractViolation, ...]:
    """检查实时事件流中最多出现一个 RunFinishedEvent。"""
    count = len(observation.terminal_events)
    if count <= 1:
        return ()
    return (
        ContractViolation("terminal", f"RunFinishedEvent 重复出现：{count} 个"),
    )


def assert_no_duplicate_terminal(observation: RuntimeObservation) -> None:
    """断言不存在重复终态。"""
    _raise(check_no_duplicate_terminal(observation))


def check_no_runtime_effect_after_rejection(
    observation: RuntimeObservation,
) -> tuple[ContractViolation, ...]:
    """检查 REJECTED command 后没有对应的 runtime_control_effect。

    该检查使用 Transcript 中已有的 command_id 关联，不猜测实时事件和命令的关系。
    """
    rejected_ids = {
        (record.get("payload") or {}).get("command_id")
        for record in observation.transcript_records
        if record.get("type") == "runtime_command_decided"
        and (record.get("payload") or {}).get("decision") == "REJECTED"
    }
    violations = []
    for record in observation.transcript_records:
        if record.get("type") != "runtime_control_effect":
            continue
        command_id = (record.get("payload") or {}).get("command_id")
        if command_id in rejected_ids:
            violations.append(
                ContractViolation(
                    "invariant",
                    f"被拒绝 command 产生了 runtime_control_effect：{command_id}",
                )
            )
    return tuple(violations)


def assert_no_runtime_effect_after_rejection(
    observation: RuntimeObservation,
) -> None:
    """断言被拒绝命令没有产生对应的持久化运行效果。"""
    _raise(check_no_runtime_effect_after_rejection(observation))
