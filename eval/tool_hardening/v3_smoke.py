"""真实模型自主只读任务的一对 Smoke；不修改正式 Runtime。"""

import argparse
from collections import Counter
from contextlib import nullcontext
from dataclasses import asdict, replace
import json
from pathlib import Path
import shutil
import sys
import threading
from time import perf_counter
from unittest.mock import patch

from hexgent.agent_engine import AgentEngine
from hexgent.client import get_client, get_model
from hexgent.config import HexgentConfig
from hexgent.loop import agent_turn
from hexgent.loop.limits import LoopLimits
from hexgent.tool.tool_registry import TOOL_REGISTRY
from hexgent.tool.tool_scheduler import ToolParallelLimits, execute_tool_batch
from eval.phase5c.provider_preflight import classify_preflight_failure, redact_diagnostic
from .v2_runner import ROOT, digest

TASKS = {
    "tool_pipeline": {"scope": "hexgent/tool、hexgent/loop/agent_turn.py、相关 tests", "required": ["hexgent/tool/tool_scheduler.py", "hexgent/tool/tool_execution.py", "hexgent/tool/tool_permission_pipeline.py"], "questions": ["相邻只读工具如何分组，写工具如何形成屏障？", "权限决策与 dispatch 的先后关系是什么？", "完成顺序与模型可见结果顺序如何处理？", "超时是否保证运行线程停止，未知副作用是否安全自动重试？"]},
    "recovery_checkpoint": {"scope": "hexgent/session、hexgent/agent_engine.py、相关 tests", "required": ["hexgent/session/session_restore.py", "hexgent/session/checkpoint.py", "hexgent/session/tool_call_recovery.py"], "questions": ["Checkpoint 与 Transcript 的权威关系是什么？", "最新 checkpoint 损坏时如何处理？", "started、dispatched、finished 不同事实下如何处理未闭合调用？", "恢复后继续原 Run 有什么状态前提，未知副作用能否自动重试？"]},
    "memory_context": {"scope": "hexgent/memory、hexgent/context、hexgent/loop/extract_context_records.py、相关 tests", "required": ["hexgent/memory/memory_store.py", "hexgent/memory/context_records.py", "hexgent/memory/context_retrieval.py", "hexgent/context/context_compaction.py"], "questions": ["Workspace Memory 与 Session ContextRecord 的作用域如何区分？", "record 来源有效性与 active/superseded 如何校验？", "检索预算及请求注入如何约束？", "压缩如何保留完整工具轮次，无效摘要如何处理？"]},
}


def prompt(task):
    """自然审计目标，不包含固定调用列表或并发提示。"""
    return f"请只读审计当前 Hexgent 的 {task['scope']}。自主查找相关实现和测试，回答：" + "；".join(task["questions"]) + "。不要改文件，不访问网络。最终返回 JSON：{\"findings\":[{\"question\":\"问题\",\"conclusion\":\"结论\",\"evidence\":[{\"path\":\"源码相对路径\",\"quote\":\"逐字源码片段\"}]}],\"limitations\":[\"证据不足项\"]}。每个问题都要回答并引用实际读取的源码，明确事实与不确定性，不得仅声称审计完成。"


from .v3_provider import Observer, usage_report


def oracle(task, records, answer, workspace):
    """检查真实读取与逐字证据；语义边界结论仍须外部人工审核。"""
    workspace = workspace.resolve()
    starts = {r["payload"]["call_id"]: r["payload"] for r in records if r["type"] == "tool_execution_started"}
    successful = {r["payload"]["call_id"] for r in records if r["type"] == "tool_execution_finished" and r["payload"]["status"] == "success"}
    read_paths = set()
    for call_id in successful:
        p = starts.get(call_id, {})
        if p.get("tool_name") == "read_file":
            args = p.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args)
            path = args.get("path", "")
            try:
                read_paths.add(str((workspace / path).resolve().relative_to(workspace)))
            except ValueError:
                pass
    errors = []
    quote_checks = []
    try:
        raw = answer.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        value = json.loads(raw)
        assert len(value["findings"]) == len(task["questions"])
        for index, finding in enumerate(value["findings"]):
            if not finding["conclusion"] or not finding["evidence"]:
                errors.append(f"EMPTY_FINDING:{index + 1}")
            for evidence_index, evidence in enumerate(finding["evidence"]):
                path = evidence["path"]
                source = (workspace / path).resolve()
                within_workspace = source.is_relative_to(workspace)
                exact = within_workspace and source.is_file() and evidence["quote"] in source.read_text()
                valid = path in read_paths and len(evidence["quote"]) >= 15 and exact
                quote_checks.append({"finding": index + 1, "evidence": evidence_index + 1, "path": path,
                                     "was_read": path in read_paths, "verbatim_match": exact, "pass": valid})
                if not valid:
                    errors.append(f"QUOTE_INVALID:{index + 1}:{evidence_index + 1}")
    except Exception as error:
        errors.append(f"OUTPUT_OR_EVIDENCE_INVALID: {type(error).__name__}")
    missing = sorted(set(task["required"]) - read_paths)
    return {"mechanical_pass": not errors and not missing, "missing_required_reads": missing, "successful_read_paths": sorted(read_paths), "errors": errors, "quote_checks": quote_checks, "semantic_review": "PENDING_MANUAL_REVIEW", "task_oracle_pass": None}


def production_audit_config(parallel):
    """沿用生产默认预算，仅覆盖调度开关和用户授权的自动放行模式。"""
    defaults = HexgentConfig()
    return replace(defaults, tool_parallel=replace(defaults.tool_parallel, enabled=parallel),
                   permission=replace(defaults.permission, default_mode="auto_approve"))


def run_one(task, directory, parallel, client, protected, *, instruction=None, evidence_oracle=None, capture_timing=False, production_profile=False):
    """隔离源码快照、正式 Loop、真实只读 Registry，开启诊断探针。"""
    workspace = directory / "workspace"
    workspace.mkdir(parents=True)
    for name in ("hexgent", "tests"):
        shutil.copytree(ROOT / name, workspace / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if production_profile:
        for name in ("docs", "README.md", "pyproject.toml", "uv.lock"):
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(source, workspace / name)
            elif source.is_file():
                shutil.copy2(source, workspace / name)
    assert all(digest(workspace / p) == h for p, h in protected.items())
    config = replace(HexgentConfig(), tool_parallel=ToolParallelLimits(enabled=parallel), loop=LoopLimits(max_model_rounds=16, max_tool_calls=100, max_elapsed_seconds=600))
    if production_profile:
        config = production_audit_config(parallel)
    observer = Observer(client, directory / "provider-calls.json")
    engine = AgentEngine.create(config, workspace)
    allowed = dict(TOOL_REGISTRY) if production_profile else {k: TOOL_REGISTRY[k] for k in ("read_file", "glob")}
    codes = {v.function.__code__ for v in allowed.values()}
    active = 0
    lock = threading.Lock()
    running = {}
    timing_events = []

    def record_timing(kind, **fields):
        if capture_timing:
            with lock:
                timing_events.append({"event_index": len(timing_events), "event_type": kind,
                                      "offset_ms": (perf_counter() - started) * 1000,
                                      "thread_id": threading.get_ident(), **fields})

    def profile(frame, event, arg):
        nonlocal active
        turn = observer.current
        if turn is None:
            return
        if frame.f_code in codes and event in {"call", "return"}:
            with lock:
                active += 1 if event == "call" else -1
                turn["handler_entries"] += event == "call"
                turn["peak_concurrency"] = max(turn["peak_concurrency"], active)
            if capture_timing:
                ancestor = frame
                call_id = None
                while ancestor is not None:
                    item = ancestor.f_locals.get("item")
                    if hasattr(item, "tool_call"):
                        call_id = item.tool_call.call_id
                        break
                    ancestor = ancestor.f_back
                record_timing("handler_" + event, tool_call_id=call_id,
                              tool_name=frame.f_code.co_name, provider_turn_id=turn["turn_id"])
        # generator 每次 resume 的实际执行区间累加，不含模型等待。
        if frame.f_code is execute_tool_batch.__code__:
            key = id(frame)
            if event == "call":
                running[key] = perf_counter()
                record_timing("pipeline_resume", generator_id=key, provider_turn_id=turn["turn_id"])
            elif event == "return" and key in running:
                turn["tool_batch_latency_ms"] += (perf_counter() - running.pop(key)) * 1000
                record_timing("pipeline_suspend_or_return", generator_id=key, provider_turn_id=turn["turn_id"])

    events = []
    old_main, old_thread = sys.getprofile(), threading.getprofile()
    started = perf_counter()
    try:
        with (nullcontext() if production_profile else patch.dict(TOOL_REGISTRY, allowed, clear=True)):
            sys.setprofile(profile)
            threading.setprofile(profile)
            for event in engine.run(prompt(task) if instruction is None else instruction, client=observer, user_confirmation=lambda *_: "deny"):
                events.append({"type": type(event).__name__, "value": asdict(event)})
                record_timing(type(event).__name__, value=asdict(event))
    finally:
        elapsed = (perf_counter() - started) * 1000
        sys.setprofile(old_main)
        threading.setprofile(old_thread)
    records = engine.session.store.read_transcript()
    terminal = next((r["payload"] for r in reversed(records) if r["type"] == "agent_run_finished"), {})
    turns = observer.turns
    for row in turns:
        if row["status"] == "CANCELLED" and row["request_type"] == "CONTEXT_RECORD_EXTRACTION":
            failures = [r["payload"] for r in records if r["type"] == "context_record_extraction_failed"]
            row["runtime_extraction_failures"] = failures
            row["cancellation_scope"] = "EXTRACTION_TIMEOUT_OR_RUN_CONTROL; see durable failure evidence"
    complete = bool(turns) and all(t.get("usage", {}).get("input_tokens") is not None and t.get("usage", {}).get("output_tokens") is not None for t in turns if t.get("usage") is not None) and all(t.get("usage") for t in turns)
    evidence = (oracle if evidence_oracle is None else evidence_oracle)(task, records, terminal.get("output_text", ""), workspace)
    result = {"variant": "ON" if parallel else "OFF", "total_agent_latency_ms": elapsed, "total_provider_latency_ms": sum(t["provider_latency_ms"] for t in turns), "total_tool_latency_ms": sum(t["tool_batch_latency_ms"] for t in turns), "provider_input_tokens": sum(t["usage"]["input_tokens"] for t in turns) if complete else None, "provider_output_tokens": sum(t["usage"]["output_tokens"] for t in turns) if complete else None, "usage_complete": bool(complete), "terminal": terminal, "events": events, "turns": turns, "oracle": evidence, "source_unchanged": all(digest(workspace / p) == h for p, h in protected.items())}
    result["provider_usage"] = usage_report(turns)
    result["execution_config"] = asdict(config)
    result["model_id"] = get_model()
    result["provider_base_url"] = str(client.base_url)
    result["observation_errors"] = observer.state["observation_errors"]
    from .v3_oracles import layered_oracles
    result["oracles"] = layered_oracles(records, turns, result["oracle"], workspace)
    if evidence.get("evaluation_mode") == "MANUAL_REVIEW":
        result["oracles"]["task_evidence"]["status"] = "PENDING_MANUAL_REVIEW"
    for t in turns:
        t["tool_calls_in_response"] = len(t["tool_calls"])
        readonly = [c for c in t["tool_calls"] if c["name"] in allowed]
        identities = {(c["name"], c["arguments"]) for c in readonly}
        t["independent_readonly_calls"] = len(identities)
        t["eligible_parallel_calls"] = len(identities) if len(identities) > 1 else 0
    (directory / "artifact.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if capture_timing:
        (directory / "timing-events.json").write_text(json.dumps({"clock": "perf_counter offset from Agent run start",
            "profiling_enabled": True, "limitation": "profiling has overhead; not an uninstrumented performance estimate",
            "events": timing_events}, ensure_ascii=False, indent=2))
    return result


def main():
    """只运行每题一对；任意 Provider/Usage/Schema 问题立即停止。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    args = parser.parse_args()
    preflight = json.loads(args.preflight.read_text())
    assert preflight["result"]["status"] == "PASS" and preflight["result"]["usage_available"]
    client = get_client()
    assert get_model() == preflight["result"]["model_id"]
    args.output_dir.mkdir(parents=True, exist_ok=False)
    protected = {str(p.relative_to(ROOT)): digest(p) for base in ("hexgent", "tests") for p in (ROOT / base).rglob("*.py")}
    report = {"status": "RUNNING", "preflight": str(args.preflight), "model": get_model(), "provider_base_url": str(client.base_url), "source_hashes": protected, "instrumented_smoke": True, "runs": []}
    for name, task in TASKS.items():
        for parallel in (True, False):
            print(name, "ON" if parallel else "OFF", flush=True)
            artifact = run_one(task, args.output_dir / name / ("ON" if parallel else "OFF"), parallel, client, protected)
            report["runs"].append({"task": name, "variant": artifact["variant"], "terminal": artifact["terminal"], "oracle": artifact["oracle"]})
            if artifact["terminal"].get("status") != "completed" or not artifact["usage_complete"] or not artifact["source_unchanged"]:
                report["status"] = "STOPPED_SMOKE_GATE"
                break
        if report["status"] == "STOPPED_SMOKE_GATE":
            break
    else:
        report["status"] = "SMOKE_COMPLETED_PENDING_ORACLE_REVIEW"
    (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(report["status"])


if __name__ == "__main__":
    main()
