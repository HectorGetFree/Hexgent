"""Agent Runtime 状态契约评测夹具。

FakeModel 只负责复现确定的模型轨迹；StateTrace 同时收集事件、终态和持久化事实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from unittest.mock import patch

from hexgent.context import ContextManager
from hexgent.events import AgentEvent, RunFinishedEvent
from hexgent.loop.loop_state import LoopState
from hexgent.plan import InMemoryPlanStore
from hexgent.run_control import RunControl
from hexgent.tool.tool_context import ToolExecutionContext


@dataclass(frozen=True)
class FakeToolCall:
    """带 JSON 序列化能力的 function_call，便于 Checkpoint 评测。"""

    type: str
    call_id: str
    name: str
    arguments: str

    def model_dump(self, mode: str = "python") -> dict[str, str]:
        """模拟 Responses SDK 对象的稳定序列化接口。"""
        return {"type": self.type, "call_id": self.call_id, "name": self.name, "arguments": self.arguments}


def tool_call(call_id: str, name: str = "read_file", arguments: str = '{"path":"a"}') -> FakeToolCall:
    """构造最小 Responses function_call。"""
    return FakeToolCall("function_call", call_id, name, arguments)


def text_response(text: str = "", output: list[Any] | None = None) -> SimpleNamespace:
    """构造一个会被 FakeModel 转成 completed 事件的响应。"""
    return SimpleNamespace(
        output=output or [],
        output_text=text,
        usage=SimpleNamespace(input_tokens=1, output_tokens=max(1, len(text))),
        text_deltas=[text] if text else [],
    )


@dataclass
class FakeModel:
    """按顺序重放预设模型响应，并记录每次请求。"""

    responses: list[Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.responses = list(self.responses)

    def create(self, **request: Any):
        """实现 Responses.create(stream=True) 的最小协议。"""
        self.calls.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        events = [
            SimpleNamespace(type="response.output_text.delta", delta=delta)
            for delta in getattr(response, "text_deltas", [])
        ]
        events.append(SimpleNamespace(type="response.completed", response=response))
        return iter(events)


@dataclass
class StateTrace:
    """一次评测运行的可断言轨迹。"""

    events: list[AgentEvent]
    transcript: list[dict[str, Any]] = field(default_factory=list)

    @property
    def finished(self) -> RunFinishedEvent:
        """返回唯一终态，否则直接让契约评测失败。"""
        finished = [event for event in self.events if isinstance(event, RunFinishedEvent)]
        if len(finished) != 1:
            raise AssertionError(f"期望唯一 RunFinishedEvent，实际为 {len(finished)} 个")
        return finished[0]

    def assert_terminal_last(self) -> None:
        """终态必须是 Core 事件流最后一个事件。"""
        if not self.events or not isinstance(self.events[-1], RunFinishedEvent):
            raise AssertionError("RunFinishedEvent 必须是事件流最后一个事件")

    def assert_tool_pairs(self) -> None:
        """已开始的工具调用必须有对应的完成事件。"""
        started = {event.call_id for event in self.events if event.__class__.__name__ == "ToolStartedEvent"}
        finished = {event.call_id for event in self.events if event.__class__.__name__ == "ToolFinishedEvent"}
        missing = started - finished
        if missing:
            raise AssertionError(f"缺少 ToolFinishedEvent：{sorted(missing)}")


def run_trace(
    responses: Iterable[Any],
    *,
    session: Any | None = None,
    run_control: RunControl | None = None,
    workdir: Path | None = None,
    config: Any | None = None,
    tool_context: ToolExecutionContext | None = None,
    user_confirmation: Any | None = None,
) -> tuple[StateTrace, FakeModel]:
    """使用正式 Agent Loop 运行一条确定性模型轨迹。"""
    from hexgent.config import HexgentConfig
    from hexgent.loop import agent_loop as loop_module

    model = FakeModel(list(responses))
    context = tool_context or ToolExecutionContext(
        InMemoryPlanStore(), (workdir or Path.cwd()).resolve()
    )
    state = LoopState(ContextManager(), context)
    with patch.object(loop_module, "get_client", return_value=SimpleNamespace(responses=model)):
        events = list(loop_module.agent_loop(
            state,
            config=config or HexgentConfig(),
            session=session,
            run_control=run_control or RunControl(),
            **(
                {"user_confirmation": user_confirmation}
                if user_confirmation is not None
                else {}
            ),
        ))
    transcript = session.store.read_transcript() if session is not None and session.store is not None else []
    return StateTrace(events, transcript), model


def run_trace_capturing_failure(
    responses: Iterable[Any],
    *,
    session: Any | None = None,
    run_control: RunControl | None = None,
    workdir: Path | None = None,
    config: Any | None = None,
    tool_context: ToolExecutionContext | None = None,
    user_confirmation: Any | None = None,
) -> tuple[StateTrace, FakeModel, BaseException | None]:
    """执行正式 Agent Loop，并把故障注入触发的异常保留为可观察结果。

    该夹具只用于评测中的受控 crash point：已经 yield 的实时事件和已经写入的
    Transcript 仍来自 Production Loop；它不补写或伪造任何 Runtime 事实。
    """
    from hexgent.config import HexgentConfig
    from hexgent.loop import agent_loop as loop_module

    model = FakeModel(list(responses))
    context = tool_context or ToolExecutionContext(
        InMemoryPlanStore(), (workdir or Path.cwd()).resolve()
    )
    state = LoopState(ContextManager(), context)
    events: list[AgentEvent] = []
    failure: BaseException | None = None
    with patch.object(loop_module, "get_client", return_value=SimpleNamespace(responses=model)):
        try:
            events.extend(
                loop_module.agent_loop(
                    state,
                    config=config or HexgentConfig(),
                    session=session,
                    run_control=run_control or RunControl(),
                    **(
                        {"user_confirmation": user_confirmation}
                        if user_confirmation is not None
                        else {}
                    ),
                )
            )
        except BaseException as error:
            failure = error
    transcript = (
        session.store.read_transcript()
        if session is not None and session.store is not None
        else []
    )
    return StateTrace(events, transcript), model, failure
