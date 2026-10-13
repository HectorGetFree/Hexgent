"""四臂Extraction Pilot；仅Eval请求变化，禁止重复已启动的实验臂。"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import difflib
import json
from pathlib import Path
import subprocess
from time import monotonic
from urllib.parse import urlsplit

from hexgent.context.context_compaction import _read_completed_response
from hexgent.context.key_fact_extractor import usage_dict
from .historical_prompts import V1 as EXTRACTION_INSTRUCTIONS
from hexgent.context.key_fact_snapshot import validate_snapshot
from hexgent.utils.token_estimator import estimate_request_tokens
from .offline import ROOT, save, fingerprints


ORDER=["00","10","01","11"]
PROMPT_ADDITION="""
事实选择补充标准v2：
有合法证据时，原样保留当前任务、批次、对象的精确标识，不以领域或主题概述替代。
保留关键已完成工作及其完成状态；阅读源码不等于完成审查，实现存在不等于测试通过，
也不能反过来因没有执行测试而否定已明确完成的其他工作。
保留未完成依赖、阻塞、下一步动作及其前置条件与禁止事项，不省略条件或否定词。
优先保留这些当前任务状态，而不是模块架构与宽泛阅读活动；原子事实不混合无关命题。
只陈述合法历史前缀支持的事实，不补充建议、授权要求或行动规则；没有证据就不编造。
确定的等待或未完成状态不等于证据不确定；只有未解决的冲突或未知取值才标uncertain。
本标准不改变Schema、来源引用、历史切点或意图参考的只读性质。
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def file_hashes(root):
    return {str(p.relative_to(root)):sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts and p.suffix!=".pyc"}


def prepare(output):
    if output.exists():
        raise RuntimeError("禁止覆盖已有Pilot目录")
    output.mkdir(parents=True)
    source=ROOT/"eval/context_remediation_v1/runs/stage-a-real-20261012-rl3"
    prior=json.loads((source/"frozen-manifest.json").read_text())
    original=json.loads((source/"extractor-request.json").read_text())
    payload=json.loads(original["input"][0]["content"])
    oracle=json.loads((source/"oracle.json").read_text())
    config=json.loads((source/"config.json").read_text())
    assert original["instructions"]==EXTRACTION_INSTRUCTIONS
    assert all(sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in prior["production_hashes"].items())
    assert fingerprints()==json.loads((source/"frozen-before.json").read_text())
    v2=original["instructions"]+PROMPT_ADDITION
    forbidden=[g["quote"] for g in oracle]+["AUDIT-MAPLE-57","参数与协议","失败恢复后的检索","两处权限契约","交接报告"]
    assert not any(term in v2 for term in forbidden), "PROMPT_ANSWER_LEAK"
    envelope=deepcopy(payload)
    envelope["envelope_v2"]={"kinds":{"U":"user","A":"assistant","C":"tool_call","T":"tool_output"},
        "order":"historical ascending; each item is one source boundary",
        "intent":"reference only; not a historical source"}
    for source_row in envelope["history_prefix"]:
        m=source_row["message"]
        source_row["k"]="T" if m.get("type")=="function_call_output" else "C" if m.get("type")=="function_call" else "U" if m.get("role")=="user" else "A"
    stripped=deepcopy(envelope)
    stripped.pop("envelope_v2")
    for s in stripped["history_prefix"]:
        s.pop("k")
    assert stripped==payload, "ENVELOPE_NOT_REVERSIBLE"
    budgets=[]
    for arm in ORDER:
        request=deepcopy(original)
        if arm[0]=="1":
            request["instructions"]=v2
        if arm[1]=="1":
            request["input"][0]["content"]=json.dumps(envelope,ensure_ascii=False)
        estimate=estimate_request_tokens(request["instructions"],request["input"],request.get("tools",[]))
        assert request["max_output_tokens"]==4000 and estimate-54345<=1024
        assert estimate+4000<=59369
        assert estimate+4000+config["safety_margin_tokens"]<=config["auxiliary_context_window_tokens"]
        save(output/"requests"/(arm+".json"),request)
        budgets.append(dict(arm=arm,estimated_input_tokens=estimate,increment_over_54345=estimate-54345,
            output_limit=4000,total_estimated_ceiling=estimate+4000,safety_margin_tokens=config["safety_margin_tokens"]))
    assert sum(r["total_estimated_ceiling"] for r in budgets)<=237476
    for name,value in [("oracle.json",oracle),("config.json",config),("original-payload.json",payload),
                       ("envelope-v2.json",envelope),("sample.json",json.loads((source/"sample.json").read_text()))]:
        save(output/name,value)
    (output/"prompt-v1.txt").write_text(original["instructions"],encoding="utf-8")
    (output/"prompt-v2.txt").write_text(v2,encoding="utf-8")
    (output/"prompt-diff.txt").write_text("".join(difflib.unified_diff(original["instructions"].splitlines(True),v2.splitlines(True),fromfile="v1",tofile="v2")),encoding="utf-8")
    save(output/"input-difference.json",dict(added_top_level=["envelope_v2"],added_per_source=["k"],
        modified_source_bodies=0,moved_sources=0,filtered_sources=0,highlighted_facts=0,
        body_id_order_cutpoint_intent_equality=True,original_history_count=len(payload["history_prefix"])))
    save(output/"isolation.json",dict(status="PASS",only_factors=["instructions:v1/v2","input:source-kind envelopes"],
        prompt_v2_answer_free=True,source_bodies_identity_order_equal=True,round_cutpoint_equal=True,
        latest_query_plan_read_only_equal=True,no_gold_metadata_in_envelope=True,budgets=budgets,
        sampling="temperature/top_p/seed未设置；四臂一致使用Provider默认，实际随机状态不可固定"))
    (output/"quality-rules.md").write_text("""# 冻结质量规则\n\n独立人工审核，同一标准用于四臂；不以关键词命中代替蕴含。\n精确身份、两项完成里程碑、待确认依赖、确认后下一步与不发布，分别五项；相关架构/源码阅读不能替代。\n每项须在Snapshot content中完整体现，并由其合法source_refs支持，不能仅在原输入或其他facts主题相似。\n统计不支持命题与状态/依赖/否定错误，逐事实拆分而非按一个fact合并遮掩；不确定的支持关系保留UNCERTAIN。\n来源建议不应升级为历史事实；当前Query/Plan仅参考，不得作为历史source。\n合法来源与Schema、快照预算、未截断、完整Usage分别判断，不把结构通过算作Recall。\n不得因某臂结果修改本规则，质量失败不重跑；无稳定性/显著性宣称。\n""",encoding="utf-8")
    inputs={str(p.relative_to(output)):sha256(p.read_bytes()).hexdigest() for p in output.rglob("*") if p.is_file()}
    save(output/"manifest.json",dict(experiment="EXTRACTOR-QUALITY-PILOT-V1",created_at=now(),order=ORDER,
        requests_per_arm=1,total_call_limit=4,model="gpt-6.1-sol",provider_hosts=["rightapi.ai","www.rightapi.ai"],
        capacity="USER_ASSERTED_CAPACITY_128K_UNVERIFIED",max_retries=0,timeout_seconds=config["fact_extraction_timeout_seconds"],
        sampling=dict(temperature="OMITTED_PROVIDER_DEFAULT",top_p="OMITTED_PROVIDER_DEFAULT",seed="OMITTED_PROVIDER_DEFAULT"),
        schema_file="hexgent/context/key_fact_snapshot.py",schema_sha256=sha256((ROOT/"hexgent/context/key_fact_snapshot.py").read_bytes()).hexdigest(),
        frozen_files=inputs,production_hashes=prior["production_hashes"],frozen_asset_hashes=fingerprints(),
        original_run_hashes=file_hashes(source),diagnosis_hashes=file_hashes(ROOT/"eval/context_remediation_v1/diagnostics/extractor_quality"),
        historical_reference=str(source),budgets=budgets,estimated_total_ceiling=sum(r["total_estimated_ceiling"] for r in budgets),
        no_summary_main_compaction=True,head=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()))
    print(json.dumps(dict(status="OFFLINE_GATE_PASS",output=str(output),budgets=budgets),ensure_ascii=False),flush=True)


def check_integrity(output,manifest):
    for p,h in manifest["frozen_files"].items():
        if sha256((output/p).read_bytes()).hexdigest()!=h:
            raise RuntimeError("PILOT_FROZEN_INPUT_CHANGED:"+p)
    for p,h in manifest["production_hashes"].items():
        if sha256((ROOT/p).read_bytes()).hexdigest()!=h:
            raise RuntimeError("PRODUCTION_CHANGED:"+p)
    if fingerprints()!=manifest["frozen_asset_hashes"]:
        raise RuntimeError("FROZEN_ASSETS_CHANGED")
    if file_hashes(Path(manifest["historical_reference"]))!=manifest["original_run_hashes"]:
        raise RuntimeError("ORIGINAL_STAGE_A_CHANGED")
    if file_hashes(ROOT/"eval/context_remediation_v1/diagnostics/extractor_quality")!=manifest["diagnosis_hashes"]:
        raise RuntimeError("DIAGNOSIS_CHANGED")


class StreamAudit:
    def __init__(self,client,output,arm,request):
        self.client,self.output,self.arm,self.request=client,output,arm,request
        self.row=dict(arm=arm,local_call_id="pilot-v1:"+arm,started_at=now(),status="STARTED",retry_count=0,
            input_tokens=None,output_tokens=None,request_sha256=sha256(json.dumps(request,ensure_ascii=False,sort_keys=True).encode()).hexdigest())

    def __iter__(self):
        stream=None
        start=monotonic()
        save(self.output/"ledger.json",self.row)
        save(self.output/"actual-request.json",self.request)
        try:
            stream=self.client.responses.create(**self.request)
            http=getattr(stream,"response",None)
            self.row.update(http_status=getattr(http,"status_code",None),
                headers={k:v for k,v in getattr(http,"headers",{}).items() if k.lower() in {"x-request-id","request-id","date","content-type"}})
            for index,event in enumerate(stream):
                body=event.model_dump(mode="json")
                save(self.output/"events"/f"{index:05d}.json",body)
                if body.get("type")=="response.completed":
                    response=body["response"]
                    save(self.output/"response.json",response)
                    usage=response.get("usage") or {}
                    self.row.update(status="COMPLETED",response_id=response.get("id"),input_tokens=usage.get("input_tokens"),output_tokens=usage.get("output_tokens"))
                elif body.get("type") in {"response.failed","response.incomplete","error"}:
                    self.row.update(status="PROVIDER_FAILED",error_event=body)
                yield event
        except Exception as error:
            self.row.update(status="PROVIDER_FAILED",error_type=type(error).__name__,error=str(error),
                request_id=getattr(error,"request_id",None),http_status=getattr(error,"status_code",self.row.get("http_status")))
            raise
        finally:
            if stream is not None:
                stream.close()
            self.row.update(ended_at=now(),latency_ms=(monotonic()-start)*1000,
                usage_available=self.row["input_tokens"] is not None and self.row["output_tokens"] is not None)
            save(self.output/"ledger.json",self.row)


def execute(output, order=None):
    manifest=json.loads((output/"manifest.json").read_text())
    if (output/"execution-started.json").exists():
        raise RuntimeError("本Pilot已启动；禁止重新执行或续跑导致重复调用")
    check_integrity(output,manifest)
    from hexgent.client import get_client,get_model
    base=get_client()
    model=get_model()
    assert model==manifest["model"] and urlsplit(str(base.base_url)).hostname in manifest["provider_hosts"]
    client=base.with_options(max_retries=0,timeout=manifest["timeout_seconds"])
    assert client.max_retries==0
    save(output/"execution-started.json",dict(started_at=now(),model=model,provider_host=urlsplit(str(base.base_url)).hostname,max_retries=0))
    config=json.loads((output/"config.json").read_text())
    original=json.loads((output/"original-payload.json").read_text())
    all_rows=[]
    gate="AWAITING_INDEPENDENT_QUALITY_REVIEW"
    for arm in (ORDER if order is None else order):
        try:
            check_integrity(output,manifest)
            request=json.loads((output/"requests"/(arm+".json")).read_text())
            tokens=estimate_request_tokens(request["instructions"],request["input"],request.get("tools",[]))
            assert tokens+4000<=59369 and tokens+4000+config["safety_margin_tokens"]<=config["auxiliary_context_window_tokens"]
        except Exception as error:
            gate="PILOT_BLOCKED_SAFETY_OR_AUDIT"
            save(output/"stop.json",dict(arm=arm,reason=str(error)))
            break
        directory=output/"arms"/arm
        if directory.exists():
            raise RuntimeError("重复臂禁止执行")
        directory.mkdir(parents=True)
        print("START "+arm,flush=True)
        audit=StreamAudit(client,directory,arm,request)
        try:
            start=monotonic()
            response=_read_completed_response(iter(audit),deadline=start+manifest["timeout_seconds"])
        except Exception as error:
            all_rows.append(audit.row)
            gate="PILOT_BLOCKED_INFRA"
            save(output/"stop.json",dict(arm=arm,reason=str(error),no_retry=True))
            break
        all_rows.append(audit.row)
        if not audit.row["usage_available"] or not audit.row.get("response_id"):
            gate="PILOT_BLOCKED_SAFETY_OR_AUDIT"
            save(output/"stop.json",dict(arm=arm,reason="USAGE_OR_RESPONSE_ASSOCIATION_MISSING"))
            break
        try:
            raw=json.loads(response.output_text)
            save(directory/"parsed-response.json",raw)
            snapshot=validate_snapshot(raw,cutpoint=original["as_of_message_id"],sources=original["history_prefix"],
                previous=original["previous_snapshot"],budget_tokens=config["key_fact_snapshot_tokens"],excluded_source_ids=())
            save(directory/"snapshot.json",snapshot)
            save(directory/"structural-validation.json",dict(status="PASS",source_schema_snapshot_budget=True))
        except Exception as error:
            save(directory/"structural-validation.json",dict(status="FAIL",error=str(error),quality_failure_continue=True))
        save(output/"provider-ledger.json",all_rows)
        print("END "+arm+" "+audit.row["status"],flush=True)
    try:
        check_integrity(output,manifest)
        integrity="PASS"
    except Exception as error:
        integrity=str(error)
        gate="PILOT_BLOCKED_SAFETY_OR_AUDIT"
    save(output/"provider-ledger.json",all_rows)
    save(output/"execution-result.json",dict(status=gate,arms_started=[r["arm"] for r in all_rows],
        real_calls=len(all_rows),summary_calls=0,main_calls=0,integrity=integrity))
    print(json.dumps(dict(output=str(output),status=gate,real_calls=len(all_rows)),ensure_ascii=False),flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--prepare",action="store_true")
    parser.add_argument("--execute",action="store_true")
    args=parser.parse_args()
    output=args.output.resolve()
    if args.prepare:
        prepare(output)
    if args.execute:
        execute(output)


if __name__=="__main__":
    main()
