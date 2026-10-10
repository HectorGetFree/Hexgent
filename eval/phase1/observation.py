"""Phase1 Runtime Contract 的统一观察结果。

Observation 只汇总已经存在的 Runtime 输出，不创建第二套状态源：
实时事件来自 Agent Loop，Transcript 来自 SessionStore，生命周期事件来自
已有 LifecycleProjector。后续 Scenario 和 Oracle 只依赖这个边界对象。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from hexgent.events import AgentEvent, RunFinishedEvent
from hexgent.integration.lifecycle import LifecycleEvent, LifecycleProjector


@dataclass(frozen=True)
class RuntimeObservation:
    """一次 Runtime Scenario 执行后可被契约层读取的事实快照。"""

    # Agent Loop 实时产生的事件；保留原对象，避免丢失事件字段。
    realtime_events: tuple[AgentEvent, ...]

    # SessionStore 已经持久化的 Transcript 事实。
    transcript_records: tuple[dict[str, Any], ...] = ()

    # 从同一份 Transcript 投影出的 Workspace 生命周期事件。
    lifecycle_events: tuple[LifecycleEvent, ...] = ()

    @classmethod
    def collect(
        cls,
        events: Iterable[AgentEvent],
        *,
        transcript: Iterable[dict[str, Any]] = (),
        session_id: str | None = None,
    ) -> "RuntimeObservation":
        """从 Runtime 输出和已有持久化事实创建观察对象。"""
        event_snapshot = tuple(events)
        transcript_snapshot = tuple(dict(record) for record in transcript)
        lifecycle = ()
        if session_id is not None and transcript_snapshot:
            lifecycle = tuple(
                LifecycleProjector().project(session_id, list(transcript_snapshot))
            )
        return cls(event_snapshot, transcript_snapshot, lifecycle)

    @property
    def event_types(self) -> tuple[str, ...]:
        """返回实时事件的稳定类型名称序列。"""
        return tuple(type(event).__name__ for event in self.realtime_events)

    @property
    def transcript_types(self) -> tuple[str, ...]:
        """返回 Transcript 的事实类型序列。"""
        return tuple(str(record.get("type")) for record in self.transcript_records)

    @property
    def terminal_events(self) -> tuple[RunFinishedEvent, ...]:
        """返回实时事件中的终止事件，不在此处强制唯一。"""
        return tuple(
            event for event in self.realtime_events if isinstance(event, RunFinishedEvent)
        )

    @property
    def terminal(self) -> RunFinishedEvent | None:
        """返回唯一终止事件；缺失或重复时返回 None 交给 Oracle 报告。"""
        terminals = self.terminal_events
        return terminals[0] if len(terminals) == 1 else None

    @property
    def terminal_status(self) -> str | None:
        """返回 Core terminal status；没有唯一终态时返回 None。"""
        terminal = self.terminal
        return terminal.result.status if terminal is not None else None

    @property
    def terminal_stop_reason(self) -> str | None:
        """返回 Core terminal stop reason。"""
        terminal = self.terminal
        return terminal.result.stop_reason if terminal is not None else None

    @property
    def checkpoint_records(self) -> tuple[dict[str, Any], ...]:
        """返回 Transcript 中已经提交的 Checkpoint 事实。"""
        return tuple(
            record
            for record in self.transcript_records
            if record.get("type") == "checkpoint_committed"
        )

    @property
    def final_run_record(self) -> dict[str, Any] | None:
        """返回最后一条 Agent Run 终态事实。"""
        for record in reversed(self.transcript_records):
            if record.get("type") == "agent_run_finished":
                return record
        return None

    @property
    def continuable(self) -> bool:
        """按当前 Core 语义判断该终态是否保存为可继续 Run。"""
        return self.terminal_status in {"interrupted", "incomplete"}


def observation_from_session(
    events: Iterable[AgentEvent], session: Any, *, session_id: str | None = None
) -> RuntimeObservation:
    """从已有 Session 读取 Transcript，避免评测夹具复制持久化逻辑。"""
    store = getattr(session, "store", None)
    records = store.read_transcript() if store is not None else ()
    resolved_session_id = session_id
    if resolved_session_id is None and store is not None:
        resolved_session_id = store.session_id
    return RuntimeObservation.collect(
        events,
        transcript=records,
        session_id=resolved_session_id,
    )
