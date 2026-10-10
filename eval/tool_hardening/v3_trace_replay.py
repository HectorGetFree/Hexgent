"""冻结真实模型轨迹，以正式 Pipeline 离线执行配对重放。"""

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
from statistics import median, mean
import sys
import threading
from time import perf_counter
from types import SimpleNamespace

from eval.modules_v1.scenarios import drain
from eval.support.agent_contracts import tool_call
from hexgent.plan import InMemoryPlanStore
from hexgent.session import Session
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_registry import TOOL_REGISTRY
from hexgent.tool.tool_scheduler import execute_tool_batch, ToolParallelLimits
from .v2_runner import digest, normalize_output, stats


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


def hashes(root):
    """源码、原 Run durable 文件也冻结；重放状态写在独立目录。"""
    return {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}


def freeze(directory, destination):
    """完整保留原始 arguments 字符串、callId 和每轮边界，不重规划。"""
    artifact = json.loads((directory / "artifact.json").read_text())
    workspace = (directory / "workspace").resolve()
    transcript = next((workspace / ".hexgent/sessions").glob("*/transcript.jsonl"))
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    outputs = {m["call_id"]: m["output"] for r in records if r["type"] == "context_messages_appended"
               for m in r["payload"].get("messages", []) if m.get("type") == "function_call_output"}
    facts = {r["payload"]["call_id"]: r["payload"] for r in records if r["type"] == "tool_execution_finished"}
    starts = {r["payload"]["call_id"]: r["sequence"] for r in records if r["type"] == "tool_execution_started"}
    clear_sequences = [r["sequence"] for r in records if r["type"] == "context_compaction_applied"]
    batches, previous = [], 0
    store = SimpleNamespace(workdir=workspace)
    for turn in artifact["turns"]:
        if turn["request_type"] != "MAIN_AGENT":
            continue
        calls = turn["tool_calls"]
        first = min((starts[c["call_id"]] for c in calls), default=previous)
        expected = []
        for call in calls:
            if call["name"] not in {"read_file", "glob"}:
                raise RuntimeError("TRACE_HAS_NON_READONLY_TOOL")
            cid = call["call_id"]
            content, _ = normalize_output(outputs[cid], store)
            expected.append({"call_id": cid, "tool_name": call["name"], "arguments": call["arguments"],
                             "status": facts[cid]["status"], "content_sha256": sha256(content.encode()).hexdigest()})
        batches.append({"response_id": turn["response_id"], "turn_id": turn["turn_id"], "calls": calls,
                        "cache_clear_before": any(previous < s < first for s in clear_sequences), "expected": expected})
        previous = max((starts[c["call_id"]] for c in calls), default=previous)
    value = {"source_artifact": str(directory / "artifact.json"), "source_artifact_sha256": digest(directory / "artifact.json"),
             "workspace": str(workspace), "snapshot": hashes(workspace), "batches": batches,
             "model_requests": 0, "arguments_rewritten": False, "boundaries_merged": False}
    value["permissions"] = {c["call_id"]: [{"type": r["type"], "payload": r["payload"]} for r in records
        if r["type"] in {"permission_evaluated", "permission_resolved"} and r["payload"].get("call_id") == c["call_id"]]
        for b in batches for c in b["calls"]}
    write(destination, value)
    return value


def verify(trace):
    if hashes(Path(trace["workspace"])) != trace["snapshot"] or digest(Path(trace["source_artifact"])) != trace["source_artifact_sha256"]:
        raise RuntimeError("REPLAY_SOURCE_CHANGED")


def execute(trace, directory, parallel, *, profile=False):
    """每条轨迹独立 Session/缓存；每个原模型响应分别执行一次 Pipeline。"""
    verify(trace)
    directory.mkdir(parents=True, exist_ok=False)
    session = Session.persistent(directory)
    session.record_user_query(SimpleNamespace(sequence=1, content="冻结真实模型只读轨迹重放"))
    context = ToolExecutionContext(InMemoryPlanStore(), Path(trace["workspace"]))
    limits = ToolParallelLimits(enabled=parallel)
    active = peak = entries = 0
    lock = threading.Lock()
    codes = {TOOL_REGISTRY[name].function.__code__ for name in ("read_file", "glob")}

    def observe(frame, event, arg):
        nonlocal active, peak, entries
        if frame.f_code in codes and event in {"call", "return"}:
            with lock:
                active += 1 if event == "call" else -1
                entries += event == "call"
                peak = max(peak, active)

    batches, normalized = [], []
    old_main, old_thread = sys.getprofile(), threading.getprofile()
    if profile:
        sys.setprofile(observe)
        threading.setprofile(observe)
    try:
        for batch in trace["batches"]:
            if batch["cache_clear_before"]:
                context.clear_tool_caches()
            calls = [tool_call(c["call_id"], c["name"], c["arguments"]) for c in batch["calls"]]
            if not calls:
                batches.append({"turn_id": batch["turn_id"], "call_count": 0, "latency_ms": 0, "actual_parallel_batches": 0})
                continue
            started = perf_counter()
            events, result = drain(execute_tool_batch(calls, context=context, user_confirmation=lambda *_: "deny", remaining_tool_calls=None,
                                                     deadline=None, session_store=session.store, parallel_limits=limits))
            elapsed = (perf_counter() - started) * 1000
            records = session.store.read_transcript()
            facts = {r["payload"]["call_id"]: r["payload"] for r in records if r["type"] == "tool_execution_finished"}
            if [o["call_id"] for o in result.outputs] != [c["call_id"] for c in batch["calls"]]:
                raise RuntimeError("REPLAY_CALL_ID_OR_ORDER_MISMATCH")
            observed = []
            for call, output in zip(batch["calls"], result.outputs):
                content, _ = normalize_output(output["output"], session.store)
                fact = facts[call["call_id"]]
                durable = (session.store.workdir / fact["persisted_output"]).read_text() if fact.get("persisted_output") else fact["output"]
                if durable != content:
                    raise RuntimeError("REPLAY_FINISHED_OUTPUT_MISMATCH")
                observed.append({"call_id": call["call_id"], "tool_name": fact["tool_name"], "arguments": call["arguments"],
                                 "status": fact["status"], "content_sha256": sha256(content.encode()).hexdigest()})
            if observed != batch["expected"]:
                write(directory / "mismatch.json", {"turn_id": batch["turn_id"], "expected": batch["expected"], "observed": observed})
                raise RuntimeError("TRACE_CANNOT_REPLAY_LOSSLESSLY")
            normalized.extend(observed)
            batches.append({"turn_id": batch["turn_id"], "call_count": len(calls), "latency_ms": elapsed,
                            "actual_parallel_batches": sum(type(e).__name__ == "ToolBatchStartedEvent" for e in events),
                            "normalized_results": observed})
    finally:
        if profile:
            sys.setprofile(old_main)
            threading.setprofile(old_thread)
    records = session.store.read_transcript()
    for batch in trace["batches"]:
        for call in batch["calls"]:
            started = [r["payload"] for r in records if r["type"] == "tool_execution_started" and r["payload"].get("call_id") == call["call_id"]]
            arguments = started[0].get("arguments") if started else None
            arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            if len(started) != 1 or started[0]["tool_name"] != call["name"] or arguments != json.loads(call["arguments"]):
                raise RuntimeError("REPLAY_ARGUMENTS_MISMATCH")
            if "permissions" in trace:
                observed_permissions = [{"type": r["type"], "payload": r["payload"]} for r in records
                    if r["type"] in {"permission_evaluated", "permission_resolved"} and r["payload"].get("call_id") == call["call_id"]]
                if observed_permissions != trace["permissions"][call["call_id"]]:
                    write(directory / "permission-mismatch.json", {"expected": trace["permissions"][call["call_id"]], "observed": observed_permissions})
                    raise RuntimeError("REPLAY_PERMISSION_MISMATCH")
    dispatched = [r for r in records if r["type"] == "tool_execution_dispatched"]
    if not all(any(p["type"] == "permission_resolved" and p["payload"].get("call_id") == r["payload"]["call_id"]
                   and p["payload"].get("behavior") == "allow" and p["sequence"] < r["sequence"] for p in records) for r in dispatched):
        raise RuntimeError("REPLAY_UNAUTHORIZED_DISPATCH")
    if profile and (peak > (limits.max_workers if parallel else 1) or not entries):
        raise RuntimeError("REPLAY_CONCURRENCY_CONFIG_MISMATCH")
    verify(trace)
    return {"tool_latency_ms": sum(b["latency_ms"] for b in batches), "batches": batches, "normalized_results": normalized,
            "result_equivalence": True, "call_id_pairing": True, "actual_parallel_batches": sum(b["actual_parallel_batches"] for b in batches),
            "handler_peak_concurrency": peak if profile else None, "handler_entries": entries if profile else None,
            "permission_equivalence": "permissions" in trace, "profiled": profile, "session_directory": str(directory)}


def run_replays(output, pairs, *, tasks=None):
    """每主题选第一条符合条件的完整真实轨迹；不按性能挑选。"""
    from .v3_experiment import TASKS
    results = []
    for task in (TASKS if tasks is None else tasks):
        selected = next((p["runs"][v] for p in pairs if p["task"] == task for v in ("ON", "OFF")
                         if v in p["runs"] and not p["runs"][v]["stop_reasons"] and p["runs"][v]["parallel_eligible_batch_count"] > 0), None)
        if selected is None:
            raise RuntimeError(f"NO_ELIGIBLE_REAL_MODEL_TRACE:{task}")
        directory = output / "replay" / task
        directory.mkdir(parents=True, exist_ok=False)
        manifest_path = directory / "trace.json"
        trace = freeze(Path(selected["artifact"]).parent, manifest_path)
        manifest_hash = digest(manifest_path)
        report = {"task": task, "trace_sha256": manifest_hash, "selected_artifact": selected["artifact"], "samples": [],
                  "timing_scope": "sum of original response batches, Production Pipeline and durable writes; excludes setup/oracles/profiler; OS hot-cache local I/O",
                  "warmup_pairs": 1, "measured_pairs": 20, "model_calls": 0, "status": "RUNNING"}
        write(directory / "report.json", report)
        try:
            for index in range(21):
                row = {"pair": index, "warmup": index == 0, "order": ["serial", "parallel"] if index % 2 == 0 else ["parallel", "serial"]}
                report["samples"].append(row)
                for variant in row["order"]:
                    row[variant] = execute(trace, directory / "sessions" / str(index) / variant, variant == "parallel")
                    write(directory / "report.json", report)
                if row["serial"]["normalized_results"] != row["parallel"]["normalized_results"]:
                    raise RuntimeError("SERIAL_PARALLEL_RESULT_MISMATCH")
                row.update(result_equivalence=True, call_id_pairing=True,
                           paired_speedup=row["serial"]["tool_latency_ms"] / row["parallel"]["tool_latency_ms"],
                           latency_reduction_pct=(1 - row["parallel"]["tool_latency_ms"] / row["serial"]["tool_latency_ms"]) * 100)
                if digest(manifest_path) != manifest_hash:
                    raise RuntimeError("FROZEN_TRACE_CHANGED")
                write(directory / "report.json", report)
            # 独立非计时探针，避免将 profiler 开销冒充真实性能。
            report["actual_parallelism_profiles"] = {v: execute(trace, directory / "sessions" / "profile" / v, v == "parallel", profile=True)
                                                     for v in ("serial", "parallel")}
            samples = report["samples"][1:]
            report["summary"] = {v: stats([s[v]["tool_latency_ms"] for s in samples]) for v in ("serial", "parallel")}
            report["summary"].update(median_paired_speedup=median(s["paired_speedup"] for s in samples),
                                     mean_latency_reduction_pct=mean(s["latency_reduction_pct"] for s in samples),
                                     min_speedup=min(s["paired_speedup"] for s in samples), max_speedup=max(s["paired_speedup"] for s in samples))
            report["batch_summary"] = [{"turn_id": b["turn_id"], "call_count": b["call_count"],
                                        **{v: stats([s[v]["batches"][i]["latency_ms"] for s in samples]) for v in ("serial", "parallel")},
                                        "median_paired_speedup": median(s["serial"]["batches"][i]["latency_ms"] / s["parallel"]["batches"][i]["latency_ms"] for s in samples) if b["call_count"] else None}
                                       for i, b in enumerate(samples[0]["serial"]["batches"])]
            report["status"] = "PASS"
        except Exception as error:
            report["status"] = "STOPPED"
            report["stop_reason"] = f"{type(error).__name__}: {error}"
            write(directory / "report.json", report)
            raise
        write(directory / "report.json", report)
        results.append({"task": task, "report": str(directory / "report.json"), "status": report["status"], "summary": report["summary"]})
        print(f"REPLAY PASS {task}", flush=True)
    return results
