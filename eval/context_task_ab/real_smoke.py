"""IS1单次真实任务Smoke；不自动启动A/B或重跑。"""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from time import perf_counter
from urllib.parse import urlsplit

from hexgent.agent_engine import AgentEngine
from hexgent.client import get_client,get_model
from hexgent.context.key_fact_extractor import EXTRACTION_INSTRUCTIONS
from hexgent.context.context_compaction import COMPACTION_INSTRUCTIONS
from eval.context_management.c1_independent import plain
from .harness import FIXTURE,config,profile,manifest,capture_before_compaction,RequestBudget,BudgetStop
from .private_oracle.oracle import assess
from .task import CONTRACT,PROMPT

ROOT=Path(__file__).resolve().parents[2]

class SmokeStop(BaseException):pass

def save(path,data):
    path.write_text(json.dumps(plain(data),ensure_ascii=False,indent=2),encoding='utf-8')

class Capture:
    """完整转发SDK选项；账本与限额跨with_options共享。"""
    def __init__(self,client,directory,state=None):
        self._client=client;self.directory=directory
        self.state=state if state is not None else dict(records=[],budget=RequestBudget(12,200000),blocked=None)
        self.responses=SimpleNamespace(create=self.create)
    def __getattr__(self,name):return getattr(self._client,name)
    def with_options(self,**options):
        if options.get('max_retries',0)!=0:raise SmokeStop('HIDDEN_RETRY_REJECTED')
        return Capture(self._client.with_options(**options),self.directory,self.state)
    def create(self,**request):
        if self.state['blocked']:raise SmokeStop(self.state['blocked'])
        self.state['budget'].reserve(request)
        ident=len(self.state['records'])+1
        kind='EXTRACTOR' if request.get('instructions')==EXTRACTION_INSTRUCTIONS else 'SUMMARY' if request.get('instructions','').startswith(COMPACTION_INSTRUCTIONS) else 'MAIN'
        record=dict(attempt=ident,request_type=kind,request=plain(request),started_at=datetime.now(timezone.utc).isoformat(),
            status='STARTED',events=[],input_tokens=None,output_tokens=None,usage_available=False,error=None)
        self.state['records'].append(record);self.flush()
        print(json.dumps(dict(event='REQUEST_START',attempt=ident,request_type=kind),ensure_ascii=False),flush=True)
        start=perf_counter()
        try:
            stream=self._client.responses.create(**request)
        except Exception as exc:
            record.update(status='PROVIDER_FAILURE',error=dict(type=type(exc).__name__,message=str(exc)),latency_ms=(perf_counter()-start)*1000)
            self.state['blocked']='PROVIDER_FAILURE';self.flush();raise SmokeStop('PROVIDER_FAILURE') from exc
        return CapturedStream(stream,self,record,start)
    def flush(self):save(self.directory/'provider-ledger.json',self.state['records'])

class CapturedStream:
    def __init__(self,stream,capture,record,start):
        self.stream=stream;self.iterator=iter(stream);self.capture=capture;self.record=record;self.start=start
    def __getattr__(self,name):return getattr(self.stream,name)
    def __iter__(self):return self
    def __next__(self):
        try:event=next(self.iterator)
        except StopIteration:
            if self.record['status']=='STARTED':
                self.fail('PROVIDER_STREAM_INTERRUPTED',None);raise SmokeStop('PROVIDER_STREAM_INTERRUPTED')
            raise
        except Exception as exc:
            self.fail('PROVIDER_FAILURE',exc);raise SmokeStop('PROVIDER_FAILURE') from exc
        self.record['events'].append(plain(event))
        if event.type=='response.completed':
            result=event.response;usage=getattr(result,'usage',None)
            inp=getattr(usage,'input_tokens',None);out=getattr(usage,'output_tokens',None)
            self.record.update(status='COMPLETED',response=plain(result),input_tokens=inp,output_tokens=out,
                usage_available=inp is not None and out is not None,latency_ms=(perf_counter()-self.start)*1000)
            self.capture.flush()
            print(json.dumps(dict(event='REQUEST_END',attempt=self.record['attempt'],status='COMPLETED',input_tokens=inp,output_tokens=out),ensure_ascii=False),flush=True)
            if inp is None or out is None:
                self.capture.state['blocked']='USAGE_INCOMPLETE';raise SmokeStop('USAGE_INCOMPLETE')
        elif event.type in {'response.failed','response.incomplete','error'}:
            self.fail('PROVIDER_RESPONSE_FAILED',None);raise SmokeStop('PROVIDER_RESPONSE_FAILED')
        return event
    def fail(self,reason,exc):
        self.record.update(status=reason,latency_ms=(perf_counter()-self.start)*1000,
            error=None if exc is None else dict(type=type(exc).__name__,message=str(exc)))
        self.capture.state['blocked']=reason;self.capture.flush()
    def close(self):
        if self.record['status']=='STARTED':self.fail('STREAM_CLOSED_WITHOUT_TERMINAL',None)
        close=getattr(self.stream,'close',None)
        if close:close()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true');parser.add_argument('--output-dir',type=Path)
    args=parser.parse_args()
    if not args.execute:raise SystemExit('需要明确 --execute；此入口会调用真实模型')
    model=get_model();native=get_client()
    host=urlsplit(str(native.base_url)).hostname
    if model!='gpt-6.1-sol' or host not in {'rightapi.ai','www.rightapi.ai'}:raise SystemExit('MODEL_OR_PROVIDER_MISMATCH：停止，不自动更换路由')
    output=args.output_dir or Path(__file__).parent/'runs'/('is1-smoke-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True,exist_ok=False);workspace=output/'workspace';shutil.copytree(FIXTURE,workspace)
    before=manifest(ROOT/'hexgent');frozen=manifest(FIXTURE);cfg=config(workspace)
    save(output/'manifest.json',dict(task=CONTRACT,model=model,provider_host=host,config=asdict(cfg),fixture_hashes=frozen,
        production_hashes=before,max_provider_attempts=12,max_estimated_tokens_including_output=200000,
        hidden_retries=0,request_timeout_seconds=300,scope='ONE_ON_TASK_NO_AB_NO_RETRY',
        capacity_status='USER_ASSERTED_CAPACITY_128K_UNVERIFIED'))
    client=Capture(native.with_options(max_retries=0,timeout=300),output)
    engine=AgentEngine(cfg,workspace);events=[];started=perf_counter();stopped=None;captured=None
    def trace(stream):
        try:
            for event in stream:
                events.append(plain(event));save(output/'events.json',events)
                yield event
        finally:stream.close()
    stream=None
    try:
        with profile('ON'):
            stream=trace(engine.run(PROMPT,client=client,user_confirmation=lambda tool,*a:'allow' if tool in {'read_file','glob','list_directory','write_file','edit_file','create_plan','update_plan'} else 'deny'))
            captured=capture_before_compaction(engine,stream,output/'stage-a-snapshot')
    except (SmokeStop,BudgetStop,Exception) as exc:
        stopped=dict(type=type(exc).__name__,message=str(exc))
    finally:
        if stream is not None:stream.close()
        client.flush()
    transcript=engine.session.store.read_transcript();save(output/'transcript.json',transcript)
    oracle=assess(workspace);save(output/'oracle.json',oracle)
    calls=client.state['records'];complete=bool(calls) and all(c['status']=='COMPLETED' and c['usage_available'] for c in calls)
    finished=[r['payload'] for r in transcript if r['type']=='agent_run_finished']
    terminal=finished[-1].get('status') if finished else None
    captured_status=captured.get('status') if captured else None
    integrity=before==manifest(ROOT/'hexgent') and frozen==manifest(FIXTURE)
    result=dict(status='SMOKE_INCOMPLETE' if stopped or not integrity or not complete else
        'SNAPSHOT_CAPTURED_AWAITING_BRANCH_REVIEW' if captured_status=='SNAPSHOT_CAPTURED' else
        'SMOKE_TASK_PASS' if terminal=='completed' and oracle['oracle_pass'] else 'SMOKE_TASK_FAIL',
        terminal_status=terminal,oracle_pass=oracle['oracle_pass'],stop_reason=stopped,capture=captured,
        compaction_count=engine.context_manager.compaction_count,provider_attempts=len(calls),
        provider_input_tokens=sum(c['input_tokens'] for c in calls) if complete else None,
        provider_output_tokens=sum(c['output_tokens'] for c in calls) if complete else None,
        usage_completeness='COMPLETE' if complete else 'INCOMPLETE',
        estimated_budget_reserved=client.state['budget'].reserved,total_agent_latency_ms=(perf_counter()-started)*1000,
        tool_calls=sum(r['type']=='tool_execution_started' for r in transcript),integrity_unchanged=integrity,
        memory_and_context_record_retrieval=False,ab_comparison='NOT_RUN',
        compression_effect='NOT_EVALUABLE_NO_COMPACTION_OPPORTUNITY' if captured_status=='NO_COMPACTION_OPPORTUNITY' else 'NOT_EVALUATED')
    save(output/'result.json',result)
    print(json.dumps(dict(output=str(output.resolve()),**result),ensure_ascii=False),flush=True)

if __name__=='__main__':main()
