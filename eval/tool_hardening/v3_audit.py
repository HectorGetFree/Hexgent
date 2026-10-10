"""只重投影既有 Smoke Artifact；不发模型请求、不覆盖原报告。"""

from collections import Counter
import json
from pathlib import Path

from .v3_smoke import TASKS, oracle


def main():
    """输出独立更正报告，区分主任务与辅助模型调用。"""
    run = Path(__file__).with_name("runs") / "v3-smoke-20261008"
    source = run / "tool_pipeline" / "ON"
    artifact = json.loads((source / "artifact.json").read_text())
    workspace = source / "workspace"
    transcript = next((workspace / ".hexgent" / "sessions").glob("*/transcript.jsonl"))
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    turns = artifact["turns"]
    main_turns = [t for t in turns if not str(t["request"].get("instructions", "")).startswith("你负责从给定的会话事实中提取可复用记录")]
    usage = [t["usage"] for t in turns if t["usage"]]
    answer = artifact["terminal"].get("output_text", "")
    revised = oracle(TASKS["tool_pipeline"], records, answer, workspace)
    value = json.loads(answer)
    quote_checks = []
    for i, finding in enumerate(value["findings"]):
        for j, evidence in enumerate(finding["evidence"]):
            path = workspace / evidence["path"]
            quote_checks.append({"finding": i + 1, "evidence": j + 1, "path": evidence["path"], "verbatim_match": evidence["quote"] in path.read_text(), "contains_ellipsis": "..." in evidence["quote"]})
    count = sum(len(t["tool_calls"]) for t in main_turns)
    finished = [r["payload"] for r in records if r["type"] == "tool_execution_finished"]
    result = {
        "status": "STOPPED_SMOKE_GATE",
        "source_artifact": str(source / "artifact.json"),
        "original_oracle_path_bug": "relative workspace compared with resolved absolute read path; fixed only Eval and reprojected existing facts",
        "corrected_oracle": revised,
        "quote_checks": quote_checks,
        "main_response_count": len(main_turns),
        "auxiliary_call_count": len(turns) - len(main_turns),
        "tool_calls": count,
        "tool_calls_per_response": dict(Counter(len(t["tool_calls"]) for t in main_turns)),
        "mean_tool_calls_per_response": count / len(main_turns),
        "multi_call_response_count": sum(len(t["tool_calls"]) > 1 for t in main_turns),
        "multi_call_response_fraction": sum(len(t["tool_calls"]) > 1 for t in main_turns) / len(main_turns),
        "eligible_readonly_calls": sum(t["eligible_parallel_calls"] for t in main_turns),
        "actual_concurrent_responses": sum(t["peak_concurrency"] > 1 for t in main_turns),
        "peak_concurrency": max(t["peak_concurrency"] for t in main_turns),
        "handler_entries": sum(t["handler_entries"] for t in main_turns),
        "tool_status_counts": dict(Counter(p["status"] for p in finished)),
        "known_provider_input_tokens_partial": sum(u["input_tokens"] for u in usage),
        "known_provider_output_tokens_partial": sum(u["output_tokens"] for u in usage),
        "usage_coverage": {"completed_usage_calls": len(usage), "all_provider_calls": len(turns)},
        "extraction_failures": [r for r in records if r["type"] == "context_record_extraction_failed"],
        "latency": {k: artifact[k] for k in ("total_agent_latency_ms", "total_provider_latency_ms", "total_tool_latency_ms")},
        "remaining_runs": "NOT_RUN: Tool OFF, Recovery ON/OFF, Memory ON/OFF",
        "trace_replay": "NOT_RUN: smoke gate not passed",
    }
    destination = run / "audit-corrected.json"
    with destination.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k not in ("quote_checks", "extraction_failures")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
