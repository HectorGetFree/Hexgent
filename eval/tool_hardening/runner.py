"""使用真实源码、正式调度器和 FakeModel 验证工具性能与结果交付。"""

import argparse
from dataclasses import replace
from hashlib import sha256
import json
from math import ceil
from pathlib import Path
from statistics import mean, median
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import re
import threading
from time import perf_counter
from unittest.mock import patch

from eval.modules_v1.scenarios import drain
from eval.phase5.scenarios import _base_config
from eval.support.agent_contracts import run_trace, text_response, tool_call
from hexgent.plan import InMemoryPlanStore
from hexgent.session import Session
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_registry import TOOL_REGISTRY
from hexgent.tool.tool_registry import RegisteredTool
from hexgent.tool.tool_result import ToolHandlerResult
from hexgent.tool.tool_validation import TOOL_SCHEMAS
from hexgent.tool.tool_scheduler import execute_tool_batch, ToolParallelLimits

ROOT = Path(__file__).resolve().parents[2]
TASKS = {
    "tool_pipeline_audit": ["hexgent/tool/tool_scheduler.py", "hexgent/tool/tool_execution.py", "hexgent/tool/tool_registry.py", "hexgent/tool/builtin_tools.py"],
    "recovery_checkpoint_audit": ["hexgent/session/session_restore.py", "hexgent/session/checkpoint.py", "hexgent/session/tool_call_recovery.py", "hexgent/session/session_store.py"],
    "memory_context_audit": ["hexgent/memory/context_records.py", "hexgent/memory/context_retrieval.py", "hexgent/memory/memory_store.py", "hexgent/context/context_manager.py"],
}


def calls_for(paths):
    """冻结互不依赖的源码读取和目录枚举调用。"""
    return [tool_call(f"read-{i}", "read_file", json.dumps({"path": p})) for i, p in enumerate(paths)] + [tool_call("enumerate", "glob", json.dumps({"pattern": str(Path(paths[0]).parent / "*.py")}))]


def batch(calls, parallel, *, profile=False):
    """每次创建独立缓存；性能计时不包含 profiler 或 Session 持久化。"""
    context = ToolExecutionContext(InMemoryPlanStore(), ROOT)
    active = peak = entered = 0
    lock = threading.Lock()
    codes = {TOOL_REGISTRY[c.name].function.__code__ for c in calls}

    def observe(frame, event, arg):
        nonlocal active, peak, entered
        if frame.f_code in codes and event in {"call", "return"}:
            with lock:
                active += 1 if event == "call" else -1
                entered += event == "call"
                peak = max(peak, active)

    old_thread, old_main = threading.getprofile(), __import__("sys").getprofile()
    if profile:
        threading.setprofile(observe)
        __import__("sys").setprofile(observe)
    try:
        started = perf_counter()
        events, result = drain(execute_tool_batch(calls, context=context, user_confirmation=lambda *_: "deny", remaining_tool_calls=None, deadline=None, parallel_limits=ToolParallelLimits(enabled=parallel)))
        elapsed = (perf_counter() - started) * 1000
    finally:
        if profile:
            threading.setprofile(old_thread)
            __import__("sys").setprofile(old_main)
    return elapsed, result.outputs, {"peak_concurrency": peak, "handler_entries": entered, "completion_order": [e.call_id for e in events if type(e).__name__ == "ToolExecutionCompletedEvent"]}


def outputs_from(records):
    """提取 durable Context 中真实提交的工具消息。"""
    return [m for r in records if r["type"] == "context_messages_appended" for m in r["payload"]["messages"] if m.get("type") == "function_call_output"]


def loop_check(calls, expected, *, root=ROOT):
    """独立比较下一轮实际请求、durable Context 与 Finished 内容。"""
    with TemporaryDirectory() as directory:
        session = Session.persistent(Path(directory))
        session.record_user_query(SimpleNamespace(sequence=1, content="执行源码审计"))
        trace, model = run_trace([text_response(output=calls), text_response("审计完成")], session=session, workdir=root, config=_base_config(), user_confirmation=lambda *_: "deny")
        request = [m for m in model.calls[1]["input"] if isinstance(m, dict) and m.get("type") == "function_call_output"]
        durable = outputs_from(trace.transcript)
        finished = {r["payload"]["call_id"]: r["payload"] for r in trace.transcript if r["type"] == "tool_execution_finished"}
        allowed = {r["payload"]["call_id"] for r in trace.transcript if r["type"] == "permission_resolved" and r["payload"].get("behavior") == "allow"}
        dispatched = [r["payload"]["call_id"] for r in trace.transcript if r["type"] == "tool_execution_dispatched"]
        def resolve(message):
            matched = re.match(r'<persisted-output\npath="([^"]+)"', message["output"])
            return (session.store.workdir / matched[1]).read_text() if matched else message["output"]
        resolved = [{**m, "output": resolve(m)} for m in request]
        finished_content = {c: (session.store.workdir / p["persisted_output"]).read_text() if p.get("persisted_output") else p["output"] for c, p in finished.items()}
        checks = {"terminal_completed": trace.finished.result.status == "completed", "request_equals_expected_after_resolving_artifacts": resolved == expected, "durable_equals_request": durable == request, "finished_equals_request": all(finished_content[m["call_id"]] == resolve(m) for m in request), "no_unauthorized_dispatch": all(c in allowed for c in dispatched), "exact_dispatch_count": len(dispatched) == len(calls)}
        return {"checks": checks, "pass": all(checks.values()), "model_request": request, "resolved_model_request": resolved, "finished_content_snapshot": finished_content, "transcript": trace.transcript, "completion_order": [e.call_id for e in trace.events if type(e).__name__ == "ToolExecutionCompletedEvent"]}


def workload(name, paths, repeats):
    """交替测量同一真实调用序列，原样保存全部样本。"""
    calls = calls_for(paths)
    samples = []
    expected = None
    for i in range(repeats + 1):
        pair = {}
        for parallel in ([False, True] if i % 2 == 0 else [True, False]):
            elapsed, output, _ = batch(calls, parallel)
            if expected is None:
                expected = output
            assert output == expected
            assert [m["call_id"] for m in output] == [c.call_id for c in calls]
            assert all(m["output"] == (ROOT / paths[j]).read_text() for j, m in enumerate(output[:-1]))
            pair["parallel" if parallel else "serial"] = elapsed
        if i:
            samples.append({"pair": i, "order": ["serial", "parallel"] if i % 2 == 0 else ["parallel", "serial"], **pair, "speedup": pair["serial"] / pair["parallel"], "reduction_pct": (1 - pair["parallel"] / pair["serial"]) * 100})
    diagnostics = {v: batch(calls, v == "parallel", profile=True)[2] for v in ("serial", "parallel")}
    summary = {v: {"median_ms": median(s[v] for s in samples), "p95_ms": sorted(s[v] for s in samples)[ceil(.95 * repeats)-1]} for v in ("serial", "parallel")}
    ratios = [s["speedup"] for s in samples]
    return {"task_name": name, "calls": [c.model_dump() for c in calls], "tool_count": len(calls), "expected_read_scope": paths, "independence_reason": "不同文件的只读观测，无写入或调用间参数依赖", "workload_characteristics": "本地源码完整读取与 Python 目录枚举，OS 热缓存；无人为延迟", "source_hashes": {p: sha256((ROOT / p).read_bytes()).hexdigest() for p in paths}, "samples": samples, "summary": summary, "median_speedup": median(ratios), "mean_reduction_pct": mean(s["reduction_pct"] for s in samples), "min_speedup": min(ratios), "max_speedup": max(ratios), "actual_measured_calls": repeats * 2 * len(calls), "result_equivalence": True, "call_id_pairing": True, "untimed_profile": diagnostics, "agent_loop": loop_check(calls, expected)}


def mixed_barrier():
    """真实临时文件读写经完整 Loop，验证串行屏障与写后可见性。"""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "a.txt").write_text("before")
        (root / "b.txt").write_text("independent")
        calls = [tool_call("before", "read_file", '{"path":"a.txt"}'), tool_call("other", "read_file", '{"path":"b.txt"}'), tool_call("write", "write_file", '{"path":"a.txt","content":"after"}'), tool_call("after", "read_file", '{"path":"a.txt"}')]
        # 只读默认放行；工作区写入使用既有人工确认入口，不改权限策略。
        session = Session.persistent(root)
        session.record_user_query(SimpleNamespace(sequence=1, content="验证读写屏障"))
        trace, model = run_trace([text_response(output=calls), text_response("完成")], session=session, workdir=root, config=_base_config(), user_confirmation=lambda *_: "allow")
        output = outputs_from(trace.transcript)
        by_id = {m["call_id"]: m["output"] for m in output}
        request = [m for m in model.calls[1]["input"] if isinstance(m, dict) and m.get("type") == "function_call_output"]
        passed = by_id["before"] == "before" and by_id["other"] == "independent" and by_id["after"] == "after" and output == request and (root / "a.txt").read_text() == "after"
        return {"pass": passed, "outputs": output, "transcript": trace.transcript}


def late_result():
    """仅边界核验使用 Event 控制的 Eval 工具；不参与真实工具性能统计。"""
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    ledger = []

    def late():
        ledger.append("entered")
        entered.set()
        if not release.wait(timeout=10):
            raise AssertionError("Eval 未释放后台 Handler")
        ledger.append("returned")
        returned.set()
        return ToolHandlerResult.success("LATE_RESULT_MUST_NOT_BE_INJECTED")

    with TemporaryDirectory() as directory:
        root = Path(directory)
        session = Session.persistent(root)
        session.record_user_query(SimpleNamespace(sequence=1, content="验证迟到结果"))
        config = replace(_base_config(), tool_parallel=ToolParallelLimits(batch_timeout_seconds=.1))
        calls = [tool_call("late-1", "eval_late", "{}"), tool_call("late-2", "eval_late", "{}")]
        registered = RegisteredTool(late, concurrency_safe=True, permission_capability="workspace_read")
        try:
            with patch.dict(TOOL_REGISTRY, {"eval_late": registered}), patch.dict(TOOL_SCHEMAS, {"eval_late": {"type": "object", "properties": {}, "additionalProperties": False}}):
                trace, model = run_trace([text_response(output=calls), text_response("超时已处理")], session=session, workdir=root, config=config, user_confirmation=lambda *_: "allow")
                before = session.store.read_transcript()
                requests = json.dumps(model.calls, ensure_ascii=False, default=str)
                release.set()
                # 等待线程真正退出，而不仅是工具函数准备返回。
                for thread in threading.enumerate():
                    if thread.name.startswith("hexgent-tool"):
                        thread.join(timeout=5)
                after = session.store.read_transcript()
        finally:
            release.set()
        outputs = outputs_from(after)
        finished = [r for r in after if r["type"] == "tool_execution_finished"]
        checks = {"handler_entered_twice": ledger.count("entered") == 2, "handler_returned_twice": ledger.count("returned") == 2, "durable_unchanged_after_return": before == after, "one_output_per_call": [m["call_id"] for m in outputs] == ["late-1", "late-2"], "paired_timeout": len(finished) == 2 and all(r["payload"].get("failure", {}).get("code") == "tool_timeout" for r in finished), "no_late_context_injection": "LATE_RESULT_MUST_NOT_BE_INJECTED" not in json.dumps(after) + requests, "agent_completed": trace.finished.result.status == "completed"}
        return {"pass": all(checks.values()), "checks": checks, "ledger": ledger, "transcript": after, "scope": "Eval-only event-controlled handler; timeout does not terminate running threads"}


def main():
    """生成独立结果目录，禁止覆盖已有报告。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=20)
    args = parser.parse_args()
    if args.repeats < 20:
        parser.error("至少测量 20 对")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    rows = []
    for name, paths in TASKS.items():
        print(name, flush=True)
        rows.append(workload(name, paths, args.repeats))
    report = {"evidence_type": "DETERMINISTIC_REAL_BUILTIN_WORKLOAD", "real_model_calls": 0, "timing_scope": "Production Tool Batch，独立冷工具缓存、OS 热缓存；不含 Session、模型、profiler", "p95_method": "nearest rank ceil(0.95*n)，20 对为每 variant 20 个顺序样本，不宣称独立机器/冷缓存抽样", "workloads": rows, "mixed_barrier": mixed_barrier(), "late_result": late_result()}
    (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    assert all(r["agent_loop"]["pass"] for r in rows) and report["mixed_barrier"]["pass"] and report["late_result"]["pass"]


if __name__ == "__main__":
    main()
