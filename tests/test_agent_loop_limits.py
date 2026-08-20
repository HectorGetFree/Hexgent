"""Agent Loop 执行限制测试。"""

from importlib import import_module
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hexgent.stream_events import (
    LoopLimitReachedEvent,
    RunFinishedEvent,
    TaskCreatedEvent,
    TaskUpdatedEvent,
    TextDeltaEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)
from hexgent.loop_control import LoopLimits
from hexgent.task import InMemoryTaskStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import ToolExecutionResult
from hexgent.tool.tool_registry import get_tool


agent_loop_module = import_module("hexgent.agent_loop")


class FakeResponses:
    """按顺序返回预设模型响应。"""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **arguments):
        """记录请求参数并将下一个完整响应转换为事件流。"""
        self.calls.append(arguments)
        response = self.responses.pop(0)
        if hasattr(response, "stream_events"):
            return iter(response.stream_events)
        events = [
            SimpleNamespace(type="response.output_text.delta", delta=delta)
            for delta in response.text_deltas
        ]
        events.append(SimpleNamespace(type="response.completed", response=response))
        return iter(events)


class FakeClient:
    """只提供测试所需 Responses 接口的客户端。"""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = FakeResponses(responses)


def make_response(
    output,
    text: str = "",
    input_tokens: int = 10,
    output_tokens: int = 5,
    text_deltas: list[str] | None = None,
) -> SimpleNamespace:
    """创建包含固定 Token 用量的测试响应。"""
    return SimpleNamespace(
        output=output,
        output_text=text,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
        text_deltas=text_deltas if text_deltas is not None else ([text] if text else []),
    )


def make_tool_call(
    call_id: str,
    name: str = "read_file",
    arguments: str = '{"path":"a"}',
) -> SimpleNamespace:
    """创建一个模型工具调用。"""
    return SimpleNamespace(
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=arguments,
    )


class AgentLoopLimitsTest(unittest.TestCase):
    """验证循环限制和工具结果配对。"""

    def setUp(self) -> None:
        """为每个用例创建独立工具上下文。"""
        self.context = ToolExecutionContext(InMemoryTaskStore())

    def test_round_limit_reserves_one_finalization_response(self) -> None:
        """正常轮次耗尽后应额外生成一次不带工具的收尾响应。"""
        client = FakeClient(
            [make_response([make_tool_call("c1")]), make_response([], "已收尾")]
        )
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(agent_loop_module, "get_model", return_value="fake"),
            patch.object(
                agent_loop_module,
                "execute_tool_call",
                return_value=ToolExecutionResult("success", "ok", False),
            ),
        ):
            result = agent_loop_module.agent_loop(
                [], self.context, LoopLimits(max_model_rounds=1)
            )

        self.assertEqual(result.stop_reason, "max_model_rounds")
        self.assertEqual(result.output_text, "已收尾")
        self.assertEqual(len(client.responses.calls), 2)
        self.assertIn("tools", client.responses.calls[0])
        self.assertNotIn("tools", client.responses.calls[1])
        self.assertTrue(all(call["stream"] for call in client.responses.calls))

    def test_every_tool_call_gets_a_result_after_tool_limit(self) -> None:
        """批次中额度耗尽后，所有未执行调用仍应获得配对结果。"""
        client = FakeClient(
            [
                make_response(
                    [make_tool_call("c1"), make_tool_call("c2"), make_tool_call("c3")]
                ),
                make_response([], "工具额度收尾"),
            ]
        )
        messages = []
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(agent_loop_module, "get_model", return_value="fake"),
            patch.object(
                agent_loop_module,
                "execute_tool_call",
                return_value=ToolExecutionResult("success", "ok", False),
            ),
        ):
            result = agent_loop_module.agent_loop(
                messages, self.context, LoopLimits(max_tool_calls=1)
            )

        outputs = [
            item
            for item in messages
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]
        self.assertEqual([item["call_id"] for item in outputs], ["c1", "c2", "c3"])
        self.assertEqual(outputs[0]["output"], "ok")
        self.assertTrue(all("未执行" in item["output"] for item in outputs[1:]))
        self.assertEqual(result.stop_reason, "max_tool_calls")

    def test_time_limit_returns_elapsed_time(self) -> None:
        """时间额度耗尽时应直接收尾并返回实际用时。"""
        client = FakeClient([make_response([], "时间收尾")])
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(agent_loop_module, "get_model", return_value="fake"),
        ):
            result = agent_loop_module.agent_loop(
                [], self.context, LoopLimits(max_elapsed_seconds=0)
            )

        self.assertEqual(result.stop_reason, "max_elapsed_seconds")
        self.assertGreaterEqual(result.elapsed_seconds, 0)
        self.assertNotIn("tools", client.responses.calls[0])

    def test_total_token_limit_enters_finalization(self) -> None:
        """累计 Token 达到上限后应在下一轮进入收尾阶段。"""
        client = FakeClient(
            [
                make_response(
                    [make_tool_call("c1")],
                    input_tokens=40,
                    output_tokens=20,
                ),
                make_response([], "Token 收尾"),
            ]
        )
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(agent_loop_module, "get_model", return_value="fake"),
            patch.object(
                agent_loop_module,
                "execute_tool_call",
                return_value=ToolExecutionResult("success", "ok", False),
            ),
        ):
            result = agent_loop_module.agent_loop(
                [], self.context, LoopLimits(max_total_tokens=50)
            )

        self.assertEqual(result.stop_reason, "max_total_tokens")
        self.assertEqual(result.output_text, "Token 收尾")
        self.assertEqual(len(client.responses.calls), 2)

    def test_text_deltas_are_emitted_in_order(self) -> None:
        """流式入口应立即按原始顺序发送文本增量和最终结果。"""
        client = FakeClient(
            [make_response([], "你好，世界", text_deltas=["你好", "，", "世界"])]
        )
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(agent_loop_module, "get_model", return_value="fake"),
        ):
            events = list(agent_loop_module.agent_loop_stream([], self.context))

        deltas = [event.delta for event in events if isinstance(event, TextDeltaEvent)]
        finished = [event for event in events if isinstance(event, RunFinishedEvent)]
        self.assertEqual(deltas, ["你好", "，", "世界"])
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].result.output_text, "你好，世界")

    def test_tool_events_wrap_execution_after_completed_response(self) -> None:
        """工具只应在完整响应到达后执行，并由开始和结束事件包围。"""
        client = FakeClient(
            [make_response([make_tool_call("c1")]), make_response([], "完成")]
        )
        execution_order = []

        def execute(*args, **kwargs):
            execution_order.append("execute")
            return ToolExecutionResult("success", "ok", False)

        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(agent_loop_module, "get_model", return_value="fake"),
            patch.object(agent_loop_module, "execute_tool_call", side_effect=execute),
        ):
            events = list(agent_loop_module.agent_loop_stream([], self.context))

        relevant_events = [
            event
            for event in events
            if isinstance(event, (ToolStartedEvent, ToolFinishedEvent))
        ]
        self.assertEqual(execution_order, ["execute"])
        self.assertIsInstance(relevant_events[0], ToolStartedEvent)
        self.assertIsInstance(relevant_events[1], ToolFinishedEvent)
        self.assertEqual(relevant_events[0].call_id, "c1")
        self.assertEqual(relevant_events[1].call_id, "c1")
        self.assertEqual(relevant_events[0].description, "read_file(a)")
        self.assertFalse(hasattr(relevant_events[1], "elapsed_seconds"))

    def test_create_task_emits_initial_plan(self) -> None:
        """Task 创建成功后应向界面层发送完整初始计划。"""
        arguments = (
            '{"objective":"检查项目","steps":['
            '{"content":"读取代码","process_message":"正在读取代码",'
            '"status":"in_progress"},'
            '{"content":"总结结果","process_message":"正在总结结果",'
            '"status":"pending"}]}'
        )
        client = FakeClient(
            [
                make_response([make_tool_call("task-1", "create_task", arguments)]),
                make_response([], "继续检查"),
                make_response([], "完成"),
            ]
        )
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(agent_loop_module, "get_model", return_value="fake"),
        ):
            events = list(agent_loop_module.agent_loop_stream([], self.context))

        created = [event for event in events if isinstance(event, TaskCreatedEvent)]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].task.objective, "检查项目")
        self.assertEqual(
            [step.content for step in created[0].task.steps],
            ["读取代码", "总结结果"],
        )

    def test_update_task_plan_emits_latest_step_statuses(self) -> None:
        """Task 更新成功后应发送最新的整体状态和步骤状态。"""
        create_arguments = (
            '{"objective":"检查项目","steps":['
            '{"content":"读取代码","process_message":"正在读取代码",'
            '"status":"in_progress"},'
            '{"content":"总结结果","process_message":"正在总结结果",'
            '"status":"pending"}]}'
        )
        update_arguments = (
            '{"status":"in_progress","steps":['
            '{"content":"读取代码","process_message":"正在读取代码",'
            '"status":"completed"},'
            '{"content":"总结结果","process_message":"正在总结结果",'
            '"status":"in_progress"}]}'
        )
        client = FakeClient(
            [
                make_response(
                    [make_tool_call("task-1", "create_task", create_arguments)]
                ),
                make_response(
                    [make_tool_call("task-2", "update_task_plan", update_arguments)]
                ),
                make_response([], "继续执行"),
                make_response([], "等待下一轮"),
            ]
        )
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(agent_loop_module, "get_model", return_value="fake"),
        ):
            events = list(agent_loop_module.agent_loop_stream([], self.context))

        updated = [event for event in events if isinstance(event, TaskUpdatedEvent)]
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0].task.status, "in_progress")
        self.assertEqual(
            [step.status for step in updated[0].task.steps],
            ["completed", "in_progress"],
        )

    def test_tool_description_hides_write_content(self) -> None:
        """写文件的调用说明只能展示路径，不能泄露写入内容。"""
        tool = get_tool("write_file")
        self.assertIsNotNone(tool)
        description = tool.describe('{"path":"notes.md","content":"敏感内容"}')

        self.assertEqual(description, "write_file(notes.md)")
        self.assertNotIn("敏感内容", description)

    def test_limit_event_is_emitted_once_before_finalization(self) -> None:
        """循环限制事件应只出现一次，并位于最终结果之前。"""
        client = FakeClient([make_response([], "已收尾")])
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(agent_loop_module, "get_model", return_value="fake"),
        ):
            events = list(
                agent_loop_module.agent_loop_stream(
                    [], self.context, LoopLimits(max_elapsed_seconds=0)
                )
            )

        limits = [event for event in events if isinstance(event, LoopLimitReachedEvent)]
        self.assertEqual(len(limits), 1)
        self.assertIsInstance(events[-1], RunFinishedEvent)

    def test_incomplete_stream_never_executes_tool(self) -> None:
        """不完整响应不得把尚未确认的工具参数交给执行管线。"""
        client = FakeClient(
            [
                SimpleNamespace(
                    stream_events=[
                        SimpleNamespace(
                            type="response.function_call_arguments.delta",
                            delta='{"path":',
                        ),
                        SimpleNamespace(type="response.incomplete"),
                    ]
                )
            ]
        )
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(agent_loop_module, "get_model", return_value="fake"),
            patch.object(agent_loop_module, "execute_tool_call") as execute,
        ):
            with self.assertRaisesRegex(RuntimeError, "未完整结束"):
                list(agent_loop_module.agent_loop_stream([], self.context))

        execute.assert_not_called()

    def test_failed_stream_raises_chinese_error(self) -> None:
        """失败事件应终止循环并提供中文错误信息。"""
        client = FakeClient(
            [
                SimpleNamespace(
                    stream_events=[
                        SimpleNamespace(type="response.failed", error="连接中断")
                    ]
                )
            ]
        )
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(agent_loop_module, "get_model", return_value="fake"),
        ):
            with self.assertRaisesRegex(RuntimeError, "模型流式响应失败：连接中断"):
                list(agent_loop_module.agent_loop_stream([], self.context))


if __name__ == "__main__":
    unittest.main()
