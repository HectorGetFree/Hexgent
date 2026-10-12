"""C1 Eval 修复回归，不是正式 C1 矩阵；不修改 Production 与冻结条件。"""
from pathlib import Path
import os
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from .c1_independent import Gate, hashes, save


class C1RunnerRepairTests(unittest.TestCase):
    def test_c06_faults_remain_failed_until_the_actual_fuse_boundary(self):
        before = hashes()
        with TemporaryDirectory(prefix="hexgent-c1-eval-repair-") as temp:
            root = Path(temp)
            gate = Gate(root)
            gate.directory = root / "C1-06"
            gate.directory.mkdir()
            gate.e = dict(requests=[], oracles=[], evidence_type="EVAL_RUNNER_REGRESSION",
                          provider_actual_usage="NOT_APPLICABLE")
            try:
                # 1. 完整复用冻结故障类型及真实 Engine/Compaction 路径，只验证 Runner 修复。
                gate.c06()
                self.assertTrue(all(row["pass_"] for row in gate.e["oracles"]))
                # 六类失败各两轮×两次 attempt；先失败后成功另有两次。
                self.assertEqual(len(gate.compactions()), 6 * 4 + 2)
                self.assertEqual(len(gate.mains()), 6 * 3 + 1)
                fixture = gate.e["no_savings_fixture"]
                self.assertLessEqual(fixture["summary_tokens"], fixture["summary_limit"])
                self.assertGreaterEqual(fixture["after_tokens"], fixture["before_tokens"])
                reasons = [row for row in gate.e["oracles"] if row["name"] == "no_savings拒绝原因必须为压缩收益不足"]
                self.assertEqual(len(reasons), 2)
                self.assertTrue(all(row["pass_"] for row in reasons))
                for label in ("timeout", "failed", "stream", "empty", "multiple", "no_savings"):
                    checks = {row["name"]: row["pass_"] for row in gate.e["oracles"]}
                    for suffix in ("首轮失败计数", "第二轮失败计数", "连续失败熔断", "熔断soft可继续"):
                        self.assertTrue(checks[label + suffix])
                self.assertEqual(hashes(), before)
            finally:
                target = os.environ.get("C1_RUNNER_REPAIR_EVIDENCE_DIR")
                if target:
                    import shutil
                    output = Path(target)
                    output.mkdir(parents=True, exist_ok=True)
                    save(output / "oracle.json", gate.e)
                    save(output / "production-hashes.json", dict(before=before, after=hashes()))
                    shutil.copytree(gate.directory, output / "C1-06", dirs_exist_ok=False)

    def test_no_savings_oracle_rejects_length_error_false_positive(self):
        gate = Gate(Path("unused"))
        gate.e = dict(oracles=[])
        with self.assertRaisesRegex(AssertionError, "拒绝原因必须为压缩收益不足"):
            gate.verify_no_savings_rejection([SimpleNamespace(message="摘要内容超过配置的摘要 Token 预算")], [])

    def test_no_savings_oracle_rejects_mixed_failure_causes(self):
        gate = Gate(Path("unused"))
        gate.e = dict(oracles=[])
        with self.assertRaisesRegex(AssertionError, "未被摘要长度校验抢先拒绝"):
            gate.verify_no_savings_rejection([SimpleNamespace(message="压缩收益不足"),
                SimpleNamespace(message="摘要内容超过配置的摘要 Token 预算")], [])


if __name__ == "__main__":
    unittest.main()
