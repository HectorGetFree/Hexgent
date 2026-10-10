"""Runtime Contract Eval 的结构化报告。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ScenarioReport:
    """单个 Scenario 的执行结果。"""

    name: str
    status: str
    violations: tuple[dict[str, str], ...] = ()
    observed_status: str | None = None
    event_types: tuple[str, ...] = ()


@dataclass
class SemanticCoverage:
    """基于实际 Observation 汇总的语义覆盖，而非代码行覆盖。

    ``transition_reasons`` 是 LoopTransitionEvent.reason 的观测集合，不是
    source_state -> target_state。当前 Runtime 没有稳定暴露后者，因此明确标记为不可用。
    """

    states: set[str] = field(default_factory=set)
    transition_reasons: set[str] = field(default_factory=set)
    events: set[str] = field(default_factory=set)
    terminal_outcomes: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "observed_states": sorted(self.states),
            "observed_events": sorted(self.events),
            "observed_terminal_outcomes": sorted(self.terminal_outcomes),
            "transition_reasons": sorted(self.transition_reasons),
            "state_transitions": {
                "availability": "unavailable",
                "observed": [],
                "denominator": None,
                "reason": "Runtime 当前未稳定暴露 source_state 与 target_state",
            },
        }


@dataclass
class EvalReport:
    """完整 Runtime Contract Eval 报告。"""

    scenario_reports: list[ScenarioReport] = field(default_factory=list)
    coverage: SemanticCoverage = field(default_factory=SemanticCoverage)

    @property
    def total(self) -> int:
        return len(self.scenario_reports)

    @property
    def passed(self) -> int:
        return sum(item.status == "passed" for item in self.scenario_reports)

    @property
    def failed(self) -> int:
        return sum(item.status == "failed" for item in self.scenario_reports)

    @property
    def not_yet_testable(self) -> int:
        return sum(item.status == "not_yet_testable" for item in self.scenario_reports)

    def to_dict(self) -> dict[str, Any]:
        """转换为稳定 JSON 结构。"""
        return {
            "title": "Runtime Contract Eval",
            "scenarios": {
                "total": self.total,
                "passed": self.passed,
                "failed": self.failed,
                "not_yet_testable": self.not_yet_testable,
            },
            "contract": {
                "required_event_contract": "checked",
                "ordering_contract": "checked",
                "terminal_contract": "checked",
                "invariants": "checked where observable",
                "violations": [
                    violation
                    for report in self.scenario_reports
                    for violation in report.violations
                ],
            },
            "scenario_reports": [asdict(report) for report in self.scenario_reports],
            "coverage": self.coverage.to_dict(),
        }

    def render_text(self) -> str:
        """生成适合命令行查看的简短报告。"""
        lines = [
            "Runtime Contract Eval",
            f"Scenarios: total={self.total}, passed={self.passed}, "
            f"failed={self.failed}, not_yet_testable={self.not_yet_testable}",
            "Observed:",
            f"  states={', '.join(sorted(self.coverage.states)) or 'none'}",
            f"  transition_reasons={', '.join(sorted(self.coverage.transition_reasons)) or 'none'}",
            "  state_transitions=unavailable (未稳定暴露 source_state -> target_state)",
            f"  events={', '.join(sorted(self.coverage.events)) or 'none'}",
            f"  terminal_outcomes={', '.join(sorted(self.coverage.terminal_outcomes)) or 'none'}",
        ]
        return "\n".join(lines)
