"""Agent Loop 执行限制测试。"""

from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hexgent.events import (
    LoopLimitReachedEvent,
    RunFinishedEvent,
    PlanCreatedEvent,
    PlanUpdatedEvent,
    TextDeltaEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)
from hexgent.loop.limits import LoopLimits
from hexgent.loop.state import LoopState
from hexgent.loop.transitions import Continue, Stop
from hexgent.plan import InMemoryPlanStore, Plan, PlanStep
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import ToolExecutionResult
from hexgent.tool.tool_registry import get_tool


agent_loop_module = import_module("hexgent.loop.agent_loop")
turn_module = import_module("hexgent.loop.agent_turn")
tool_execution_module = import_module("hexgent.tool.tool_execution")


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


def drain_generator(generator):
    """收集生成器事件，并取得其最终返回值。"""
    events = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stopped:
            return events, stopped.value


def run_agent(*args, **kwargs):
    """运行完整事件流，并返回事件和唯一 Terminal。"""
    events = list(agent_loop_module.agent_loop(*args, **kwargs))
    finished = [event for event in events if isinstance(event, RunFinishedEvent)]
    if len(finished) != 1:
        raise AssertionError("Agent Loop 必须产生唯一 RunFinishedEvent")
    return events, finished[0].result


class AgentLoopLimitsTest(unittest.TestCase):
    """验证循环限制和工具结果配对。"""

    def setUp(self) -> None:
        """为每个用例创建独立工具上下文。"""
        self.context = ToolExecutionContext(InMemoryPlanStore(), Path.cwd().resolve())

    def test_round_limit_reserves_one_finalization_response(self) -> None:
        """正常轮次耗尽后应额外生成一次不带工具的收尾响应。"""
        client = FakeClient(
            [make_response([make_tool_call("c1")]), make_response([], "已收尾")]
        )
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(turn_module, "get_model", return_value="fake"),
            patch.object(
                tool_execution_module,
                "execute_tool_call",
                return_value=ToolExecutionResult("success", "ok", False),
            ),
        ):
            _, result = run_agent(
                [], self.context, LoopLimits(max_model_rounds=1)
            )

        self.assertEqual(result.stop_reason, "max_model_rounds")
        self.assertEqual(result.output_text, "已收尾")
        self.assertEqual(len(client.responses.calls), 2)
        self.assertIn("tools", client.responses.calls[0])
        self.assertNotIn("tools", client.responses.calls[1])
        self.assertTrue(all(call["stream"] for call in client.responses.calls))

    def test_budget_limit_returns_continue_with_finalization_state(self) -> None:
        """预算命中应构造收尾 State，而不是在当前分支直接结束。"""
        client = FakeClient([])
        state = LoopState(messages=[], tool_context=self.context)

        events, outcome = drain_generator(
            turn_module.run_turn(
                state,
                client,
                LoopLimits(max_elapsed_seconds=0),
                lambda _tool, _message: "allow",
            )
        )

        self.assertIsInstance(outcome, Continue)
        self.assertEqual(outcome.reason, "budget_finalization")
        self.assertEqual(outcome.next_state.mode, "finalizing")
        self.assertEqual(outcome.next_state.stop_reason, "max_elapsed_seconds")
        self.assertEqual(len(client.responses.calls), 0)
        self.assertIsInstance(events[0], LoopLimitReachedEvent)

    def test_valid_response_without_tools_returns_stop(self) -> None:
        """有效响应没有工具调用时应产生正常 Stop。"""
        client = FakeClient([make_response([], "完成")])
        state = LoopState(messages=[], tool_context=self.context)
        with patch.object(turn_module, "get_model", return_value="fake"):
            _, outcome = drain_generator(
                turn_module.run_turn(
                    state,
                    client,
                    LoopLimits(),
                    lambda _tool, _message: "allow",
                )
            )

        self.assertIsInstance(outcome, Stop)
        self.assertEqual(outcome.terminal.status, "completed")
        self.assertEqual(outcome.terminal.output_text, "完成")

    def test_completed_plan_is_cleared_before_normal_stop(self) -> None:
        """已完成 Plan 应正常结束，并在结束前从会话存储中清除。"""
        self.context.plan_store.set_plan(
            Plan(
                "完成检查",
                "completed",
                [PlanStep("检查结果", "正在检查结果", "completed", "step-1")],
            )
        )
        client = FakeClient([make_response([], "全部完成")])
        state = LoopState(messages=[], tool_context=self.context)

        with patch.object(turn_module, "get_model", return_value="fake"):
            _, outcome = drain_generator(
                turn_module.run_turn(
                    state,
                    client,
                    LoopLimits(),
                    lambda _tool, _message: "allow",
                )
            )

        self.assertIsInstance(outcome, Stop)
        self.assertEqual(outcome.terminal.status, "completed")
        self.assertIsNone(self.context.plan_store.get_plan())

    def test_blocked_plan_returns_blocked_terminal_and_is_retained(self) -> None:
        """阻塞 Plan 应产生 blocked 终态，并保留给后续用户输入恢复。"""
        plan = Plan(
            "读取受限文件",
            "blocked",
            [PlanStep("等待授权", "正在等待授权", "pending", "step-1")],
            blocked_reason="需要用户授权读取工作区外文件",
        )
        self.context.plan_store.set_plan(plan)
        client = FakeClient([make_response([], "需要你的授权")])
        state = LoopState(messages=[], tool_context=self.context)

        with patch.object(turn_module, "get_model", return_value="fake"):
            _, outcome = drain_generator(
                turn_module.run_turn(
                    state,
                    client,
                    LoopLimits(),
                    lambda _tool, _message: "allow",
                )
            )

        self.assertIsInstance(outcome, Stop)
        self.assertEqual(outcome.terminal.status, "blocked")
        self.assertEqual(outcome.terminal.stop_reason, "plan_blocked")
        self.assertIs(self.context.plan_store.get_plan(), plan)

    def test_active_plan_after_nudges_returns_incomplete_and_is_retained(self) -> None:
        """活动 Plan 提醒耗尽后应明确未完成，而不能伪装成正常完成。"""
        plan = Plan(
            "检查项目",
            "in_progress",
            [PlanStep("读取代码", "正在读取代码", "in_progress", "step-1")],
        )
        self.context.plan_store.set_plan(plan)
        client = FakeClient([make_response([], "暂时停止")])
        state = LoopState(messages=[], tool_context=self.context)

        with patch.object(turn_module, "get_model", return_value="fake"):
            _, outcome = drain_generator(
                turn_module.run_turn(
                    state,
                    client,
                    LoopLimits(max_plan_completion_nudges=0),
                    lambda _tool, _message: "allow",
                )
            )

        self.assertIsInstance(outcome, Stop)
        self.assertEqual(outcome.terminal.status, "incomplete")
        self.assertEqual(outcome.terminal.stop_reason, "plan_still_active")
        self.assertIs(self.context.plan_store.get_plan(), plan)

    def test_active_plan_nudge_constructs_explicit_next_state(self) -> None:
        """活动 Plan 尚有提醒额度时应构造原因明确的下一轮 State。"""
        self.context.plan_store.set_plan(
            Plan(
                "检查项目",
                "in_progress",
                [PlanStep("读取代码", "正在读取代码", "in_progress", "step-1")],
            )
        )
        client = FakeClient([make_response([], "准备结束", input_tokens=7, output_tokens=3)])
        state = LoopState(messages=[], tool_context=self.context)

        with patch.object(turn_module, "get_model", return_value="fake"):
            _, outcome = drain_generator(
                turn_module.run_turn(
                    state,
                    client,
                    LoopLimits(max_plan_completion_nudges=1),
                    lambda _tool, _message: "allow",
                )
            )

        self.assertIsInstance(outcome, Continue)
        self.assertEqual(outcome.reason, "plan_continuation")
        self.assertEqual(outcome.next_state.plan_completion_nudges, 1)
        self.assertEqual(outcome.next_state.model_rounds, 1)
        self.assertEqual(outcome.next_state.total_tokens, 10)
        self.assertIn("不能直接结束", outcome.next_state.messages[-1]["content"])

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
            patch.object(turn_module, "get_model", return_value="fake"),
            patch.object(
                tool_execution_module,
                "execute_tool_call",
                return_value=ToolExecutionResult("success", "ok", False),
            ),
        ):
            _, result = run_agent(
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
            patch.object(turn_module, "get_model", return_value="fake"),
        ):
            _, result = run_agent(
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
            patch.object(turn_module, "get_model", return_value="fake"),
            patch.object(
                tool_execution_module,
                "execute_tool_call",
                return_value=ToolExecutionResult("success", "ok", False),
            ),
        ):
            _, result = run_agent(
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
            patch.object(turn_module, "get_model", return_value="fake"),
        ):
            events = list(agent_loop_module.agent_loop([], self.context))

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
            patch.object(turn_module, "get_model", return_value="fake"),
            patch.object(tool_execution_module, "execute_tool_call", side_effect=execute),
        ):
            events = list(agent_loop_module.agent_loop([], self.context))

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

    def test_create_plan_emits_initial_plan(self) -> None:
        """Plan 创建成功后应向界面层发送完整初始计划。"""
        arguments = (
            '{"objective":"检查项目","steps":['
            '{"content":"读取代码","process_message":"正在读取代码",'
            '"status":"in_progress"},'
            '{"content":"总结结果","process_message":"正在总结结果",'
            '"status":"pending"}]}'
        )
        client = FakeClient(
            [
                make_response([make_tool_call("plan-1", "create_plan", arguments)]),
                make_response([], "继续检查"),
                make_response([], "完成"),
            ]
        )
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(turn_module, "get_model", return_value="fake"),
        ):
            events = list(agent_loop_module.agent_loop([], self.context))

        created = [event for event in events if isinstance(event, PlanCreatedEvent)]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].plan.objective, "检查项目")
        self.assertEqual(
            [step.content for step in created[0].plan.steps],
            ["读取代码", "总结结果"],
        )

    def test_update_plan_emits_latest_step_statuses(self) -> None:
        """Plan 更新成功后应发送最新的整体状态和步骤状态。"""
        create_arguments = (
            '{"objective":"检查项目","steps":['
            '{"content":"读取代码","process_message":"正在读取代码",'
            '"status":"in_progress"},'
            '{"content":"总结结果","process_message":"正在总结结果",'
            '"status":"pending"}]}'
        )
        update_arguments = (
            '{"expected_revision":1,"status":"in_progress",'
            '"reason":null,"blocked_reason":null,"steps":['
            '{"step_id":"step-1","content":"读取代码",'
            '"process_message":"正在读取代码",'
            '"status":"completed"},'
            '{"step_id":"step-2","content":"总结结果",'
            '"process_message":"正在总结结果",'
            '"status":"in_progress"}]}'
        )
        client = FakeClient(
            [
                make_response(
                    [make_tool_call("plan-1", "create_plan", create_arguments)]
                ),
                make_response(
                    [make_tool_call("plan-2", "update_plan", update_arguments)]
                ),
                make_response([], "继续执行"),
                make_response([], "等待下一轮"),
            ]
        )
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(turn_module, "get_model", return_value="fake"),
        ):
            events = list(agent_loop_module.agent_loop([], self.context))

        updated = [event for event in events if isinstance(event, PlanUpdatedEvent)]
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0].plan.status, "in_progress")
        self.assertEqual(updated[0].plan.revision, 2)
        self.assertIsNone(updated[0].plan.last_replan_reason)
        self.assertEqual(
            [step.status for step in updated[0].plan.steps],
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
            patch.object(turn_module, "get_model", return_value="fake"),
        ):
            events = list(
                agent_loop_module.agent_loop(
                    [], self.context, LoopLimits(max_elapsed_seconds=0)
                )
            )

        limits = [event for event in events if isinstance(event, LoopLimitReachedEvent)]
        self.assertEqual(len(limits), 1)
        self.assertIsInstance(events[-1], RunFinishedEvent)

    def test_incomplete_stream_never_executes_tool(self) -> None:
        """不完整响应不得执行残缺工具，并应形成 Error Terminal。"""
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
            patch.object(turn_module, "get_model", return_value="fake"),
            patch.object(tool_execution_module, "execute_tool_call") as execute,
        ):
            events = list(agent_loop_module.agent_loop([], self.context))

        execute.assert_not_called()
        self.assertEqual(len(client.responses.calls), 1)
        self.assertIsInstance(events[-1], RunFinishedEvent)
        self.assertEqual(events[-1].result.status, "failed")
        self.assertIn("未完整结束", events[-1].result.output_text)

    def test_failed_stream_returns_error_terminal(self) -> None:
        """失败事件应转换为包含中文信息的 Error Terminal。"""
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
            patch.object(turn_module, "get_model", return_value="fake"),
        ):
            events = list(agent_loop_module.agent_loop([], self.context))

        self.assertIsInstance(events[-1], RunFinishedEvent)
        self.assertEqual(events[-1].result.status, "failed")
        self.assertIn("模型流式响应失败：连接中断", events[-1].result.output_text)

if __name__ == "__main__":
    unittest.main()
