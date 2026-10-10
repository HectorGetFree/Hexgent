"""Phase5B 真实模型 Benchmark 的显式命令入口。"""

from __future__ import annotations

import argparse
from pathlib import Path

from .real_model import default_output_dir, run_phase5b, write_report


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 Phase5B 真实模型 paired benchmark")
    parser.add_argument("--runs", type=int, default=5, help="每个 variant 的运行次数，最少为 5")
    parser.add_argument("--output-dir", type=Path, default=default_output_dir())
    args = parser.parse_args()
    print(f"starting Phase5B: runs_per_variant={args.runs}", flush=True)
    report = run_phase5b(runs=args.runs)
    path = write_report(report, args.output_dir)
    for scenario in report["scenarios"]:
        print(
            f"{scenario['scenario']}: "
            f"success {scenario['baseline']['success']['count']} → "
            f"{scenario['enabled']['success']['count']}"
        )
    print(f"report={path}")


if __name__ == "__main__":
    main()
