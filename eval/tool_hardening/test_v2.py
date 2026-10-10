"""V2 固定计划和任务级统计口径回归。"""

import json
from pathlib import Path
import unittest

from .v2_runner import summarize, stats


class V2ContractTests(unittest.TestCase):
    def test_frozen_manifest_size_and_no_duplicate_reads(self):
        """只读取人工审核计划，不重新构建。"""
        manifest = json.loads(Path(__file__).with_name("V2_SOURCE_MANIFEST.json").read_text())
        self.assertEqual([t["total_tool_calls"] for t in manifest["tasks"]], [26, 25, 31])
        for task in manifest["tasks"]:
            self.assertEqual(len(task["batches"]), 4)
            self.assertEqual(task["duplicate_reads"], 0)
            self.assertEqual(sum(task["calls_per_batch"]), task["total_tool_calls"])

    def test_task_speedup_not_batch_average(self):
        """四批加速完全相同时仍以真实完整任务时间计算。"""
        batches = [{"stage": str(i), "call_count": 1, "latency_ms": 1} for i in range(4)]
        rows = [{"serial": {"total_latency_ms": 100, "batches": batches}, "parallel": {"total_latency_ms": 200, "batches": batches}}] * 20
        result = summarize(rows)
        self.assertEqual(result["median_paired_speedup"], .5)
        self.assertEqual(result["mean_paired_reduction_pct"], -100)
        self.assertEqual(result["batches"][0]["median_paired_speedup"], 1)

    def test_p95_nearest_rank_preserves_slow_samples(self):
        """二十次测量取第十九个有序值，不丢弃长尾。"""
        values = list(range(1, 20)) + [1000]
        self.assertEqual(stats(values)["p95_ms"], 19)
        self.assertEqual(max(values), 1000)


if __name__ == "__main__":
    unittest.main()
