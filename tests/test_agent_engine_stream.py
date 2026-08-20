"""终端流式展示测试。"""

from importlib import import_module
from io import StringIO
import unittest
from unittest.mock import patch

from hexgent.stream_events import (
    AgentLoopResult,
    RunFinishedEvent,
    TaskCreatedEvent,
    TaskUpdatedEvent,
    TextDeltaEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)
from hexgent.task import Task, TaskStep


agent_engine_module = import_module("hexgent.agent_engine")


class AgentEngineStreamTest(unittest.TestCase):
    """验证终端不会重复打印已经流式输出的最终回答。"""

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
            patch("builtins.input", side_effect=["你好", "q"]),
            patch.object(agent_engine_module, "agent_loop_stream", return_value=events),
            patch("sys.stdout", output),
        ):
            agent_engine_module.main()

        rendered = output.getvalue()
        self.assertEqual(rendered.count("你好，世界"), 1)
        self.assertIn("本次用时：1.2 秒", rendered)

    def test_tool_details_and_task_plan_are_rendered_without_tool_time(self) -> None:
        """终端应展示工具摘要和计划，但不展示单工具用时。"""
        task = Task(
            "检查项目",
            "in_progress",
            [
                TaskStep("读取代码", "正在读取代码", "in_progress"),
                TaskStep("总结结果", "正在总结结果", "pending"),
            ],
        )
        events = iter(
            [
                ToolStartedEvent("create_task", "c1", "create_task(检查项目)"),
                ToolFinishedEvent("create_task", "c1", "success"),
                TaskCreatedEvent(task),
                TaskUpdatedEvent(
                    Task(
                        "检查项目",
                        "in_progress",
                        [
                            TaskStep("读取代码", "正在读取代码", "completed"),
                            TaskStep("总结结果", "正在总结结果", "in_progress"),
                        ],
                    )
                ),
                RunFinishedEvent(AgentLoopResult("", 0.5)),
            ]
        )
        output = StringIO()
        with (
            patch("builtins.input", side_effect=["检查", "q"]),
            patch.object(agent_engine_module, "agent_loop_stream", return_value=events),
            patch("sys.stdout", output),
        ):
            agent_engine_module.main()

        rendered = output.getvalue()
        self.assertIn("开始处理工具：create_task(检查项目)", rendered)
        self.assertIn("任务计划：检查项目", rendered)
        self.assertIn("1. [进行中] 读取代码", rendered)
        self.assertIn("任务进度：检查项目", rendered)
        self.assertIn("1. [已完成] 读取代码", rendered)
        self.assertIn("2. [进行中] 总结结果", rendered)
        self.assertNotIn("工具结束：create_task，状态：成功，用时", rendered)

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
            patch("builtins.input", side_effect=["开始任务", "继续", "q"]),
            patch.object(
                agent_engine_module,
                "agent_loop_stream",
                side_effect=[first_events, second_events],
            ) as stream,
            patch("sys.stdout", output),
        ):
            agent_engine_module.main()

        self.assertEqual(stream.call_count, 2)
        self.assertIn("第一轮收尾", output.getvalue())
        self.assertIn("第二轮继续", output.getvalue())

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
            patch("builtins.input", side_effect=["", "有效输入", "q"]),
            patch.object(
                agent_engine_module,
                "agent_loop_stream",
                return_value=events,
            ) as stream,
            patch("sys.stdout", output),
        ):
            agent_engine_module.main()

        self.assertEqual(stream.call_count, 1)
        self.assertIn("继续运行", output.getvalue())


if __name__ == "__main__":
    unittest.main()
