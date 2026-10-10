"""Phase4 Context / Memory Correctness and Efficiency Eval Runner。"""

from __future__ import annotations

from .report import ContextMemoryEvalReport, ContextMemoryScenarioReport
from .scenarios import ContextMemoryScenario, v1_scenarios


def run_scenarios(
    scenarios: tuple[ContextMemoryScenario, ...] | None = None,
) -> ContextMemoryEvalReport:
    """运行确定性 Context / Memory 场景，不以实现存在替代可观察证据。"""

    report = ContextMemoryEvalReport()
    for scenario in scenarios or v1_scenarios():
        try:
            observation = scenario.execute()
            scenario.verify(observation)
            unavailable = scenario.name == "workspace_memory_session_isolation"
            report.scenario_reports.append(ContextMemoryScenarioReport(
                scenario.name,
                scenario.invariant,
                "UNAVAILABLE" if unavailable else "PASS",
                observation.facts,
                (
                    "这不是 Session 隔离能力：当前长期 Memory 以工作区为共享边界。"
                    if unavailable else None
                ),
            ))
        except Exception as error:
            report.scenario_reports.append(ContextMemoryScenarioReport(
                scenario.name, scenario.invariant, "FAIL", {}, str(error),
            ))
    return report


if __name__ == "__main__":
    print(run_scenarios().render_text())
