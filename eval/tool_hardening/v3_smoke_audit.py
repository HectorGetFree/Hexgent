"""只读原始 Smoke 事实，生成增强 Oracle 与验证报告；不调用模型。"""

import argparse
import json
from pathlib import Path

from .v2_runner import ROOT, digest
from .v3_oracles import layered_oracles
from .v3_smoke import TASKS, oracle


def main():
    """保留原始报告，独立复核 Finished、交付和逐条引用。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    source = args.run / "tool_pipeline" / "ON"
    raw = json.loads((source / "artifact.json").read_text())
    report = json.loads((args.run / "report.json").read_text())
    workspace = source / "workspace"
    transcript = next((workspace / ".hexgent" / "sessions").glob("*/transcript.jsonl"))
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    evidence = oracle(TASKS["tool_pipeline"], records, raw["terminal"].get("output_text", ""), workspace)
    layers = layered_oracles(records, raw["turns"], evidence, workspace)
    source_unchanged = all(digest(ROOT / p) == h for p, h in report["source_hashes"].items())
    gate = report["gate"] == "PASS" and source_unchanged and all(layers[k]["status"] == "PASS" for k in ("tool_execution", "tool_result_delivery"))
    result = {"gate": "PASS" if gate else "BLOCKED", "original_report": str(args.run / "report.json"),
              "original_artifact": str(source / "artifact.json"), "transcript": str(transcript),
              "original_artifact_sha256": digest(source / "artifact.json"), "oracles": layers,
              "production_source_unchanged": source_unchanged, "provider_usage": raw["provider_usage"],
              "validation": {"executed_by_this_auditor": False, "evidence": "V3_HARDENING_REPORT.md records separately executed validation; this auditor does not manufacture test counts"},
              "auxiliary_missing_usage": [t for t in raw["turns"] if t["request_type"] != "MAIN_AGENT" and not t["usage_available"]],
              "allow_next_on_off_and_replay": gate, "new_model_requests": 0,
              "limitation": "Task Evidence PASS proves required reads and exact quotations, not an automatic semantic truth judgment. Known tokens exclude cancelled extraction."}
    with (args.run / "audit-report.json").open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"gate": result["gate"], "oracles": {k: v["status"] for k, v in layers.items()},
                      "quotes": len(evidence["quote_checks"]), "source_unchanged": source_unchanged}, ensure_ascii=False))


if __name__ == "__main__":
    main()
