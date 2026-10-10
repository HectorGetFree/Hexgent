"""Pinned Constraints 确定性研究对照；仅 EVAL_SIMULATION，无网络。"""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.config import HexgentConfig
from hexgent.context.context_compaction import compact_context_if_needed
from hexgent.events import ToolFinishedEvent
from hexgent.observation import source_fingerprint
from hexgent.run_control import RunControl
from hexgent.utils.token_estimator import estimate_request_tokens
from eval.memory_m1 import dump, tree_hash
from eval.support.agent_contracts import FakeModel, text_response, tool_call

A = "禁止修改原始资料文件，仅允许读取并在回复中给出结论。"
B = "我明确撤销之前的只读要求，改为仅允许写入 result.json；原始资料文件仍禁止修改。"
SCENARIOS = [
    dict(id="E1", summary=None, correction=False, resume=False, pressure=False),
    dict(id="E2_KEEP", summary="当前目标：核对资料。用户约束："+A+"已完成：读取资料。剩余工作：继续核对。", correction=False, resume=False, pressure=False),
    dict(id="E2_OMIT", summary="当前目标：核对资料。已完成：读取资料。剩余工作：继续核对。", correction=False, resume=False, pressure=False),
    dict(id="E3", summary=None, correction=True, resume=False, pressure=False),
    dict(id="E4_A", summary=None, correction=False, resume=True, pressure=False),
    dict(id="E4_B", summary=None, correction=True, resume=True, pressure=False),
    dict(id="E5", summary=None, correction=False, resume=False, pressure=True),
]


def serial(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=lambda x: x.model_dump(mode="json") if hasattr(x,"model_dump") else vars(x)))


def normalize(value, workdir):
    # 只归一化克隆工作区路径；不移除任何模型参数、调用 ID 或历史内容。
    if isinstance(value,str):
        return value.replace(str(workdir),"<workspace>")
    if isinstance(value,list):
        return [normalize(x,workdir) for x in value]
    if isinstance(value,dict):
        return {k:normalize(v,workdir) for k,v in value.items()}
    return value


class BudgetDenied(RuntimeError):
    pass


class EvalClient:
    """只包装最终 create 边界；主请求追加保护区，摘要请求原样转发。"""
    base_url="https://offline.invalid"
    def __init__(self, responses, constraints=(), pinned=False, pressure=False):
        self.model=FakeModel(responses)
        self.constraints=constraints
        self.pinned,self.pressure=pinned,pressure
        self.attempts=[]
        self.responses=SimpleNamespace(create=self.create)
    def with_options(self,**options):
        return self
    def create(self,**request):
        original=serial(request)
        is_summary=request.get("instructions","").startswith("你负责压缩 Agent")
        block=""
        if self.pinned and not is_summary:
            active=[r for r in self.constraints if r["status"]=="active"]
            block="\n\n<eval-pinned-constraints>\n"+json.dumps(active,ensure_ascii=False)+"\n</eval-pinned-constraints>"
            request={**request,"instructions":request["instructions"]+block}
        before=estimate_request_tokens(original.get("instructions",""),original.get("input",[]),original.get("tools",[]))
        after=estimate_request_tokens(request.get("instructions",""),request.get("input",[]),request.get("tools",[]))
        # E5 预注册预算政策：同一原始请求的估算输入+输出额度，仅留 1 Token 注入余量。
        # 两侧公式相同；不依据结果修改阈值。不是生产容量配置。
        reserved=request.get("max_output_tokens",0)
        capacity=before+reserved+1 if self.pressure and not is_summary else 1_000_000
        denied=after+reserved>capacity
        self.attempts.append(dict(original=original,request=serial(request),block=block,
            request_type="COMPACTION" if is_summary else "MAIN_AGENT",tokens_before=before,tokens_after=after,
            reserved_output_tokens=reserved,capacity=capacity,denied=denied,
            failure_reason="EVAL_PINNED_CONTEXT_BUDGET_EXCEEDED" if denied else None))
        if denied:
            raise BudgetDenied("EVAL_PINNED_CONTEXT_BUDGET_EXCEEDED：完整输入与输出预留无法容纳，未派发模型请求")
        return self.model.create(**request)


def execute(engine,query,responses,pause=False):
    model=EvalClient(responses)
    control=RunControl()
    events=[]
    for event in engine.run(query,client=model,run_control=control):
        events.append(event)
        if pause and isinstance(event,ToolFinishedEvent):
            control.pause()
    return model,events


def current_constraints(engine,corrected):
    # 预注册显式事件映射，不解析自然语言，不向生产 Store 写入事实。
    transcript=engine.session.store.read_transcript()
    def source(text):
        return next(r["sequence"] for r in transcript if r["type"]=="context_user_query_appended" and r["payload"]["content"]==text)
    rows=[dict(constraint_id="C-A",text=A,source_event_seq=source(A),status="superseded" if corrected else "active",superseded_by="C-B" if corrected else None)]
    if corrected:
        rows.append(dict(constraint_id="C-B",text=B,source_event_seq=source(B),status="active",superseded_by=None))
    return rows


def source_oracle(rows,transcript):
    sources={r["sequence"]:r for r in transcript}
    return all(sources[r["source_event_seq"]]["type"]=="context_user_query_appended" and sources[r["source_event_seq"]]["payload"]["content"]==r["text"] for r in rows)


def config():
    base=HexgentConfig()
    return replace(base, context_extraction=replace(base.context_extraction,enabled=False),
        context_retrieval=replace(base.context_retrieval,enabled=False),
        memory_retrieval=replace(base.memory_retrieval,enabled=False))


def run_gate(output):
    output.mkdir(parents=True,exist_ok=False)
    repo=Path(__file__).resolve().parents[2]
    before=source_fingerprint()
    frozen={str(p):tree_hash(p) for p in [repo/"eval/memory_m1/runs",repo/"eval/memory_m2/runs"]}
    cfg=config()
    dump(output/"manifest.json",dict(evidence_type="EVAL_SIMULATION",scenarios=SCENARIOS,constraints=[A,B],config=asdict(cfg),
        budget_policy="E5 full request input + max_output_tokens; capacity=unpinned estimate+max_output_tokens+1",
        model="FakeModel only",responses="read_file then completion; E4 completed update_plan then completion",
        fixture="two original readonly business documents",runtime_sha256=before,frozen_hashes=frozen))
    all_results=[]
    for scenario in SCENARIOS:
        target=output/scenario["id"]
        baseline=target/"stage-a"
        baseline.mkdir(parents=True)
        (baseline/"one.md").write_text("业务资料：审批需要负责人确认。\n",encoding="utf-8")
        (baseline/"two.md").write_text("业务资料：交付前核对资料完整性。\n",encoding="utf-8")
        engine=AgentEngine(cfg,baseline)
        execute(engine,A,[text_response("约束已收到")])
        for index in (1,2):
            name="one.md" if index==1 else "two.md"
            execute(engine,"核对第"+str(index)+"份资料。",[text_response(output=[tool_call("read-"+str(index),"read_file",json.dumps(dict(path=name)))]),text_response("核对完成")])
        if scenario["correction"]:
            execute(engine,B,[text_response("更正已收到")])
        if scenario["resume"]:
            execute(engine,"继续",[text_response(output=[tool_call("pause-plan","create_plan",json.dumps(dict(objective="核对剩余资料",steps=[dict(content="完成核对",process_message="正在核对",status="in_progress")]),ensure_ascii=False))])],pause=True)
            if not engine.has_continuable_run:
                raise AssertionError("正式 Continue 不可达")
        sid=engine.session.store.session_id
        constraints=current_constraints(engine,scenario["correction"])
        dump(target/"constraints.json",constraints)
        snapshot=tree_hash(baseline)
        paired={}
        for variant in ("Baseline","Pinned"):
            work=target/variant/"workspace"
            shutil.copytree(baseline,work)
            snapshot_equal=tree_hash(work)==snapshot
            initial_transcript=[json.loads(line) for line in (work/".hexgent/sessions"/sid/"transcript.jsonl").read_text().splitlines()]
            active=AgentEngine.restore(cfg,work,sid)
            initial_files={p.name:p.read_text() for p in work.glob("*.md")}
            pre=dict(context=serial(active.context_manager.active_messages),plan=asdict(active.tool_context.plan_store.get_plan()) if active.tool_context.plan_store.get_plan() else None,
                config=asdict(cfg),settings=asdict(active.session.settings),transcript=initial_transcript,
                memory=tree_hash(work/".hexgent/memory"),records=[asdict(r) for r in active.session.context_records.records],snapshot_equal=snapshot_equal)
            summaries=[]
            if scenario["summary"]:
                summary_client=EvalClient([text_response("<summary>"+scenario["summary"]+"</summary>")],constraints,variant=="Pinned")
                limits=replace(cfg.context,recent_context_tokens=1,min_compaction_savings_tokens=1,min_compaction_savings_ratio=0.001)
                stream=compact_context_if_needed(active.context_manager,summary_client,"offline","系统",[],limits,force=True)
                while True:
                    try:
                        next(stream)
                    except StopIteration as stop:
                        outcome=stop.value
                        break
                if outcome.status not in {"compacted","partial"}:
                    raise AssertionError("Production Compaction 未成功："+str(outcome.status))
                active.session.record_compaction(outcome.boundary)
                summaries=summary_client.attempts
            if scenario["resume"]:
                plan=active.tool_context.plan_store.get_plan()
                response=text_response(output=[tool_call("finish-plan","update_plan",json.dumps(dict(expected_revision=plan.revision,status="completed",reason=None,blocked_reason=None,steps=[dict(step_id=plan.steps[0].step_id,content=plan.steps[0].content,process_message=plan.steps[0].process_message,status="completed")]),ensure_ascii=False))])
                query=RunRequest.continue_run()
            else:
                response=text_response(output=[tool_call("final-read","read_file",'{"path":"one.md"}')])
                query="继续"
            client=EvalClient([response,text_response("已核对，未修改原始资料。")],constraints,variant=="Pinned",scenario["pressure"])
            count_before=len(active.session.store.read_transcript())
            events=list(active.run(query,client=client))
            transcript=active.session.store.read_transcript()
            delta=transcript[count_before:]
            calls=[r["payload"] for r in delta if r["type"]=="tool_execution_finished"]
            attempts=client.attempts
            active_rows=[r for r in constraints if r["status"]=="active"]
            source=source_oracle(constraints,transcript)
            injected=[json.loads(a["block"].split("\n",3)[3].split("\n</")[0]) for a in attempts if a["block"]]
            request_ok=all(rows==active_rows for rows in injected) and (bool(injected) if variant=="Pinned" else all(not a["block"] for a in attempts))
            behavior=initial_files=={p.name:p.read_text() for p in work.glob("*.md")} and not any(t["tool_name"] in {"write_file","edit_file","bash"} for t in calls) and not (work/"result.json").exists()
            denied=any(a["denied"] for a in attempts)
            budget=(denied and not client.model.calls) if scenario["pressure"] and variant=="Pinned" else not denied
            query_events=[r for r in delta if r["type"]=="context_user_query_appended"]
            resume=not query_events and active.session.store.session_id==sid if scenario["resume"] else None
            execution=(events[-1].result.status=="failed" and not calls) if denied else events[-1].result.status=="completed" and bool(calls) and all(t["status"]=="success" for t in calls)
            actual_requests=serial(client.model.calls)
            if not denied:
                request_ok=request_ok and actual_requests==[a["request"] for a in attempts]
            effective=B if scenario["correction"] else A
            history_text=json.dumps(attempts[0]["original"]["input"],ensure_ascii=False)
            raw_user_constraint=any(isinstance(m,dict) and m.get("role")=="user" and A in str(m.get("content","")) for m in attempts[0]["original"]["input"])
            result=dict(evidence_type="EVAL_SIMULATION",variant=variant,pre=pre,constraints=constraints,summary_attempts=summaries,
                attempted_requests=attempts,actual_fake_model_requests=serial(client.model.calls),transcript_delta=delta,
                terminal=serial(events[-1].result),tool_calls=calls,original_constraint_in_history=A in history_text,
                raw_user_constraint_present=raw_user_constraint,
                summary_retains_constraint=effective in scenario["summary"] if scenario["summary"] else None,
                oracles=dict(source=source,active_set=active_rows==[constraints[-1]],request=request_ok,execution=execution,
                    summary=None if not scenario["summary"] else ((effective in scenario["summary"])==(scenario["id"]=="E2_KEEP")),
                    resume=resume,budget=budget,behavior=behavior),
                request_count=len(client.model.calls),estimated_token_delta=[a["tokens_after"]-a["tokens_before"] for a in attempts],provider_tokens=None,
                capability_gap=[] if variant=="Pinned" else ["NO_INDEPENDENT_PINNED_CONSTRAINTS"])
            dump(work.parent/"evidence.json",result)
            paired[variant]=result
        left,right=paired["Baseline"],paired["Pinned"]
        comparable=lambda p: [normalize(a["original"],target/p["variant"]/"workspace") for a in p["attempted_requests"]]
        checks=dict(snapshot_identical=all(p["pre"]["snapshot_equal"] for p in paired.values()),
            pre_state_identical=normalize(left["pre"],target/"Baseline/workspace")==normalize(right["pre"],target/"Pinned/workspace"),
            request_parameters_identical=comparable(left)==comparable(right),
            compaction_requests_identical=normalize(left["summary_attempts"],target/"Baseline/workspace")==normalize(right["summary_attempts"],target/"Pinned/workspace"))
        checks["wrapper_only_adds_protection_block"]=all({**a["request"],"instructions":a["request"]["instructions"][:-len(a["block"])] if a["block"] else a["request"]["instructions"]}==a["original"] for p in paired.values() for a in p["attempted_requests"])
        checks["initial_budget_identical"]=left["attempted_requests"][0]["capacity"]==right["attempted_requests"][0]["capacity"]
        if scenario["pressure"]:
            checks["request_parameters_identical"]=comparable(left)[:1]==comparable(right)
        passes=all(checks.values()) and all(v is not False for p in paired.values() for v in p["oracles"].values())
        row=dict(id=scenario["id"],status="PASS" if passes else "FAIL",isolation=checks,variants={v:{k:p[k] for k in ["oracles","request_count","estimated_token_delta","original_constraint_in_history","raw_user_constraint_present","summary_retains_constraint","capability_gap"]} for v,p in paired.items()})
        dump(target/"comparison.json",row)
        all_results.append(row)
        print(scenario["id"],row["status"],flush=True)
    report=dict(evidence_type="EVAL_SIMULATION",scenarios=all_results,status="PASS" if all(r["status"]=="PASS" for r in all_results) else "FAIL",
        production_unchanged=source_fingerprint()==before,m1_m2_unchanged=all(tree_hash(Path(p))==h for p,h in frozen.items()),
        paid_model_requests=0,provider_tokens=None,limitations=["固定摘要不是模型实际保留率","FakeModel 遵守轨迹不是实际模型行为收益","保护区由预注册来源提供，不自动识别","E5 为 Eval 包装预算拒绝，不是 Production 功能"])
    dump(output/"report.json",report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",type=Path,required=True)
    args=parser.parse_args()
    result=run_gate(args.output_dir.resolve())
    print(result["status"],args.output_dir/"report.json")


if __name__=="__main__":
    main()
