"""Phase3 Tool Execution crash consistency 的确定性故障注入场景。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Callable, Literal
from unittest.mock import patch

from eval.support.agent_contracts import (
    run_trace_capturing_failure,
    text_response,
    tool_call,
)
from hexgent.agent_engine import AgentEngine
from hexgent.config import HexgentConfig
from hexgent.session import Session


Outcome = Literal["PASS", "FAIL", "RECOVERY_GAP"]


class InjectedCrash(RuntimeError):
    """评测故障注入使用的进程中断替身。"""


@dataclass(frozen=True)
class RecoveryObservation:
    """一次 crash → restore 实验的 durable 前后事实。"""

    call_id: str
    pre_crash_records: tuple[dict[str, object], ...]
    final_records: tuple[dict[str, object], ...]
    external_side_effect_exists: bool
    restore_outputs: tuple[dict[str, object], ...]
    repeated_restore_outputs: tuple[dict[str, object], ...]
    crash: BaseException | None

    @property
    def pre_crash_types(self) -> tuple[str, ...]:
        return tuple(str(record.get("type")) for record in self.pre_crash_records)

    @property
    def final_types(self) -> tuple[str, ...]:
        return tuple(str(record.get("type")) for record in self.final_records)

    @property
    def recovery_actions(self) -> tuple[str, ...]:
        return tuple(
            str(item.get("recovery_action"))
            for record in self.final_records
            if record.get("type") == "session_recovery_repaired"
            for item in _payload(record).get("repaired_calls", [])
            if isinstance(item, dict)
        )


@dataclass(frozen=True)
class RecoveryScenario:
    """一个真实 Agent Loop crash boundary 的评测声明。"""

    name: str
    crash_point: str
    invariant: str
    expected_outcome: Outcome
    execute: Callable[[], RecoveryObservation]
    verify: Callable[[RecoveryObservation], None]


def _run_with_crash(
    root: Path,
    call_id: str,
    crash_hook: Callable[[Session], object],
) -> RecoveryObservation:
    """运行真实 Loop 至注入 crash，再两次独立 restore 验证幂等性。"""
    target = root / "side-effect.txt"
    session = Session.persistent(root)
    session.record_user_query(SimpleNamespace(sequence=1, content="写入恢复评测文件"))
    assert session.store is not None

    with crash_hook(session):
        trace, _, crash = run_trace_capturing_failure(
            [
                text_response(
                    output=[
                        tool_call(
                            call_id,
                            "write_file",
                            '{"path":"side-effect.txt","content":"一次副作用"}',
                        )
                    ]
                ),
                text_response("工具结果已处理"),
            ],
            session=session,
            workdir=root,
            user_confirmation=lambda *_: "allow",
        )
    if not isinstance(crash, InjectedCrash):
        raise AssertionError(f"预期 InjectedCrash，实际为 {crash!r}")

    pre_crash_records = tuple(trace.transcript)
    session_id = session.store.session_id
    first = AgentEngine.restore(HexgentConfig(), root, session_id)
    first_outputs = _function_outputs(first.context_manager.active_messages, call_id)
    second = AgentEngine.restore(HexgentConfig(), root, session_id)
    repeated_outputs = _function_outputs(second.context_manager.active_messages, call_id)
    final_records = tuple(second.session.store.read_transcript())
    return RecoveryObservation(
        call_id,
        pre_crash_records,
        final_records,
        target.exists(),
        first_outputs,
        repeated_outputs,
        crash,
    )


def _crash_after_record(record_type: str) -> Callable[[Session], object]:
    """在指定 Transcript fact 已 durable 后模拟进程崩溃。"""
    from contextlib import contextmanager

    @contextmanager
    def hook(session: Session):
        assert session.store is not None
        original = session.store.append_transcript

        def append(kind: str, payload: dict[str, object]):
            result = original(kind, payload)
            if kind == record_type:
                raise InjectedCrash(f"after durable {record_type}")
            return result

        with patch.object(session.store, "append_transcript", side_effect=append):
            yield

    return hook


def _crash_before_record(record_type: str) -> Callable[[Session], object]:
    """在指定事实落盘前模拟进程崩溃，保留此前真实副作用。"""
    from contextlib import contextmanager

    @contextmanager
    def hook(session: Session):
        assert session.store is not None
        original = session.store.append_transcript

        def append(kind: str, payload: dict[str, object]):
            if kind == record_type:
                raise InjectedCrash(f"before durable {record_type}")
            return original(kind, payload)

        with patch.object(session.store, "append_transcript", side_effect=append):
            yield

    return hook


def _crash_before_tool_result_context() -> Callable[[Session], object]:
    """在工具 finished 已落盘、function_call_output 落盘前模拟进程崩溃。"""
    from contextlib import contextmanager

    @contextmanager
    def hook(session: Session):
        original = session.record_context_messages

        def record(messages: list[object], *, completes_round: bool, source: str, loop_state=None):
            if source == "tool_results":
                raise InjectedCrash("before durable function_call_output")
            return original(messages, completes_round=completes_round, source=source, loop_state=loop_state)

        with patch.object(session, "record_context_messages", side_effect=record):
            yield

    return hook


def started_before_dispatch() -> RecoveryScenario:
    """started 已 durable、dispatch 前崩溃：恢复必须声明未执行。"""
    def execute() -> RecoveryObservation:
        with TemporaryDirectory() as directory:
            return _run_with_crash(
                Path(directory), "started-1", _crash_after_record("tool_execution_started")
            )

    def verify(observation: RecoveryObservation) -> None:
        assert "tool_execution_started" in observation.pre_crash_types
        assert "tool_execution_dispatched" not in observation.pre_crash_types
        assert not observation.external_side_effect_exists
        assert observation.recovery_actions == ("not_started",)
        assert len(observation.restore_outputs) == len(observation.repeated_restore_outputs) == 1
        assert "未执行" in str(observation.restore_outputs[0]["output"])
        assert "tool_execution_dispatched" not in observation.final_types

    return RecoveryScenario(
        "started_before_dispatch",
        "tool_execution_started durable → dispatch 前",
        "started-but-unfinished call 不得静默丢失，且不得执行副作用",
        "PASS",
        execute,
        verify,
    )


def dispatched_before_tool_returns() -> RecoveryScenario:
    """dispatch 已 durable、handler 前崩溃：恢复只能要求外部核验。"""
    def execute() -> RecoveryObservation:
        with TemporaryDirectory() as directory:
            return _run_with_crash(
                Path(directory), "dispatch-1", _crash_after_record("tool_execution_dispatched")
            )

    def verify(observation: RecoveryObservation) -> None:
        assert "tool_execution_dispatched" in observation.pre_crash_types
        assert not observation.external_side_effect_exists
        assert observation.recovery_actions == ("verify_before_retry",)
        assert len(observation.restore_outputs) == len(observation.repeated_restore_outputs) == 1
        assert "副作用状态未知" in str(observation.restore_outputs[0]["output"])
        assert observation.final_types.count("tool_execution_dispatched") == 1

    return RecoveryScenario(
        "dispatched_before_tool_returns",
        "tool_execution_dispatched durable → handler 返回前",
        "已 dispatch 的副作用调用不得自动重试，必须保守要求核验",
        "PASS",
        execute,
        verify,
    )


def side_effect_before_finished() -> RecoveryScenario:
    """副作用已发生、finished 未落盘：恢复不得第二次执行。"""
    def execute() -> RecoveryObservation:
        with TemporaryDirectory() as directory:
            return _run_with_crash(
                Path(directory), "effect-1", _crash_before_record("tool_execution_finished")
            )

    def verify(observation: RecoveryObservation) -> None:
        assert "tool_execution_dispatched" in observation.pre_crash_types
        assert "tool_execution_finished" not in observation.pre_crash_types
        assert observation.external_side_effect_exists
        assert observation.recovery_actions == ("verify_before_retry",)
        assert len(observation.restore_outputs) == len(observation.repeated_restore_outputs) == 1
        assert "副作用状态未知" in str(observation.restore_outputs[0]["output"])
        assert observation.final_types.count("tool_execution_dispatched") == 1

    return RecoveryScenario(
        "side_effect_before_finished",
        "工具副作用已发生 → tool_execution_finished 落盘前",
        "已发生副作用不得因 restore/replay 被第二次 dispatch",
        "PASS",
        execute,
        verify,
    )


def finished_before_function_output() -> RecoveryScenario:
    """finished 已 durable、结果 Context 未落盘：恢复必须补回模型可见结果。"""
    def execute() -> RecoveryObservation:
        with TemporaryDirectory() as directory:
            return _run_with_crash(
                Path(directory), "finished-1", _crash_before_tool_result_context()
            )

    def verify(observation: RecoveryObservation) -> None:
        assert "tool_execution_finished" in observation.pre_crash_types
        assert observation.external_side_effect_exists
        assert observation.recovery_actions == ("restore_finished_output",)
        assert len(observation.restore_outputs) == len(observation.repeated_restore_outputs) == 1
        assert str(observation.restore_outputs[0]["output"]).endswith("写入 5 个字符")

    return RecoveryScenario(
        "finished_before_function_output",
        "tool_execution_finished durable → function_call_output / 下一轮模型调用前",
        "每个 callId 最终应有一个有效 function_call_output",
        "PASS",
        execute,
        verify,
    )


def _payload(record: dict[str, object]) -> dict[str, object]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def _function_outputs(messages: list[object], call_id: str) -> tuple[dict[str, object], ...]:
    return tuple(
        message
        for message in messages
        if isinstance(message, dict)
        and message.get("type") == "function_call_output"
        and message.get("call_id") == call_id
    )


def v1_scenarios() -> tuple[RecoveryScenario, ...]:
    """返回 Phase3 第一批 Tool Execution crash consistency 场景。"""
    return (
        started_before_dispatch(),
        dispatched_before_tool_returns(),
        side_effect_before_finished(),
        finished_before_function_output(),
    )
