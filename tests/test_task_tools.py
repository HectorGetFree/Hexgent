"""Task 工具的状态语义测试。"""

import json
from pathlib import Path
import unittest

from hexgent.task import InMemoryTaskStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_call


def make_step(content: str, status: str) -> dict[str, str]:
    """创建一个用于测试的 Task 步骤参数。"""
    return {
        "content": content,
        "process_message": f"正在{content}",
        "status": status,
    }


class TaskToolsTest(unittest.TestCase):
    """验证 Task 目标稳定性和状态不变量。"""

    def setUp(self) -> None:
        """为每个用例创建独立的内存 Task Store。"""
        self.context = ToolExecutionContext(InMemoryTaskStore(), Path.cwd().resolve())

    def execute(self, tool_name: str, arguments: dict):
        """通过完整工具执行管线调用 Task 工具。"""
        return execute_tool_call(
            tool_name,
            json.dumps(arguments, ensure_ascii=False),
            context=self.context,
        )

    def test_task_objective_remains_stable_when_plan_changes(self) -> None:
        """动态调整步骤时不得改写 Task 的整体目标。"""
        created = self.execute(
            "create_task",
            {
                "objective": "完成工具系统检查",
                "steps": [
                    make_step("检查定义", "in_progress"),
                    make_step("生成报告", "pending"),
                ],
            },
        )
        updated = self.execute(
            "update_task_plan",
            {
                "status": "in_progress",
                "steps": [
                    make_step("检查定义", "completed"),
                    make_step("补充测试", "in_progress"),
                ],
            },
        )

        self.assertEqual(created.status, "success")
        self.assertEqual(updated.status, "success")
        task = self.context.task_store.get_task()
        self.assertIsNotNone(task)
        self.assertEqual(task.objective, "完成工具系统检查")
        self.assertEqual(task.steps[1].process_message, "正在补充测试")

    def test_completed_task_requires_all_steps_completed(self) -> None:
        """仍有未完成步骤时不得把整体 Task 标记为完成。"""
        self.execute(
            "create_task",
            {
                "objective": "完成检查",
                "steps": [make_step("执行检查", "in_progress")],
            },
        )
        result = self.execute(
            "update_task_plan",
            {
                "status": "completed",
                "steps": [make_step("执行检查", "pending")],
            },
        )

        self.assertEqual(result.status, "invalid")
        self.assertIn("所有步骤都必须已完成", result.output)

    def test_blocked_task_cannot_have_in_progress_step(self) -> None:
        """阻塞 Task 中不能保留仍在执行的步骤。"""
        self.execute(
            "create_task",
            {
                "objective": "读取待提供的文件",
                "steps": [make_step("读取文件", "in_progress")],
            },
        )
        result = self.execute(
            "update_task_plan",
            {
                "status": "blocked",
                "steps": [make_step("读取文件", "in_progress")],
            },
        )

        self.assertEqual(result.status, "invalid")
        self.assertIn("不能包含进行中的步骤", result.output)


if __name__ == "__main__":
    unittest.main()
