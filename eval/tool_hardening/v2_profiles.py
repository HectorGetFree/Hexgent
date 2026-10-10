"""为已有 V2 计时结果补充独立非计时探针，不重跑或覆盖计时样本。"""

import argparse
import json
from pathlib import Path

from .v2_runner import ROOT, digest, execute_task, validate_sources


def main():
    """绑定原报告与 Manifest 哈希，单独验证真实 Handler 并发度。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    source = args.source_run / "report.json"
    original = json.loads(source.read_text())
    manifest_path = Path(original["manifest_path"])
    assert digest(manifest_path) == original["manifest_sha256"]
    manifest = json.loads(manifest_path.read_text())
    protected = original["production_sha256"]
    validate_sources(manifest, protected)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {"source_report": str(source), "source_report_sha256": digest(source), "manifest_sha256": digest(manifest_path), "timing_samples_reexecuted": False, "tasks": []}
    for task in manifest["tasks"]:
        result = {"task_name": task["task_name"]}
        for v in ("serial", "parallel"):
            result[v] = execute_task(task, args.output_dir / "sessions" / task["task_name"] / v, v == "parallel", profile=True)["profiles"]
            validate_sources(manifest, protected)
        report["tasks"].append(result)
    assert digest(source) == report["source_report_sha256"]
    (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report["tasks"], ensure_ascii=False))


if __name__ == "__main__":
    main()
