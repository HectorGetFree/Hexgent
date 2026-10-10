"""执行人工审核后的 V2 固定计划；不生成或调整调用序列。"""

import argparse
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
import json
from math import ceil
from pathlib import Path
import re
from statistics import mean, median
import sys
import threading
from time import perf_counter
from types import SimpleNamespace

from eval.modules_v1.scenarios import drain
from eval.support.agent_contracts import tool_call
from hexgent.plan import InMemoryPlanStore
from hexgent.session import Session, ToolResultLimits
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_registry import TOOL_REGISTRY
from hexgent.tool.tool_scheduler import execute_tool_batch, ToolParallelLimits

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    """读取当前字节计算哈希，不依赖 mtime。"""
    return sha256(path.read_bytes()).hexdigest()


def validate_sources(manifest, protected):
    """计划、发现集合和所有正式源码必须匹配；变化即停止并保留样本。"""
    for task in manifest["tasks"]:
        for source in task["source_coverage_manifest"]:
            if digest(ROOT / source["path"]) != source["sha256"]:
                raise RuntimeError(f"INVALID_SOURCE_CHANGED: {source['path']}")
        for call in task["batches"][0]["calls"]:
            actual = sorted(str(p.relative_to(ROOT)) for p in ROOT.glob(call["arguments"]["pattern"]))
            if actual != call["expected_paths"]:
                raise RuntimeError(f"INVALID_DISCOVERY_CHANGED: {call['call_id']}")
    if {str(p.relative_to(ROOT)): digest(p) for p in (ROOT / "hexgent").rglob("*.py")} != protected:
        raise RuntimeError("INVALID_PRODUCTION_SOURCE_CHANGED")


def normalize_output(output, store):
    """解析正式外置引用正文；绝不将 preview 视为完整结果。"""
    matched = re.match(r'<persisted-output\npath="([^"]+)"', output)
    if matched:
        target = (store.workdir / matched[1]).resolve()
        if not target.is_relative_to(store.workdir.resolve()):
            raise AssertionError("外置引用逃逸 Session 工作区")
        return target.read_text(encoding="utf-8"), matched[1]
    return output, None


def execute_task(task, directory, parallel, *, profile=False):
    """通过正式 Pipeline 顺序执行四批；每任务独立缓存和 durable Session。"""
    directory.mkdir(parents=True, exist_ok=False)
    session = Session.persistent(directory)
    session.record_user_query(SimpleNamespace(sequence=1, content=task["task_name"]))
    store = session.store
    context = ToolExecutionContext(InMemoryPlanStore(), ROOT)
    limits = ToolParallelLimits(enabled=parallel)
    profiles = []
    normalized = []
    batches = []
    # 1. Session/Context 初始化在计时外，四批调用和输出核验在任务时钟内。
    task_start = perf_counter()
    for batch in task["batches"]:
        calls = [tool_call(c["call_id"], c["tool_name"], json.dumps(c["arguments"], ensure_ascii=False)) for c in batch["calls"]]
        active = peak = 0
        entries = Counter()
        lock = threading.Lock()
        functions = {TOOL_REGISTRY[c.name].function.__code__: c.name for c in calls}

        def observe(frame, event, arg):
            nonlocal active, peak
            if frame.f_code in functions and event in {"call", "return"}:
                with lock:
                    active += 1 if event == "call" else -1
                    peak = max(peak, active)
                    if event == "call":
                        entries[functions[frame.f_code]] += 1

        old_main, old_thread = sys.getprofile(), threading.getprofile()
        if profile:
            sys.setprofile(observe)
            threading.setprofile(observe)
        try:
            started = perf_counter()
            events, result = drain(execute_tool_batch(calls, context=context, user_confirmation=lambda *_: "deny", remaining_tool_calls=None, deadline=None, session_store=store, parallel_limits=limits))
            elapsed = (perf_counter() - started) * 1000
        finally:
            if profile:
                sys.setprofile(old_main)
                threading.setprofile(old_thread)
        # 2. 按 durable Finished 状态与实际输出正文逐 call 验证。
        records = store.read_transcript()
        finished = {r["payload"]["call_id"]: r["payload"] for r in records if r["type"] == "tool_execution_finished"}
        assert [o["call_id"] for o in result.outputs] == [c.call_id for c in calls]
        observed = []
        for spec, output in zip(batch["calls"], result.outputs):
            payload = finished[spec["call_id"]]
            assert payload["status"] == "success", payload
            assert payload["tool_name"] == spec["tool_name"]
            content, artifact = normalize_output(output["output"], store)
            if spec["tool_name"] == "read_file":
                assert sha256(content.encode()).hexdigest() == spec["expected_sha256"]
            else:
                assert content == "\n".join(str(ROOT / p) for p in spec["expected_paths"])
            durable_content = (store.workdir / payload["persisted_output"]).read_text() if payload.get("persisted_output") else payload["output"]
            assert durable_content == content
            item = {"call_id": spec["call_id"], "tool_name": spec["tool_name"], "arguments": spec["arguments"], "status": payload["status"], "content_sha256": sha256(content.encode()).hexdigest()}
            observed.append(item)
            normalized.append(item)
        batches.append({"stage": batch["stage"], "depends_on": batch["depends_on"], "latency_ms": elapsed, "call_count": len(calls), "normalized_results": observed, "model_visible_outputs": result.outputs, "completion_order": [e.call_id for e in events if type(e).__name__ == "ToolExecutionCompletedEvent"]})
        if profile:
            assert sum(entries.values()) == len(calls)
            assert 1 <= peak <= (limits.max_workers if parallel else 1)
            profiles.append({"stage": batch["stage"], "peak_concurrency": peak, "handler_entries": dict(entries), "call_count": len(calls)})
    total_ms = (perf_counter() - task_start) * 1000
    # 3. 计时外再核对权限先于派发、恰好一次实际派发。
    records = store.read_transcript()
    allow_sequence = {r["payload"]["call_id"]: r["sequence"] for r in records if r["type"] == "permission_resolved" and r["payload"].get("behavior") == "allow"}
    dispatches = [r for r in records if r["type"] == "tool_execution_dispatched"]
    expected_ids = [c["call_id"] for b in task["batches"] for c in b["calls"]]
    assert Counter(r["payload"]["call_id"] for r in dispatches) == Counter(expected_ids)
    assert all(allow_sequence[r["payload"]["call_id"]] < r["sequence"] for r in dispatches)
    return {"total_latency_ms": total_ms, "batch_latency_sum_ms": sum(b["latency_ms"] for b in batches), "batches": batches, "normalized_results": normalized, "actual_dispatch_count": len(dispatches), "no_unauthorized_dispatch": True, "profiles": profiles, "session_directory": str(directory), "config": {"tool_parallel": asdict(limits), "tool_result": asdict(ToolResultLimits())}}


def stats(values):
    """P95 使用 nearest-rank；全部顺序样本原样纳入。"""
    return {"p50_ms": median(values), "p95_ms": sorted(values)[ceil(.95 * len(values)) - 1]}


def summarize(rows):
    """任务加速来自完整任务配对，不由批次加速比平均代替。"""
    ratios = [r["serial"]["total_latency_ms"] / r["parallel"]["total_latency_ms"] for r in rows]
    reductions = [(1 - 1 / ratio) * 100 for ratio in ratios]
    result = {v: stats([r[v]["total_latency_ms"] for r in rows]) for v in ("serial", "parallel")}
    result.update({"median_paired_speedup": median(ratios), "mean_paired_reduction_pct": mean(reductions), "min_paired_speedup": min(ratios), "max_paired_speedup": max(ratios), "batches": []})
    for i in range(4):
        batch = {"stage": rows[0]["serial"]["batches"][i]["stage"], "call_count": rows[0]["serial"]["batches"][i]["call_count"]}
        for v in ("serial", "parallel"):
            batch[v] = stats([r[v]["batches"][i]["latency_ms"] for r in rows])
            batch[v]["mean_task_contribution_pct"] = mean(r[v]["batches"][i]["latency_ms"] / r[v]["total_latency_ms"] * 100 for r in rows)
        batch["median_paired_speedup"] = median(r["serial"]["batches"][i]["latency_ms"] / r["parallel"]["batches"][i]["latency_ms"] for r in rows)
        result["batches"].append(batch)
    return result


def main():
    """持久保存每个 variant 后再检查；异常保留已有样本并明确退出。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest_path = Path(__file__).with_name("V2_SOURCE_MANIFEST.json")
    manifest_hash = digest(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    protected = {str(p.relative_to(ROOT)): digest(p) for p in (ROOT / "hexgent").rglob("*.py")}
    validate_sources(manifest, protected)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {"status": "RUNNING", "manifest_path": str(manifest_path), "manifest_sha256": manifest_hash, "production_sha256": protected, "repeats": 20, "warmup_pairs": 1, "real_model_calls": 0, "timing_scope": "task includes four sequential Production batches with durable writes and per-batch content verification; excludes initialization and pre/post source hashing", "tasks": []}

    def save():
        (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))

    try:
        for task in manifest["tasks"]:
            print(task["task_name"], flush=True)
            output = {"task_name": task["task_name"], "samples": [], "untimed_profiles": {}}
            report["tasks"].append(output)
            for i in range(21):
                validate_sources(manifest, protected)
                assert digest(manifest_path) == manifest_hash
                pair = {"pair": i, "warmup": i == 0, "valid": False, "order": ["serial", "parallel"] if i % 2 == 0 else ["parallel", "serial"]}
                output["samples"].append(pair)
                for v in pair["order"]:
                    pair[v] = execute_task(task, args.output_dir / "sessions" / task["task_name"] / str(i) / v, v == "parallel")
                    save()
                    validate_sources(manifest, protected)
                    assert digest(manifest_path) == manifest_hash
                assert pair["serial"]["normalized_results"] == pair["parallel"]["normalized_results"]
                pair["valid"] = pair["result_equivalence"] = True
                save()
            for v in ("serial", "parallel"):
                validate_sources(manifest, protected)
                output["untimed_profiles"][v] = execute_task(task, args.output_dir / "sessions" / task["task_name"] / "profile" / v, v == "parallel", profile=True)["profiles"]
                validate_sources(manifest, protected)
            output["summary"] = summarize(output["samples"][1:])
        report["status"] = "PASS"
    except Exception as error:
        report["status"] = "INVALID_OR_FAILED"
        report["error"] = f"{type(error).__name__}: {error}"
        save()
        raise
    save()


if __name__ == "__main__":
    main()
