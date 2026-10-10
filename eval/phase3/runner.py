"""Phase3 Recovery / Crash Consistency Eval Runner。"""

from __future__ import annotations

from .report import RecoveryEvalReport, RecoveryScenarioReport
from .scenarios import RecoveryScenario, v1_scenarios


def run_scenarios(
    scenarios: tuple[RecoveryScenario, ...] | None = None,
) -> RecoveryEvalReport:
    """运行 crash → restore 场景；已知语义缺口显式报告而非伪装 PASS。"""
    report = RecoveryEvalReport()
    for scenario in scenarios or v1_scenarios():
        try:
            observation = scenario.execute()
            scenario.verify(observation)
            outcome = scenario.expected_outcome
            detail = (
                "当前 Runtime 已 durable tool_execution_finished，但 SessionRestorer 不补 "
                "function_call_output；该调用会在恢复 Context 中丢失。"
                if outcome == "RECOVERY_GAP"
                else None
            )
            report.scenario_reports.append(
                RecoveryScenarioReport(
                    scenario.name,
                    scenario.crash_point,
                    scenario.invariant,
                    outcome,
                    observation.pre_crash_types,
                    "committed" if observation.external_side_effect_exists else "not_started",
                    (
                        f"first_restore_outputs={len(observation.restore_outputs)}, "
                        f"second_restore_outputs={len(observation.repeated_restore_outputs)}"
                    ),
                    observation.recovery_actions,
                    observation.final_types,
                    detail,
                )
            )
        except Exception as error:
            report.scenario_reports.append(
                RecoveryScenarioReport(
                    scenario.name,
                    scenario.crash_point,
                    scenario.invariant,
                    "FAIL",
                    (),
                    "unknown",
                    "restore 未完成",
                    (),
                    (),
                    str(error),
                )
            )
    return report


if __name__ == "__main__":
    print(run_scenarios().render_text())
