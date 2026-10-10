"""Phase1 Runtime Contract 场景定义与执行入口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from hexgent.config import HexgentConfig
from hexgent.loop.limits import LoopLimits

from eval.phase1.observation import RuntimeObservation, observation_from_session
from eval.support.agent_contracts import run_trace, text_response
from eval.support.agent_contracts import FakeModel
from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.run_control import RunControl
from types import SimpleNamespace
from hexgent.session import Session
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace


@dataclass(frozen=True)
class RuntimeScenario:
    """描述一个不绑定内部函数的 Runtime Contract 场景。"""

    name: str
    required_events: tuple[str, ...]
    ordering: tuple[tuple[str, str], ...]
    expected_status: str
    execute: Callable[[], RuntimeObservation]
    verify: Callable[[RuntimeObservation], None] | None = None


def normal_completion() -> RuntimeScenario:
    """模型直接返回最终文本，形成 completed terminal。"""
    def execute() -> RuntimeObservation:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = Session.persistent(root)
            session.record_user_query(SimpleNamespace(sequence=1, content="完成任务"))
            trace, _ = run_trace(
                [text_response("已完成")], session=session, workdir=root
            )
            return observation_from_session(trace.events, session)

    return RuntimeScenario(
        name="normal_completion",
        required_events=("RunFinishedEvent",),
        ordering=(),
        expected_status="completed",
        execute=execute,
    )


def model_failure() -> RuntimeScenario:
    """模型调用抛出确定性异常，形成 failed terminal。"""
    def execute() -> RuntimeObservation:
        trace, _ = run_trace([RuntimeError("模型连接失败")])
        return RuntimeObservation.collect(trace.events)

    return RuntimeScenario(
        name="model_failure",
        required_events=("RunFinishedEvent",),
        ordering=(),
        expected_status="failed",
        execute=execute,
    )


def limit_reached() -> RuntimeScenario:
    """模型轮次预算为零，必须先收尾再产生 limit_reached。"""
    def execute() -> RuntimeObservation:
        trace, _ = run_trace(
            [text_response("预算收尾")],
            config=HexgentConfig(loop=LoopLimits(max_model_rounds=0)),
        )
        return RuntimeObservation.collect(trace.events)

    return RuntimeScenario(
        name="limit_reached",
        required_events=("LoopLimitReachedEvent", "LoopTransitionEvent", "RunFinishedEvent"),
        ordering=(
            ("LoopLimitReachedEvent", "LoopTransitionEvent"),
            ("LoopTransitionEvent", "RunFinishedEvent"),
        ),
        expected_status="limit_reached",
        execute=execute,
    )


def pause() -> RuntimeScenario:
    """在模型调用前请求 Pause，形成可继续的 interrupted terminal。"""
    def execute() -> RuntimeObservation:
        control = RunControl()
        control.pause()
        trace, _ = run_trace([text_response("不会执行")], run_control=control)
        return RuntimeObservation.collect(trace.events)

    return RuntimeScenario(
        name="pause",
        required_events=("RunFinishedEvent",),
        ordering=(),
        expected_status="interrupted",
        execute=execute,
    )


def continue_after_pause() -> RuntimeScenario:
    """先暂停，再通过 AgentEngine 的 continue 入口恢复执行。"""
    def execute() -> RuntimeObservation:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            engine = AgentEngine.create(HexgentConfig(), root)
            pause_control = RunControl()
            pause_control.pause()
            first_client = SimpleNamespace(responses=FakeModel([text_response("不会执行")]))
            list(engine.run("继续任务", run_control=pause_control, client=first_client))

            second_client = SimpleNamespace(responses=FakeModel([text_response("已恢复")]))
            events = list(
                engine.run(RunRequest.continue_run(), client=second_client)
            )
            return observation_from_session(events, engine.session)

    return RuntimeScenario(
        name="continue_after_pause",
        required_events=("RunFinishedEvent",),
        ordering=(),
        expected_status="completed",
        execute=execute,
    )


def duplicate_command() -> RuntimeScenario:
    """重复提交同一无效命令时复用第一次 Decision，不产生新效果。"""
    def execute() -> RuntimeObservation:
        import asyncio
        from hexgent.adapters.web.run_manager import RunManager
        from hexgent.integration.protocol import RuntimeCommand
        from hexgent.integration.runtime_adapter import RuntimeCommandAdapter

        async def run() -> RuntimeObservation:
            with TemporaryDirectory() as directory:
                manager = RunManager(HexgentConfig(), Path(directory))
                adapter = RuntimeCommandAdapter(manager)
                command = RuntimeCommand("duplicate-1", "StartRun", "R1", "C1", "now", {})
                first = await adapter.handle(command)
                second = await adapter.handle(command)
                if first != second:
                    raise AssertionError("重复 command 未复用第一次 Decision")
                return RuntimeObservation.collect([])

        return asyncio.run(run())

    return RuntimeScenario(
        name="duplicate_command",
        required_events=(),
        ordering=(),
        expected_status=None,
        execute=execute,
    )


def v1_scenarios() -> tuple[RuntimeScenario, ...]:
    """返回当前已经具备稳定 deterministic fixture 的 V1 场景。"""
    return (
        normal_completion(),
        model_failure(),
        limit_reached(),
        pause(),
        continue_after_pause(),
        duplicate_command(),
    )
