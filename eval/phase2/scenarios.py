"""Phase2 Tool / Permission Contract 的确定性 Scenario。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Callable

from eval.phase1.observation import observation_from_session
from eval.support.agent_contracts import run_trace, text_response, tool_call
from hexgent.session import Session

from .observation import ToolPermissionObservation, tool_permission_observation


@dataclass(frozen=True)
class ToolPermissionScenario:
    """描述一个以真实 Agent Loop 为入口的工具协议场景。"""

    name: str
    expected_status: str
    execute: Callable[[], ToolPermissionObservation]
    verify: Callable[[ToolPermissionObservation], None]


def _run_tool_scenario(
    root: Path,
    calls: list[object],
    *,
    confirmation: Callable[..., str],
) -> ToolPermissionObservation:
    """通过正式 Agent Loop 重放一轮工具调用及其后续完成答复。"""
    session = Session.persistent(root)
    session.record_user_query(SimpleNamespace(sequence=1, content="执行评测工具调用"))
    trace, _ = run_trace(
        [text_response(output=calls), text_response("工具结果已处理")],
        session=session,
        workdir=root,
        user_confirmation=confirmation,
    )
    return tool_permission_observation(observation_from_session(trace.events, session))


def tool_allow_success() -> ToolPermissionScenario:
    """工作区内读取经策略允许后应真实执行并正常结束。"""
    def execute() -> ToolPermissionObservation:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "allowed.txt").write_text("允许读取", encoding="utf-8")
            return _run_tool_scenario(
                root,
                [tool_call("allow-1", "read_file", '{"path":"allowed.txt"}')],
                confirmation=lambda *_: "allow",
            )

    def verify(observation: ToolPermissionObservation) -> None:
        finished = observation.records("tool_execution_finished", call_id="allow-1")
        assert _payload(finished[0]).get("status") == "success"
        assert _payload(observation.records("permission_resolved", call_id="allow-1")[0]).get("resolution") == "policy_allowed"

    return ToolPermissionScenario("tool_allow_success", "completed", execute, verify)


def permission_ask_allow() -> ToolPermissionScenario:
    """ask → allow 必须先落权限事实，再执行工作区外读取。"""
    def execute() -> ToolPermissionObservation:
        with TemporaryDirectory() as directory:
            container = Path(directory)
            root = container / "workdir"
            root.mkdir()
            (container / "outside.txt").write_text("允许后读取", encoding="utf-8")
            return _run_tool_scenario(
                root,
                [tool_call("ask-allow-1", "read_file", '{"path":"../outside.txt"}')],
                confirmation=lambda *_: "allow",
            )

    def verify(observation: ToolPermissionObservation) -> None:
        resolution = _payload(observation.records("permission_resolved", call_id="ask-allow-1")[0])
        assert resolution.get("resolution") == "user_allowed"
        assert _payload(observation.records("tool_execution_finished", call_id="ask-allow-1")[0]).get("status") == "success"

    return ToolPermissionScenario("permission_ask_allow", "completed", execute, verify)


def permission_ask_deny() -> ToolPermissionScenario:
    """ask → deny 不得触发真实工作区外写入。"""
    def execute() -> ToolPermissionObservation:
        with TemporaryDirectory() as directory:
            container = Path(directory)
            root = container / "workdir"
            root.mkdir()
            observation = _run_tool_scenario(
                root,
                [tool_call("ask-deny-1", "write_file", '{"path":"../blocked.txt","content":"不应写入"}')],
                confirmation=lambda *_: "deny",
            )
            if (container / "blocked.txt").exists():
                raise AssertionError("用户拒绝后仍写入了工作区外文件")
            return observation

    def verify(observation: ToolPermissionObservation) -> None:
        resolution = _payload(observation.records("permission_resolved", call_id="ask-deny-1")[0])
        finished = _payload(observation.records("tool_execution_finished", call_id="ask-deny-1")[0])
        assert resolution.get("resolution") == "user_denied"
        assert finished.get("status") == "denied"
        assert isinstance(finished.get("failure"), dict)
        assert finished["failure"].get("code") == "user_denied"

    return ToolPermissionScenario("permission_ask_deny", "completed", execute, verify)


def policy_deny() -> ToolPermissionScenario:
    """不可绕过的安全规则拒绝后不得执行 shell。"""
    def execute() -> ToolPermissionObservation:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            return _run_tool_scenario(
                root,
                [tool_call("policy-deny-1", "bash", '{"command":"sudo reboot"}')],
                confirmation=lambda *_: "allow",
            )

    def verify(observation: ToolPermissionObservation) -> None:
        resolution = _payload(observation.records("permission_resolved", call_id="policy-deny-1")[0])
        finished = _payload(observation.records("tool_execution_finished", call_id="policy-deny-1")[0])
        assert resolution.get("resolution") == "policy_denied"
        assert finished.get("status") == "denied"
        assert isinstance(finished.get("failure"), dict)
        assert finished["failure"].get("code") == "permission_denied"

    return ToolPermissionScenario("policy_deny", "completed", execute, verify)


def tool_execution_failure() -> ToolPermissionScenario:
    """真实工具失败仍须回填结构化结果，并允许 Agent 完成下一轮。"""
    def execute() -> ToolPermissionObservation:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            return _run_tool_scenario(
                root,
                [tool_call("failure-1", "read_file", '{"path":"missing.txt"}')],
                confirmation=lambda *_: "allow",
            )

    def verify(observation: ToolPermissionObservation) -> None:
        finished = _payload(observation.records("tool_execution_finished", call_id="failure-1")[0])
        assert finished.get("status") == "error"
        assert isinstance(finished.get("failure"), dict)
        assert finished["failure"].get("phase") == "execution"

    return ToolPermissionScenario("tool_execution_failure", "completed", execute, verify)


def duplicate_exact_tool_call() -> ToolPermissionScenario:
    """同轮精确重复只执行首次 glob，并为第二个 call_id 复用结果。"""
    def execute() -> ToolPermissionObservation:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.py").write_text("", encoding="utf-8")
            return _run_tool_scenario(
                root,
                [
                    tool_call("duplicate-1", "glob", '{"pattern":"*.py"}'),
                    tool_call("duplicate-2", "glob", '{ "pattern" : "*.py" }'),
                ],
                confirmation=lambda *_: "allow",
            )

    def verify(observation: ToolPermissionObservation) -> None:
        first = _payload(observation.records("tool_execution_finished", call_id="duplicate-1")[0])
        second = _payload(observation.records("tool_execution_finished", call_id="duplicate-2")[0])
        assert first.get("status") == "success"
        assert second.get("status") == "deduplicated"
        assert second.get("source_call_id") == "duplicate-1"
        assert not observation.records("tool_execution_dispatched", call_id="duplicate-2")

    return ToolPermissionScenario("duplicate_exact_tool_call", "completed", execute, verify)


def _payload(record: dict[str, object]) -> dict[str, object]:
    """取得场景断言使用的 Transcript payload。"""
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def v1_scenarios() -> tuple[ToolPermissionScenario, ...]:
    """返回当前 Phase2 的确定性 Tool / Permission 契约场景。"""
    return (
        tool_allow_success(),
        permission_ask_allow(),
        permission_ask_deny(),
        policy_deny(),
        tool_execution_failure(),
        duplicate_exact_tool_call(),
    )
