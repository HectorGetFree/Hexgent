"""Agent Turn 协作式中断和工具结果配对测试。"""

import json
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from threading import Event

from hexgent.context import ContextManager
from hexgent.events import RunFinishedEvent, TextDeltaEvent, ToolFinishedEvent
from hexgent.loop.interrupt import InterruptController
from hexgent.plan import InMemoryPlanStore
from hexgent.session import Session
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import ToolExecutionResult


agent_loop_module = import_module("hexgent.loop.agent_loop")
turn_module = import_module("hexgent.loop.agent_turn")
tool_execution_module = import_module("hexgent.tool.tool_execution")


class FakeResponses:
    """按顺序返回测试提供的流式事件。"""

    def __init__(self, streams: list[object]) -> None:
        self.streams = list(streams)
        self.calls: list[dict] = []

    def create(self, **arguments):
        """记录模型请求并返回下一个事件流。"""
        self.calls.append(arguments)
        return iter(self.streams.pop(0))


class FakeClient:
    """提供 Agent Loop 测试所需的最小 Responses 客户端。"""

    def __init__(self, streams: list[object]) -> None:
        self.responses = FakeResponses(streams)


def make_response(output: list[object], text: str = "") -> SimpleNamespace:
    """构造协议完整的模型响应。"""
    return SimpleNamespace(
        output=output,
        output_text=text,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def make_tool_call(call_id: str, name: str, arguments: str) -> SimpleNamespace:
    """构造一个最小函数调用。"""
    return SimpleNamespace(
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=arguments,
    )


def finished_result(events: list[object]):
    """取得事件流中唯一的 Agent Loop 终态。"""
    finished = [event for event in events if isinstance(event, RunFinishedEvent)]
    if len(finished) != 1:
        raise AssertionError("中断后的 Agent Loop 必须产生唯一终态")
    return finished[0].result


class LoopInterruptTest(unittest.TestCase):
    """验证中断结束当前 Loop，同时保持 Context 可继续使用。"""

    def setUp(self) -> None:
        """为每个测试创建独立的工具上下文。"""
        self.tool_context = ToolExecutionContext(
            InMemoryPlanStore(),
            Path.cwd().resolve(),
        )

    def test_controller_keeps_first_interrupt_reason(self) -> None:
        """并发调用方不能覆盖首次形成的中断原因。"""
        controller = InterruptController()

        controller.interrupt("steering")
        controller.interrupt("user_cancel")

        self.assertTrue(controller.interrupted)
        self.assertEqual(controller.reason, "steering")

    def test_controller_invokes_registered_callback_once(self) -> None:
        """中断应立即唤醒模型流等已注册的阻塞资源。"""
        controller = InterruptController()
        invocations: list[str] = []
        controller.add_callback(lambda: invocations.append("closed"))

        controller.interrupt("user_cancel")
        controller.interrupt("steering")

        self.assertEqual(invocations, ["closed"])

    def test_signal_interrupt_does_not_wait_for_blocking_callback(self) -> None:
        """信号入口应先形成中断状态，不能同步等待阻塞资源关闭。"""
        controller = InterruptController()
        callback_started = Event()
        release_callback = Event()

        def blocking_callback() -> None:
            callback_started.set()
            release_callback.wait(timeout=1)

        controller.add_callback(blocking_callback)
        controller.interrupt_from_signal("user_cancel")

        self.assertTrue(controller.interrupted)
        self.assertEqual(controller.reason, "user_cancel")
        self.assertTrue(callback_started.wait(timeout=1))
        release_callback.set()

    def test_interrupt_at_turn_entry_does_not_call_model(self) -> None:
        """Turn 开始前收到中断时不得发起模型请求。"""
        controller = InterruptController()
        controller.interrupt("user_cancel")
        client = FakeClient([])

        with patch.object(agent_loop_module, "get_client", return_value=client):
            events = list(
                agent_loop_module.agent_loop(
                    [],
                    self.tool_context,
                    interrupt_controller=controller,
                )
            )

        result = finished_result(events)
        self.assertEqual(client.responses.calls, [])
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(result.stop_reason, "user_cancel")

    def test_interrupt_during_stream_does_not_commit_partial_response(self) -> None:
        """模型流中断后只展示已有 Delta，不得提交伪完整响应。"""
        controller = InterruptController()
        context_manager = ContextManager()
        completed = make_response([], "不应提交")

        def stream():
            yield SimpleNamespace(
                type="response.output_text.delta",
                delta="已经展示",
            )
            controller.interrupt("steering")
            yield SimpleNamespace(type="response.completed", response=completed)

        client = FakeClient([stream()])
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(turn_module, "get_model", return_value="fake"),
        ):
            events = list(
                agent_loop_module.agent_loop(
                    context_manager,
                    self.tool_context,
                    interrupt_controller=controller,
                )
            )

        result = finished_result(events)
        deltas = [event.delta for event in events if isinstance(event, TextDeltaEvent)]
        self.assertEqual(deltas, ["已经展示"])
        self.assertEqual(context_manager.active_messages, [])
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(result.stop_reason, "steering")

    def test_interrupt_during_tools_pairs_every_tool_call(self) -> None:
        """已完成工具保留结果，后续工具必须获得配对中断结果。"""
        controller = InterruptController()
        context_manager = ContextManager()
        calls = [
            make_tool_call(
                "write-1",
                "write_file",
                '{"path":"a.txt","content":"内容"}',
            ),
            make_tool_call("read-2", "read_file", '{"path":"a.txt"}'),
        ]
        response = make_response(calls)
        stream = [SimpleNamespace(type="response.completed", response=response)]
        client = FakeClient([stream])

        def execute(*_args, **_kwargs):
            controller.interrupt("user_cancel")
            return ToolExecutionResult("success", "写入完成", False)

        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(turn_module, "get_model", return_value="fake"),
            patch.object(
                tool_execution_module,
                "execute_tool_call",
                side_effect=execute,
            ) as execute_mock,
        ):
            events = list(
                agent_loop_module.agent_loop(
                    context_manager,
                    self.tool_context,
                    user_confirmation=lambda _tool, _message: "allow",
                    interrupt_controller=controller,
                )
            )

        result = finished_result(events)
        tool_outputs = [
            message
            for message in context_manager.active_messages
            if isinstance(message, dict)
            and message.get("type") == "function_call_output"
        ]
        finished_statuses = {
            event.call_id: event.status
            for event in events
            if isinstance(event, ToolFinishedEvent)
        }
        self.assertEqual(execute_mock.call_count, 1)
        self.assertEqual(
            [output["call_id"] for output in tool_outputs],
            ["write-1", "read-2"],
        )
        self.assertEqual(tool_outputs[0]["output"], "写入完成")
        self.assertIn("已被中断", tool_outputs[1]["output"])
        self.assertEqual(finished_statuses["write-1"], "success")
        self.assertEqual(finished_statuses["read-2"], "error")
        self.assertEqual(result.status, "interrupted")

    def test_session_records_interrupt_boundary_and_terminal(self) -> None:
        """Session 应记录中断安全边界以及最终 Run 事实。"""
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            context_manager = ContextManager()
            query = context_manager.append_user_query("执行长任务")
            session = Session.persistent(root)
            session.record_user_query(query)
            controller = InterruptController()
            controller.interrupt("user_cancel")
            client = FakeClient([])

            with patch.object(agent_loop_module, "get_client", return_value=client):
                list(
                    agent_loop_module.agent_loop(
                        context_manager,
                        ToolExecutionContext(InMemoryPlanStore(), root),
                        session=session,
                        interrupt_controller=controller,
                    )
                )
            records = [
                json.loads(line)
                for line in session.store.transcript_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

        record_types = [record["type"] for record in records]
        self.assertIn("agent_run_interrupted", record_types)
        self.assertEqual(record_types[-1], "agent_run_finished")
        self.assertEqual(records[-1]["payload"]["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
