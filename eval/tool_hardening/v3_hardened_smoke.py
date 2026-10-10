"""只执行 Tool Pipeline ON 一次；不启动对照或重放。"""

import argparse
from collections import Counter
import json
from pathlib import Path

from hexgent.client import get_client, get_model
from .v3_smoke import TASKS, run_one
from .v2_runner import ROOT, digest


def main():
    """使用既有成功 Preflight，输出独立 Smoke Gate 报告。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    preflight = json.loads(args.preflight.read_text())["result"]
    client = get_client()
    assert preflight["status"] == "PASS" and get_model() == preflight["model_id"]
    args.output_dir.mkdir(parents=True, exist_ok=False)
    protected = {str(p.relative_to(ROOT)): digest(p) for base in ("hexgent", "tests") for p in (ROOT / base).rglob("*.py")}
    artifact = run_one(TASKS["tool_pipeline"], args.output_dir / "tool_pipeline" / "ON", True, client, protected)
    main_turns = [t for t in artifact["turns"] if t["request_type"] == "MAIN_AGENT"]
    unexpected_provider = [t["turn_id"] for t in artifact["turns"] if t["status"] not in {"SUCCEEDED", "CANCELLED", "TIMEOUT"}]
    unexplained_usage = [t["turn_id"] for t in artifact["turns"] if not t["usage_available"] and not t["failure_reason"]]
    checks = {"tool_execution": artifact["oracles"]["tool_execution"]["status"] == "PASS",
              "tool_delivery": artifact["oracles"]["tool_result_delivery"]["status"] == "PASS",
              "observations_persisted": not artifact["observation_errors"], "usage_missing_attributed": not unexplained_usage,
              "no_systemic_provider_failure": not unexpected_provider, "source_unchanged": artifact["source_unchanged"],
              "terminal_completed": artifact["terminal"].get("status") == "completed"}
    report = {"gate": "PASS" if all(checks.values()) else "BLOCKED", "checks": checks,
              "proxy_equivalence": "validated by test_v3_provider before execution",
              "oracles": artifact["oracles"], "provider_usage": artifact["provider_usage"],
              "main_response_count": len(main_turns), "tool_calls_distribution": dict(Counter(len(t["tool_calls"]) for t in main_turns)),
              "successful_tool_calls": artifact["oracles"]["tool_execution"]["call_count"],
              "multi_call_response_fraction": sum(len(t["tool_calls"]) > 1 for t in main_turns) / len(main_turns) if main_turns else None,
              "actual_parallel_batches": sum(e["type"] == "ToolBatchStartedEvent" for e in artifact["events"]),
              "peak_concurrency": max((t["peak_concurrency"] for t in main_turns), default=0),
              "latency_ms": {k: artifact[k] for k in ("total_agent_latency_ms", "total_provider_latency_ms", "total_tool_latency_ms")},
              "unexplained_usage_requests": unexplained_usage, "unexpected_provider_requests": unexpected_provider,
              "task_success_not_implied_by_gate": True, "next_experiments_executed": False,
              "preflight": str(args.preflight), "source_hashes": protected}
    (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
