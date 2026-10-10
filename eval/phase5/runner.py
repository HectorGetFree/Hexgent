"""Phase5A Deterministic Runtime Efficiency Benchmark Runner。"""

from __future__ import annotations

from .report import Phase5AReport
from .scenarios import v1_scenarios


def run_benchmark() -> Phase5AReport:
    """运行四个 paired mechanism benchmark；失败保留为显式异常供测试定位。"""

    return Phase5AReport([scenario() for scenario in v1_scenarios()])


if __name__ == "__main__":
    print(run_benchmark().render_text())
