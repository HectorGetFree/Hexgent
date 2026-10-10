"""真实工具证据与两个安全边界的 Eval 回归。"""

import unittest

from .runner import TASKS, batch, calls_for, late_result, loop_check, mixed_barrier


class ToolHardeningTests(unittest.TestCase):
    def test_real_tools_equivalence_and_loop_delivery(self):
        """真实源码工具返回值必须等价并交付下一轮模型请求。"""
        for name, paths in TASKS.items():
            with self.subTest(task=name):
                calls = calls_for(paths)
                _, serial, _ = batch(calls, False)
                _, parallel, observed = batch(calls, True, profile=True)
                self.assertEqual(serial, parallel)
                self.assertEqual(observed["handler_entries"], len(calls))
                self.assertLessEqual(observed["peak_concurrency"], 4)
                self.assertTrue(loop_check(calls, serial)["pass"])

    def test_mixed_barrier(self):
        """写入屏障前后读取观察正确。"""
        self.assertTrue(mixed_barrier()["pass"])

    def test_late_result(self):
        """线程真实返回后不得改写 durable 结果或再次注入。"""
        result = late_result()
        self.assertTrue(result["pass"], result["checks"])


if __name__ == "__main__":
    unittest.main()
