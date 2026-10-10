"""Module V1 离线 runner：原始证据、具名 oracle、独立指标与限制。"""

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
from statistics import mean
import unittest
from unittest.mock import patch

from eval.phase4.runner import run_scenarios as context_memory_contracts
from eval.phase5.runner import run_benchmark
from eval.phase5c.specifications import FEASIBILITY_TASKS
from .scenarios import compaction_case, parallel_case, recovery, safety, workspace_memory, context_record_continuation


ROOT = Path(__file__).resolve().parents[2]
SPEC = Path(__file__).with_name("SPECIFICATION.md")


def encode(value):
    """序列化 fixture 的 SDK 替身，不丢弃原始观察字段。"""
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    if hasattr(value, "__dict__"):
        return vars(value)
    raise TypeError(f"未支持的 artifact 类型：{type(value).__name__}")


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=encode)+"\n", encoding="utf-8")


def protected_hashes():
    """保护 Production、已有测试、冻结 fixture/oracle/spec 与配置。"""
    paths = list((ROOT/"hexgent").rglob("*.py")) + list((ROOT/"tests").rglob("*.py"))
    paths += list((ROOT/"eval/phase5c/fixtures").rglob("*"))
    paths += [ROOT/"eval/phase5c/specifications.py", ROOT/"eval/phase5c/oracles.py", ROOT/".env"]
    return {str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file() and "__pycache__" not in p.parts}


class EvidenceResult(unittest.TextTestResult):
    """逐方法保留实际执行结果，不把测试文件存在当成通过。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.evidence = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.evidence.append({"test_id":test.id(), "status":"PASS"})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.evidence.append({"test_id":test.id(), "status":"FAIL", "detail":self._exc_info_to_string(err,test)})

    def addError(self, test, err):
        super().addError(test, err)
        self.evidence.append({"test_id":test.id(), "status":"ERROR", "detail":self._exc_info_to_string(err,test)})

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.evidence.append({"test_id":test.id(), "status":"SKIP", "detail":reason})


def existing_tests(patterns, directory="tests"):
    suite = unittest.TestSuite()
    for pattern in patterns:
        suite.addTests(unittest.TestLoader().discover(str(ROOT/directory), pattern=pattern,
            top_level_dir=str(ROOT) if directory.startswith("eval/") else None))
    result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=EvidenceResult).run(suite)
    return {"total":result.testsRun, "passed":sum(e["status"]=="PASS" for e in result.evidence),
            "failures":len(result.failures), "errors":len(result.errors), "skipped":len(result.skipped), "cases":result.evidence}


def historical_evidence():
    """读取原始真实模型报告，缺失则显式 UNVERIFIED。"""
    paths = {
        "phase5b": ROOT/"eval/phase5/runs/real-model-phase5b-20261007/phase5b-real-model-report.json",
        "phase5c": ROOT/"eval/phase5c/runs/batch1-20261008T071913Z-corrected-r1/corrected-batch1-report.json",
    }
    output = {}
    for name,path in paths.items():
        if not path.exists():
            output[name] = {"status":"UNVERIFIED", "source":str(path)}
            continue
        raw = json.loads(path.read_text())
        output[name] = {"status":"VERIFIED_FROM_ARTIFACT", "source":str(path), "sha256":sha256(path.read_bytes()).hexdigest()}
        if name == "phase5b":
            pair = next(s for s in raw["scenarios"] if s["scenario"]=="T1_long_context_compression")
            b, e = pair["baseline"],pair["enabled"]
            output[name]["compaction"] = {"provider_input_tokens_before":b["provider_input_tokens"]["mean"],
                "provider_input_tokens_after":e["provider_input_tokens"]["mean"],
                "task_total_provider_input_reduction_pct":(1-e["provider_input_tokens"]["mean"]/b["provider_input_tokens"]["mean"])*100,
                "task_latency_change_pct":(e["latency_ms"]["mean"]/b["latency_ms"]["mean"]-1)*100,
                "success_before":b["success"], "success_after":e["success"],
                "provider_usage_complete_runs":{"baseline":b["provider_usage_complete_runs"], "enabled":e["provider_usage_complete_runs"]},
                "provider_cost_comparison_complete":b["provider_usage_complete_runs"] == e["provider_usage_complete_runs"] == raw["runs_per_variant"],
                "scope":"historical aggregate arithmetic only; unequal complete-usage counts prevent complete paired cost claim; preseeded memory is not auto extraction"}
        else:
            tasks = raw["tasks"]
            required = ["call_id","tool_name","status","error_category","permission_outcome","dispatch_occurred","arguments","output","denial_reason"]
            calls = [c for t in tasks.values() for a in t["individual_artifacts"] for c in a["runtime"]["tool_calls"]]
            counts = {field:sum(field in c for c in calls) for field in required}
            output[name].update({
                "implemented_candidate_task_count":len(FEASIBILITY_TASKS),
                "candidate_task_count":None, "candidate_task_count_status":"12-candidate design inventory UNVERIFIED; six implemented specs verified",
                "qualified_task_count":sum(any(a["corrected_assessment"]["task_qualified"] is True for a in t["individual_artifacts"]) for t in tasks.values()),
                "executed_task_count":len(tasks)+int(bool(raw["l1"]["stage_a_artifacts"])),
                "completed_task_count":sum(t["terminal_completed_runs"]>0 for t in tasks.values()),
                "oracle_pass_task_count":sum(t["oracle_pass_runs"]>0 for t in tasks.values()),
                "workload_category_count":len({t.domain for t in FEASIBILITY_TASKS}),
                "oracle_coverage":{"implemented_task_specs":len(FEASIBILITY_TASKS), "with_oracle_contract":sum(bool(t.oracle) for t in FEASIBILITY_TASKS)},
                "provider_failures":raw["provider_failure_total"],
                "audit_field_coverage":{"required_fields":required,"observed_calls":len(calls),"present_field_counts":counts,
                                        "presence_ratio":sum(counts.values())/(len(calls)*len(required)) if calls else None,
                                        "scope":"field presence only; null values and missing body not claimed observable"},
                "l1":raw["l1"]["evaluability"],
                "task_qualification_caveat":"D2 retained historical qualification; schema contract underspecified; L1 not evaluable",
            })
    return output


def run(destination):
    """执行冻结场景；网络 transport 设为拒绝，全部 artifact 写到独立新目录。"""
    destination.mkdir(parents=True, exist_ok=False)
    protected = protected_hashes()
    with patch("httpx.Client.send", side_effect=AssertionError("Module V1 禁止真实网络请求")):
        print("Module 1: recovery", flush=True)
        r = recovery()
        checkpoints = existing_tests(["test_session_recovery.py"])
        print("Module 2: parallelism + safety", flush=True)
        parallel = [parallel_case(n,d,w) for n in (2,4,8) for d in (10,30) for w in (2,4)]
        parallel.append(parallel_case(8,10,4,staggered=True))
        s = safety()
        safety_tests = existing_tests(["test_tool_parallel_execution.py","test_tool_failure_boundaries.py","test_tool_deduplication.py","test_session_store.py"])
        print("Module 3: structured memory", flush=True)
        memory = workspace_memory()
        continuation = context_record_continuation()
        contracts = context_memory_contracts().to_dict()
        memory_tests = existing_tests(["test_memory.py","test_context_records.py","test_context_retrieval.py","test_context_record_extraction.py"])
        print("Module 4: compaction", flush=True)
        compaction = [compaction_case(n) for n in ("long_history","multi_read","large_result","active_plan","near_budget","complete_rounds")]
        compression_tests = existing_tests(["test_context_compaction.py","test_agent_loop_limits.py"])
        phase5a = run_benchmark().to_dict()
        print("Module 5: audit", flush=True)
        # 复用 Phase5C 基础设施测试，避免建立新的 Task/Oracle 框架。
        audit_tests = existing_tests(["test_*.py"], "eval/phase5c")
    if protected != protected_hashes():
        raise RuntimeError("Production 或冻结输入发生变化，拒绝生成成功报告")
    historical = historical_evidence()
    rows = [r, {"parallel":parallel,"safety":s,"tests":safety_tests},
            {"workspace_memory":memory,"context_record_continuation":continuation,"context_record_contracts":contracts,"tests":memory_tests},
            {"compaction":compaction,"tests":compression_tests,"phase5a":phase5a},
            {"tests":audit_tests,"historical":historical}]
    for i,row in enumerate(rows,1):
        write(destination/f"module-{i}-raw.json",row)
    write(destination/"checkpoint-tests.json",checkpoints)
    ratios = [c["compression_ratio"] for c in compaction]
    metrics = [
        {"recovery_scenarios":len(r),"recovery_cases_total":len(r),"recovery_cases_passed":sum(c["oracle_pass"] for c in r),
         "window_case_counts":{c["scenario"]:1 for c in r},
         "transcript_consistency_passed":sum(c["transcript_consistent"] for c in r),
         "checkpoint_consistency_passed":checkpoints["passed"],"checkpoint_test_total":checkpoints["total"],
         "unexpected_duplicate_dispatch_count":sum(c["unexpected_duplicate_dispatch_count"] for c in r),"recovery_latency_ms":None},
        {"parallel_cases":len(parallel),"parallel_output_equivalence":all(p["output_equivalence"] for p in parallel),
         "speedup_ratio_range":[min(p["speedup_ratio"] for p in parallel),max(p["speedup_ratio"] for p in parallel)],
         "safety_boundary_categories":["permission","argument_validation","concurrent_pairing","execution_failure_timeout","duplicate_replay","large_output_reference"],
         "safety_scenario_count":len(s),"safety_cases_total":len(s)+safety_tests["total"],
         "safety_cases_passed":sum(c["oracle_pass"] for c in s)+safety_tests["passed"],
         **{key:sum(c[key] for c in s) for key in ("unauthorized_dispatch_count","call_result_mismatch_count","unexpected_duplicate_dispatch_count")},
         "counter_scope":"Phase2 ledger counters; extra unit tests contribute pass counts, not fabricated ledger observations"},
        {"explicit_write_success_rate":mean(m["explicit_write_success"] for m in memory),
         "cross_session_recall_at_4":mean(m["recall_at_4"] for m in memory),"fact_accuracy":mean(m["fact_accuracy"] for m in memory),
         "workspace_isolation_violations":sum(m["workspace_isolation_violations"] for m in memory),
         "memory_cases":len(memory), "metadata_update_passed":sum(m["metadata_update_same_identity"] for m in memory),
         "new_session_context_injection_rate":mean(m["new_session_context_injection"] for m in memory),
         "stale_memory_recall_rate":None,
         "context_record":{"cases":1,"qualified_record_production_rate":int(continuation["qualified"]),
             "active_record_recall_rate":int(continuation["checks"]["active_record_recalled"]),
             "required_fact_retention_rate":int(continuation["checks"]["required_fact_accessible_after_compaction"]),
             "stale_record_misuse_count":int(not continuation["checks"]["stale_record_excluded"]),
             "continuation_oracle_pass_rate":int(continuation["oracle_pass"]),
             "scope":"one fixed-candidate mechanism fixture; not autonomous extraction or behavioral success",
             "external_source_auto_invalidation":"UNAVAILABLE"},
         "context_record_regression_passed":memory_tests["passed"],
         "real_model_use":"PENDING_REAL_MODEL_VALIDATION"},
        {"scenario_count":len(compaction),"mean_compression_ratio":mean(ratios),"min_compression_ratio":min(ratios),"max_compression_ratio":max(ratios),
         "integrity_pass_rate":mean(c["oracle_pass"] for c in compaction),"post_compaction_task_success_rate":None,
         "emergency_compaction_recovery":[c for c in compression_tests["cases"] if "prompt_too_long" in c["test_id"]],
         "historical_real_model":historical["phase5b"],"token_scope":"single compaction estimated request ratio; task total provider delta separate"},
        {"framework_tests":audit_tests["total"],"framework_tests_passed":audit_tests["passed"],
         "reproducibility_checks":audit_tests["cases"],"historical":historical["phase5c"]},
    ]
    limits = [
        "FakeModel fault injection and local restore; no external exactly-once or real process-kill claim.",
        "Fixed sleep read-only fixture microbenchmark; excludes Provider, includes scheduler overhead; P95 small sample only.",
        "Explicit remember and lexical recall only; no semantic replacement/revocation API; external-source invalidation UNVERIFIED.",
        "Fixed summary deterministic structural invariants; not autonomous semantic preservation or real task success.",
        "Six implemented tasks; larger candidate inventory UNVERIFIED; historical command/output gaps and D2 contract caveat retained.",
    ]
    claims = [
        f"以 FakeModel 故障注入覆盖 4 类关键崩溃窗口，{metrics[0]['recovery_cases_passed']}/4 项恢复 oracle 通过；验证结果补回和不确定副作用保守恢复，不宣称外部 exactly-once。",
        f"以固定只读工具 workload 完成 {len(parallel)} 组串并行对照，每组 7 次测量，观察加速比 {metrics[1]['speedup_ratio_range'][0]:.2f}–{metrics[1]['speedup_ratio_range'][1]:.2f} 倍；限于 fixture 调度基准。",
        f"通过现有 remember 管线完成 {len(memory)} 项显式写入与跨 Session 召回验证，精确事实命中 {sum(m['fact_accuracy'] for m in memory)}/{len(memory)}，跨工作区泄漏 {metrics[2]['workspace_isolation_violations']}；属于确定性持久化与检索证据。",
        f"覆盖 6 类固定上下文场景，结构完整性 {sum(c['oracle_pass'] for c in compaction)}/6，平均单次估算输入缩减 {metrics[3]['mean_compression_ratio']*100:.1f}%；固定摘要证据，不代表真实模型累计成本。",
        f"复用 Phase5C 六个 TaskSpecification 与外部 Oracle，以 {audit_tests['total']} 项离线框架测试审计 fixture、诊断及统计；通过 {audit_tests['passed']} 项，保留 Provider/usage/qualification 边界。",
    ]
    passed = (all(c["oracle_pass"] and c["transcript_consistent"] for c in r)
        and all(p["call_result_pairing_correct"] and all(t["concurrency_limit_respected"] for t in p["samples"]) for p in parallel)
        and all(c["oracle_pass"] for c in s) and continuation["qualified"] and continuation["oracle_pass"]
        and all(m["explicit_write_success"] and m["recall_at_4"] and m["new_session_context_injection"]
                and not m["workspace_isolation_violations"] for m in memory)
        and all(c["oracle_pass"] for c in compaction) and contracts["failed"] == 0
        and phase5a["passed"] == phase5a["scenario_count"]
        and all(t["total"] > 0 and t["passed"] == t["total"] for t in (checkpoints,safety_tests,memory_tests,compression_tests,audit_tests)))
    report = {"benchmark":"modules-v1.0","status":"PASS" if passed else "FAIL","evidence_type":"DETERMINISTIC_ONLY", "created_at":datetime.now(timezone.utc).isoformat(),
        "specification":{"path":str(SPEC),"sha256":sha256(SPEC.read_bytes()).hexdigest()},
        "source_hashes":{str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob("*.py")},
        "protected_input_hashes":protected,"production_and_frozen_inputs_unchanged":True,
        "modules":[{"module":i+1,"specification":str(SPEC),"fixture_or_scenario":str(Path(__file__).with_name("scenarios.py")),
                    "oracle":"existing named oracle plus frozen SPEC invariants","raw_artifact":f"module-{i+1}-raw.json",
                    "metrics":m,"limitations":limits[i],"resume_ready_claim":claims[i]} for i,m in enumerate(metrics)],
        "pending_real_model_validation":["explicit remember → new Session constraint application + external oracle",
            "real StageA → qualified ContextRecord → StageB ON/OFF", "real compaction workload → external task oracle"],
        "real_provider_requests":0}
    write(destination/"report.json",report)
    (destination/"report.md").write_text("# Module Capability Benchmark V1\n\n"+"\n\n".join(
        f"## Module {i+1}\n\n{claims[i]}\n\n限制：{limits[i]}\n\n原始证据：module-{i+1}-raw.json" for i in range(5)),encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir",type=Path,required=True)
    args = parser.parse_args()
    result = run(args.output_dir)
    print(args.output_dir/"report.json")
    raise SystemExit(0 if result["status"] == "PASS" else 1)
