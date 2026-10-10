"""Phase4 Context / Memory Eval 的结构化报告。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


Outcome = Literal["PASS", "FAIL", "UNAVAILABLE"]


@dataclass(frozen=True)
class ContextMemoryScenarioReport:
    """一条 Context / Memory 评测场景的可读结果。"""

    name: str
    invariant: str
    outcome: Outcome
    observation: dict[str, Any]
    detail: str | None = None


@dataclass
class ContextMemoryEvalReport:
    """Phase4 V1 场景矩阵及不可观测边界。"""

    scenario_reports: list[ContextMemoryScenarioReport] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(item.outcome == "PASS" for item in self.scenario_reports)

    @property
    def failed(self) -> int:
        return sum(item.outcome == "FAIL" for item in self.scenario_reports)

    @property
    def unavailable(self) -> int:
        return sum(item.outcome == "UNAVAILABLE" for item in self.scenario_reports)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": "Context / Memory Correctness and Efficiency Eval",
            "total": len(self.scenario_reports),
            "passed": self.passed,
            "failed": self.failed,
            "unavailable": self.unavailable,
            "scenarios": [asdict(item) for item in self.scenario_reports],
        }

    def render_text(self) -> str:
        lines = [
            "Context / Memory Correctness and Efficiency Eval",
            (
                f"Scenarios: total={len(self.scenario_reports)}, "
                f"passed={self.passed}, failed={self.failed}, "
                f"unavailable={self.unavailable}"
            ),
        ]
        lines.extend(
            f"  {item.outcome}: {item.name} — {item.invariant}"
            for item in self.scenario_reports
        )
        return "\n".join(lines)
