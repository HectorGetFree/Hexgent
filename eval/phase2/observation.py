"""Phase2 Tool / Permission Contract 的只读观察对象。

本模块复用 Phase1 RuntimeObservation 的事实边界，不创建新的记录器：
实时 Tool 事件来自 Agent Loop，权限和工具执行事实来自 Transcript，
function_call_output 来自已经提交的 Context 消息记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from eval.phase1.observation import RuntimeObservation
from hexgent.events import ToolFinishedEvent, ToolStartedEvent


@dataclass(frozen=True)
class ToolPermissionObservation:
    """一次 Tool / Permission Scenario 的可观察事实快照。"""

    runtime: RuntimeObservation

    @property
    def realtime_events(self) -> tuple[Any, ...]:
        """透传 Phase1 已收集的实时 Runtime 事件。"""
        return self.runtime.realtime_events

    @property
    def transcript_records(self) -> tuple[dict[str, Any], ...]:
        """透传同一次 Run 的 durable Transcript 事实。"""
        return self.runtime.transcript_records

    @property
    def event_types(self) -> tuple[str, ...]:
        """返回稳定事件类型名称。"""
        return self.runtime.event_types

    @property
    def terminal_status(self) -> str | None:
        """返回唯一 Core terminal status。"""
        return self.runtime.terminal_status

    @property
    def tool_started_events(self) -> tuple[ToolStartedEvent, ...]:
        """返回实时工具管线开始事件。"""
        return tuple(
            event
            for event in self.realtime_events
            if isinstance(event, ToolStartedEvent)
        )

    @property
    def tool_finished_events(self) -> tuple[ToolFinishedEvent, ...]:
        """返回实时工具管线完成事件。"""
        return tuple(
            event
            for event in self.realtime_events
            if isinstance(event, ToolFinishedEvent)
        )

    def records(
        self,
        record_type: str,
        *,
        call_id: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """按 Transcript 类型和可选 call_id 返回 durable records。"""
        return tuple(
            record
            for record in self.transcript_records
            if record.get("type") == record_type
            and (
                call_id is None
                or (record.get("payload") or {}).get("call_id") == call_id
            )
        )

    def function_call_outputs(self, call_id: str) -> tuple[dict[str, Any], ...]:
        """从已提交的 Context 事实中读取指定工具调用的模型输入结果。"""
        outputs: list[dict[str, Any]] = []
        for record in self.records("context_messages_appended"):
            messages = (record.get("payload") or {}).get("messages", [])
            if not isinstance(messages, list):
                continue
            for message in messages:
                if (
                    isinstance(message, dict)
                    and message.get("type") == "function_call_output"
                    and message.get("call_id") == call_id
                ):
                    outputs.append(message)
        return tuple(outputs)

    @property
    def observed_call_ids(self) -> tuple[str, ...]:
        """返回按实时 ToolStartedEvent 首次出现顺序去重的 call_id。"""
        seen: set[str] = set()
        call_ids: list[str] = []
        for event in self.tool_started_events:
            if event.call_id not in seen:
                seen.add(event.call_id)
                call_ids.append(event.call_id)
        return tuple(call_ids)


def tool_permission_observation(
    runtime: RuntimeObservation,
) -> ToolPermissionObservation:
    """把 Phase1 observation 包装为 Tool / Permission 只读视图。"""
    return ToolPermissionObservation(runtime)
