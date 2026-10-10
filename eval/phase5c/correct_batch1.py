"""从既有 Batch 1 Artifact 离线生成修正版；不执行 Agent 或 Provider。"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

from .batch1 import _summarize_task, _usage_metrics
from .provider_preflight import classify_preflight_failure
from .smoke import PROVIDER_FAILURE_ATTRIBUTIONS


REVISION = "batch1-artifact-correction-r1"


def correct_run(original: dict) -> dict:
    """复制原始观测并按可用证据修正，不改写原文件。"""

    row = deepcopy(original)
    metadata, runtime = row["metadata"], row["runtime"]
    before = {
        "failure_attribution": metadata.get("failure_attribution"),
        "benchmark_qualification": metadata.get("benchmark_qualification"),
        "usage": deepcopy(metadata.get("usage")),
    }
    failures = runtime.get("model_response_failed", [])
    if failures:
        category = classify_preflight_failure(str(failures[-1].get("message") or ""))
        if category != "UNKNOWN":
            metadata["failure_attribution"] = category
    metadata["usage"] = _usage_metrics(metadata["provider_calls"])
    provider_failed = metadata.get("failure_attribution") in PROVIDER_FAILURE_ATTRIBUTIONS
    policy_denials = [
        call for call in runtime["tool_calls"]
        if call.get("status") == "denied" and call.get("error_category") == "eval_tool_policy_denied"
    ]
    completed = bool(row.get("agent_completed"))
    oracle_pass = row["oracle"]["state"] == "ORACLE_PASS"
    qualified = metadata.get("benchmark_qualification") == "QUALIFIED_FOR_FINAL_BENCHMARK"
    qualification_state = "QUALIFIED" if qualified else "NOT_QUALIFIED"
    if completed and oracle_pass and policy_denials and not metadata.get("shortcut_flags"):
        qualified = True
        qualification_state = "QUALIFIED"
        metadata["benchmark_qualification"] = "QUALIFIED_FOR_FINAL_BENCHMARK"
        metadata["qualification_reason"] = "external oracle passed; recovered Eval policy denial retained"
    if provider_failed:
        qualified = None
        qualification_state = "NOT_EVALUABLE_DUE_TO_PROVIDER"
    calls = metadata["provider_calls"]
    extraction_calls = [c for c in calls if str(c.get("instructions", "")).startswith("你负责从给定的会话事实中提取可复用记录")]
    row["corrected_assessment"] = {
        "task_qualified": qualified,
        "qualification_state": qualification_state,
        "terminal_completed": completed,
        "oracle_pass": oracle_pass,
        "provider_failure": provider_failed,
        "agent_task_failure": not provider_failed and completed and not oracle_pass,
        "tool_policy_denial_count": len(policy_denials),
        "execution_quality_diagnostics": {
            "denied_calls": runtime["denied_calls"],
            "failed_calls": runtime["failed_calls"],
            "original_failure_attribution": before["failure_attribution"],
            "model_response_failed": deepcopy(failures),
        },
        "usage_audit": {
            "context_record_extraction_calls": len(extraction_calls),
            "extraction_calls_without_complete_usage": sum(
                not c.get("completed") or not isinstance(c.get("input_tokens"), int)
                or not isinstance(c.get("output_tokens"), int) for c in extraction_calls
            ),
            "estimated_tokens_used_as_provider_tokens": False,
            "incomplete_call_cause": "NOT_RECONSTRUCTABLE_FROM_SAVED_PROVIDER_CALLS",
        },
        "before": before,
    }
    return row


def denial_audit(rows: list[dict]) -> list[dict]:
    """缺少 command 的旧工件只能保留 UNKNOWN，不根据错误码猜命令。"""

    result = []
    for row in rows:
        run_id = f"T1/run-{row['metadata']['batch_run_index']}"
        for call in row["runtime"]["tool_calls"]:
            if call.get("tool_name") != "bash" or call.get("status") != "denied":
                continue
            result.append({
                "run_id": run_id, "call_id": call["call_id"],
                "command": None, "command_observability": "COMMAND_NOT_OBSERVABLE",
                "category": "UNKNOWN",
                "policy_rule": "EXACT_COMMAND_ALLOWLIST_OR_TRUSTED_CWD_CHECK; SPECIFIC_RULE_NOT_OBSERVABLE",
                "denial_reason": call.get("error_category"),
                "permission_outcome": call.get("permission_outcome"),
                "dispatch_occurred": call.get("dispatch_occurred"),
                "equivalent_authorized_tool": "UNKNOWN_WITHOUT_COMMAND",
                "repeated_attempt": None,
                "repeat_observability": "CANNOT_COMPARE_WITHOUT_COMMAND",
            })
    return result


def build_correction(source: Path) -> dict:
    """校验独立工件与原报告一致后，生成可追溯的离线修正结果。"""

    original = json.loads(source.read_text(encoding="utf-8"))
    inputs = {str(source.resolve()): sha256(source.read_bytes()).hexdigest()}
    tasks = {}
    for task_id, task in original["tasks"].items():
        rows = []
        for embedded in task["individual_artifacts"]:
            index = embedded["metadata"]["batch_run_index"]
            path = source.parent / "general" / task_id / f"run-{index}" / f"{task_id.lower()}-artifact.json"
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved != embedded:
                raise ValueError(f"独立工件与报告不一致：{path}")
            inputs[str(path.resolve())] = sha256(path.read_bytes()).hexdigest()
            rows.append(correct_run(saved))
        summary = _summarize_task(rows)
        assessments = [r["corrected_assessment"] for r in rows]
        summary["corrected_counts"] = {
            "provider_failures": sum(a["provider_failure"] for a in assessments),
            "agent_task_failures": sum(a["agent_task_failure"] for a in assessments),
            "task_qualified": sum(a["task_qualified"] is True for a in assessments),
            "qualification_not_evaluable": sum(a["task_qualified"] is None for a in assessments),
            "tool_policy_denials": sum(a["tool_policy_denial_count"] for a in assessments),
        }
        summary["before"] = {k: v for k, v in task.items() if k != "individual_artifacts"}
        tasks[task_id] = summary
    stage_a = []
    for embedded in original["l1"]["stage_a_artifacts"]:
        index = embedded["metadata"]["batch_run_index"]
        path = source.parent / "l1" / f"run-{index}" / "stage-a" / "l1-artifact.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved != embedded:
            raise ValueError(f"独立工件与报告不一致：{path}")
        inputs[str(path.resolve())] = sha256(path.read_bytes()).hexdigest()
        row = correct_run(saved)
        row["evaluability"] = (
            "L1_NOT_EVALUABLE_DUE_TO_PROVIDER"
            if row["corrected_assessment"]["provider_failure"] else "REQUIRES_REVIEW"
        )
        stage_a.append(row)
    denials = denial_audit(tasks["T1"]["individual_artifacts"])
    d2 = [{
        "run_id": f"D2/run-{row['metadata']['batch_run_index']}",
        "json_valid": row["oracle"]["checks"].get("valid_json"),
        "recorded_failed_checks": [k for k, v in row["oracle"]["checks"].items() if not v],
        "raw_output": "NOT_SAVED_CANNOT_RECONSTRUCT",
        "schema_failure_fields": "NOT_OBSERVABLE",
        "business_rule_verdict": "Exact Alice/Bob oracle assertions failed; substantive decision correctness cannot be separated from schema mismatch",
        "oracle_detail": row["oracle"].get("detail"),
    } for row in tasks["D2"]["individual_artifacts"]]
    return {
        "original_report": str(source.resolve()),
        "correction_revision": REVISION,
        "corrected_at": datetime.now(timezone.utc).isoformat(),
        "source_manifest": original["manifest"],
        "source_file_sha256": inputs,
        "tasks": tasks,
        "provider_failure_total": sum(t["corrected_counts"]["provider_failures"] for t in tasks.values())
        + sum(r["corrected_assessment"]["provider_failure"] for r in stage_a),
        "l1": {
            "evaluability": "L1_NOT_EVALUABLE_DUE_TO_PROVIDER" if all(r["corrected_assessment"]["provider_failure"] for r in stage_a) else "REQUIRES_REVIEW",
            "provider_failures": sum(r["corrected_assessment"]["provider_failure"] for r in stage_a),
            "extraction_attempts": original["l1"]["extraction_attempted_count"],
            "extraction_failures": 0 if not original["l1"]["extraction_attempted_count"] else None,
            "stage_b_executed": original["l1"]["stage_b_pair_executed_count"],
            "before": {k:v for k,v in original["l1"].items() if k not in {"stage_a_artifacts", "stage_b_pairs"}},
            "stage_a_artifacts": stage_a,
        },
        "t1_denials": denials,
        "t1_denial_categories": dict(Counter(d["category"] for d in denials)),
        "d2_audit": d2,
        "d2_contract_audit": {
            "oracle_contract": {"root_keys": ["reviews"], "entry_exact_keys": ["name", "decision", "reason"], "required_reasons": ["score_at_least_80", "score_below_80"]},
            "visible_contract": "Prompt asks for traceable review-result.json; policy only states minimum_score=80 and required_reason_format=score",
            "finding": "Exact root/entry schema and reason enums are not explicitly specified in visible fixture/prompt; contract underspecification exists. Raw output missing prevents attributing these runs solely to reasoning or solely to contract.",
            "prompt_fixture_oracle_modified": False,
        },
        "known_limitations": [
            "Saved artifacts omit raw commands, response output and Transcript; temporary workspaces were cleaned up.",
            "Provider cause, HTTP status, request ID and error code cannot be reconstructed beyond saved messages.",
            "Partial provider usage is observed cost only, not full cost; estimates never fill missing usage.",
            "Task qualification, terminal completion, oracle pass and policy diagnostics are separate dimensions.",
            "Three runs per task do not support broad statistical performance claims.",
        ],
        "freeze_decision": "CAN_FREEZE_AS_INCOMPLETE_BATCH_WITH_LIMITATIONS; NOT_COMPLETE_FINAL_BENCHMARK",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="仅从已有工件离线修正 Batch 1")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = build_correction(args.source)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    target = args.output_dir / "corrected-batch1-report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for path, expected in report["source_file_sha256"].items():
        if sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"原始工件发生变化：{path}")
    print(target)


if __name__ == "__main__":
    main()
