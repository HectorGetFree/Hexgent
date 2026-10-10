"""Phase 5C Final Benchmark Batch 1。

这是 representative general-agent benchmark，不把跨任务结果归因于单个
Context Engineering 机制。Production Runtime 不在本模块的修改范围内。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from statistics import mean, median
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit
from contextlib import nullcontext

from hexgent.agent_engine import AgentEngine
from hexgent.client import get_client, get_model
from hexgent.config import HexgentConfig
from hexgent.memory.context_retrieval import ContextRetrievalLimits

from .fixtures import cloned_snapshot, cloned_workspace, freeze_snapshot, workspace_manifest
from .l1 import freeze_qualified_stage_a
from .oracles import OracleResult, evaluate
from .restricted_bash import PROFILE_NAME, restricted_bash_profile
from .smoke import PROVIDER_FAILURE_ATTRIBUTIONS, _l1_stage_a_oracle, _qualify_l1, _run_engine
from .specifications import FEASIBILITY_TASKS, TaskSpecification, task_by_id
from .tool_exposure import task_tool_exposure, visible_tool_names


EVIDENCE_TYPE = "REPRESENTATIVE_GENERAL_AGENT_BENCHMARK"
TASK_IDS = ("R1", "D2", "W1", "C1", "T1")
STAGE_B_PROMPT = "依据此前的发布资料完成项目交接：更新 project-state.json 和 handoff.md。"


def _file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _eval_revision() -> dict[str, str]:
    """记录 Eval 实现内容身份，避免把旧 batch 与新规则混为同一 revision。"""

    source_names = (
        "batch1.py",
        "smoke.py",
        "tool_exposure.py",
        "restricted_bash.py",
        "provider_preflight.py",
    )
    sources = {name: _file_hash(Path(__file__).with_name(name)) for name in source_names}
    canonical = json.dumps(sources, ensure_ascii=False, sort_keys=True)
    return {
        "id": "phase5c-eval-reliability-r1",
        "content_sha256": sha256(canonical.encode("utf-8")).hexdigest(),
        "source_sha256": sources,
    }


def _fixture_hash(fixture_id: str) -> str:
    canonical = json.dumps(workspace_manifest(
        Path(__file__).with_name("fixtures") / fixture_id,
        include_hexgent=True,
    ), sort_keys=True, ensure_ascii=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _model_config() -> dict[str, str | bool | None]:
    """只保存模型标识与 provider origin，绝不写入 API Key。"""

    get_client()  # 复用 Production client 的 dotenv 加载路径。
    base_url = os.environ.get("OPENAI_BASE_URL")
    parsed = urlsplit(base_url or "")
    return {
        "model_id": get_model(),
        "provider_origin": f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else None,
        "streaming": True,
    }


def _load_artifact(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _transcript_metrics(engine: AgentEngine) -> dict[str, Any]:
    transcript = engine.session.store.read_transcript() if engine.session.store else []
    read_paths: list[str] = []
    record_retrieval_count = 0
    for row in transcript:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if row.get("type") == "tool_execution_started" and payload.get("tool_name") == "read_file":
            try:
                arguments = json.loads(str(payload.get("arguments") or "{}"))
            except json.JSONDecodeError:
                arguments = {}
            if isinstance(arguments.get("path"), str):
                read_paths.append(arguments["path"])
        elif row.get("type") == "context_records_retrieved":
            records = payload.get("records")
            record_retrieval_count += len(records) if isinstance(records, list) else 0
    return {
        "compaction_count": sum(row.get("type") == "context_compaction_applied" for row in transcript),
        "context_record_recall_count": record_retrieval_count,
        # Durable retrieval facts deliberately avoid copying record body, so exact injected tokens are unavailable.
        "context_record_injected_tokens": None,
        "context_record_injected_tokens_availability": "UNAVAILABLE_FROM_DURABLE_FACTS",
        "unique_files_read": len(set(read_paths)),
        "repeated_file_reads": max(0, len(read_paths) - len(set(read_paths))),
    }


def _usage_metrics(provider_calls: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = len(provider_calls)
    covered = sum(
        bool(item.get("completed"))
        and isinstance(item.get("input_tokens"), int)
        and isinstance(item.get("output_tokens"), int)
        for item in provider_calls
    )
    observed_input = sum(item.get("input_tokens", 0) for item in provider_calls if isinstance(item.get("input_tokens"), int))
    observed_output = sum(item.get("output_tokens", 0) for item in provider_calls if isinstance(item.get("output_tokens"), int))
    completeness = "COMPLETE" if eligible and covered == eligible else "PARTIAL" if covered else "UNAVAILABLE"
    return {
        "usage_eligible_calls": eligible,
        "usage_covered_calls": covered,
        "usage_coverage_ratio": covered / eligible if eligible else 0.0,
        "usage_completeness": completeness,
        "observed_input_tokens": observed_input if covered else None,
        "observed_output_tokens": observed_output if covered else None,
        "observed_total_tokens": observed_input + observed_output if covered else None,
    }


def _decorate_artifact(path: Path, engine: AgentEngine, *, task_id: str, run_index: int, stage: str) -> dict[str, Any]:
    artifact = _load_artifact(path)
    metadata = artifact["metadata"]
    provider_calls = metadata.get("provider_calls")
    assert isinstance(provider_calls, list)
    metadata.update({
        "benchmark_evidence_type": EVIDENCE_TYPE,
        "batch_run_index": run_index,
        "benchmark_stage": stage,
        "tool_execution_profile": PROFILE_NAME,
        "usage": _usage_metrics(provider_calls),
        "runtime_metrics": _transcript_metrics(engine),
    })
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return artifact


def _run_task(spec: TaskSpecification, destination: Path, run_index: int, *, config: HexgentConfig | None = None,
              stage: str = "general", oracle_factory: Any = None) -> dict[str, Any]:
    with cloned_workspace(spec.fixture) as workspace:
        # 只有 T1 注册 eval-only bash；其余任务既不暴露也不能 dispatch bash。
        bash_profile = restricted_bash_profile(workspace) if spec.task_id == "T1" else nullcontext()
        with task_tool_exposure(spec), bash_profile:
            run, engine, _ = _run_engine(spec, workspace, destination, config=config, oracle_factory=oracle_factory)
        return _decorate_artifact(Path(run.artifact_path), engine, task_id=spec.task_id, run_index=run_index, stage=stage)


def _stage_b_oracle(workspace: Path, before: dict[str, str], _terminal: dict[str, Any], _transcript: list[dict[str, Any]]) -> OracleResult:
    """L1 Stage B 与冻结的项目交接合同使用相同的确定性 W1 oracle。"""

    return evaluate("W1", workspace, before)


def _l1_stage_b_eligibility(
    *,
    agent_completed: bool,
    extraction_path_executed: bool,
    record_qualified: bool,
    record_reason: str,
) -> tuple[bool, str]:
    """只有完整 Stage A 才能成为 Stage B paired snapshot 的来源。"""

    if not agent_completed:
        return False, "STAGE_A_NOT_COMPLETED"
    if not extraction_path_executed:
        return False, "STAGE_A_EXTRACTION_NOT_EXECUTED"
    if not record_qualified:
        return False, record_reason
    return True, "ELIGIBLE"


def _run_l1(destination: Path, run_index: int) -> dict[str, Any]:
    spec = task_by_id("L1")
    stage_a_spec = TaskSpecification(
        **{**asdict(spec), "user_prompt": (
            "不要修改文件。请按顺序核对项目说明、需求、风险和发布策略；"
            "每完成一项再继续下一项，最后整理供后续交接使用的可复用事实。"
        )}
    )
    with cloned_workspace(spec.fixture) as workspace:
        with task_tool_exposure(stage_a_spec):
            stage_a_run, engine, _ = _run_engine(
                stage_a_spec, workspace, destination / "stage-a",
                oracle_factory=_l1_stage_a_oracle,
            )
        stage_a = _decorate_artifact(
            Path(stage_a_run.artifact_path), engine, task_id="L1", run_index=run_index, stage="stage_a",
        )
        qualification = _qualify_l1(engine, STAGE_B_PROMPT)
        active_records = len(engine.session.context_records.active_records)
        extraction_path_executed = bool(stage_a["oracle"]["checks"].get("extraction_path_executed"))
        stage_b_eligible, eligibility_reason = _l1_stage_b_eligibility(
            agent_completed=bool(stage_a["agent_completed"]),
            extraction_path_executed=extraction_path_executed,
            record_qualified=qualification.qualified,
            record_reason=qualification.reason,
        )
        stage_a["metadata"]["l1_qualification"] = {
            "active_record_count": active_records,
            "qualified_record_observed": qualification.qualified,
            "qualified_stage_a": stage_b_eligible,
            "record_qualified": qualification.qualified,
            "record_qualification_reason": qualification.reason,
            "record_id": qualification.record_id,
            "stage_a_terminal_completed": bool(stage_a["agent_completed"]),
            "extraction_path_executed": extraction_path_executed,
            "stage_b_eligible": stage_b_eligible,
            "stage_b_eligibility_reason": eligibility_reason,
        }
        Path(stage_a_run.artifact_path).write_text(json.dumps(stage_a, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not stage_b_eligible:
            return {
                "stage_a": stage_a,
                "qualification": eligibility_reason,
                "stage_b": None,
                "stage_b_pair": {"state": "NOT_ELIGIBLE", "reason": eligibility_reason},
            }
        snapshot = destination / "stage-a-snapshot"
        freeze_qualified_stage_a(workspace, snapshot, qualification)

    stage_b: dict[str, dict[str, Any]] = {}
    for variant, enabled in (("context_record_on", True), ("context_record_off", False)):
        with cloned_snapshot(snapshot) as cloned:
            stage_b_spec = TaskSpecification(**{**asdict(spec), "user_prompt": STAGE_B_PROMPT})
            with task_tool_exposure(stage_b_spec):
                run, engine, _ = _run_engine(
                    stage_b_spec,
                    cloned,
                    destination / variant,
                    config=HexgentConfig(context_retrieval=ContextRetrievalLimits(enabled=enabled)),
                    oracle_factory=_stage_b_oracle,
                )
            stage_b[variant] = _decorate_artifact(
                Path(run.artifact_path), engine, task_id="L1", run_index=run_index, stage=variant,
            )
    on_retrieval_count = stage_b["context_record_on"]["metadata"]["runtime_metrics"]["context_record_recall_count"]
    pair_state = "PAIR_EXECUTED_WITH_RETRIEVAL" if on_retrieval_count > 0 else "PAIR_EXECUTED_NO_RETRIEVAL"
    return {
        "stage_a": stage_a,
        "qualification": qualification.reason,
        "stage_b": stage_b,
        "stage_b_pair": {
            "state": pair_state,
            "context_record_on_recall_count": on_retrieval_count,
            "context_record_off_recall_count": stage_b["context_record_off"]["metadata"]["runtime_metrics"]["context_record_recall_count"],
        },
    }


def _mean_median(rows: list[dict[str, Any]], field: str, *, complete_only: bool = False) -> dict[str, float | int | None]:
    values = []
    for row in rows:
        if complete_only and row["metadata"]["usage"]["usage_completeness"] != "COMPLETE":
            continue
        value = (
            row["metadata"]["usage"].get(field)
            if field in {"observed_input_tokens", "observed_output_tokens", "observed_total_tokens"}
            else row["metadata"].get(field)
        )
        if isinstance(value, (int, float)):
            values.append(value)
    return {"mean": round(mean(values), 2) if values else None, "median": round(median(values), 2) if values else None, "sample_count": len(values)}


def _summarize_task(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scheduled = len(rows)
    terminal_completed = [row for row in rows if row.get("agent_completed")]
    oracle_pass = [row for row in rows if row["oracle"]["state"] == "ORACLE_PASS"]
    oracle_fail = [row for row in rows if row["oracle"]["state"] == "ORACLE_FAIL"]
    infrastructure = [
        row for row in rows
        if row["oracle"]["state"] == "INFRA_FAILURE"
        or row["metadata"]["failure_attribution"] in PROVIDER_FAILURE_ATTRIBUTIONS | {"AGENT_RUNTIME_FAILURE"}
    ]
    successes = [row for row in oracle_pass if row.get("agent_completed")]
    usage = [row["metadata"]["usage"] for row in rows]
    calls = [call for row in rows for call in row["runtime"]["tool_calls"]]
    return {
        "scheduled_runs": scheduled,
        "terminal_completed_runs": len(terminal_completed),
        "infrastructure_failures": len(infrastructure),
        "oracle_pass_runs": len(oracle_pass),
        "oracle_fail_runs": len(oracle_fail),
        "end_to_end_success_runs": len(successes),
        "terminal_completion_rate": len(terminal_completed) / scheduled if scheduled else None,
        "oracle_pass_rate": len(oracle_pass) / scheduled if scheduled else None,
        "end_to_end_success_rate": len(successes) / scheduled if scheduled else None,
        "usage_coverage": {
            "eligible_calls": sum(item["usage_eligible_calls"] for item in usage),
            "covered_calls": sum(item["usage_covered_calls"] for item in usage),
            "complete_runs": sum(item["usage_completeness"] == "COMPLETE" for item in usage),
            "partial_runs": sum(item["usage_completeness"] == "PARTIAL" for item in usage),
        },
        "metrics": {
            "provider_input_tokens_complete_runs": _mean_median(rows, "observed_input_tokens", complete_only=True),
            "provider_output_tokens_complete_runs": _mean_median(rows, "observed_output_tokens", complete_only=True),
            "model_rounds": _mean_median(rows, "model_rounds"),
            "latency_ms": _mean_median(rows, "latency_ms"),
            "total_tool_calls": {"mean": round(mean(row["runtime"]["total_tool_calls"] for row in rows), 2), "median": median(row["runtime"]["total_tool_calls"] for row in rows)},
            "unique_files_read": {"mean": round(mean(row["metadata"]["runtime_metrics"]["unique_files_read"] for row in rows), 2), "median": median(row["metadata"]["runtime_metrics"]["unique_files_read"] for row in rows)},
            "repeated_file_reads": {"mean": round(mean(row["metadata"]["runtime_metrics"]["repeated_file_reads"] for row in rows), 2), "median": median(row["metadata"]["runtime_metrics"]["repeated_file_reads"] for row in rows)},
            "compaction_count": {"mean": round(mean(row["metadata"]["runtime_metrics"]["compaction_count"] for row in rows), 2), "median": median(row["metadata"]["runtime_metrics"]["compaction_count"] for row in rows)},
        },
        "tool_calls_by_name": dict(Counter(call["tool_name"] or "unknown" for call in calls)),
        "failure_attribution": dict(Counter(row["metadata"]["failure_attribution"] or "NONE" for row in rows)),
        "individual_artifacts": rows,
    }


def _l1_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    stage_a = [row["stage_a"] for row in rows]
    completed = [row for row in stage_a if row["agent_completed"]]
    record_qualified = [row for row in stage_a if row["metadata"]["l1_qualification"]["record_qualified"]]
    eligible = [row for row in stage_a if row["metadata"]["l1_qualification"]["stage_b_eligible"]]
    reasons = Counter(
        row["metadata"]["l1_qualification"]["stage_b_eligibility_reason"]
        for row in stage_a
        if not row["metadata"]["l1_qualification"]["stage_b_eligible"]
    )
    pairs = [row for row in rows if row["stage_b"] is not None]
    pairs_with_retrieval = [row for row in pairs if row["stage_b_pair"]["state"] == "PAIR_EXECUTED_WITH_RETRIEVAL"]
    return {
        "stage_a_scheduled_count": len(stage_a),
        "stage_a_completed_count": len(completed),
        "extraction_attempted_count": sum(bool(row["oracle"]["checks"].get("extraction_path_executed")) for row in stage_a),
        "active_record_produced_count": sum(row["metadata"]["l1_qualification"]["active_record_count"] > 0 for row in stage_a),
        "record_qualified_count": len(record_qualified),
        "qualified_record_observed_count": len(record_qualified),
        "record_qualification_rate": len(record_qualified) / len(stage_a) if stage_a else None,
        "stage_b_pair_eligible_count": len(eligible),
        "qualified_stage_a_count": len(eligible),
        "stage_b_pair_executed_count": len(pairs),
        "stage_b_pair_with_retrieval_count": len(pairs_with_retrieval),
        "stage_b_ineligibility_reasons": dict(reasons),
        "stage_b_pairs": pairs,
        "stage_a_artifacts": stage_a,
    }


def run_batch1(destination: Path, *, runs_per_task: int = 3, l1_stage_a_runs: int = 3) -> dict[str, Any]:
    """固定 Batch 1 执行次数；不因失败补跑，也不更改 task contract。"""

    destination.mkdir(parents=True, exist_ok=False)
    manifest = {
        "title": "Phase 5C Final Benchmark Batch 1",
        "evidence_type": EVIDENCE_TYPE,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "eval_revision": _eval_revision(),
        "tool_execution_profile": PROFILE_NAME,
        "restricted_bash_profile": {"id": PROFILE_NAME, "applies_to": ["T1"]},
        "tool_exposure_profile": {task.task_id: list(visible_tool_names(task)) for task in FEASIBILITY_TASKS},
        "permission_strategy": "fixed_always_allow_with_eval_tool_policy",
        "model_configuration": _model_config(),
        "provider_configuration": {
            "credential_handling": "environment-managed-not-recorded",
            "preflight": "explicit-separate-not-included-in-benchmark-statistics",
        },
        "task_specification_sha256": sha256(json.dumps([asdict(item) for item in FEASIBILITY_TASKS], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
        "fixture_baseline_sha256": {fixture: _fixture_hash(fixture) for fixture in sorted({item.fixture for item in FEASIBILITY_TASKS})},
        "oracle_source_sha256": _file_hash(Path(__file__).with_name("oracles.py")),
        "runs_per_general_task": runs_per_task,
        "l1_stage_a_runs": l1_stage_a_runs,
        "limitation": "T1 的 bash handler 是 eval_restricted_bash_v1，不是 Production bash implementation。",
    }
    (destination / "benchmark-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    general: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in TASK_IDS}
    for task_id in TASK_IDS:
        for index in range(1, runs_per_task + 1):
            print(f"batch1 {task_id} run {index}/{runs_per_task}", flush=True)
            general[task_id].append(_run_task(task_by_id(task_id), destination / "general" / task_id / f"run-{index}", index))

    l1_rows = []
    for index in range(1, l1_stage_a_runs + 1):
        print(f"batch1 L1 Stage A run {index}/{l1_stage_a_runs}", flush=True)
        l1_rows.append(_run_l1(destination / "l1" / f"run-{index}", index))

    report = {
        "title": "Phase 5C Final Benchmark Batch 1",
        "evidence_type": EVIDENCE_TYPE,
        "manifest": manifest,
        "tasks": {task_id: _summarize_task(rows) for task_id, rows in general.items()},
        "l1": _l1_summary(l1_rows),
        "known_limitations": [
            "每题仅 3 次，P95 不作为正式性能结论。",
            "Provider token 仅在 usage_completeness=COMPLETE 的 run 中可作为完整成本。",
            "ContextRecord injected tokens 不在 durable retrieval facts 中保存正文，因此此字段为 UNAVAILABLE。",
            "T1 使用 eval_restricted_bash_v1；其安全性不能归功于 Production Runtime。",
        ],
    }
    path = destination / "batch1-report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def default_output_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("eval") / "phase5c" / "runs" / f"batch1-{stamp}"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-per-task", type=int, default=3)
    parser.add_argument("--l1-stage-a-runs", type=int, default=3)
    arguments = parser.parse_args()
    output = default_output_dir()
    print(f"batch1 output {output}", flush=True)
    run_batch1(output, runs_per_task=arguments.runs_per_task, l1_stage_a_runs=arguments.l1_stage_a_runs)
    print(output / "batch1-report.json")
