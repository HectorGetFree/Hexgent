"""复用正式 Runtime 的确定性模块场景；没有真实模型入口。"""

from collections import Counter
from dataclasses import asdict
import json
from math import ceil
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from threading import Lock
from time import perf_counter, sleep
from types import SimpleNamespace
from unittest.mock import patch

from eval.support.agent_contracts import FakeModel, text_response, tool_call
from eval.phase3.scenarios import v1_scenarios as recovery_scenarios
from eval.phase2.scenarios import v1_scenarios as safety_scenarios
from eval.phase2.oracle import check_all_contracts
from eval.phase5.scenarios import BenchmarkResponses, _base_config
from hexgent.agent_engine import AgentEngine
from hexgent.context import ContextManager, ContextLimits
from hexgent.context.context_compaction import compact_context_if_needed
from hexgent.memory import MemoryStore, MemoryRetriever, MemoryRetrievalLimits
from hexgent.plan import InMemoryPlanStore, Plan, PlanStep
from hexgent.session import Session, SessionRestorer
from hexgent.memory import ContextRecordInput
from hexgent.memory.context_retrieval import ContextRecordRetriever, format_retrieved_context
from hexgent.memory.context_record_extraction import ContextExtractionLimits
from hexgent.loop.extract_context_records import extract_context_records
from hexgent.run_control import InterruptController
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_registry import TOOL_REGISTRY, RegisteredTool
from hexgent.tool.tool_validation import TOOL_SCHEMAS
from hexgent.tool.tool_result import ToolHandlerResult
from hexgent.tool.tool_scheduler import execute_tool_batch, ToolParallelLimits


def drain(generator):
    """收集真实 generator 事件以及 StopIteration 中的正式返回值。"""
    events = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stopped:
            return events, stopped.value


def recovery():
    """复用四个既有 crash oracle，并保存可重查的 durable 原始事实。"""
    rows = []
    for spec in recovery_scenarios():
        observation = spec.execute()
        error = None
        try:
            spec.verify(observation)
        except AssertionError as failure:
            error = str(failure) or "既有恢复 oracle 失败"
        pre, final = observation.pre_crash_records, observation.final_records
        sequence = [r["sequence"] for r in final]
        pre_dispatch = sum(r["type"] == "tool_execution_dispatched" for r in pre)
        final_dispatch = sum(r["type"] == "tool_execution_dispatched" for r in final)
        rows.append({
            "scenario": spec.name, "crash_point": spec.crash_point, "invariant": spec.invariant,
            "oracle_pass": error is None, "error": error,
            "transcript_consistent": sequence == sorted(set(sequence)) and len(observation.restore_outputs) == 1
                and observation.restore_outputs == observation.repeated_restore_outputs,
            "unexpected_duplicate_dispatch_count": max(0, final_dispatch - pre_dispatch),
            "pre_crash_records": pre, "final_records": final,
            "restore_outputs": observation.restore_outputs,
            "repeated_restore_outputs": observation.repeated_restore_outputs,
            "external_side_effect_exists": observation.external_side_effect_exists,
            "recovery_actions": observation.recovery_actions,
        })
    return rows


def safety():
    """保留 Phase2 原有 oracle，并从实际 ledger 计数违规。"""
    rows = []
    for spec in safety_scenarios():
        obs = spec.execute()
        violations = [asdict(v) for v in check_all_contracts(obs)]
        try:
            spec.verify(obs)
        except AssertionError as failure:
            violations.append({"category": "scenario", "message": str(failure)})
        records = obs.transcript_records
        dispatches = Counter(r["payload"]["call_id"] for r in records if r["type"] == "tool_execution_dispatched")
        allows = {r["payload"]["call_id"] for r in records if r["type"] == "permission_resolved" and r["payload"].get("behavior") == "allow"}
        rows.append({
            "scenario": spec.name, "oracle_pass": not violations, "violations": violations,
            "unauthorized_dispatch_count": sum(n for c, n in dispatches.items() if c not in allows),
            "unexpected_duplicate_dispatch_count": sum(max(0, n-1) for n in dispatches.values()),
            "call_result_mismatch_count": sum(v["category"] in {"tool_pairing", "context_pairing"} for v in violations),
            "transcript": records,
        })
    return rows


def percentile(values, p):
    """nearest-rank，仅用于固定微基准样本描述。"""
    return sorted(values)[max(0, ceil(len(values)*p)-1)]


def parallel_case(count, delay_ms, workers, *, staggered=False, repeats=7):
    """仅切换 Production scheduler 的 enabled，计时排除 FakeModel/Provider。"""
    samples = {"serial": [], "parallel": []}
    traces = []
    with TemporaryDirectory() as directory:
        root = Path(directory)
        for i in range(count):
            (root / f"{i}.txt").write_text(f"事实-{i}", encoding="utf-8")
        model = FakeModel([text_response(output=[
            tool_call(f"call-{i}", "module_fixture_read", json.dumps({"index": i})) for i in range(count)
        ])])
        response = list(model.create(model="fake", input=[], stream=True))[-1].response
        calls = response.output
        for iteration in range(repeats+1):
            paired = {}
            for variant in (("serial", "parallel") if iteration % 2 == 0 else ("parallel", "serial")):
                active = 0
                peak = 0
                completion = []
                lock = Lock()

                def fixture_read(index):
                    nonlocal active, peak
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    sleep(delay_ms/1000 * (count-index if staggered else 1))
                    value = (root / f"{index}.txt").read_text(encoding="utf-8")
                    with lock:
                        active -= 1
                        completion.append(f"call-{index}")
                    return ToolHandlerResult.success(value)

                registered = RegisteredTool(fixture_read, concurrency_safe=True, permission_capability="workspace_read")
                context = ToolExecutionContext(InMemoryPlanStore(), root)
                schema = {"type": "object", "properties": {"index": {"type": "integer"}}, "required": ["index"], "additionalProperties": False}
                with patch.dict(TOOL_REGISTRY, {"module_fixture_read": registered}), patch.dict(TOOL_SCHEMAS, {"module_fixture_read": schema}):
                    start = perf_counter()
                    _, batch = drain(execute_tool_batch(
                        calls, context=context, user_confirmation=lambda *_: "allow",
                        remaining_tool_calls=None, deadline=None,
                        parallel_limits=ToolParallelLimits(enabled=variant == "parallel", max_workers=workers),
                    ))
                    elapsed = (perf_counter()-start)*1000
                expected = [{"type": "function_call_output", "call_id": f"call-{i}", "output": f"事实-{i}"} for i in range(count)]
                paired[variant] = batch.outputs
                if iteration:
                    samples[variant].append(elapsed)
                    traces.append({"iteration": iteration, "variant": variant, "latency_ms": elapsed,
                                   "completion_order": completion, "peak_concurrency": peak,
                                   "outputs": batch.outputs, "pairing_correct": batch.outputs == expected,
                                   "concurrency_limit_respected": peak <= (workers if variant == "parallel" else 1)})
            if paired["serial"] != paired["parallel"]:
                raise AssertionError("串并行输出不等价")
    s, p = median(samples["serial"]), median(samples["parallel"])
    return {"count": count, "delay_ms": delay_ms, "workers": workers, "staggered": staggered,
            "warmup_pairs": 1, "repeated_pairs": repeats, "samples": traces,
            "serial_latency_ms": s, "parallel_latency_ms": p, "speedup_ratio": s/p,
            "latency_reduction_pct": (s-p)/s*100,
            "p50_ms": {k:median(v) for k,v in samples.items()},
            "p95_ms": {k:percentile(v, .95) for k,v in samples.items()},
            "output_equivalence": True, "call_result_pairing_correct": all(t["pairing_correct"] for t in traces)}


def workspace_memory():
    """真实 remember 管线写入，销毁 Session 后重新加载并检索；模型响应固定。"""
    rows = []
    for index, fact in enumerate(("发布服务使用蓝绿部署", "代码注释统一使用中文", "审核结果必须保留来源")):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            engine = AgentEngine.create(_base_config(), root)
            session_a = engine.session.store.session_id
            model = BenchmarkResponses([
                text_response(output=[tool_call("remember-1", "remember", json.dumps({"content": fact, "category": "project", "keywords": [fact]}, ensure_ascii=False))]),
                text_response("已记住"),
            ])
            events = list(engine.run(f"请长期记住：{fact}", client=model, user_confirmation=lambda *_:"allow"))
            transcript = engine.session.store.read_transcript()
            del engine
            store = MemoryStore(root)
            identity = store.items[0].memory_id
            for noise in range(10):
                store.upsert(f"无关背景条目编号 {noise}", "project", keywords=["其他主题"], source="explicit")
            update = store.upsert(fact, "project", keywords=["补充关键词"], source="explicit")
            second = AgentEngine.create(_base_config(memory_retrieval=MemoryRetrievalLimits(enabled=True)), root)
            query = ("发布服务有什么约定？", "代码注释有什么约定？", "审核结果有什么约定？")[index]
            second_model = BenchmarkResponses([text_response("已处理")])
            second_events = list(second.run(query, client=second_model, user_confirmation=lambda *_: "allow"))
            recalled = MemoryRetriever().retrieve(query, second.memory_store.items)
            other = MemoryRetriever().retrieve(fact, MemoryStore(root / "isolated").items)
            facts = [item.content for item in recalled if item.memory_id == identity]
            rows.append({"scenario": f"explicit_cross_session_{index}", "source": "remember tool → production pipeline → disk → new Session",
                "session_a": session_a, "session_b": second.session.store.session_id,
                "explicit_write_success": events[-1].result.status == "completed" and any(r["type"] == "tool_execution_finished" and r["payload"].get("status") == "success" for r in transcript),
                "recall_at_4": bool(facts), "fact_accuracy": facts == [fact],
                "new_session_context_injection": fact in second_model.calls[0]["instructions"],
                "session_b_completed": second_events[-1].result.status == "completed",
                "session_b_query": query, "session_b_requests": second_model.calls,
                "workspace_isolation_violations": len(other),
                "metadata_update_same_identity": update.action == "updated" and update.item.memory_id == identity,
                "memory_items": [asdict(item) for item in store.items],
                "recalled": [asdict(item) for item in recalled], "transcript": transcript})
    return rows


def compaction_case(name):
    """固定摘要下测量正式 estimator 的单次压缩率与 durable 恢复不变量。"""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        context = ContextManager()
        session = Session.persistent(root)
        query = context.append_user_query("读取资料并继续工作")
        session.record_user_query(query)
        rounds = 4 if name in {"multi_read", "complete_rounds"} else 2
        expected_ids = []
        for i in range(rounds):
            if name == "long_history":
                messages = [{"role": "assistant", "content": "已观察到 FACT=ALPHA。历史信息。"*180}]
            else:
                expected_ids.append(f"read-{i}")
                messages = [tool_call(f"read-{i}", "read_file", '{"path":"fixture.txt"}'),
                            {"type": "function_call_output", "call_id": f"read-{i}", "output": "FACT=ALPHA\n" + "只读资料。"*(800 if name == "large_result" else 200)}]
            context.append_messages(messages, completes_round=True)
            session.record_context_messages(messages, completes_round=True, source="tool_results" if expected_ids else "model_response")
        latest = context.append_user_query("继续完成当前任务")
        session.record_user_query(latest)
        plan = Plan("继续任务", "in_progress", [PlanStep("验证", "正在验证", "in_progress", "step-1")]) if name == "active_plan" else None
        if plan:
            session.record_plan_state(plan, action="created")
        client = BenchmarkResponses([], summary="FACT=ALPHA；保留当前目标，继续验证。")
        before_messages = context.model_messages()
        limits = ContextLimits(context_window_tokens=2000 if name == "near_budget" else 100000,
                               reserved_output_tokens=100, safety_margin_tokens=100,
                               proactive_threshold_tokens=100, recent_context_tokens=100,
                               min_compaction_savings_tokens=1, min_compaction_savings_ratio=.01)
        _, outcome = drain(compact_context_if_needed(context, client, "fake", "系统提示", [], limits))
        if outcome.boundary:
            session.record_compaction(outcome.boundary)
        checkpoint = session.create_checkpoint(context_manager=context, plan=plan,
            reason="context_compaction_completed", loop_state=None, run_status="continuable")
        restored = SessionRestorer(root).restore(session.store.session_id)
        summary_inputs = [m for req in client.calls for m in req["input"]]
        def cid(m):
            return m.get("call_id") if isinstance(m, dict) else getattr(m,"call_id",None)
        counts = Counter(cid(m) for m in summary_inputs if cid(m))
        integrity = all(counts[c] == 2 for c in expected_ids)
        after = context.model_messages()
        checks = {"required_fact_retention": "FACT=ALPHA" in str(after),
                  "tool_round_integrity": integrity,
                  "latest_query_integrity": restored.context_manager.user_queries[-1].content == latest.content,
                  "active_plan_integrity": restored.tool_context.plan_store.get_plan() == plan,
                  "compaction_commit_success": outcome.boundary is not None and restored.context_manager.model_messages() == after,
                  "reduction_positive": outcome.after_tokens < outcome.before_tokens}
        return {"scenario": name, "input_tokens_before": outcome.before_tokens, "input_tokens_after": outcome.after_tokens,
                "compression_ratio": (outcome.before_tokens-outcome.after_tokens)/outcome.before_tokens,
                "checks": checks, "oracle_pass": all(checks.values()),
                "before_messages": before_messages, "after_messages": after,
                "summary_requests": client.calls, "checkpoint": asdict(checkpoint),
                "transcript": session.store.read_transcript(), "token_kind": "PRODUCTION_ESTIMATED_REQUEST_TOKENS"}


def context_record_continuation():
    """固定候选通过正式来源校验，再验证 supersede、压缩和恢复后的注入。"""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        session = Session.persistent(root)
        context = ContextManager()
        old_fact, new_fact = "服务部署端口是 8080。", "服务部署端口是 8098。"
        session.record_user_query(context.append_user_query(old_fact))
        source = session.store.next_sequence - 1
        messages = [{"role": "assistant", "content": "已记录观察。" * 600}]
        context.append_messages(messages, completes_round=True)
        session.record_context_messages(messages, completes_round=True, source="model_response")
        candidate = json.dumps({"records": [{"content": old_fact, "category": "fact",
            "source_sequences": [source], "supersedes": [], "evidence_quote": old_fact}]}, ensure_ascii=False)
        response = SimpleNamespace(output_text=candidate, usage=None)
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: iter([
            SimpleNamespace(type="response.completed", response=response)])))
        with patch("hexgent.loop.extract_context_records.get_model", return_value="fixture-model"):
            extract_context_records(session, client, ContextExtractionLimits(), InterruptController(), force=True)
        produced = session.context_records.active_records
        if not produced:
            return {"qualified": False, "oracle_pass": False, "transcript": session.store.read_transcript()}
        old_id = produced[0].record_id
        session.record_user_query(context.append_user_query(new_fact))
        new_source = session.store.next_sequence - 1
        session.upsert_context_records([ContextRecordInput(new_fact, "fact", (new_source,), (old_id,))])
        context.append_messages(messages, completes_round=True)
        session.record_context_messages(messages, completes_round=True, source="model_response")
        session.record_user_query(context.append_user_query("服务部署端口是什么？"))
        _, outcome = drain(compact_context_if_needed(context, BenchmarkResponses([], summary="继续处理服务部署问题。"),
            "fixture-model", "系统提示", [], ContextLimits(proactive_threshold_tokens=100, recent_context_tokens=100,
            min_compaction_savings_tokens=1, min_compaction_savings_ratio=.01)))
        if outcome.boundary:
            session.record_compaction(outcome.boundary)
        session.create_checkpoint(context_manager=context, plan=None, loop_state=None, reason="context_compaction_completed", run_status="continuable")
        restored = SessionRestorer(root).restore(session.store.session_id)
        recalled = ContextRecordRetriever().retrieve("服务部署端口", restored.session.context_records.active_records)
        injected = format_retrieved_context(recalled)
        checks = {"active_record_recalled": new_fact in injected,
                  "required_fact_accessible_after_compaction": new_fact in injected and outcome.boundary is not None,
                  "stale_record_excluded": old_fact not in injected,
                  "provenance_valid": all(s in {r["sequence"] for r in session.store.read_transcript()}
                                           for record in recalled for s in record.source_sequences)}
        return {"qualified": produced[0].content == old_fact and produced[0].source_sequences == (source,),
                "checks": checks, "oracle_pass": all(checks.values()), "injected_context": injected,
                "records": [asdict(r) for r in restored.session.context_records.records],
                "transcript": session.store.read_transcript(),
                "evidence_scope": "fixed candidate extraction; explicit supersession; deterministic continuation, not autonomous model use",
                "external_source_auto_invalidation": "UNAVAILABLE: no implicit file-change invalidation contract"}
