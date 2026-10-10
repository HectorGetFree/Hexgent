"""两条既有真实轨迹独立重放；禁止网络，不重新调用模型。"""

import argparse
from collections import Counter
import json
from pathlib import Path
from unittest.mock import patch

from .v3_experiment import eligible_batches, source_hashes
from .v3_trace_replay import freeze, run_replays, write
from .v2_runner import digest


def main():
    """先完成两条轨迹预检，再各执行二十对交替重放。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--on-run", type=Path, required=True)
    parser.add_argument("--off-run", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    baseline = json.loads(args.source_manifest.read_text())["source_hashes"]
    if source_hashes() != baseline:
        raise RuntimeError("PRODUCTION_SOURCE_SNAPSHOT_MISMATCH")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    audit, pairs = {}, []
    for name, directory in (("ON_TRACE", args.on_run), ("OFF_TRACE", args.off_run)):
        trace = freeze(directory.resolve(), args.output_dir / f"{name}-frozen.json")
        workspace = Path(trace["workspace"])
        if {p: digest(workspace / p) for p in baseline} != baseline:
            raise RuntimeError("ORIGINAL_WORKSPACE_SOURCE_MISMATCH")
        dependencies = []
        for batch in trace["batches"]:
            for call in batch["calls"]:
                if call["name"] == "read_file":
                    path = Path(json.loads(call["arguments"])["path"])
                    path = (workspace / path).resolve()
                    if not path.is_relative_to(workspace) or not path.is_file():
                        raise RuntimeError("NON_REPLAYABLE_READ_SCOPE")
                    if ".hexgent" in path.parts:
                        dependencies.append({"call_id": call["call_id"], "path": str(path), "sha256": digest(path)})
                if not trace["permissions"][call["call_id"]] or any(
                    p["payload"].get("behavior") != "allow" for p in trace["permissions"][call["call_id"]]):
                    raise RuntimeError("ORIGINAL_PERMISSION_NOT_ALLOWED")
        turns = [{"tool_calls": b["calls"]} for b in trace["batches"]]
        eligible = eligible_batches(turns)
        audit[name] = {"status": "REPLAYABLE", "source": str(directory),
            "calls_per_response": [len(b["calls"]) for b in trace["batches"]],
            "tool_distribution": dict(Counter(c["name"] for b in trace["batches"] for c in b["calls"])),
            "eligible_batches": eligible, "frozen_prior_output_dependencies": dependencies,
            "state_isolation": "immutable original workspace; separate replay Session and cache per execution",
            "source_snapshot_matches": True}
        pairs.append({"task": name, "runs": {"ON": {"artifact": str(directory / "artifact.json"),
            "stop_reasons": [], "parallel_eligible_batch_count": eligible}}})
    write(args.output_dir / "replayability-audit.json", audit)
    # 网络请求在本实验中属于错误，而不是可重试故障。
    with patch("socket.socket.connect", side_effect=RuntimeError("NETWORK_FORBIDDEN_IN_REPLAY")), patch(
            "socket.create_connection", side_effect=RuntimeError("NETWORK_FORBIDDEN_IN_REPLAY")):
        results = run_replays(args.output_dir, pairs, tasks=list(audit))
    if source_hashes() != baseline:
        raise RuntimeError("PRODUCTION_SOURCE_CHANGED_DURING_REPLAY")
    write(args.output_dir / "report.json", {"status": "PASS", "model_requests": 0,
        "audit": audit, "results": results, "p95_method": "nearest rank: ceil(0.95 * 20)",
        "cross_trace_combined_speedup": None, "production_source_unchanged": True})


if __name__ == "__main__":
    main()
