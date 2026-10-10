"""Phase3 Recovery / Crash Consistency Eval 报告。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class RecoveryScenarioReport:
    """一条 crash recovery 场景的结构化结果。"""

    name: str
    crash_point: str
    invariant: str
    outcome: str
    pre_crash_durable_state: tuple[str, ...]
    external_side_effect_state: str
    restore_observation: str
    recovery_action: tuple[str, ...]
    final_durable_state: tuple[str, ...]
    detail: str | None = None


@dataclass
class RecoveryEvalReport:
    """Phase3 recovery matrix 的可读报告。"""

    scenario_reports: list[RecoveryScenarioReport] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(item.outcome == "PASS" for item in self.scenario_reports)

    @property
    def failed(self) -> int:
        return sum(item.outcome == "FAIL" for item in self.scenario_reports)

    @property
    def recovery_gaps(self) -> int:
        return sum(item.outcome == "RECOVERY_GAP" for item in self.scenario_reports)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": "Recovery / Crash Consistency Eval",
            "scenarios": {
                "total": len(self.scenario_reports),
                "passed": self.passed,
                "failed": self.failed,
                "recovery_gaps": self.recovery_gaps,
            },
            "scenario_reports": [asdict(item) for item in self.scenario_reports],
        }

    def render_text(self) -> str:
        lines = [
            "Recovery / Crash Consistency Eval",
            f"Scenarios: total={len(self.scenario_reports)}, passed={self.passed}, "
            f"failed={self.failed}, recovery_gaps={self.recovery_gaps}",
        ]
        lines.extend(
            f"  {item.outcome}: {item.name} — {item.crash_point}"
            for item in self.scenario_reports
        )
        return "\n".join(lines)
