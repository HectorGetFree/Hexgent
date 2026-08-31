"""Plan 退出判断的领域边界测试。"""

import unittest

from hexgent.plan import Plan, PlanStep, review_plan_exit


class PlanExitTest(unittest.TestCase):
    """验证 Plan 只返回领域判断，不构造 Agent Loop 状态迁移。"""

    def test_missing_and_completed_plan_can_finish(self) -> None:
        """无 Plan 可直接结束，已完成 Plan 还应通知调用方清理。"""
        missing = review_plan_exit(None, 0, 1)
        completed = review_plan_exit(
            Plan(
                "完成目标",
                "completed",
                [PlanStep("执行工作", "正在执行工作", "completed", "step-1")],
            ),
            0,
            1,
        )

        self.assertEqual(missing.action, "complete")
        self.assertFalse(missing.clear_completed_plan)
        self.assertEqual(completed.action, "complete")
        self.assertTrue(completed.clear_completed_plan)

    def test_active_plan_returns_continue_then_incomplete(self) -> None:
        """活动 Plan 在提醒额度内继续，额度耗尽后形成未完成判断。"""
        plan = Plan(
            "完成目标",
            "in_progress",
            [PlanStep("执行工作", "正在执行工作", "in_progress", "step-1")],
        )

        continuation = review_plan_exit(plan, 0, 1)
        exhausted = review_plan_exit(plan, 1, 1)

        self.assertEqual(continuation.action, "continue")
        self.assertIsNotNone(continuation.continuation_message)
        self.assertEqual(exhausted.action, "incomplete")

    def test_blocked_plan_returns_blocked_decision(self) -> None:
        """阻塞 Plan 应保留给调用方形成可恢复终态。"""
        plan = Plan(
            "完成目标",
            "blocked",
            [PlanStep("等待输入", "正在等待输入", "pending", "step-1")],
            blocked_reason="缺少用户输入",
        )

        self.assertEqual(review_plan_exit(plan, 0, 1).action, "blocked")


if __name__ == "__main__":
    unittest.main()
