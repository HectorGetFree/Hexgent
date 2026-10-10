"""Phase5C-2：一次性真实模型 representative-task smoke run。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterable

from hexgent.agent_engine import AgentEngine
from hexgent.client import get_client
from hexgent.config import HexgentConfig
from hexgent.memory.context_retrieval import ContextRetrievalLimits

from eval.phase5.real_model import ObservedProductionClient

from .artifacts import FeasibilityArtifact, write_artifact
from .fixtures import cloned_snapshot, cloned_workspace, freeze_snapshot, workspace_manifest
from .l1 import Qualification, freeze_qualified_stage_a, qualify_stage_a_record
from .observation import RuntimeObservation, project_transcript
from .oracles import OracleResult, evaluate
from .provider_preflight import classify_preflight_failure
from .specifications import FEASIBILITY_TASKS, TaskSpecification, task_by_id


EVIDENCE_TYPE = "REAL_MODEL_FEASIBILITY_SMOKE"
PermissionStrategy = Callable[..., str]
PROVIDER_FAILURE_ATTRIBUTIONS = frozenset({
    "PROVIDER_TIMEOUT",
    "PROVIDER_STREAM_INTERRUPTED",
    "PROVIDER_UPSTREAM_POLICY_BLOCK",
    "PROVIDER_RESPONSE_BINDING_ERROR",
    "PROVIDER_OVERLOADED",
    "PROVIDER_ACCOUNT_DISABLED",
    "PROVIDER_UPSTREAM_REQUEST_FAILED",
})


def always_allow(*_: object) -> str:
    """所有 smoke task 使用同一固定策略；不把 Permission 差异混入可行性判断。"""

    return "allow"


@dataclass(frozen=True)
class SmokeRun:
    task_id: str
    final_status: str | None
    oracle_state: str
    model_rounds: int
    provider_input_tokens: int | None
    provider_output_tokens: int | None
    latency_ms: float
    failure_attribution: str | None
    shortcut_flags: tuple[str, ...]
    benchmark_qualification: str
    qualification_reason: str
    artifact_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _provider_usage(client: ObservedProductionClient) -> tuple[int | None, int | None]:
    calls = client.calls
    complete = bool(calls) and all(
        item.completed and item.input_tokens is not None and item.output_tokens is not None
        for item in calls
    )
    if not complete:
        return None, None
    return (
        sum(item.input_tokens or 0 for item in calls),
        sum(item.output_tokens or 0 for item in calls),
    )


def _provider_call_diagnostics(client: ObservedProductionClient) -> list[dict[str, Any]]:
    """保留每次 Provider 调用，而不是用 all-or-nothing aggregate 掩盖部分 usage。"""

    return [call.to_dict() for call in client.calls]


def _terminal_payload(transcript: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [item["payload"] for item in transcript if item.get("type") == "agent_run_finished"]
    return rows[-1] if rows else {}


def _attribute_failure(
    terminal: dict[str, Any],
    observation: RuntimeObservation,
    oracle: OracleResult,
    exception: BaseException | None,
) -> str | None:
    if exception is not None:
        return "AGENT_RUNTIME_FAILURE"
    if observation.model_response_failures:
        failure = observation.model_response_failures[-1]
        category = classify_preflight_failure(str(failure.get("message") or ""))
        if category != "UNKNOWN":
            return category
        return "MODEL_RESPONSE_ERROR"
    if any(call.permission_outcome == "deny" or call.status == "denied" for call in observation.tool_calls):
        return "PERMISSION_FAILURE"
    if any(call.status in {"error", "invalid"} for call in observation.tool_calls):
        return "TOOL_FAILURE"
    if terminal.get("status") != "completed" or oracle.state != "ORACLE_PASS":
        return "MODEL_REASONING_FAILURE" if terminal else "AGENT_RUNTIME_FAILURE"
    return None


def _shortcut_flags(spec: TaskSpecification, observation: RuntimeObservation, oracle: OracleResult) -> tuple[str, ...]:
    reads = [call for call in observation.tool_calls if call.tool_name in {"read_file", "glob"}]
    flags: list[str] = []
    # 此处是运行后警告，不把单次模型少探索误写成 fixture leak。
    if len(reads) <= 1:
        flags.append("TASK_TOO_EASY: single_or_no_exploration_observed")
    # L1 Stage A 的合同就是只读探索；没有 workspace diff 是预期行为，
    # 不能被通用写入任务的 shortcut 检查误判为 oracle 缺口。
    if oracle.state == "ORACLE_PASS" and not oracle.changed and spec.stage_definition is None:
        flags.append("ORACLE_DESIGN_FAILURE: pass_without_workspace_change")
    # 输出文件名是任务协议，不是答案泄露；只有直接指向 fixture 输入源才是 leak。
    if any(path in spec.user_prompt for path in spec.forbidden_changes):
        flags.append("FIXTURE_INFORMATION_LEAK: prompt_names_input_source")
    return tuple(flags)


def _qualification(
    spec: TaskSpecification,
    observation: RuntimeObservation,
    oracle: OracleResult,
    attribution: str | None,
    shortcuts: tuple[str, ...],
) -> tuple[str, str]:
    if shortcuts:
        return "NOT_QUALIFIED", "; ".join(shortcuts)
    if (
        attribution == "PERMISSION_FAILURE"
        and observation.model_terminal_status == "completed"
        and oracle.state == "ORACLE_PASS"
        and all(
            call.error_category == "eval_tool_policy_denied"
            for call in observation.tool_calls
            if call.status == "denied" or call.permission_outcome == "deny"
        )
    ):
        return "QUALIFIED_FOR_FINAL_BENCHMARK", "external oracle passed; recovered Eval policy denials remain execution diagnostics"
    if attribution in {"PERMISSION_FAILURE", "MODEL_RESPONSE_ERROR", "AGENT_RUNTIME_FAILURE", *PROVIDER_FAILURE_ATTRIBUTIONS}:
        return "NOT_QUALIFIED", f"{attribution} prevents a stable task contract"
    if not observation.tool_calls:
        return "NOT_QUALIFIED", "no observable tool trajectory"
    # 单次 reasoning/oracle 失败是模型行为证据，不自动否定 task 设计。
    if oracle.state != "ORACLE_PASS":
        return "QUALIFIED_FOR_FINAL_BENCHMARK", "oracle is deterministic; single model miss retained for attribution"
    if attribution == "TOOL_FAILURE":
        return (
            "QUALIFIED_FOR_FINAL_BENCHMARK",
            "external oracle passed; intermediate tool failure is preserved for attribution",
        )
    return "QUALIFIED_FOR_FINAL_BENCHMARK", "natural multi-step task, external oracle, stable fixture and attributable transcript"


def _run_engine(
    spec: TaskSpecification,
    workspace: Path,
    output_dir: Path,
    *,
    config: HexgentConfig | None = None,
    oracle_factory: Callable[[Path, dict[str, str], dict[str, Any], list[dict[str, Any]]], OracleResult] | None = None,
) -> tuple[SmokeRun, AgentEngine, Qualification | None]:
    before = workspace_manifest(workspace)
    engine = AgentEngine.create(config or HexgentConfig(), workspace)
    client = ObservedProductionClient(get_client())
    exception: BaseException | None = None
    started = perf_counter()
    try:
        list(engine.run(spec.user_prompt, client=client, user_confirmation=always_allow))
    except BaseException as error:  # 真实 Provider/fixture 故障也必须形成 artifact。
        exception = error
    latency_ms = round((perf_counter() - started) * 1_000, 2)
    transcript = engine.session.store.read_transcript() if engine.session.store else []
    observation = project_transcript(transcript)
    terminal = _terminal_payload(transcript)
    try:
        oracle = (
            oracle_factory(workspace, before, terminal, transcript)
            if oracle_factory is not None else evaluate(spec.task_id, workspace, before)
        )
    except BaseException as error:
        oracle = OracleResult("INFRA_FAILURE", {}, (), f"{type(error).__name__}: {error}")
        exception = exception or error
    attribution = _attribute_failure(terminal, observation, oracle, exception)
    shortcuts = _shortcut_flags(spec, observation, oracle)
    qualification, reason = _qualification(spec, observation, oracle, attribution, shortcuts)
    input_tokens, output_tokens = _provider_usage(client)
    artifact = FeasibilityArtifact(
        spec.task_id,
        terminal.get("status") == "completed",
        observation,
        oracle,
        {
            "final_status": terminal.get("status"),
            "model_rounds": sum(item.get("type") == "model_response_completed" for item in transcript),
            "provider_input_tokens": input_tokens,
            "provider_output_tokens": output_tokens,
            "provider_calls": _provider_call_diagnostics(client),
            "latency_ms": latency_ms,
            "failure_attribution": attribution,
            "exception": (
                {"type": type(exception).__name__, "message": str(exception)}
                if exception is not None else None
            ),
            "benchmark_qualification": qualification,
            "qualification_reason": reason,
            "shortcut_flags": list(shortcuts),
        },
    )
    artifact_path = write_artifact(output_dir / f"{spec.task_id.lower()}-artifact.json", artifact)
    run = SmokeRun(
        spec.task_id, terminal.get("status"), oracle.state,
        sum(item.get("type") == "model_response_completed" for item in transcript),
        input_tokens, output_tokens, latency_ms, attribution, shortcuts,
        qualification, reason, str(artifact_path),
    )
    return run, engine, None


def _run_standard_task(spec: TaskSpecification, output_dir: Path) -> SmokeRun:
    with cloned_workspace(spec.fixture) as workspace:
        run, _, _ = _run_engine(spec, workspace, output_dir)
        return run


def _l1_stage_a_oracle(
    workspace: Path,
    before: dict[str, str],
    terminal: dict[str, Any],
    transcript: list[dict[str, Any]],
) -> OracleResult:
    """L1 Stage A 没有文件产物；只校验终态与 durable 事实，不伪造 qualification。"""

    return OracleResult(
        "ORACLE_PASS" if terminal.get("status") == "completed" else "ORACLE_FAIL",
        {
            "terminal_completed": terminal.get("status") == "completed",
            "durable_transcript": bool(transcript),
            "extraction_path_executed": any(
                row.get("type") in {
                    "context_record_extraction_started",
                    "context_record_extraction_committed",
                }
                for row in transcript
            ),
            "no_fixture_file_mutation": workspace_manifest(workspace) == before,
        },
        (),
    )


def _qualify_l1(engine: AgentEngine, prompt: str) -> Qualification:
    transcript = engine.session.store.read_transcript() if engine.session.store else []
    for record in engine.session.context_records.active_records:
        result = qualify_stage_a_record(
            record, transcript, stage_b_prompt=prompt,
            final_answer_markers=("handoff_ready",),
        )
        if result.qualified:
            return result
    return Qualification(False, "STAGE_A_RECORD_NOT_QUALIFIED")


def _run_l1(output_dir: Path) -> dict[str, Any]:
    spec = task_by_id("L1")
    stage_b_prompt = "依据此前的发布资料完成项目交接：更新 project-state.json 和 handoff.md。"
    with cloned_workspace(spec.fixture) as workspace:
        stage_a_spec = TaskSpecification(
            **{**asdict(spec), "user_prompt": (
                "不要修改文件。请按顺序核对项目说明、需求、风险和发布策略；"
                "每完成一项再继续下一项，最后整理供后续交接使用的可复用事实。"
            )}
        )
        stage_a, engine, _ = _run_engine(
            stage_a_spec, workspace, output_dir, oracle_factory=_l1_stage_a_oracle,
        )
        qualification = _qualify_l1(engine, stage_b_prompt)
        if not qualification.qualified:
            return {
                "stage_a": stage_a.to_dict(),
                "qualification": qualification.reason,
                "stage_b": None,
                "benchmark_qualification": "NOT_QUALIFIED",
                "reason": "L1 cannot exercise paired retrieval without a real qualified Stage A record",
            }
        snapshot = output_dir / "l1-stage-a-snapshot"
        freeze_qualified_stage_a(workspace, snapshot, qualification)
    runs: dict[str, dict[str, Any]] = {}
    for variant, enabled in (("context_record_on", True), ("context_record_off", False)):
        with cloned_snapshot(snapshot) as stage_b_workspace:
            stage_b_spec = TaskSpecification(
                **{**asdict(spec), "user_prompt": stage_b_prompt, "mechanism_variant": "context_record_pair"}
            )
            run, _, _ = _run_engine(
                stage_b_spec,
                stage_b_workspace,
                output_dir / variant,
                config=HexgentConfig(context_retrieval=ContextRetrievalLimits(enabled=enabled)),
            )
            runs[variant] = run.to_dict()
    return {
        "stage_a": stage_a.to_dict(),
        "qualification": qualification.reason,
        "stage_b": runs,
        "benchmark_qualification": "QUALIFIED_FOR_FINAL_BENCHMARK",
        "reason": "Stage A record qualified; identical durable snapshot used for both Stage B variants",
    }


def run_phase5c_smoke(output_dir: Path, *, task_ids: Iterable[str] = ("R1", "D2", "W1", "C1", "T1", "L1")) -> dict[str, Any]:
    """执行一次 R1/D2/W1/C1/T1 与按资格门控的 L1；不做统计比较。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    selected = tuple(task_ids)
    standard = []
    for task_id in (item for item in ("R1", "D2", "W1", "C1", "T1") if item in selected):
        print(f"smoke running {task_id}", flush=True)
        standard.append(_run_standard_task(task_by_id(task_id), output_dir))
    l1 = None
    if "L1" in selected:
        print("smoke running L1 Stage A", flush=True)
        l1 = _run_l1(output_dir / "l1")
    return {
        "title": "Phase5C-2 Representative General Agent Runtime Smoke Test",
        "evidence_type": EVIDENCE_TYPE,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "permission_strategy": "fixed_always_allow",
        "standard_tasks": [item.to_dict() for item in standard],
        "l1": l1,
        "note": "One run per task. Diagnostics only; no cross-task averages or mechanism-performance claims.",
    }


def write_smoke_report(report: dict[str, Any], output_dir: Path) -> Path:
    path = output_dir / "phase5c-smoke-report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def default_output_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("eval") / "phase5c" / "runs" / f"smoke-{stamp}"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", nargs="+", choices=("R1", "D2", "W1", "C1", "T1", "L1"))
    arguments = parser.parse_args()
    destination = default_output_dir()
    print(f"smoke output {destination}", flush=True)
    report = run_phase5c_smoke(destination, task_ids=arguments.tasks or ("R1", "D2", "W1", "C1", "T1", "L1"))
    print(write_smoke_report(report, destination))
