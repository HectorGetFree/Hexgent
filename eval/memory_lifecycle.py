"""保存 M2 确定性验收证据；不调用真实 Provider，不覆盖旧工件。"""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import unittest


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.scenarios = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.scenarios.append({"test": test.id(), "result": "PASS"})

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.scenarios.append({"test": test.id(), "result": "FAIL", "error": self._exc_info_to_string(error, test)})

    def addError(self, test, error):
        super().addError(test, error)
        self.scenarios.append({"test": test.id(), "result": "ERROR", "error": self._exc_info_to_string(error, test)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    previous = os.environ.get("M2_EVIDENCE_DIR")
    os.environ["M2_EVIDENCE_DIR"] = str(output)
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_memory_lifecycle")
        result = unittest.TextTestRunner(verbosity=2, resultclass=EvidenceResult).run(suite)
    finally:
        if previous is None:
            os.environ.pop("M2_EVIDENCE_DIR", None)
        else:
            os.environ["M2_EVIDENCE_DIR"] = previous
    root = Path(__file__).resolve().parents[1]
    report = {
        "status": "PASS" if result.wasSuccessful() else "NOT_PASS",
        "evidence_type": "DETERMINISTIC_MECHANISM_EVIDENCE",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tests_run": result.testsRun, "failures": len(result.failures),
        "errors": len(result.errors), "skipped": len(result.skipped),
        "scenarios": result.scenarios,
        "source_sha256": {str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
                          for path in sorted((root / "hexgent").rglob("*.py"))},
        "real_model_smoke": "NOT_RUN",
        "limitations": ["已发送内容及历史 Transcript/摘要中的事实不被清除",
                        "仅支持受控 API 与本地文件系统；未验证断电/网络文件系统",
                        "跨进程竞争验证不是所有调度排列的穷举证明"],
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(output / "report.json")}, ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
