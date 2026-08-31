"""Run 业务控制对象和 Steering FIFO 测试。"""

import unittest

from hexgent.loop.run_control import RunControl, SteeringMailbox


class RunControlTest(unittest.TestCase):
    """验证 Interrupt 与 Pause、Cancel、Steer 的职责边界。"""

    def test_steering_mailbox_preserves_fifo_and_original_content(self) -> None:
        """Steering Query 必须保持提交顺序和原始文本。"""
        mailbox = SteeringMailbox()

        first = mailbox.submit("第一条\n保持原文")
        second = mailbox.submit("第二条")

        self.assertEqual(first.sequence, 1)
        self.assertEqual(second.sequence, 2)
        self.assertEqual(
            [query.content for query in mailbox.drain()],
            ["第一条\n保持原文", "第二条"],
        )
        self.assertFalse(mailbox.has_pending)

    def test_steer_does_not_interrupt_current_phase(self) -> None:
        """第一版 Steer 只等待 Turn 边界，不应中断模型流或工具。"""
        control = RunControl()

        control.steer("调整方向")

        self.assertTrue(control.steering_mailbox.has_pending)
        self.assertFalse(control.interrupt_controller.interrupted)
        self.assertIsNone(control.terminal_action)

    def test_cancel_overrides_pause(self) -> None:
        """同一 Run 中 Cancel 的终止优先级应高于先到达的 Pause。"""
        control = RunControl()

        control.pause()
        control.cancel()

        self.assertEqual(control.terminal_action, "cancel")
        self.assertTrue(control.interrupt_controller.interrupted)


if __name__ == "__main__":
    unittest.main()
