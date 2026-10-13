"""R-L3 单次真实提取探针；语义审核前绝不派发 Summary。"""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import subprocess
from time import monotonic
from urllib.parse import urlsplit

from hexgent.client import get_client, get_model
from hexgent.context import ContextLimits, ContextManager
from hexgent.context.context_compaction import _select_cut_index, COMPACTION_INSTRUCTIONS
from hexgent.context.key_fact_extractor import EXTRACTION_INSTRUCTIONS, extract_snapshot, FactExtractionError
from .offline import ROOT, save, fingerprints


def now():
    return datetime.now(timezone.utc).isoformat()


class AuditClient:
    """透明转发 SDK 选项，只允许冻结的唯一 Extraction 请求。"""
    def __init__(self, client, output, expected, shared=None):
        self.client, self.output, self.expected = client, output, expected
        self.shared = shared if shared is not None else {"calls": 0, "ledger": []}
        self.responses = self

    def with_options(self, **options):
        if options.get("max_retries") != 0:
            raise RuntimeError("SDK_RETRY_GUARD")
        save(self.output/"sdk-options.json", options)
        return AuditClient(self.client.with_options(**options), self.output, self.expected, self.shared)

    def create(self, **request):
        if request != self.expected or self.shared["calls"] != 0:
            raise RuntimeError("FROZEN_REQUEST_OR_CALL_LIMIT_VIOLATION")
        self.shared["calls"] += 1
        save(self.output/"extractor-request.json", request)
        row = dict(request_type="FACT_EXTRACTION", local_call_id="R-L3-extractor-1", started_at=now(),
                   retry_count=0, input_tokens=None, output_tokens=None, status="STARTED")
        self.shared["ledger"].append(row)
        save(self.output/"provider-ledger.json", self.shared["ledger"])
        start = monotonic()
        stream = None
        try:
            stream = self.client.responses.create(**request)
            response = getattr(stream, "response", None)
            row["http_status"] = getattr(response, "status_code", None)
            row["http_headers"] = {k:v for k,v in getattr(response, "headers", {}).items()
                if k.lower() in {"x-request-id", "request-id", "date", "content-type"}}
            for index, event in enumerate(stream):
                value = event.model_dump(mode="json")
                save(self.output/"extractor-events"/f"{index:05d}.json", value)
                if value.get("type") == "response.completed":
                    body = value["response"]
                    save(self.output/"extractor-response.json", body)
                    usage = body.get("usage") or {}
                    row.update(status="COMPLETED", response_id=body.get("id"),
                               input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"))
                elif value.get("type") in {"response.failed", "response.incomplete", "error"}:
                    row.update(status="PROVIDER_FAILED", error_event=value)
                yield event
        except Exception as error:
            row.update(status="PROVIDER_FAILED", error_type=type(error).__name__, error=str(error),
                       request_id=getattr(error, "request_id", None))
            raise
        finally:
            if stream is not None:
                stream.close()
            row.update(ended_at=now(), latency_ms=(monotonic()-start)*1000,
                       usage_available=row["input_tokens"] is not None and row["output_tokens"] is not None)
            save(self.output/"provider-ledger.json", self.shared["ledger"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = ROOT/args.output
    if output.exists():
        raise RuntimeError("禁止覆盖历史 Artifact")
    output.mkdir(parents=True)
    acceptance = ROOT/"eval/context_remediation_v1/runs/acceptance-20261012-final"
    prep = ROOT/"eval/context_remediation_v1/runs/preflight-review-20261012-v2"
    hashes = json.loads((acceptance/"production-after.json").read_text())
    frozen = fingerprints()
    assert all(sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in hashes.items()), "PRODUCTION_HASH_CHANGED"
    assert frozen == json.loads((acceptance/"frozen-after.json").read_text()), "FROZEN_HASH_CHANGED"
    engineering = json.loads((acceptance/"result.json").read_text())
    assert engineering["errors"] == engineering["failures"] == 0
    row = json.loads((prep/"samples/R-L3.json").read_text())
    expected = json.loads((prep/"candidate-requests/R-L3.json").read_text())
    pre = json.loads((prep/"preflight.json").read_text())
    budget = next(r for r in pre["ledger"] if r["id"]=="R-L3")
    assert budget["first_cycle_estimated_token_ceiling"] <= 161067
    limits = ContextLimits()
    manager = ContextManager()
    for r in row["rounds"]:
        manager.append_user_query(r["query"])
        manager.append_messages(r["messages"], completes_round=True)
    manager.append_user_query(row["final_query"])
    cut = _select_cut_index(manager, limits.recent_context_tokens)
    client, model = get_client(), get_model()
    host = urlsplit(str(client.base_url)).hostname
    assert model == "gpt-6.1-sol" and host in {"rightapi.ai", "www.rightapi.ai"}
    for name, value in [("sample.json", row), ("candidate-requests.json", expected),
                        ("oracle.json", row["required_facts"]), ("config.json", vars(limits)),
                        ("prompts.json", dict(extractor=EXTRACTION_INSTRUCTIONS, summary=COMPACTION_INSTRUCTIONS)),
                        ("frozen-before.json", frozen)]:
        save(output/name, value)
    save(output/"frozen-manifest.json", dict(created_at=now(), sample="R-L3", main="FAKE",
        capacity="USER_ASSERTED_CAPACITY_128K_UNVERIFIED", auxiliary_call_limit=3, cycles=1,
        extractor_call_limit=1, summary_call_limit=2, sdk_hidden_retries=0, budget=budget,
        production_hashes=hashes, head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,text=True).strip(),
        worktree=subprocess.check_output(["git", "status", "--short"],cwd=ROOT,text=True).splitlines(),
        frozen_files={p.name:sha256(p.read_bytes()).hexdigest() for p in output.glob("*.json")},
        history_cutpoint=manager.message_ids[cut-1], provider_host=host, model=model))
    audit = AuditClient(client, output, expected["extraction"])
    try:
        snapshot, ledger = extract_snapshot(audit, model, manager, cut, limits)
        save(output/"snapshot.json", snapshot)
        save(output/"extraction-validation.json", ledger)
        result = dict(status="AWAITING_INDEPENDENT_EXTRACTION_QUALITY_REVIEW", real_auxiliary_calls=1,
                      real_main_calls=0, summary_calls=0, snapshot_runtime_validated=True)
    except FactExtractionError as error:
        save(output/"extraction-validation.json", error.ledger)
        gate = "STAGE_A_BLOCKED_INFRA" if error.ledger["status"]=="PROVIDER_FAILED" else "STAGE_A_FAIL_QUALITY"
        result = dict(gate=gate, reason=str(error), real_auxiliary_calls=audit.shared["calls"],
                      real_main_calls=0, summary_calls=0, quality="NOT_EVALUABLE" if gate.endswith("INFRA") else "REJECTED")
    save(output/"result.json", result)
    save(output/"frozen-after-extraction.json", fingerprints())
    print(json.dumps(dict(output=str(output), **result), ensure_ascii=False))


if __name__ == "__main__":
    main()
