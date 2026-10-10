"""Phase5A paired deterministic benchmark 报告。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .observation import VariantObservation


@dataclass(frozen=True)
class PairedScenarioResult:
    """同一任务、同一初始 Session 下两个 Variant 的对照结果。"""

    scenario: str
    baseline: VariantObservation
    enabled: VariantObservation
    paired_delta: dict[str, Any]
    interpretation: str


@dataclass
class Phase5AReport:
    """只承载 deterministic mechanism evidence 的 Phase5A 报告。"""

    results: list[PairedScenarioResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(
            all(result.baseline.correctness_guardrails.values())
            and all(result.enabled.correctness_guardrails.values())
            and result.baseline.success
            and result.enabled.success
            for result in self.results
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": "Phase5A Deterministic Runtime Efficiency Benchmark",
            "evidence_type": "DETERMINISTIC_MECHANISM_EVIDENCE",
            "scenario_count": len(self.results),
            "passed": self.passed,
            "results": [asdict(result) for result in self.results],
        }

    def render_text(self) -> str:
        lines = [
            "Phase5A Deterministic Runtime Efficiency Benchmark",
            f"Scenarios: total={len(self.results)}, guardrail_passed={self.passed}",
        ]
        for result in self.results:
            delta = result.paired_delta
            lines.append(
                f"  {result.scenario}: "
                f"tokens {result.baseline.estimated_input_tokens_total} → "
                f"{result.enabled.estimated_input_tokens_total}; "
                f"delta={delta.get('estimated_input_tokens_delta')}"
            )
        return "\n".join(lines)
