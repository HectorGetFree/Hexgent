"""Phase2 Tool / Permission Contract Eval Runner。"""

from __future__ import annotations

from eval.phase1.oracle import ContractViolation, check_terminal

from .oracle import check_all_contracts
from .report import ScenarioReport, ToolPermissionEvalReport
from .scenarios import ToolPermissionScenario, v1_scenarios


def _scenario_verify(scenario: ToolPermissionScenario, observation: object) -> tuple[ContractViolation, ...]:
    """将场景级 Python 断言转成统一的可报告违规。"""
    try:
        scenario.verify(observation)  # type: ignore[arg-type]
    except AssertionError as error:
        return (ContractViolation("scenario", str(error) or "场景断言失败"),)
    return ()


def run_scenarios(
    scenarios: tuple[ToolPermissionScenario, ...] | None = None,
) -> ToolPermissionEvalReport:
    """执行确定性 Tool / Permission 场景并汇总契约结果。"""
    report = ToolPermissionEvalReport()
    for scenario in scenarios or v1_scenarios():
        try:
            observation = scenario.execute()
            violations = [
                *check_terminal(observation.runtime, scenario.expected_status),
                *check_all_contracts(observation),
                *_scenario_verify(scenario, observation),
            ]
            report.scenario_reports.append(
                ScenarioReport(
                    scenario.name,
                    "passed" if not violations else "failed",
                    tuple(
                        {"category": violation.category, "message": violation.message}
                        for violation in violations
                    ),
                    observation.terminal_status,
                    observation.observed_call_ids,
                )
            )
            report.observed_events.update(observation.event_types)
            report.observed_tool_statuses.update(
                event.status for event in observation.tool_finished_events
            )
            report.permission_resolutions.update(
                str((record.get("payload") or {}).get("resolution"))
                for record in observation.records("permission_resolved")
            )
        except Exception as error:
            report.scenario_reports.append(
                ScenarioReport(
                    scenario.name,
                    "failed",
                    ({"category": "runner", "message": str(error)},),
                )
            )
    return report


if __name__ == "__main__":
    print(run_scenarios().render_text())
