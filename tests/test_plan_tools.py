"""Plan 工具的状态语义测试。"""

import json
from pathlib import Path
import unittest

from hexgent.plan import InMemoryPlanStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_call


def make_step(
    content: str,
    status: str,
    step_id: str | None = None,
) -> dict:
    """创建一个用于测试的 Plan 步骤参数。"""
    step = {
        "content": content,
        "process_message": f"正在{content}",
        "status": status,
    }
    if step_id is not None:
        step["step_id"] = step_id
    return step


def update_arguments(
    status: str,
    steps: list[dict],
    *,
    revision: int = 1,
    reason: str | None = None,
    blocked_reason: str | None = None,
) -> dict:
    """创建完整的 Plan 重规划参数。"""
    return {
        "expected_revision": revision,
        "status": status,
        "reason": reason,
        "blocked_reason": blocked_reason,
        "steps": steps,
    }


class PlanToolsTest(unittest.TestCase):
    """验证 Plan 目标稳定性和状态不变量。"""

    def setUp(self) -> None:
        """为每个用例创建独立的内存 PlanStore。"""
        self.context = ToolExecutionContext(InMemoryPlanStore(), Path.cwd().resolve())

    def execute(self, tool_name: str, arguments: dict):
        """通过完整工具执行管线调用 Plan 工具。"""
        return execute_tool_call(
            tool_name,
            json.dumps(arguments, ensure_ascii=False),
            context=self.context,
        )

    def test_plan_objective_remains_stable_when_plan_changes(self) -> None:
        """动态调整步骤时不得改写 Plan 的整体目标。"""
        created = self.execute(
            "create_plan",
            {
                "objective": "完成工具系统检查",
                "steps": [
                    make_step("检查定义", "in_progress"),
                    make_step("生成报告", "pending"),
                ],
            },
        )
        updated = self.execute(
            "update_plan",
            update_arguments(
                "in_progress",
                [
                    make_step("检查定义", "completed", "step-1"),
                    {
                        **make_step("补充测试", "in_progress"),
                        "step_id": None,
                    },
                ],
                reason="发现缺少边界测试，调整后续计划",
            ),
        )

        self.assertEqual(created.status, "success")
        self.assertEqual(updated.status, "success")
        plan = self.context.plan_store.get_plan()
        self.assertIsNotNone(plan)
        self.assertEqual(plan.objective, "完成工具系统检查")
        self.assertEqual(plan.steps[1].process_message, "正在补充测试")
        self.assertEqual(plan.revision, 2)
        self.assertEqual(plan.last_replan_reason, "发现缺少边界测试，调整后续计划")
        self.assertEqual(plan.steps[0].step_id, "step-1")
        self.assertEqual(plan.steps[1].step_id, "step-r2-1")

    def test_completed_plan_requires_all_steps_completed(self) -> None:
        """仍有未完成步骤时不得把整体 Plan 标记为完成。"""
        self.execute(
            "create_plan",
            {
                "objective": "完成检查",
                "steps": [make_step("执行检查", "in_progress")],
            },
        )
        result = self.execute(
            "update_plan",
            update_arguments(
                "completed",
                [make_step("执行检查", "pending", "step-1")],
            ),
        )

        self.assertEqual(result.status, "invalid")
        self.assertIn("所有步骤都必须已完成", result.output)

    def test_blocked_plan_cannot_have_in_progress_step(self) -> None:
        """阻塞 Plan 中不能保留仍在执行的步骤。"""
        self.execute(
            "create_plan",
            {
                "objective": "读取待提供的文件",
                "steps": [make_step("读取文件", "in_progress")],
            },
        )
        result = self.execute(
            "update_plan",
            update_arguments(
                "blocked",
                [make_step("读取文件", "in_progress", "step-1")],
                blocked_reason="等待用户提供文件",
            ),
        )

        self.assertEqual(result.status, "invalid")
        self.assertIn("不能包含进行中的步骤", result.output)

    def test_completed_step_cannot_be_removed_or_reopened(self) -> None:
        """已经完成的执行事实不能在后续重规划中消失或回退。"""
        self.execute(
            "create_plan",
            {
                "objective": "完成两阶段检查",
                "steps": [
                    make_step("读取代码", "in_progress"),
                    make_step("总结结果", "pending"),
                ],
            },
        )
        first_update = self.execute(
            "update_plan",
            update_arguments(
                "in_progress",
                [
                    make_step("读取代码", "completed", "step-1"),
                    make_step("总结结果", "in_progress", "step-2"),
                ],
            ),
        )
        removed = self.execute(
            "update_plan",
            update_arguments(
                "in_progress",
                [make_step("总结结果", "in_progress", "step-2")],
                revision=2,
            ),
        )
        reopened = self.execute(
            "update_plan",
            update_arguments(
                "in_progress",
                [
                    make_step("读取代码", "pending", "step-1"),
                    make_step("总结结果", "in_progress", "step-2"),
                ],
                revision=2,
            ),
        )

        self.assertEqual(first_update.status, "success")
        plan_after_progress = self.context.plan_store.get_plan()
        self.assertIsNotNone(plan_after_progress)
        self.assertIsNone(plan_after_progress.last_replan_reason)
        self.assertEqual(removed.status, "invalid")
        self.assertIn("不能删除已完成步骤", removed.output)
        self.assertEqual(reopened.status, "invalid")
        self.assertIn("不能回退", reopened.output)

    def test_stale_revision_and_missing_blocked_reason_are_rejected(self) -> None:
        """过期计划不能覆盖新计划，阻塞状态必须解释原因。"""
        self.execute(
            "create_plan",
            {
                "objective": "等待外部输入",
                "steps": [make_step("读取输入", "in_progress")],
            },
        )
        stale = self.execute(
            "update_plan",
            update_arguments(
                "in_progress",
                [make_step("读取输入", "in_progress", "step-1")],
                revision=0,
            ),
        )
        missing_reason = self.execute(
            "update_plan",
            update_arguments(
                "blocked",
                [make_step("读取输入", "pending", "step-1")],
            ),
        )

        self.assertEqual(stale.status, "invalid")
        self.assertIn("版本冲突", stale.output)
        self.assertEqual(missing_reason.status, "invalid")
        self.assertIn("必须提供 blocked_reason", missing_reason.output)

    def test_replanning_requires_reason_but_progress_update_does_not_show_it(self) -> None:
        """只有步骤结构变化时才保存并展示重规划原因。"""
        self.execute(
            "create_plan",
            {
                "objective": "检查并总结",
                "steps": [
                    make_step("检查", "in_progress"),
                    make_step("总结", "pending"),
                ],
            },
        )
        progress = self.execute(
            "update_plan",
            update_arguments(
                "in_progress",
                [
                    make_step("检查", "completed", "step-1"),
                    make_step("总结", "in_progress", "step-2"),
                ],
                reason="这只是正常进度，不应保存为调整原因",
            ),
        )
        missing_replan_reason = self.execute(
            "update_plan",
            update_arguments(
                "in_progress",
                [
                    make_step("检查", "completed", "step-1"),
                    make_step("总结", "in_progress", "step-2"),
                    {**make_step("补充验证", "pending"), "step_id": None},
                ],
                revision=2,
            ),
        )

        self.assertEqual(progress.status, "success")
        plan = self.context.plan_store.get_plan()
        self.assertIsNotNone(plan)
        self.assertIsNone(plan.last_replan_reason)
        self.assertEqual(missing_replan_reason.status, "invalid")
        self.assertIn("必须提供重规划 reason", missing_replan_reason.output)


if __name__ == "__main__":
    unittest.main()
