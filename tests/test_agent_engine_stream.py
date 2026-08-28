"""终端流式展示测试。"""

from importlib import import_module
from io import StringIO
import unittest
from unittest.mock import patch

from hexgent.events import (
    AgentLoopResult,
    RunFinishedEvent,
    PlanCreatedEvent,
    PlanUpdatedEvent,
    TextDeltaEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
    ToolExecutionCompletedEvent,
)
from hexgent.context import ContextManager
from hexgent.session import Session
from hexgent.plan import Plan, PlanStep


agent_engine_module = import_module("hexgent.agent_engine")


class FakeTerminalIO:
    """按顺序返回预设消息的终端输入替身。"""

    def __init__(self, messages: list[str]) -> None:
        self._messages = iter(messages)

    def read_message(self) -> str:
        """返回下一条预设消息。"""
        return next(self._messages)

    def request_confirmation(self, _tool_name: str, _message: str) -> str:
        """测试默认允许工具权限确认。"""
        return "allow"


class AgentEngineStreamTest(unittest.TestCase):
    """验证终端不会重复打印已经流式输出的最终回答。"""

    def setUp(self) -> None:
        """终端展示测试使用纯内存上下文，避免写入真实工作区。"""
        persistent = patch.object(
            agent_engine_module.Session,
            "persistent",
            return_value=Session(),
        )
        persistent.start()
        self.addCleanup(persistent.stop)

    def test_final_text_is_not_printed_again_on_run_finished(self) -> None:
        """最终事件只应展示耗时，不应再次输出完整回答。"""
        events = iter(
            [
                TextDeltaEvent("你好"),
                TextDeltaEvent("，世界"),
                RunFinishedEvent(AgentLoopResult("你好，世界", 1.2)),
            ]
        )
        output = StringIO()
        with (
            patch.object(
                agent_engine_module,
                "TerminalIO",
                return_value=FakeTerminalIO(["你好", "q"]),
            ),
            patch.object(agent_engine_module, "agent_loop", return_value=events),
            patch("sys.stdout", output),
        ):
            agent_engine_module.main()

        rendered = output.getvalue()
        self.assertEqual(rendered.count("你好，世界"), 1)
        self.assertIn("正在处理，请稍候", rendered)
        self.assertIn("本次用时：1.2 秒", rendered)

    def test_tool_details_and_plan_are_rendered_without_tool_time(self) -> None:
        """终端应展示工具摘要和计划，但不展示单工具用时。"""
        plan = Plan(
            "检查项目",
            "in_progress",
            [
                PlanStep("读取代码", "正在读取代码", "in_progress", "step-1"),
                PlanStep("总结结果", "正在总结结果", "pending", "step-2"),
            ],
        )
        events = iter(
            [
                ToolStartedEvent("create_plan", "c1", "create_plan(检查项目)"),
                ToolFinishedEvent("create_plan", "c1", "success"),
                PlanCreatedEvent(plan),
                PlanUpdatedEvent(
                    Plan(
                        "检查项目",
                        "in_progress",
                        [
                            PlanStep(
                                "读取代码",
                                "正在读取代码",
                                "completed",
                                "step-1",
                            ),
                            PlanStep(
                                "总结结果",
                                "正在总结结果",
                                "in_progress",
                                "step-2",
                            ),
                            PlanStep(
                                "补充验证",
                                "正在补充验证",
                                "pending",
                                "step-r2-1",
                            ),
                        ],
                        revision=2,
                        last_replan_reason="代码读取完成，进入总结阶段",
                    )
                ),
                RunFinishedEvent(AgentLoopResult("", 0.5)),
            ]
        )
        output = StringIO()
        with (
            patch.object(
                agent_engine_module,
                "TerminalIO",
                return_value=FakeTerminalIO(["检查", "q"]),
            ),
            patch.object(agent_engine_module, "agent_loop", return_value=events),
            patch("sys.stdout", output),
        ):
            agent_engine_module.main()

        rendered = output.getvalue()
        self.assertIn("开始处理工具：create_plan(检查项目)", rendered)
        self.assertIn("执行计划：检查项目", rendered)
        self.assertIn("计划版本：1", rendered)
        self.assertIn("1. [进行中] 读取代码", rendered)
        self.assertIn("(step-1)", rendered)
        self.assertIn("计划进度：检查项目", rendered)
        self.assertIn("计划版本：2", rendered)
        self.assertIn("调整原因：代码读取完成，进入总结阶段", rendered)
        self.assertIn("1. [已完成] 读取代码", rendered)
        self.assertIn("2. [进行中] 总结结果", rendered)
        self.assertIn("3. [待处理] 补充验证", rendered)
        self.assertNotIn("工具结束：create_plan，状态：成功，用时", rendered)

    def test_parallel_execution_completion_is_not_printed_twice(self) -> None:
        """并行工具即时完成后，最终顺序提交事件不应重复展示。"""
        events = iter(
            [
                ToolStartedEvent("read_file", "c1", "read_file(a.py)"),
                ToolExecutionCompletedEvent("read_file", "c1", "success"),
                ToolFinishedEvent("read_file", "c1", "success"),
                RunFinishedEvent(AgentLoopResult("", 0.1)),
            ]
        )
        output = StringIO()
        with (
            patch.object(
                agent_engine_module,
                "TerminalIO",
                return_value=FakeTerminalIO(["读取文件", "q"]),
            ),
            patch.object(agent_engine_module, "agent_loop", return_value=events),
            patch("sys.stdout", output),
        ):
            agent_engine_module.main()

        self.assertEqual(
            output.getvalue().count("工具结束：read_file，状态：成功"),
            1,
        )

    def test_limit_result_returns_to_next_user_input(self) -> None:
        """预算收尾结束后 CLI 应继续接收下一条用户输入。"""
        first_events = iter(
            [TextDeltaEvent("第一轮收尾"), RunFinishedEvent(AgentLoopResult("第一轮收尾", 1.0, "max_total_tokens"))]
        )
        second_events = iter(
            [TextDeltaEvent("第二轮继续"), RunFinishedEvent(AgentLoopResult("第二轮继续", 0.5))]
        )
        output = StringIO()
        with (
            patch.object(
                agent_engine_module,
                "TerminalIO",
                return_value=FakeTerminalIO(["开始任务", "继续", "q"]),
            ),
            patch.object(
                agent_engine_module,
                "agent_loop",
                side_effect=[first_events, second_events],
            ) as stream,
            patch("sys.stdout", output),
        ):
            agent_engine_module.main()

        self.assertEqual(stream.call_count, 2)
        self.assertIn("第一轮收尾", output.getvalue())
        self.assertIn("第二轮继续", output.getvalue())

    def test_incomplete_plan_message_does_not_end_session(self) -> None:
        """Plan 未完成终态应给出恢复提示，并继续接收下一条用户输入。"""
        first_events = iter(
            [
                RunFinishedEvent(
                    AgentLoopResult(
                        "",
                        0.4,
                        stop_reason="plan_still_active",
                        status="incomplete",
                    )
                )
            ]
        )
        second_events = iter(
            [TextDeltaEvent("已经继续"), RunFinishedEvent(AgentLoopResult("已经继续", 0.2))]
        )
        output = StringIO()

        with (
            patch.object(
                agent_engine_module,
                "TerminalIO",
                return_value=FakeTerminalIO(["检查项目", "继续", "q"]),
            ),
            patch.object(
                agent_engine_module,
                "agent_loop",
                side_effect=[first_events, second_events],
            ) as stream,
            patch("sys.stdout", output),
        ):
            agent_engine_module.main()

        self.assertEqual(stream.call_count, 2)
        self.assertIn("Plan 尚未完成", output.getvalue())
        self.assertIn("已经继续", output.getvalue())

    def test_empty_input_does_not_end_session(self) -> None:
        """空行应被忽略，不应结束整个交互会话。"""
        events = iter(
            [
                TextDeltaEvent("继续运行"),
                RunFinishedEvent(AgentLoopResult("继续运行", 0.2)),
            ]
        )
        output = StringIO()
        with (
            patch.object(
                agent_engine_module,
                "TerminalIO",
                return_value=FakeTerminalIO(["", "有效输入", "q"]),
            ),
            patch.object(
                agent_engine_module,
                "agent_loop",
                return_value=events,
            ) as stream,
            patch("sys.stdout", output),
        ):
            agent_engine_module.main()

        self.assertEqual(stream.call_count, 1)
        self.assertIn("继续运行", output.getvalue())


if __name__ == "__main__":
    unittest.main()
