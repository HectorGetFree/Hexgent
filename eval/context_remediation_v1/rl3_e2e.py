"""单次 R-L3 E2E：使用 Production 默认 v2，无 Prompt 覆盖，四次派发硬上限。"""
import argparse
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
from time import monotonic
from urllib.parse import urlsplit

from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.config import load_config
from hexgent.context import ContextManager
from hexgent.context.context_compaction import compact_context_if_needed, COMPACTION_INSTRUCTIONS
from hexgent.context.context_builder import build_context_details
from hexgent.context.key_fact_extractor import EXTRACTION_INSTRUCTIONS, EXTRACTION_PROMPT_VERSION
from hexgent.loop.agent_turn import _available_tools_for_state
from hexgent.loop.loop_state import LoopState
from hexgent.session.checkpoint import _context_snapshot
from hexgent.utils.token_estimator import estimate_request_tokens
from .factorial_pilot import file_hashes
from .historical_prompts import V2
from .offline import ROOT, fingerprints

ORDER=['EXTRACTOR','SUMMARY','MAIN','RESTORE_MAIN']

def save(out,name,value):
    p=out/name;p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n')

def read(p):return json.loads(p.read_text())
def digest(value):return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
def now():return datetime.now(timezone.utc).isoformat()

class ExperimentStop(BaseException):
    """不可被 Production 的异常重试捕获；先保存证据再停止整轮。"""

class Audit:
    def __init__(self,client,out,config,shared=None):
        self.client,self.out,self.config=client,out,config
        self.shared=shared if shared is not None else dict(rows=[],phase='MAIN',offline=False)
        self.responses=self
        self.base_url=getattr(client,'base_url','offline')
    def with_options(self,**options):
        if options.get('max_retries',0)!=0:raise ExperimentStop('SDK_RETRY_FORBIDDEN')
        return Audit(self.client.with_options(**options),self.out,self.config,self.shared)
    def create(self,**request):
        stage='EXTRACTOR' if request.get('instructions')==EXTRACTION_INSTRUCTIONS else 'SUMMARY' if request.get('instructions','').startswith(COMPACTION_INSTRUCTIONS) else self.shared['phase']
        idx=len(self.shared['rows'])
        if idx>=4 or stage!=ORDER[idx]:raise ExperimentStop('CALL_ORDER_OR_LIMIT: '+stage)
        actual=deepcopy(request)
        if stage=='EXTRACTOR' and actual['instructions']!=V2:
            raise ExperimentStop('PRODUCTION_DEFAULT_PROMPT_NOT_FROZEN_V2')
        delta=[k for k in actual if actual[k]!=request[k]]
        if delta:raise ExperimentStop('UNEXPECTED_REQUEST_ADAPTATION')
        config=self.config;limits=config.context
        estimate=estimate_request_tokens(actual.get('instructions',''),actual.get('input',[]),actual.get('tools',[]))
        window=limits.auxiliary_context_window_tokens if stage in ORDER[:2] else limits.context_window_tokens
        if estimate+actual.get('max_output_tokens',0)+limits.safety_margin_tokens>window:raise ExperimentStop('REQUEST_BUDGET_EXCEEDED')
        if stage=='EXTRACTOR' and estimate+actual['max_output_tokens']>58650:raise ExperimentStop('EXTRACTOR_FROZEN_BUDGET_EXCEEDED')
        if stage=='SUMMARY' and estimate+actual['max_output_tokens']>51361:raise ExperimentStop('SUMMARY_FROZEN_BUDGET_EXCEEDED')
        save(self.out,f'{stage}/production-request.json',request)
        save(self.out,f'{stage}/actual-request.json',actual)
        save(self.out,f'{stage}/adaptation.json',dict(production_version=EXTRACTION_PROMPT_VERSION,actual_version=EXTRACTION_PROMPT_VERSION if stage=='EXTRACTOR' else 'PRODUCTION_UNCHANGED',
            changed_fields=delta,production_request_sha256=digest(request),actual_request_sha256=digest(actual),estimated_input_tokens=estimate,output_allowance=actual.get('max_output_tokens'),safety_margin=limits.safety_margin_tokens))
        row=dict(stage=stage,started_at=now(),status='STARTED',input_tokens=None,output_tokens=None,estimated_input_tokens=estimate,retries=0,request_sha256=digest(actual))
        self.shared['rows'].append(row);save(self.out,'provider-ledger.json',self.shared['rows'])
        return self.stream(actual,row)
    def stream(self,actual,row):
        start=monotonic();stream=None;stage=row['stage']
        try:
            stream=self.client.responses.create(**actual)
            for index,event in enumerate(stream):
                body=event.model_dump(mode='json') if hasattr(event,'model_dump') else dict(type=event.type,response=event.response.model_dump(mode='json') if hasattr(event.response,'model_dump') else {})
                save(self.out,f'{stage}/events/{index:05d}.json',body)
                if event.type=='response.completed':
                    response=event.response
                    raw=response.model_dump(mode='json') if hasattr(response,'model_dump') else dict(output_text=response.output_text,output=[],usage=vars(response.usage))
                    save(self.out,f'{stage}/response.json',raw)
                    usage=raw.get('usage') or {};row.update(status='COMPLETED',response_id=raw.get('id'),input_tokens=usage.get('input_tokens'),output_tokens=usage.get('output_tokens'))
                    if stage in ORDER[2:] and any(getattr(item,'type',None)=='function_call' for item in response.output):
                        row['stop_reason']='MAIN_REQUESTED_TOOL_CALL_SINGLE_ROUND_BOUNDARY'
                        raise ExperimentStop(row['stop_reason'])
                elif event.type in {'response.failed','response.incomplete','error'}:
                    row.update(status='PROVIDER_FAILED',error_event=body)
                    raise ExperimentStop('PROVIDER_TERMINAL_FAILURE')
                yield event
            if row['status']!='COMPLETED':raise ExperimentStop('PROVIDER_MISSING_COMPLETED')
            if row['input_tokens'] is None or row['output_tokens'] is None:raise ExperimentStop('USAGE_MISSING')
        except BaseException as error:
            row['error']=str(error);row['error_type']=type(error).__name__
            if row['status']=='STARTED':row['status']='PROVIDER_FAILED'
            raise ExperimentStop(str(error)) from error
        finally:
            if stream is not None and callable(getattr(stream,'close',None)):stream.close()
            row.update(ended_at=now(),latency_ms=(monotonic()-start)*1000,usage_available=row['input_tokens'] is not None and row['output_tokens'] is not None)
            save(self.out,'provider-ledger.json',self.shared['rows'])

def setup(out):
    config=load_config(ROOT)
    config=replace(config,context=replace(config.context,max_compaction_attempts=1),
        context_extraction=replace(config.context_extraction,enabled=False),
        context_retrieval=replace(config.context_retrieval,enabled=False),memory_retrieval=replace(config.memory_retrieval,enabled=False))
    sample=read(ROOT/'eval/context_remediation_v1/runs/preflight-review-20261012-v2/samples/R-L3.json')
    manager=ContextManager();engine=AgentEngine(config,out/'workspace',context_manager=manager)
    engine.workdir.mkdir(parents=True,exist_ok=True)
    engine.session.initialize_runtime_session('rl3-e2e')
    for r in sample['rounds']:
        q=manager.append_user_query(r['query']);engine.session.record_user_query(q,source='eval_frozen_fixture')
        manager.append_messages(deepcopy(r['messages']),completes_round=True)
        engine.session.record_context_messages(deepcopy(r['messages']),completes_round=True,source='eval_frozen_fixture')
    q=manager.append_user_query(sample['final_query']);engine.session.record_user_query(q,source='eval_frozen_fixture')
    return engine,config,sample

def drain(stream):
    while True:
        try:next(stream)
        except StopIteration as stop:return stop.value

def run(out,wire,offline=False):
    engine,config,sample=setup(out)
    audit=Audit(wire,out,config);audit.shared['offline']=offline
    manager=engine.context_manager
    state=LoopState(context_manager=manager,tool_context=engine.tool_context)
    built=build_context_details(engine.tool_context,(),(),None,config.context.runtime_injection_tokens)
    tools=_available_tools_for_state(state)
    def commit(boundary):
        calls=[boundary['cost_ledger']['extractor'],*boundary['cost_ledger']['summary']]
        resumed=replace(state,input_tokens=sum(c.get('input_tokens') or 0 for c in calls),output_tokens=sum(c.get('output_tokens') or 0 for c in calls),transition='context_compacted')
        boundary['resume_state']=resumed.to_snapshot().to_dict()
        return engine.session.record_compaction(boundary)
    outcome=drain(compact_context_if_needed(manager,audit,'gpt-6.1-sol',built.instructions,tools,config.context,force=True,
        instruction_budget=built.instruction_tokens,commit_boundary=commit,output_allowance=config.loop.max_output_tokens_per_round))
    save(out,'compaction-outcome.json',asdict(outcome))
    if outcome.status not in {'compacted','partial'}:raise ExperimentStop('COMPACTION_REJECTED: '+str(outcome.message))
    from hexgent.loop.loop_state import RunStateSnapshot
    resume=RunStateSnapshot.from_dict(outcome.boundary['resume_state'])
    next_state=LoopState.from_snapshot(resume,context_manager=manager,tool_context=engine.tool_context)
    cp=engine.session.create_checkpoint(context_manager=manager,plan=None,reason='context_compaction_completed',loop_state=next_state,run_status='active')
    save(out,'checkpoint.json',asdict(cp))
    save(out,'committed-context.json',_context_snapshot(manager))
    save(out,'transcript-at-commit.json',engine.session.store.read_transcript())
    # 冻结同一提交后边界；第二分支从未改写的durable快照恢复，不恢复已完成的终态。
    cloned=out/'restore-workspace';shutil.copytree(engine.workdir,cloned)
    current=AgentEngine(config,engine.workdir,context_manager=manager,session=engine.session,tool_context=engine.tool_context,pending_run_state=resume)
    before_queries=sum(r['type']=='context_user_query_appended' for r in current.session.store.read_transcript())
    audit.shared['phase']='MAIN'
    events=list(current.run(RunRequest.continue_run(),client=audit))
    save(out,'MAIN/agent-events.json',[asdict(e) for e in events])
    terminal=events[-1].result
    if terminal.status!='completed':raise ExperimentStop('MAIN_NOT_COMPLETED: '+str(terminal))
    assert sum(r['type']=='context_user_query_appended' for r in current.session.store.read_transcript())==before_queries
    restored=AgentEngine.restore(config,cloned,'rl3-e2e')
    if not restored.has_continuable_run:raise ExperimentStop('RESTORE_NOT_CONTINUABLE')
    if _context_snapshot(restored.context_manager)!=_context_snapshot(ContextManager.from_checkpoint(cp.context)):
        raise ExperimentStop('RESTORE_CONTEXT_MISMATCH')
    save(out,'restore-evidence.json',dict(has_continuable_run=True,context_equal=True,original_session_id='rl3-e2e',restored_context=_context_snapshot(restored.context_manager),
        semantics='从相同Compaction后安全Checkpoint分支恢复；不在MAIN完成终态后创建新Submit。'))
    audit.shared['phase']='RESTORE_MAIN'
    events=list(restored.run(RunRequest.continue_run(),client=audit))
    save(out,'RESTORE_MAIN/agent-events.json',[asdict(e) for e in events])
    if events[-1].result.status!='completed':raise ExperimentStop('RESTORE_MAIN_NOT_COMPLETED')
    save(out,'final-transcripts.json',dict(current=current.session.store.read_transcript(),restored=restored.session.store.read_transcript()))
    return audit.shared

class OfflineWire:
    """仅验证接线；不把Fake事实正确率写成真实质量证据。"""
    def __init__(self):
        from tests.test_key_fact_remediation import Model
        from eval.support.agent_contracts import text_response
        self.model=Model(main=[text_response('离线链路检查'),text_response('离线恢复检查')]);self.responses=self
    def with_options(self,**options):return self
    def create(self,**request):
        r=deepcopy(request)
        return self.model.create(**r)

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--offline',action='store_true');p.add_argument('--execute',action='store_true');args=p.parse_args()
    out=args.output.resolve()
    if out.exists():raise RuntimeError('禁止覆盖或重跑已有E2E目录')
    out.mkdir(parents=True)
    before=file_hashes(ROOT/'hexgent');frozen=fingerprints()
    save(out,'manifest.json',dict(started_at=now(),mode='OFFLINE_GATE' if args.offline else 'REAL_SINGLE_ATTEMPT',production_hashes=before,frozen_hashes=frozen,
        max_calls=4,order=ORDER,production_default_prompt=EXTRACTION_PROMPT_VERSION,actual_extractor_prompt=EXTRACTION_PROMPT_VERSION,v2_sha256=sha256(V2.encode()).hexdigest(),
        capacity='USER_ASSERTED_CAPACITY_128K_UNVERIFIED',sampling='PROVIDER_DEFAULT_UNCHANGED',summary_attempts=1,hidden_retries=0,
        same_checkpoint_two_continuations=True,head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()))
    gate='C2_RL3_E2E_INCOMPLETE';error=None
    try:
        if args.offline:wire=OfflineWire()
        elif args.execute:
            from hexgent.client import get_client,get_model
            base=get_client();assert get_model()=='gpt-6.1-sol'
            assert urlsplit(str(base.base_url)).hostname in {'rightapi.ai','www.rightapi.ai'}
            wire=base.with_options(max_retries=0)
            assert wire.max_retries==0
        else:raise ExperimentStop('需要--offline或--execute')
        run(out,wire,args.offline)
        gate='OFFLINE_ENGINEERING_PASS' if args.offline else 'AWAITING_INDEPENDENT_MAIN_QUALITY_REVIEW'
    except BaseException as e:
        error=dict(type=type(e).__name__,message=str(e))
        if 'COMPACTION_REJECTED' in str(e) and 'PROVIDER' not in str(e):gate='C2_RL3_E2E_FAIL'
    save(out,'execution-result.json',dict(gate=gate,error=error,paid_calls=0 if args.offline else len(read(out/'provider-ledger.json')) if (out/'provider-ledger.json').exists() else 0,
        engineering='PASS' if gate in {'OFFLINE_ENGINEERING_PASS','AWAITING_INDEPENDENT_MAIN_QUALITY_REVIEW'} else 'INCOMPLETE_OR_FAILED',task_quality='NOT_EVALUABLE' if args.offline or error else 'REVIEW_PENDING'))
    diff=subprocess.run(['git','diff','--check'],cwd=ROOT,capture_output=True,text=True)
    save(out,'integrity.json',dict(production_unchanged=before==file_hashes(ROOT/'hexgent'),frozen_unchanged=frozen==fingerprints(),git_diff_check=dict(exit_code=diff.returncode,output=diff.stdout+diff.stderr)))
    print(json.dumps(dict(output=str(out),gate=gate,error=error),ensure_ascii=False),flush=True)

if __name__=='__main__':main()
