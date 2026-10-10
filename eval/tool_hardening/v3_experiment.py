"""真实 V3 配对实验；先 Smoke，后九对正式运行，最后离线重放。"""

import argparse
from collections import Counter
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
from statistics import median, mean
import subprocess
import sys

from hexgent.client import get_client, get_model
from hexgent.config import HexgentConfig
from hexgent.loop.limits import LoopLimits
from hexgent.tool.tool_scheduler import ToolParallelLimits, partition_tool_calls
from eval.support.agent_contracts import tool_call
from .v2_runner import ROOT, digest, stats
from .v3_smoke import TASKS, prompt, run_one


def save(path, value):
    """只更新本轮运行中的报告，不触碰旧目录。"""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def source_hashes():
    """冻结会复制给 Agent 的完整源码与测试集合。"""
    return {str(p.relative_to(ROOT)): digest(p) for base in ("hexgent", "tests")
            for p in sorted((ROOT / base).rglob("*")) if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}


def eligible_batches(turns):
    """按真实响应边界统计相邻独立只读机会，不跨轮合并。"""
    count = 0
    for turn in turns:
        calls = [tool_call(c["call_id"], c["name"], c["arguments"]) for c in turn["tool_calls"]]
        for group in partition_tool_calls(calls):
            if group.concurrency_safe and len({(c.name, c.arguments) for c in group.calls}) > 1:
                count += 1
    return count


def execution_contract(records, turns):
    """区分正确返回的业务失败与工具协议破坏，不把模型错路径算作 Scheduler bug。"""
    calls = [c for t in turns if t["request_type"] == "MAIN_AGENT" for c in t["tool_calls"]]
    errors = []
    statuses = Counter()
    for call in calls:
        cid = call["call_id"]
        starts = [r["payload"] for r in records if r["type"] == "tool_execution_started" and r["payload"].get("call_id") == cid]
        finished = [r["payload"] for r in records if r["type"] == "tool_execution_finished" and r["payload"].get("call_id") == cid]
        dispatch = [r for r in records if r["type"] == "tool_execution_dispatched" and r["payload"].get("call_id") == cid]
        if len(starts) != 1 or len(finished) != 1 or len(dispatch) > 1:
            errors.append(f"FACT_CARDINALITY:{cid}")
            continue
        start, fact = starts[0], finished[0]
        args = start.get("arguments")
        args = json.loads(args) if isinstance(args, str) else args
        if start.get("tool_name") != call["name"] or fact.get("tool_name") != call["name"] or args != json.loads(call["arguments"]):
            errors.append(f"IDENTITY_OR_ARGUMENTS:{cid}")
        status = fact["status"]
        statuses[status] += 1
        if status not in {"success", "error", "invalid", "denied", "deduplicated"}:
            errors.append(f"UNKNOWN_STATUS:{cid}")
        if status == "success" and len(dispatch) != 1:
            errors.append(f"SUCCESS_WITHOUT_DISPATCH:{cid}")
        if status in {"invalid", "denied", "deduplicated"} and dispatch:
            errors.append(f"UNEXPECTED_DISPATCH:{cid}")
        if status in {"error", "invalid", "denied"} and not fact.get("failure"):
            errors.append(f"MISSING_FAILURE_FACT:{cid}")
        if dispatch and not any(r["type"] == "permission_resolved" and r["payload"].get("call_id") == cid
                                and r["payload"].get("behavior") == "allow" and r["sequence"] < dispatch[0]["sequence"] for r in records):
            errors.append(f"UNAUTHORIZED_DISPATCH:{cid}")
    ids = [c["call_id"] for c in calls]
    actual = [r["payload"]["call_id"] for r in records if r["type"] == "tool_execution_finished"]
    if Counter(ids) != Counter(actual) or len(ids) != len(set(ids)):
        errors.append("CALL_SET_MISMATCH")
    return {"status": "FAIL" if errors else "PASS" if calls else "BLOCKED", "errors": errors,
            "operation_status_counts": dict(statuses), "call_count": len(calls),
            "scope": "protocol/permission correctness; controlled tool errors do not imply task success"}


def project(artifact, directory):
    """投影成本、调用分布、配置与三层 Oracle；完整语义成功始终待审核。"""
    workspace = directory / "workspace"
    transcript = next((workspace / ".hexgent/sessions").glob("*/transcript.jsonl"))
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    turns = artifact["turns"]
    main = [t for t in turns if t["request_type"] == "MAIN_AGENT"]
    auxiliary = [t for t in turns if t["request_type"] != "MAIN_AGENT"]
    main_usage, aux_usage, all_usage = (artifact["provider_usage"][k] for k in ("main", "auxiliary", "all"))
    failures = [{"turn_id": t["turn_id"], "request_type": t["request_type"], "status": t["status"], "reason": t["failure_reason"]}
                for t in turns if t["status"] != "SUCCEEDED"]
    oracle = {**artifact["oracles"], "tool_execution": execution_contract(records, turns)}
    calls = [len(t["tool_calls"]) for t in main]
    actual_batches = sum(e["type"] == "ToolBatchStartedEvent" for e in artifact["events"])
    peak = max((t["peak_concurrency"] for t in main), default=0)
    enabled = artifact["execution_config"]["tool_parallel"]["enabled"]
    config_ok = enabled == (artifact["variant"] == "ON") and (enabled or (peak <= 1 and actual_batches == 0))
    errors = []
    if not config_ok:
        errors.append("CONFIGURATION_NOT_EFFECTIVE")
    if oracle["tool_execution"]["status"] != "PASS" or oracle["tool_result_delivery"]["status"] != "PASS":
        errors.append("TOOL_CONTRACT_FAILED")
    if artifact["observation_errors"]:
        errors.append("OBSERVER_FAILURE")
    if not artifact["source_unchanged"]:
        errors.append("SOURCE_CHANGED")
    # 保留正式 extraction 的 deadline 取消；主请求失败或其他上游错误保守停止。
    if any(t["status"] != "SUCCEEDED" for t in main) or any(t["status"] not in {"SUCCEEDED", "TIMEOUT", "CANCELLED"} for t in auxiliary):
        errors.append("PROVIDER_FAILURE")
    if any(t["status"] == "RUNNING" or not t["start_timestamp"] or not t["end_timestamp"] for t in turns):
        errors.append("INCOMPLETE_REQUEST_OBSERVATION")
    if artifact["terminal"].get("status") != "completed":
        errors.append("AGENT_NOT_COMPLETED")
    return {"variant": artifact["variant"], "task_oracle_pass": None, "task_status": "SEMANTIC_REVIEW_PENDING",
            "strict_citation_pass": artifact["oracle"]["mechanical_pass"], "oracles": oracle,
            "terminal_status": artifact["terminal"].get("status"), "total_agent_latency_ms": artifact["total_agent_latency_ms"],
            "main_provider_latency_ms": sum(t["provider_latency_ms"] for t in main),
            "auxiliary_provider_latency_ms": sum(t["provider_latency_ms"] for t in auxiliary),
            "total_tool_execution_latency_ms": artifact["total_tool_latency_ms"],
            "main_provider_requests": len(main), "auxiliary_provider_requests": len(auxiliary),
            "input_tokens": all_usage["observable_input_tokens"] if all_usage["completeness"] == "COMPLETE" else None,
            "output_tokens": all_usage["observable_output_tokens"] if all_usage["completeness"] == "COMPLETE" else None,
            "usage_completeness": all_usage["completeness"], "provider_usage": artifact["provider_usage"],
            "tool_calls_total": sum(calls), "tool_calls_per_response": calls,
            "multi_tool_response_count": sum(n > 1 for n in calls), "parallel_eligible_batch_count": eligible_batches(main),
            "actual_parallel_batch_count": actual_batches, "actual_peak_concurrency": peak,
            "provider_failures": failures, "tool_operation_status_counts": oracle["tool_execution"]["operation_status_counts"],
            "retries": {"tool_retry_attempts": sum(max(0, r["payload"].get("attempt_count", 1) - 1) for r in records if r["type"] == "tool_execution_finished"),
                        "sdk_internal_http_retries": None, "production_model_transport_retries": None,
                        "limitation": "SDK internal attempts are not separate observed create calls; no inferred zero"},
            "execution_config": artifact["execution_config"], "configuration_verified": config_ok,
            "stop_reasons": errors, "artifact": str(directory / "artifact.json"), "transcript": str(transcript),
            "attribution": "Agent ON/OFF trajectories may differ; end-to-end delta is not scheduler causal evidence"}


def aggregate(pairs):
    """仅统计实际保留的完整配对，失败样本单独保留且不算成功。"""
    result = {}
    for task in TASKS:
        rows = [p for p in pairs if p["task"] == task and all(v in p["runs"] for v in ("ON", "OFF"))]
        if not rows:
            continue
        value = {"pair_count": len(rows), "semantic_success_count": None, "semantic_review": "SEMANTIC_REVIEW_PENDING"}
        for variant in ("ON", "OFF"):
            runs = [p["runs"][variant] for p in rows]
            value[variant] = {key: stats([r[key] for r in runs]) for key in
                              ("total_agent_latency_ms", "main_provider_latency_ms", "auxiliary_provider_latency_ms", "total_tool_execution_latency_ms")}
            value[variant]["strict_citation_pass_count"] = sum(r["strict_citation_pass"] for r in runs)
            value[variant]["tool_contract_pass_count"] = sum(r["oracles"]["tool_execution"]["status"] == r["oracles"]["tool_result_delivery"]["status"] == "PASS" for r in runs)
        ratios = [p["runs"]["OFF"]["total_agent_latency_ms"] / p["runs"]["ON"]["total_agent_latency_ms"] for p in rows]
        value.update(median_off_over_on_agent_latency=median(ratios), mean_agent_latency_reduction_pct=mean((1 - 1/r) * 100 for r in ratios),
                     limitation="n=3 intended; P95 is nearest-rank small-sample diagnostic, not stable population tail; trajectory differences confound scheduler attribution")
        result[task] = value
    return result


def worker(args):
    """子进程独立 SDK/Registry/Session；一个任务只运行一次。"""
    manifest = json.loads(args.manifest.read_text())
    if source_hashes() != manifest["source_hashes"]:
        raise RuntimeError("SOURCE_CHANGED_BEFORE_RUN")
    client = get_client()
    if get_model() != manifest["model"] or str(client.base_url) != manifest["provider"]:
        raise RuntimeError("PROVIDER_CONFIG_CHANGED")
    artifact = run_one(TASKS[args.worker_task], args.worker_output, args.worker_variant == "ON", client, manifest["source_hashes"])
    result = project(artifact, args.worker_output)
    if source_hashes() != manifest["source_hashes"]:
        result["stop_reasons"].append("PRODUCTION_SOURCE_CHANGED")
    save(args.worker_output / "metrics.json", result)
    print(json.dumps({"task": args.worker_task, "variant": args.worker_variant, "stop_reasons": result["stop_reasons"]}), flush=True)


def main():
    """先一对 Smoke，再九对正式实验，最后无模型重放。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--worker-task", choices=TASKS)
    parser.add_argument("--worker-variant", choices=("ON", "OFF"))
    parser.add_argument("--worker-output", type=Path)
    args = parser.parse_args()
    if args.worker_task:
        worker(args)
        return
    if args.output_dir is None or args.preflight is None:
        parser.error("需要 --output-dir 和 --preflight")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    client = get_client()
    preflight = json.loads(args.preflight.read_text())["result"]
    if preflight["status"] != "PASS" or preflight["model_id"] != get_model():
        raise RuntimeError("PREFLIGHT_NOT_MATCHED")
    config = replace(HexgentConfig(), loop=LoopLimits(max_model_rounds=16, max_tool_calls=100, max_elapsed_seconds=600))
    manifest = {"model": get_model(), "provider": str(client.base_url), "source_hashes": source_hashes(),
                "prompts": {name: prompt(task) for name, task in TASKS.items()}, "config": asdict(config),
                "allowed_tools": ["read_file", "glob"], "permission": "production default; confirmation deny",
                "formal_pairs_per_task": 3, "replay_pairs_per_trace": 20, "smoke_excluded_from_formal": True}
    manifest_path = output / "manifest.json"
    save(manifest_path, manifest)
    report = {"status": "RUNNING_SMOKE", "manifest_sha256": digest(manifest_path), "smoke_pairs": [], "formal_pairs": [], "replays": []}
    save(output / "report.json", report)
    try:
        for phase, jobs in (("smoke", [("tool_pipeline", 0)]),
                            ("formal", [(task, index) for task in TASKS for index in range(3)])):
            report["status"] = "RUNNING_" + phase.upper()
            for task, index in jobs:
                pair = {"task": task, "pair": index + 1, "order": ["ON", "OFF"] if index % 2 == 0 else ["OFF", "ON"], "runs": {}}
                report[phase + "_pairs"].append(pair)
                for variant in pair["order"]:
                    directory = output / phase / task / str(index + 1) / variant
                    print(f"START {phase} {task} pair={index+1} {variant}", flush=True)
                    completed = subprocess.run([sys.executable, "-m", "eval.tool_hardening.v3_experiment", "--manifest", str(manifest_path),
                                                "--worker-task", task, "--worker-variant", variant, "--worker-output", str(directory)], cwd=ROOT)
                    metric_file = directory / "metrics.json"
                    if metric_file.exists():
                        pair["runs"][variant] = json.loads(metric_file.read_text())
                    save(output / "report.json", report)
                    if completed.returncode or not metric_file.exists():
                        raise RuntimeError(f"WORKER_FAILURE:{phase}:{task}:{index+1}:{variant}")
                    metric = pair["runs"][variant]
                    if metric["stop_reasons"]:
                        raise RuntimeError(";".join(metric["stop_reasons"]))
                    if metric["execution_config"]["loop"] != manifest["config"]["loop"]:
                        raise RuntimeError("LOOP_CONFIG_CHANGED")
                    if source_hashes() != manifest["source_hashes"] or digest(manifest_path) != report["manifest_sha256"]:
                        raise RuntimeError("SOURCE_OR_MANIFEST_CHANGED")
                left, right = (pair["runs"][v] for v in ("ON", "OFF"))
                lconfig, rconfig = json.loads(json.dumps(left["execution_config"])), json.loads(json.dumps(right["execution_config"]))
                lconfig["tool_parallel"].pop("enabled")
                rconfig["tool_parallel"].pop("enabled")
                if lconfig != rconfig:
                    raise RuntimeError("ON_OFF_NON_SCHEDULER_CONFIG_DIFFERENCE")
        report["agent_summary"] = aggregate(report["formal_pairs"])
        report["status"] = "RUNNING_REPLAY"
        save(output / "report.json", report)
        from .v3_trace_replay import run_replays
        report["replays"] = run_replays(output, report["formal_pairs"])
        report["status"] = "COMPLETED_SEMANTIC_REVIEW_PENDING"
    except Exception as error:
        report["status"] = "STOPPED"
        report["stop_reason"] = f"{type(error).__name__}: {error}"
        report["agent_summary"] = aggregate(report["formal_pairs"])
        save(output / "report.json", report)
        print(report["stop_reason"], flush=True)
        return
    save(output / "report.json", report)
    print(report["status"], flush=True)


if __name__ == "__main__":
    main()
