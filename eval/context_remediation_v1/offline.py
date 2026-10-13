"""生成离线工程报告和六样本真实模型预注册；没有网络执行入口。"""
import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import subprocess
import unittest
from urllib.parse import urlsplit

from hexgent.context import ContextLimits, ContextManager
from hexgent.context.context_compaction import _select_cut_index, COMPACTION_INSTRUCTIONS
from hexgent.context.key_fact_extractor import EXTRACTION_INSTRUCTIONS, EXTRACTION_PROMPT_VERSION
from hexgent.context.key_fact_snapshot import json_value
from hexgent.utils.token_estimator import estimate_request_tokens
from tests.test_key_fact_remediation import RemediationTests


ROOT = Path(__file__).resolve().parents[2]
FINAL = ROOT / "eval/context_management/c2/reviews/final-20261012T024339991178Z"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


def fingerprints():
    """只读枚举冻结范围；排除解释器缓存，绝不运行旧 Benchmark。"""
    scopes = [ROOT/"eval/context_management", ROOT/"eval/memory_m1/runs", ROOT/"eval/memory_m2/runs"]
    return {str(path.relative_to(ROOT)): sha256(path.read_bytes()).hexdigest()
            for scope in scopes if scope.exists() for path in sorted(scope.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}


def cases():
    legacy = ROOT/"eval/context_management/c2_prep_2/long_fixtures/L3.json"
    oracle = ROOT/"eval/context_management/c2_prep_3/oracle_v2/L3.json"
    fixture, gold = json.loads(legacy.read_text()), json.loads(oracle.read_text())
    required = [f for f in gold["facts"] if f["channel"] == "SUMMARY_REQUIRED"]
    rows = [dict(id="R-L3", category="L3 原始历史回归", rounds=fixture["rounds"], final_query=fixture["final_query"],
                 required_facts=required, source_fixture=str(legacy.relative_to(ROOT)),
                 source_fixture_sha256=sha256(legacy.read_bytes()).hexdigest(),
                 source_oracle_sha256=sha256(oracle.read_bytes()).hexdigest())]
    specifications = [
        ("R-STATE", "独立当前任务状态", ["工单 CEDAR-84", "数据核对已完成", "等待导师验收", "验收后提交归档"], "继续当前工单"),
        ("R-UPDATE", "状态更新与未解决冲突", ["早期端口 48123 已替换为 59261", "当前端口 59261", "容量两份资料矛盾，尚未确认"], "汇报当前配置及不确定项"),
        ("R-DEPENDENCY", "条件依赖", ["只有负责人签字后才能生成交接包", "目前签字未完成", "签字后先校验哈希，再生成交接包"], "继续交接工作"),
        ("R-SPARSE", "长工具输出中的稀疏关键事实", ["审查范围仅限 ORBIT-93", "唯一未决依赖是证书更换审批", "审批后再生成报告"], "继续当前审查"),
        ("R-MULTI", "多次 Compaction", ["批次 PINE-26", "当前审阅待确认", "确认后归档"], "继续审阅任务"),
    ]
    for identity, category, facts, query in specifications:
        rounds = []
        for i, fact in enumerate(facts):
            call_id = f"{identity}:read:{i}"
            # 冻结的业务资料噪声不包含隐藏答案，关键事实只有此处的明确资料来源。
            background = ("历史资料说明：记录校对范围、负责人及证据编号；完成阅读不等于验收通过。\n"*120)
            text = background + "资料当前结论：" + fact + "\n" + background
            if identity == "R-SPARSE":
                text = background*2 + "资料当前结论："+fact+"\n"+background*2
            rounds.append(dict(query=f"阅读资料 {i+1}", messages=[
                dict(type="function_call", call_id=call_id, name="read_file", arguments=json.dumps({"path":f"source-{i+1}.txt"})),
                dict(type="function_call_output", call_id=call_id, output=text)]))
        row = dict(id=identity, category=category, rounds=rounds, final_query=query,
                   required_facts=[dict(fact_id=f"{identity}:fact:{i}", quote=fact,
                                        canonical_fact=fact, source_call_id=f"{identity}:read:{i}")
                                   for i, fact in enumerate(facts)])
        if identity == "R-MULTI":
            row["second_stage"] = dict(new_evidence="导师已确认审阅，可以归档", final_query="按新状态继续",
                                       new_evidence_source_kind="function_call_output",
                                       required_disposition="UPDATED", old_oracle_fact_id="R-MULTI:fact:1",
                                       old_snapshot_fact_selector={"canonical_fact": facts[1]},
                                       expected_fact="审阅已确认，可以归档", unchanged_facts=[facts[0], facts[2]])
        rows.append(row)
    return rows


def preflight(output):
    import os
    from hexgent.client import get_model
    try:
        configured_model = get_model()
    except (KeyError, ValueError):
        configured_model = None
    provider_host = urlsplit(os.environ.get("OPENAI_BASE_URL", "")).hostname
    limits = ContextLimits()
    budgets, manifests = [], []
    for row in cases():
        manager = ContextManager()
        for round_ in row["rounds"]:
            manager.append_user_query(round_["query"])
            manager.append_messages(round_["messages"], completes_round=True)
        manager.append_user_query(row["final_query"])
        recent_tokens = 1 if row["id"] == "R-MULTI" else limits.recent_context_tokens
        row["execution_contract"] = dict(force_compaction=True, recent_context_tokens=recent_tokens,
                                          reactive_recent_context_tokens=min(recent_tokens, limits.reactive_recent_context_tokens))
        cut = _select_cut_index(manager, recent_tokens)
        payload = dict(history_prefix=[s for s in manager.prefix_sources(cut) if s["message_id"] not in manager.intent_source_ids()], previous_snapshot=None,
                       intent_reference=dict(latest_query=row["final_query"], plan=None),
                       as_of_message_id=manager.message_ids[cut-1])
        extract = dict(model="gpt-6.1-sol", instructions=EXTRACTION_INSTRUCTIONS,
                       input=[dict(role="user", content=json.dumps(payload, ensure_ascii=False))],
                       max_output_tokens=limits.fact_extraction_output_tokens, stream=True)
        summary = dict(model="gpt-6.1-sol", instructions=COMPACTION_INSTRUCTIONS,
                       input=manager.materialize_messages(manager.active_messages[:cut]),
                       max_output_tokens=limits.summary_output_tokens, stream=True)
        tokens = estimate_request_tokens(extract["instructions"], extract["input"], [])
        summary_tokens = estimate_request_tokens(summary["instructions"], summary["input"], [])
        prefix_calls = {s["message"].get("call_id") for s in payload["history_prefix"]
                        if s["message"].get("type") == "function_call_output"}
        tail_calls = {s.get("call_id") for s in manager.materialize_messages(manager.active_messages[cut:])
                      if isinstance(s, dict) and s.get("type") == "function_call_output"}
        for fact in row["required_facts"]:
            fact["expected_channel"] = "SNAPSHOT" if fact["source_call_id"] in prefix_calls else "RECENT_TAIL" if fact["source_call_id"] in tail_calls else "UNRESOLVED"
        path = output/"samples"/(row["id"]+".json")
        save(path, row)
        save(output/"candidate-requests"/(row["id"]+".json"), dict(extraction=extract, summary=summary,
            history_cutpoint=payload["as_of_message_id"], recent_tail=manager.materialize_messages(manager.active_messages[cut:])))
        cycles = 2 if row["id"] == "R-MULTI" else 1
        # 第二次输入尚依赖第一次 Snapshot；预算使用辅助容量上限，不伪造已确定请求。
        first_ceiling = tokens + limits.fact_extraction_output_tokens + 2*(summary_tokens+limits.summary_output_tokens)
        second_ceiling = 3*limits.auxiliary_context_window_tokens if cycles == 2 else 0
        budgets.append(dict(id=row["id"], extractor_estimated_input_tokens=tokens,
                            summary_estimated_input_tokens=summary_tokens,
                            first_cycle_estimated_token_ceiling=first_ceiling,
                            second_cycle_conservative_capacity_ceiling=second_ceiling,
                            provider_actual_usage="NOT_AVAILABLE_NOT_RUN",
                            max_extractor_requests=cycles, max_summary_requests=cycles*limits.max_compaction_attempts,
                            max_main_provider_requests=0,
                            auxiliary_request_fits=all(t+o+limits.safety_margin_tokens <= limits.auxiliary_context_window_tokens
                                for t,o in [(tokens,limits.fact_extraction_output_tokens),(summary_tokens,limits.summary_output_tokens)])))
        manifests.append(dict(id=row["id"], fixture=str(path.relative_to(output)), sha256=sha256(path.read_bytes()).hexdigest(),
                              oracle="预注册 quote/entity/value/status/dependency 人工语义复核；来源有效性不能代替质量",
                              required_facts=row["required_facts"], input_budget_ceiling=budgets[-1]))
    result = dict(status="OFFLINE_PREFLIGHT_PASS" if all(b["auxiliary_request_fits"] for b in budgets) else "OFFLINE_PREFLIGHT_BLOCKED",
                  model="gpt-6.1-sol", network_calls=0, paid_calls=0, provider_connectivity="NOT_TESTED",
                  configured_model=configured_model, provider_host=provider_host,
                  model_configuration_matches=configured_model=="gpt-6.1-sol",
                  route_capacity="REQUIRES_RECONFIRMATION_BEFORE_PAID_RUN", samples=manifests, ledger=budgets,
                  maximum_auxiliary_calls=sum(b["max_extractor_requests"]+b["max_summary_requests"] for b in budgets),
                  minimum_auxiliary_calls=14, main_execution="FAKE_PRODUCTION_LOOP_NO_PAID_MAIN",
                  compaction_cycles=7,
                  token_ceiling=sum(b["first_cycle_estimated_token_ceiling"]+b["second_cycle_conservative_capacity_ceiling"] for b in budgets),
                  failure_budget="保留每个失败；Extractor 不重试；Summary 最多两候选；无替换样本",
                  stop_conditions=["来源/切点/继承错误", "独立或总预算绕过", "提交/恢复不一致", "缺少请求响应审计", "Usage 无法对账", "系统性 Provider 故障"],
                  amortized_benefit="AMORTIZED_BENEFIT_NOT_EVALUATED", monetary_cost="NOT_EVALUATED",
                  extraction_prompt_version=EXTRACTION_PROMPT_VERSION)
    result["runtime_configuration"] = dict(context_extraction_enabled=False, memory_retrieval_enabled=False,
        main_model="FAKE", auxiliary_model="gpt-6.1-sol", preserve_production_permission_pipeline=True,
        budget_guard="PRODUCTION", hidden_sdk_retries=0)
    save(output/"preflight.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--regressions", action="store_true", help="执行安全 Runtime/Memory/Recovery 回归，不运行 Benchmark")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise RuntimeError("输出目录已存在，禁止覆盖历史证据")
    output.mkdir(parents=True)
    before = fingerprints()
    save(output/"frozen-before.json", before)
    RemediationTests.evidence = []
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RemediationTests)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    (output/"test-output.txt").write_text(stream.getvalue(), encoding="utf-8")
    save(output/"engineering-evidence.json", RemediationTests.evidence)
    prep = preflight(output)
    regressions = []
    if args.regressions:
        import sys
        commands = [
            [sys.executable,"-m","unittest","discover","-s","tests","-q"],
            [sys.executable,"-m","unittest","eval.test_memory_m1","eval.test_memory_m2_real",
             "eval.test_memory_hardening","eval.phase3.test_recovery_contract",
             "eval.phase4.test_context_memory_contract","eval.recovery_hardening.test_hardening",
             "eval.memory_m3.test_real_smoke","-q"],
            [sys.executable,"-m","unittest","tests.test_frozen_context_oracle_compatibility","-q"],
            ["git","diff","--check"],
            [sys.executable,"-m","unittest","eval.test_memory_m1","eval.test_memory_m2_real",
             "eval.test_memory_hardening","eval.phase3.test_recovery_contract",
             "eval.recovery_hardening.test_hardening","eval.memory_m3.test_real_smoke","-q"],
        ]
        for index, command in enumerate(commands):
            execution = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            (output/f"regression-{index}.txt").write_text(execution.stdout+execution.stderr, encoding="utf-8")
            regressions.append(dict(command=command, exit_code=execution.returncode,
                                    output=f"regression-{index}.txt",
                                    classification="FROZEN_FIXTURE_OR_API_DRIFT" if index==1 and execution.returncode else
                                                   "PASS" if not execution.returncode else "FAIL"))
        save(output/"regressions.json", regressions)
    after = fingerprints()
    save(output/"frozen-after.json", after)
    source = {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/"hexgent").rglob("*.py"))}
    save(output/"production-after.json", source)
    tracked = subprocess.check_output(["git","ls-files","hexgent"], cwd=ROOT, text=True).splitlines()
    original = {p: sha256(subprocess.check_output(["git","show",f"HEAD:{p}"], cwd=ROOT)).hexdigest() for p in tracked if p.endswith(".py")}
    save(output/"production-head-before.json", original)
    old_manifest = json.loads((FINAL/"freeze_manifest.json").read_text())
    old_ok = all(sha256((FINAL/name).read_bytes()).hexdigest()==digest for name,digest in old_manifest["files"].items())
    summary = dict(engineering_tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                   frozen_unchanged=before==after, final_review_manifest_unchanged=old_ok,
                   offline_preflight=prep["status"], paid_calls=0,
                   status="ENGINEERING_PASS_REQUIRES_REGRESSION_REVIEW" if result.wasSuccessful() and before==after and old_ok else "BLOCKED",
                   known_limitations=["Fake 只证明工程管线，不证明 Extractor 语义质量", "不承诺 fsync", "普通 JSONL 追加未统一加固"])
    summary["regressions"] = regressions
    save(output/"result.json", summary)
    print(json.dumps(dict(output=str(output), **summary), ensure_ascii=False))


if __name__ == "__main__":
    main()
