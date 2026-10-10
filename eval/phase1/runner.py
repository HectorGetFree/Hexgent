"""Runtime Contract Eval Runner。"""

from __future__ import annotations

from .oracle import (
    check_happens_before,
    check_no_duplicate_terminal,
    check_required_events,
    check_terminal,
)
from .report import EvalReport, ScenarioReport
from .scenarios import RuntimeScenario, v1_scenarios


def run_scenarios(
    scenarios: tuple[RuntimeScenario, ...] | None = None,
) -> EvalReport:
    """执行场景并汇总 Contract 违规和 semantic coverage。"""
    report = EvalReport()
    for scenario in scenarios or v1_scenarios():
        try:
            observation = scenario.execute()
            violations = [
                *check_required_events(observation, scenario.required_events),
                *check_happens_before(observation, scenario.ordering),
            ]
            if scenario.expected_status is not None:
                violations.extend(check_terminal(observation, scenario.expected_status))
                violations.extend(check_no_duplicate_terminal(observation))
            status = "passed" if not violations else "failed"
            report.scenario_reports.append(
                ScenarioReport(
                    scenario.name,
                    status,
                    tuple({"category": item.category, "message": item.message} for item in violations),
                    observation.terminal_status,
                    observation.event_types,
                )
            )
            report.coverage.events.update(observation.event_types)
            if observation.terminal_status is not None:
                report.coverage.terminal_outcomes.add(observation.terminal_status)
                report.coverage.states.add(observation.terminal_status)
            report.coverage.transition_reasons.update(
                event.reason
                for event in observation.realtime_events
                if event.__class__.__name__ == "LoopTransitionEvent"
            )
        except NotImplementedError as error:
            report.scenario_reports.append(
                ScenarioReport(scenario.name, "not_yet_testable", (("support", str(error)),))
            )
        except Exception as error:
            report.scenario_reports.append(
                ScenarioReport(scenario.name, "failed", (("runner", str(error)),))
            )
    return report


if __name__ == "__main__":
    print(run_scenarios().render_text())
