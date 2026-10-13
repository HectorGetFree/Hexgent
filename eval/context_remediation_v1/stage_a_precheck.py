"""Stage A 前置冻结与非推理路由元数据检查；不提供质量调用入口。"""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from time import monotonic
from urllib.parse import urlsplit

from hexgent.client import get_client, get_model
from hexgent.context import ContextLimits
from hexgent.context.context_compaction import COMPACTION_INSTRUCTIONS
from hexgent.context.key_fact_extractor import EXTRACTION_INSTRUCTIONS
from .offline import ROOT, fingerprints, save


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata", action="store_true", help="只允许一次 GET models，不发送推理请求")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise RuntimeError("禁止覆盖已有 Stage A 目录")
    output.mkdir(parents=True)
    baseline = ROOT/"eval/context_remediation_v1/runs/acceptance-20261012-final"
    preflight = ROOT/"eval/context_remediation_v1/runs/preflight-review-20261012-v2"
    before = fingerprints()
    source = json.loads((baseline/"production-after.json").read_text())
    production_ok = all((ROOT/p).is_file() and sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in source.items())
    frozen_ok = before == json.loads((baseline/"frozen-after.json").read_text())
    evidence = json.loads((baseline/"result.json").read_text())
    pre = json.loads((preflight/"preflight.json").read_text())
    ledger = next(r for r in pre["ledger"] if r["id"]=="R-L3")
    oracle = next(r for r in pre["samples"] if r["id"]=="R-L3")["required_facts"]
    frozen_files = {}
    for name in ("samples/R-L3.json", "candidate-requests/R-L3.json"):
        data = (preflight/name).read_bytes()
        destination = output/name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        frozen_files[name] = sha256(data).hexdigest()
    save(output/"oracle.json", dict(required_facts=oracle, expected_count=5,
                                  semantic_review="每条原始证据支持、状态/条件/依赖独立人工判定"))
    save(output/"prompts.json", dict(extractor=EXTRACTION_INSTRUCTIONS, summary=COMPACTION_INSTRUCTIONS))
    save(output/"config.json", vars(ContextLimits()))
    for name in ("oracle.json", "prompts.json", "config.json"):
        frozen_files[name] = sha256((output/name).read_bytes()).hexdigest()
    budgets_ok = ledger["max_extractor_requests"]==1 and ledger["max_summary_requests"]==2 and ledger["first_cycle_estimated_token_ceiling"]<=161067
    manifest = dict(created_at=datetime.now(timezone.utc).isoformat(), sample="R-L3", cycles=1,
        head=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
        worktree=subprocess.check_output(["git","status","--short"],cwd=ROOT,text=True).splitlines(),
        production_hashes=source, production_matches_engineering_acceptance=production_ok,
        frozen_matches_engineering_acceptance=frozen_ok,
        engineering_tests=evidence["engineering_tests"], engineering_errors=evidence["errors"],
        engineering_failures=evidence["failures"], frozen_files=frozen_files, budget=ledger,
        main="FAKE", sdk_hidden_retries=0, quality_calls_allowed=3,
        stops=["容量无独立证据", "预算不成立", "审计不完整", "来源越界/无依据事实", "预算绕过", "提交/恢复不一致"],
        prior_capacity_evidence="MANUAL_USER_CONFIRMATION_NOT_ACCEPTED_AS_PROVIDER_PROOF")
    save(output/"frozen-manifest.json", manifest)
    save(output/"frozen-before.json", before)
    metadata = dict(request_type="MODEL_METADATA_NON_INFERENCE", real_auxiliary_calls=0,
                    provider_capacity_confirmed=False, provider_connectivity="NOT_TESTED")
    if not production_ok or not frozen_ok or not budgets_ok or evidence["errors"] or evidence["failures"]:
        gate = "STAGE_A_BLOCKED_SAFETY_OR_AUDIT"
        reason = "工程哈希、冻结资产或预算 Gate 不成立"
    elif not args.metadata:
        gate = "STAGE_A_BLOCKED_INFRA"
        reason = "尚未取得独立 Provider 容量证据"
    else:
        client = get_client().with_options(max_retries=0, timeout=30.0)
        model = get_model()
        host = urlsplit(str(client.base_url)).hostname
        if host not in {"www.rightapi.ai", "rightapi.ai"} or model!="gpt-6.1-sol":
            gate, reason = "STAGE_A_BLOCKED_SAFETY_OR_AUDIT", "实际 Provider/model 与批准范围不符"
        else:
            started = monotonic()
            metadata.update(provider_host=host, model=model, method="GET", started_at=datetime.now(timezone.utc).isoformat(), retry_count=0)
            try:
                response = client.models.with_raw_response.list()
                body = response.http_response.json()
                save(output/"provider-models-response.json", body)
                metadata.update(http_status=response.status_code,
                    response_headers={k:v for k,v in response.headers.items() if k.lower() in {"x-request-id","request-id","date","content-type"}},
                    provider_connectivity="METADATA_ENDPOINT_REACHABLE_NOT_INFERENCE_VALIDATION")
                rows = body.get("data", []) if isinstance(body, dict) else []
                matching = [r for r in rows if isinstance(r, dict) and r.get("id")==model]
                metadata["matching_model_metadata"] = matching
                # 不从别名、模型名或厂商基础模型容量猜测账号实际路由容量。
                capacity_fields = {k:v for row in matching for k,v in row.items()
                                   if k in {"context_window", "context_window_tokens", "max_context_tokens", "max_input_tokens"}}
                metadata["capacity_fields"] = capacity_fields
                if capacity_fields:
                    metadata["capacity_evidence_requires_review"] = True
                    reason = "模型元数据含容量字段，需核对字段语义与本账号路由绑定；未发送质量调用"
                else:
                    reason = "Provider models 元数据没有可核对的本路由上下文容量，不能进入质量调用"
                gate = "STAGE_A_BLOCKED_INFRA"
            except Exception as error:
                metadata.update(provider_connectivity="FAILED", error_type=type(error).__name__,
                                http_status=getattr(error,"status_code",None), request_id=getattr(error,"request_id",None))
                gate, reason = "STAGE_A_BLOCKED_INFRA", "模型元数据请求失败，且容量仍未确认"
            metadata.update(latency_ms=(monotonic()-started)*1000, ended_at=datetime.now(timezone.utc).isoformat())
    save(output/"provider-precheck.json", metadata)
    after = fingerprints()
    save(output/"frozen-after.json", after)
    if before!=after:
        gate, reason = "STAGE_A_BLOCKED_SAFETY_OR_AUDIT", "冻结资产在预检查期间变化"
    result = dict(gate=gate, reason=reason, real_auxiliary_calls=0, real_main_calls=0,
                  non_inference_metadata_requests=1 if metadata.get("method")=="GET" else 0,
                  estimated_auxiliary_token_ceiling=ledger["first_cycle_estimated_token_ceiling"],
                  provider_actual_tokens=None, capacity_confirmed=False, frozen_unchanged=before==after,
                  quality="NOT_RUN", next="等待可验证 Provider 容量与路由证据，不自动重试")
    save(output/"result.json", result)
    (output/"REPORT.md").write_text("# R-L3 Stage A 前置检查\n\n"+gate+"\n\n"+reason+
        "\n\n质量调用与真实 MAIN 均未执行。完整输入、Oracle、Prompt、预算及工作区已冻结。"
        "元数据端点成功不代表推理可用，也不证明 128K。无真实调用，Usage 为 null，不填零。\n",encoding="utf-8")
    print(json.dumps(dict(output=str(output),**result),ensure_ascii=False))


if __name__=="__main__":
    main()
