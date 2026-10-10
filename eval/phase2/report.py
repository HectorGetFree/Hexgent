"""Phase2 Tool / Permission Contract Eval 报告。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ScenarioReport:
    """单个 Tool / Permission 场景的执行结果。"""

    name: str
    status: str
    violations: tuple[dict[str, str], ...] = ()
    observed_terminal_status: str | None = None
    observed_call_ids: tuple[str, ...] = ()


@dataclass
class ToolPermissionEvalReport:
    """Phase2 场景与可观察安全指标的稳定报告。"""

    scenario_reports: list[ScenarioReport] = field(default_factory=list)
    observed_events: set[str] = field(default_factory=set)
    observed_tool_statuses: set[str] = field(default_factory=set)
    permission_resolutions: set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        return len(self.scenario_reports)

    @property
    def passed(self) -> int:
        return sum(item.status == "passed" for item in self.scenario_reports)

    @property
    def failed(self) -> int:
        return sum(item.status == "failed" for item in self.scenario_reports)

    def to_dict(self) -> dict[str, Any]:
        """转换为可供自动化读取的稳定 JSON 表示。"""
        return {
            "title": "Tool / Permission Contract Eval",
            "scenarios": {
                "total": self.total,
                "passed": self.passed,
                "failed": self.failed,
            },
            "contract": {
                "tool_event_pairing": "checked",
                "durable_tool_pairing": "checked",
                "permission_before_dispatch": "checked",
                "deny_without_dispatch": "checked",
                "function_output_pairing": "checked",
                "duplicate_dispatch": "checked",
            },
            "scenario_reports": [asdict(report) for report in self.scenario_reports],
            "coverage": {
                "observed_events": sorted(self.observed_events),
                "observed_tool_statuses": sorted(self.observed_tool_statuses),
                "permission_resolutions": sorted(self.permission_resolutions),
            },
        }

    def render_text(self) -> str:
        """生成简明的命令行报告。"""
        return "\n".join(
            (
                "Tool / Permission Contract Eval",
                f"Scenarios: total={self.total}, passed={self.passed}, failed={self.failed}",
                "Observed:",
                f"  events={', '.join(sorted(self.observed_events)) or 'none'}",
                f"  tool_statuses={', '.join(sorted(self.observed_tool_statuses)) or 'none'}",
                f"  permission_resolutions={', '.join(sorted(self.permission_resolutions)) or 'none'}",
            )
        )
