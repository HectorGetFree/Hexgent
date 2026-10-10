"""只读原始 Smoke 工件，按 Provider span 生成独立修订报告，不重跑模型。"""
import argparse
import json
from pathlib import Path

from eval.memory_m1 import dump


def correlate(events):
    starts = {e["span_id"]: e for e in events if e["event"] == "provider_started"}
    ends = {e["span_id"]: e for e in events if e["event"] == "provider_finished"}
    return [{**start, **ends.get(span, {}), "request_type": start["request_type"],
             "finished_observed": span in ends} for span, start in starts.items()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    root = args.run.resolve()
    original = json.loads((root / "report.json").read_text())
    cases, requests = [], []
    for row in original["scenarios"]:
        case = root / row["id"]
        stages = [json.loads(p.read_text()) for p in sorted(case.glob("*/result.json"))]
        spans = correlate([e for s in stages for e in s["provider_events"]])
        requests.extend([{**r, "case": row["id"]} for r in spans])
        extraction = [r for r in spans if r["request_type"] == "CONTEXT_RECORD_EXTRACTION"]
        failed = [r for r in extraction if r.get("status") != "SUCCEEDED"]
        fixed = dict(row)
        if row["qualification"] != "QUALIFIED" and failed:
            fixed["qualification"] = "PROVIDER_FAILURE"
            fixed["oracles"] = {**row["oracles"], "source_extraction": "PROVIDER_FAILURE"}
        fixed["failure_evidence"] = failed
        fixed["main_terminal_states"] = [s["terminal_status"] for s in stages]
        fixed["successful_file_reads"] = sum(t["tool_name"] == "read_file" and t["status"] == "success" for s in stages for t in s["tool_calls"])
        fixed["qualified_record_count"] = sum(r["qualified"] for r in json.loads((case / "stage-a-qualification.json").read_text())["records"])
        fixed["unexecuted_oracles"] = ["selection", "injection", "final_answer"] if row["status"] == "BLOCKED" else []
        fixed["other_channels"] = dict(memory_injected=sum(len(receipt.get("memories", [])) for s in stages for receipt in s["memory_receipts"]),
                                      history_preserved=True, query_not_executed=row["status"] == "BLOCKED")
        cases.append(fixed)
    usage = {}
    for category in ("MAIN_AGENT", "CONTEXT_RECORD_EXTRACTION", "COMPACTION"):
        selected = [r for r in requests if r["request_type"] == category]
        available = [r for r in selected if r.get("usage_available") and r.get("input_tokens") is not None and r.get("output_tokens") is not None]
        complete = bool(selected) and len(selected) == len(available)
        usage[category] = dict(requests=len(selected), usage_available=len(available), usage_missing=len(selected)-len(available),
            completeness="NOT_APPLICABLE" if not selected else "COMPLETE" if complete else "INCOMPLETE",
            provider_input_tokens=sum(r["input_tokens"] for r in available) if complete else None,
            provider_output_tokens=sum(r["output_tokens"] for r in available) if complete else None,
            observable_input_tokens=sum(r["input_tokens"] for r in available),
            observable_output_tokens=sum(r["output_tokens"] for r in available))
    report = {**original, "revision": "span-correlated-v3", "original_report": str(root / "report.json"),
              "scenarios": cases, "provider_usage": usage, "provider_requests": requests,
              "next_stage": "NO_GO" if any(c["status"] != "PASS" for c in cases) else "GO_PENDING_REVIEW",
              "raw_artifacts_unchanged": True}
    dump(root / "report-audited-v3.json", report)
    print(json.dumps(dict(report=str(root / "report-audited-v3.json"), status=report["status"], qualified=report["qualified"], usage=usage), ensure_ascii=False))


if __name__ == "__main__":
    main()
