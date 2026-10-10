"""M3 两例预注册真实 Extraction Smoke；只新增 Eval，不补写记录。"""
import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from hexgent.agent_engine import AgentEngine
from hexgent.client import get_client, get_model
from hexgent.config import load_config
from hexgent.observation import source_fingerprint
from eval.memory_m1 import dump, tree_hash
from eval.memory_m2_real import RequestRecorder, run_stage
from eval.memory_m3.audit_smoke import correlate

CASES = [
    dict(id="C1", entity="Selka-R8B", documents={
        "capacity.md":"Selka-R8B 的处理区域为 cedar-47。\nSelka-R8B 每批最多处理11份申请。\n这是本周审批任务的固定业务配置，不涉及真实用户。\n",
        "schedule.md":"Selka-R8B 当日提交截止时间为16:20。\n截止后未提交的任务转到下一工作日，不能由执行者自行延长窗口。\n",
        "other.md":"Ferin-J3 的检查项目为资料完整性与负责人签名；只需列出两个检查项目，不涉及Selka-R8B配置。\n"},
        facts=["cedar-47", "11", "16:20"], expected=dict(region="cedar-47", batch_count=3, deadline="16:20"),
        prompts=["阅读 capacity.md 和 schedule.md，核对 Selka-R8B 的区域、单批容量和截止时间。请实际读取资料，简要说明依据，不调用 remember。",
                 "接下来处理独立资料 other.md，实际读取后列出 Ferin-J3 的检查项目。不要修改文件或保存长期记忆。"],
        query='回到 Selka-R8B，今天有23份申请需要提交，请结合已核对的配置给出处理决策。只返回JSON，字段恰好为 region、batch_count、deadline；不调用 remember。'),
    dict(id="C2", entity="Korin-W6P", documents={
        "old-config.md":"Korin-W6P 当前服务端口为43127。\n该端口是旧配置，仅在用户明确更正之前有效。\n",
        "checklist.md":"Korin-W6P 的上线检查是健康探针与负责人确认。检查清单不改变端口配置。\n"},
        facts=["43127"], new_fact="Korin-W6P 当前服务端口为58239。", expected=dict(port=58239),
        correction_prompts=["我明确更正并撤销旧端口43127，以新配置替代旧记录：Korin-W6P 当前服务端口为58239。后续以新端口为准，不修改文件，不调用 remember。",
                            "实际读取 checklist.md，核对端口更正是否改变其中的上线检查要求。不要读取旧配置或修改文件，不调用 remember。",
                            "简要总结 Korin-W6P 当前端口与上线检查，明确哪个值已由用户撤销。不读取旧配置，不修改文件，不调用 remember。"],
        prompts=["实际读取 old-config.md，核对 Korin-W6P 的当前服务端口并简要说明依据。不要修改文件或调用 remember。",
                 "实际读取 checklist.md，总结 Korin-W6P 的两个上线检查项目，不修改配置，不调用 remember。"],
        query='Korin-W6P 的当前服务端口是什么？只返回JSON，字段恰好为 port，值为数字。不调用 remember。'),
]


class CaptureStream:
    def __init__(self, stream, record):
        self.stream, self.record = stream, record
        self.iterator = iter(stream)
    def __iter__(self):
        return self
    def __next__(self):
        event = next(self.iterator)
        if event.type == "response.completed":
            response = event.response
            self.record["completed_response"] = response.model_dump(mode="json") if hasattr(response, "model_dump") else dict(output_text=response.output_text)
            self.record["completed_response"]["output_text"] = response.output_text
        elif event.type in {"response.failed", "response.incomplete", "error"}:
            self.record["failed_event"] = event.model_dump(mode="json") if hasattr(event,"model_dump") else str(event)
        return event
    def close(self):
        close = getattr(self.stream,"close",None)
        if callable(close):
            close()
    def __getattr__(self,name):
        return getattr(self.stream,name)


class CaptureClient(RequestRecorder):
    def with_options(self, **options):
        return CaptureClient(self._client.with_options(**options), self.records)
    def create(self, **request):
        stream = super().create(**request)
        return CaptureStream(stream, self.records[-1])


def perform(root, stage, engine, prompt):
    captured = []
    result = run_stage(root,stage,engine,prompt,client=CaptureClient(get_client(), captured))
    dump(root/stage/"complete-provider-requests-responses.json",captured)
    return result


def blocks(requests):
    result=[]
    for record in requests:
        text=record["request"].get("instructions","")
        match=re.search(r"<relevant-session-context>.*?</relevant-session-context>",text,re.S)
        if match:
            part=match.group(0)
            result.extend(json.loads(part[part.index("[{"):part.index("</relevant-session-context>")].strip()))
    return result


def qualify(case, engine, stages):
    transcript=engine.session.store.read_transcript()
    sources={r["sequence"]:r for r in transcript}
    candidates=[]
    for path in (engine.workdir.parent).glob("*/complete-provider-requests-responses.json"):
        for call in json.loads(path.read_text()):
            if "existing_records" not in str(call["request"].get("input")):
                continue
            try:
                raw=json.loads(call.get("completed_response",{}).get("output_text", ""))
                candidates.extend(raw.get("records",[]))
            except (ValueError,TypeError):
                continue
    qualified=[]
    details=[]
    committed=[m["record"] for r in transcript if r["type"]=="context_record_extraction_committed" for m in r["payload"]["mutations"] if m["action"] in {"created","updated"}]
    for record in engine.session.context_records.active_records:
        matches=[c for c in candidates if " ".join(c.get("content","").split())==record.content and set(c.get("source_sequences",[]))<=set(record.source_sequences)]
        citations=[sources.get(s) for s in record.source_sequences]
        observed=[r for r in citations if r and (case.get("user_source") and r["type"]=="context_user_query_appended" or r["type"]=="tool_execution_finished" and r["payload"].get("status")=="success")]
        quote_ok=any(c.get("evidence_quote") and any(c["evidence_quote"] in json.dumps(r["payload"],ensure_ascii=False) for r in observed) for c in matches)
        source_text="\n".join(json.dumps(r["payload"],ensure_ascii=False) for r in observed)
        supported=[v for v in case["facts"] if re.search(r"(?<![\d])"+re.escape(v)+r"(?![\d])", record.content) and v in source_text]
        valid=bool(matches and quote_ok and supported and case["entity"] in record.content and any(m["record_id"]==record.record_id for m in committed))
        details.append(dict(record=asdict(record), raw_candidates=matches, source_events=observed, quote_locatable=quote_ok, supported_values=supported, qualified=valid))
        if valid:
            qualified.append(record)
    covered={v for d in details if d["qualified"] for v in d["supported_values"]}
    return dict(status="QUALIFIED" if set(case["facts"])<=covered else "NO_QUALIFIED_RECORD", records=details,
                required_values=case["facts"],covered_values=sorted(covered))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",type=Path,required=True)
    args=parser.parse_args()
    output=args.output_dir.resolve()
    output.mkdir(parents=True,exist_ok=False)
    repo=Path(__file__).resolve().parents[2]
    cfg=load_config(repo)
    # 保留真实配置的频率/超时/摘要设置；不以提取失败临时放宽。
    fingerprint=source_fingerprint()
    frozen={str(p):tree_hash(p) for p in [repo/"eval/memory_m1/runs",repo/"eval/memory_m2/runs"]}
    dump(output/"manifest.json",dict(cases=CASES,config=asdict(cfg),model=get_model(),provider_host=urlsplit(str(get_client().base_url)).hostname,
        runtime_sha256=fingerprint,frozen_hashes=frozen,qualification_denominator=2,
        oracle="source entity/value/quote + durable automatic commit + injection + exact JSON; no trusted API seeding"))
    results=[]
    for case in CASES:
        print("START "+case["id"],flush=True)
        root=output/case["id"]
        workdir=root/"workspace"
        workdir.mkdir(parents=True)
        for name,content in case["documents"].items():
            (workdir/name).write_text(content,encoding="utf-8")
        dump(root/"fixture-hashes.json",tree_hash(workdir))
        engine=AgentEngine(cfg,workdir)
        stages=[]
        for index,prompt in enumerate(case["prompts"]):
            stages.append(perform(root,"STAGE_A_"+str(index+1),engine,prompt))
            if stages[-1]["terminal_status"]!="completed":
                break
        qualification=qualify(case,engine,stages)
        failures=[e for e in correlate([e for s in stages for e in s["provider_events"]]) if e.get("status")!="SUCCEEDED"]
        if any(s["terminal_status"]!="completed" for s in stages):
            qualification["status"]="PROVIDER_FAILURE" if failures else "NO_QUALIFIED_RECORD"
        elif qualification["status"]!="QUALIFIED" and any(r["type"]=="context_record_extraction_failed" for r in engine.session.store.read_transcript()):
            qualification["status"]="PROVIDER_FAILURE" if any(e.get("request_type")=="CONTEXT_RECORD_EXTRACTION" for e in failures) else "EXTRACTION_REJECTED"
        dump(root/"stage-a-qualification.json",qualification)
        result=dict(id=case["id"],qualification=qualification["status"],status="BLOCKED",oracles=dict(source_extraction=qualification["status"]))
        if qualification["status"]=="QUALIFIED":
            old_ids=[r.record_id for r in engine.session.context_records.active_records if case["entity"] in r.content and "43127" in r.content]
            if case["id"]=="C2":
                for index,prompt in enumerate(case["correction_prompts"]):
                    stages.append(perform(root,"CORRECTION_"+str(index+1),engine,prompt))
                    if stages[-1]["terminal_status"]!="completed":
                        break
                updated=qualify({**case,"facts":["58239"],"user_source":True},engine,stages)
                dump(root/"correction-qualification.json",updated)
            query=perform(root,"QUERY",engine,case["query"])
            stages.append(query)
            injected=blocks(query["captured_requests"])
            active=[asdict(r) for r in engine.session.context_records.active_records]
            dump(root/"final-store.json",[asdict(r) for r in engine.session.context_records.records])
            dump(root/"actual-record-injection.json",injected)
            try:
                answer=json.loads(query["final_answer"] or "")
            except (ValueError,TypeError):
                answer=None
            content="\n".join(r["content"] for r in injected)
            if case["id"]=="C1":
                injection=all(v in content for v in case["facts"])
                store=qualify(case,engine,stages)["status"]=="QUALIFIED"
            else:
                store=updated["status"]=="QUALIFIED" and any("58239" in r["content"] for r in active) and not any(r["record_id"] in old_ids for r in active)
                injection="58239" in content and "43127" not in content and not any(r["record_id"] in old_ids for r in injected)
            retrieved=[r["payload"] for r in engine.session.store.read_transcript() if r["type"]=="context_records_retrieved"]
            dump(root/"retrieval-events.json",retrieved)
            result["oracles"].update(store="PASS" if store else "FAIL",selection="PASS" if injection and retrieved else "FAIL",injection="PASS" if injection else "FAIL",final_answer="PASS" if answer==case["expected"] else "FAIL")
            result.update(status="PASS" if store and injection and answer==case["expected"] and query["terminal_status"]=="completed" else "FAIL",answer=answer)
        dump(root/"oracle.json",result)
        results.append(result)
        print(case["id"]+" "+result["qualification"]+" "+result["status"],flush=True)
    all_stages=[json.loads(p.read_text()) for p in output.glob("C*/*/result.json")]
    ends=correlate([e for s in all_stages for e in s["provider_events"]])
    starts=[e for s in all_stages for e in s["provider_events"] if e["event"]=="provider_started"]
    usage={}
    for category in {e.get("request_type") for e in starts}:
        begun=[e for e in starts if e.get("request_type")==category]
        selected=[e for e in ends if e.get("request_type")==category]
        available=[e for e in selected if e.get("usage_available")]
        usage[category]=dict(requests=len(begun),available=len(available),unobservable=len(begun)-len(available),completeness="COMPLETE" if len(begun)==len(available) else "INCOMPLETE",
            observable_input_tokens=sum(e["input_tokens"] for e in available),observable_output_tokens=sum(e["output_tokens"] for e in available))
    dump(output/"report.json",dict(status="PASS" if all(r["status"]=="PASS" for r in results) else "NOT_PASS",scenarios=results,
        qualification_denominator=2,qualified=sum(r["qualification"]=="QUALIFIED" for r in results),provider_usage=usage,
        production_unchanged=fingerprint==source_fingerprint(),m1_m2_unchanged=all(tree_hash(Path(p))==h for p,h in frozen.items()),
        limits=["两例Smoke，非ON/OFF因果实验", "历史和工具回读可能支持答案，不能只凭答案证明使用了Record", "没有Engine API补写", "未执行C4/C5真实模型"] ))
    print(str(output/"report.json"),flush=True)


if __name__=="__main__":
    main()
