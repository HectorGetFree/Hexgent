"""跨进程恢复补强 runner 与外部 Oracle；不会调用真实模型。"""

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

from eval.modules_v1.runner import ROOT, protected_hashes
from .worker import CALL_ID, CRASH_EXIT, SCENARIOS


EXISTING = ROOT / "eval/modules_v1/runs/modules-v1-20261008-final"


def bytes_or_empty(path):
    """无副作用场景没有账本时明确记为空，不补造 handler 事件。"""
    return path.read_bytes() if path.exists() else b""


def protected_inputs():
    """原始证据、Production、既有测试与冻结输入均受内容 Hash 保护。"""
    hashes = protected_hashes()
    for directory in (EXISTING, ROOT / "eval/phase3"):
        for path in directory.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                hashes[str(path.relative_to(ROOT))] = sha256(path.read_bytes()).hexdigest()
    return hashes


def function_outputs(messages):
    """独立检查指定 call 的模型可见配对消息。"""
    return [m for m in messages if isinstance(m, dict) and m.get("type") == "function_call_output" and m.get("call_id") == CALL_ID]


def oracle(name, pre, observation, before_ledger, after_ledger, business_before, business_after):
    """从原始日志、模型实际 input 和外部账本共同判定，不复用 Runtime 修复算法。"""
    ledger = [json.loads(line) for line in before_ledger.splitlines()]
    counts = Counter(r["event"] for r in ledger)
    expected_events = {
        "finished_before_context": ["handler_entered", "side_effect_started", "side_effect_committed", "handler_returned"],
        "safe_checkpoint": ["handler_entered", "side_effect_started", "side_effect_committed", "handler_returned"],
        "committed_before_finished": ["handler_entered", "side_effect_started", "side_effect_committed", "handler_returned"],
        "handler_before_effect": ["handler_entered"],
        "handler_partial_effect": ["handler_entered", "side_effect_started"],
        "safe_started_before_dispatch": [], "permission_denied": [], "permission_pending": [],
    }[name]
    expected_business = "COMMITTED" if "side_effect_committed" in expected_events else "PARTIAL" if name == "handler_partial_effect" else None
    final = observation["final_records"]
    requests = observation["model_requests"]
    outputs = function_outputs(observation["first_restored_messages"])
    final_outputs = function_outputs(observation["final_messages"])
    model_outputs = function_outputs(requests[0]["input"]) if requests else []
    durable_outputs = [m for r in final if r["type"] == "context_messages_appended"
        for m in r["payload"]["messages"] if isinstance(m, dict)
        and m.get("type") == "function_call_output" and m.get("call_id") == CALL_ID]
    finished = [r["payload"] for r in pre if r["type"] == "tool_execution_finished" and r["payload"]["call_id"] == CALL_ID]
    if finished:
        expected_output = finished[-1]["output"]
        result_matches = len(outputs) == 1 and outputs[0]["output"] == expected_output == "AUDIT_RESULT:COMMITTED"
    elif name in {"handler_before_effect", "handler_partial_effect", "committed_before_finished"}:
        result_matches = len(outputs) == 1 and "副作用状态未知" in outputs[0]["output"] and "没有自动重试" in outputs[0]["output"]
    else:
        result_matches = len(outputs) == 1 and "未执行" in outputs[0]["output"]
    dispatch_pre = sum(r["type"] == "tool_execution_dispatched" for r in pre)
    dispatch_final = sum(r["type"] == "tool_execution_dispatched" for r in final)
    checks = {
        "external_handler_trajectory_exact": [r["event"] for r in ledger] == expected_events,
        "external_business_state_exact": business_before == business_after == expected_business,
        "no_external_execution_during_restore_or_resume": before_ledger == after_ledger,
        "no_extra_dispatch_during_restore_or_resume": dispatch_pre == dispatch_final,
        "durable_history_preserved": final[:len(pre)] == pre,
        "first_restore_history_preserved": observation["first_restore_records"][:len(pre)] == pre,
        "recovered_output_matches_durable_or_conservative_result": result_matches,
        "model_actually_received_recovered_output": bool(outputs) and model_outputs == outputs,
        "one_effective_output_in_final_context_and_transcript": len(outputs) == len(final_outputs) == len(durable_outputs) == 1 and outputs == final_outputs == durable_outputs,
        "agent_loop_completed_with_fixture_result": observation["terminal_status"] == "completed" and observation["terminal_output"] == "RECOVERY_OBSERVED" and len(requests) == 1,
    }
    permission_checks = {}
    if name.startswith("permission_"):
        resolved_pre = [r["payload"] for r in pre if r["type"] == "permission_resolved"]
        resolved_final = [r["payload"] for r in final if r["type"] == "permission_resolved"]
        pending = [r for r in pre if r["type"] == "permission_requested" and r["payload"]["status"] == "PENDING"]
        permission_checks = {
            "no_unauthorized_dispatch": dispatch_pre == dispatch_final == 0,
            "handler_never_entered": counts["handler_entered"] == 0 and before_ledger == after_ledger == b"",
            "no_new_permission_resolution_or_allow": resolved_pre == resolved_final and not any(r["behavior"] == "allow" for r in resolved_final),
            "real_durable_permission_state": (len(resolved_pre) == 1 and resolved_pre[0]["behavior"] == "deny") if name == "permission_denied" else len(pending) == 1 and not resolved_pre,
        }
    return {"checks": checks, "permission_checks": permission_checks,
        "oracle_status": "PASS" if all(checks.values()) and all(permission_checks.values()) else "FAIL",
        "handler_entered_count": counts["handler_entered"], "side_effect_started_count": counts["side_effect_started"],
        "side_effect_committed_count": counts["side_effect_committed"], "handler_returned_count": counts["handler_returned"],
        "business_state": business_after, "extra_dispatch_count": dispatch_final-dispatch_pre,
        "requires_manual_verification": name in {"handler_before_effect", "handler_partial_effect", "committed_before_finished"}}


def execute_case(destination, name):
    """父进程串行等待 A 完全退出，再启动独立解释器 B。"""
    root = destination / name / "workspace"
    root.mkdir(parents=True)
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT") if key in os.environ}
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [sys.executable, "-m", "eval.recovery_hardening.worker"]
    a = subprocess.run([*command, "crash", name, str(root)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=25)
    if a.returncode != CRASH_EXIT:
        return {"scenario": name, "status": "FAIL", "reason": "未到达目标进程退出边界", "process_a": {"returncode": a.returncode, "stdout": a.stdout, "stderr": a.stderr}}
    boundary = json.loads((root / "crash-boundary.json").read_text())
    transcript_path = root / ".hexgent/sessions" / boundary["session_id"] / "transcript.jsonl"
    # 真实目录结构由 SessionStore 定义；只读取，不注入领域事实。
    if not transcript_path.exists():
        candidates = list((root / ".hexgent").rglob("transcript.jsonl"))
        if len(candidates) != 1:
            raise AssertionError("无法唯一定位真实 Transcript")
        transcript_path = candidates[0]
    pre = [json.loads(line) for line in transcript_path.read_text().splitlines()]
    before_ledger = bytes_or_empty(root / "effects.jsonl")
    business_before = (root / "business.txt").read_text() if (root / "business.txt").exists() else None
    b = subprocess.run([*command, "restore", name, str(root)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=25)
    if b.returncode:
        return {"scenario": name, "status": "FAIL", "reason": "恢复子进程失败", "process_b": {"returncode": b.returncode, "stdout": b.stdout, "stderr": b.stderr}, "pre_crash_records": pre}
    observation = json.loads((root / "restored-observation.json").read_text())
    after_ledger = bytes_or_empty(root / "effects.jsonl")
    business_after = (root / "business.txt").read_text() if (root / "business.txt").exists() else None
    assessment = oracle(name, pre, observation, before_ledger, after_ledger, business_before, business_after)
    assessment["checks"]["independent_exited_processes"] = boundary["pid"] != observation["pid"] and a.returncode == CRASH_EXIT and b.returncode == 0
    if not all(assessment["checks"].values()):
        assessment["oracle_status"] = "FAIL"
    resume_status = "BLOCKED" if observation["continue_blocked_reason"] else "PASS"
    return {"scenario": name, "status": "FAIL" if assessment["oracle_status"] == "FAIL" else resume_status,
        "same_run_resume": resume_status,
        "session_followup": ("PASS" if assessment["oracle_status"] == "PASS" else "FAIL") if observation["execution_mode"] == "NEW_QUERY_SESSION_FOLLOWUP" else "NOT_APPLICABLE",
        "blocker": observation["continue_blocked_reason"], "process_a": {"pid": boundary["pid"], "returncode": a.returncode},
        "process_b": {"pid": observation["pid"], "returncode": b.returncode},
        "crash_boundary": boundary, "pre_crash_records": pre, "observation": observation,
        "external_ledger_before": [json.loads(line) for line in before_ledger.splitlines()],
        "external_ledger_after": [json.loads(line) for line in after_ledger.splitlines()], **assessment}


def run(destination):
    """保留已有 Artifact，以独立证据补充报告，而非覆盖旧 PASS 定义。"""
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    before = protected_inputs()
    rows = []
    for name in SCENARIOS:
        print(name, flush=True)
        row = execute_case(destination, name)
        rows.append(row)
        (destination / name / "evidence.json").write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n")
    after = protected_inputs()
    if before != after:
        raise AssertionError("Production、冻结输入或原 Artifact 改变")
    existing = json.loads((EXISTING / "report.json").read_text())
    summary = {"benchmark": "recovery-evidence-hardening-v1", "evidence_type": "CROSS_PROCESS_DETERMINISTIC",
        "created_at": datetime.now(timezone.utc).isoformat(), "scenario_count": len(rows),
        "status_counts": dict(Counter(r["status"] for r in rows)),
        "status": "FAIL" if any(r["status"] == "FAIL" for r in rows) else "PARTIAL_BLOCKED" if any(r["status"] == "BLOCKED" for r in rows) else "PASS",
        "existing_protocol_recovery_evidence": {"source": str(EXISTING / "report.json"), "metrics": existing["modules"][0]["metrics"], "interpretation_unchanged": True},
        "new_cross_process_resume_evidence": [{"scenario": r["scenario"], "same_run_resume": r.get("same_run_resume"), "session_followup": r.get("session_followup"), "blocker": r.get("blocker"), "oracle_status": r.get("oracle_status")} for r in rows],
        "new_side_effect_safety_evidence": [{k: r.get(k) for k in ("scenario", "handler_entered_count", "side_effect_started_count", "side_effect_committed_count", "handler_returned_count", "business_state", "extra_dispatch_count", "requires_manual_verification", "oracle_status")} for r in rows],
        "new_permission_recovery_evidence": [{"scenario": r["scenario"], "checks": r.get("permission_checks"), "same_run_resume": r.get("same_run_resume")} for r in rows if r["scenario"].startswith("permission_")],
        "production_frozen_and_original_artifacts_unchanged": True, "protected_hashes": before,
        "source_hashes": {p.name: sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob("*.py")},
        "limitations": ["No power-loss/fsync guarantee for Production Transcript", "No concurrent restore or crash-during-repair coverage",
            "No automatic retry/repair of unknown effects", "PendingPermission HTTP reopen/resolve after restart unverified",
            "New submit continuation is not original Run continue; deterministic model does not prove arbitrary model behavior"],
        "cases": [{"scenario": r["scenario"], "status": r["status"], "artifact": str(destination/r["scenario"]/"evidence.json")} for r in rows],
        "real_model_requests": 0}
    (destination / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.output_dir)
    print(json.dumps({"status": report["status"], "counts": report["status_counts"]}, ensure_ascii=False))
    raise SystemExit(1 if report["status"] == "FAIL" else 0)
