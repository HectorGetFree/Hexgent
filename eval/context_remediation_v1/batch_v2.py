"""冻结15样本的一次性压缩评测：无MAIN、无人工填充、无重跑。"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from hexgent.agent_engine import AgentEngine
from hexgent.config import load_config
from hexgent.context import ContextManager
from hexgent.context.context_builder import build_context_details
from hexgent.context.context_compaction import compact_context_if_needed, _select_cut_index, COMPACTION_INSTRUCTIONS
from hexgent.context.key_fact_extractor import EXTRACTION_INSTRUCTIONS
from hexgent.context.key_fact_snapshot import snapshot_message
from hexgent.loop.loop_state import LoopState
from hexgent.loop.agent_turn import _available_tools_for_state
from hexgent.session.checkpoint import _context_snapshot
from hexgent.utils.token_estimator import estimate_request_tokens, estimate_value_tokens
from eval.support.agent_contracts import text_response
from .offline import ROOT, fingerprints
from .factorial_pilot import file_hashes
from .rl3_e2e import Audit, ExperimentStop, drain

IDS=[f'F{f}-{i:02d}' for f in range(1,5) for i in range(1,4)]+['L1','L2','L3']

def save(out,name,value):
    p=out/name;p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n')
def read(p):return json.loads(p.read_text())
def fixture_paths(identity):
    if identity.startswith('F'):
        return ROOT/f'eval/context_management/c2_prep/fixtures/{identity}.json',ROOT/f'eval/context_management/c2_prep/oracles/{identity}.json'
    return ROOT/f'eval/context_management/c2_prep_2/long_fixtures/{identity}.json',ROOT/f'eval/context_management/c2_prep_3/oracle_v2/{identity}.json'

def setup(out,identity):
    fixture,oracle=map(read,fixture_paths(identity))
    config=load_config(ROOT)
    config=replace(config,context=replace(config.context,max_compaction_attempts=1),context_extraction=replace(config.context_extraction,enabled=False),
        context_retrieval=replace(config.context_retrieval,enabled=False),memory_retrieval=replace(config.memory_retrieval,enabled=False))
    if identity.startswith('F'):config=replace(config,context=replace(config.context,recent_context_tokens=100,reactive_recent_context_tokens=50))
    manager=ContextManager();engine=AgentEngine(config,out/'workspace',context_manager=manager)
    engine.workdir.mkdir(parents=True,exist_ok=True);engine.session.initialize_runtime_session(identity.lower())
    source_map={}
    if identity.startswith('F'):
        for i,m in enumerate(fixture['messages']):
            query=m['text'] if m['role']=='user' else '核对资料来源 '+m['id']
            q=manager.append_user_query(query);engine.session.record_user_query(q,source='frozen_fixture')
            source_map[m['id']]=manager.message_ids[-1] if m['role']=='user' else f'msg-{len(manager.active_messages)+2}'
            if m['role']=='tool':
                messages=[dict(type='function_call',call_id=m['id'],name='read_file',arguments=json.dumps(dict(path=f'source-{i}.md'))),
                    dict(type='function_call_output',call_id=m['id'],output=m['text']),*text_response('资料已核对，尚未执行业务操作。').output]
            else:messages=list(text_response('已收到本轮明确更正。').output)
            manager.append_messages(messages,completes_round=True);engine.session.record_context_messages(messages,completes_round=True,source='frozen_fixture')
        for f in oracle['facts']:
            if f['channel']=='EXTERNALLY_PROTECTED':engine.manage_pinned_constraint('pin',f['fact_id'],text=f['quote'])
        final_query='继续核对当前有效事实与待办状态，不执行业务变更。'
    else:
        for r in fixture['rounds']:
            q=manager.append_user_query(r['query']);engine.session.record_user_query(q,source='frozen_fixture')
            manager.append_messages(deepcopy(r['messages']),completes_round=True);engine.session.record_context_messages(deepcopy(r['messages']),completes_round=True,source='frozen_fixture')
        final_query=fixture['final_query']
        for idx,s in enumerate(manager.prefix_sources(len(manager.active_messages))):
            if s['message'].get('type')=='function_call_output':source_map[s['message']['call_id']]=s['message_id']
    q=manager.append_user_query(final_query);engine.session.record_user_query(q,source='frozen_fixture')
    state=LoopState(manager,engine.tool_context)
    built=build_context_details(engine.tool_context,(),(),None,config.context.runtime_injection_tokens,pinned_constraints=engine.list_pinned_constraints(),pinned_constraints_tokens=config.context.pinned_constraints_tokens)
    return engine,state,built,_available_tools_for_state(state),fixture,oracle,source_map

def requests(engine,state,built,tools):
    manager=engine.context_manager;limits=engine.config.context
    cut=_select_cut_index(manager,limits.recent_context_tokens)
    sources=manager.prefix_sources(cut);excluded=manager.intent_source_ids()
    payload=dict(history_prefix=[s for s in sources if s['message_id'] not in excluded],previous_snapshot=None,
        intent_reference=dict(latest_query=manager.latest_query_reference,plan=None),as_of_message_id=sources[-1]['message_id'])
    extract=dict(model='gpt-6.1-sol',instructions=EXTRACTION_INSTRUCTIONS,input=[dict(role='user',content=json.dumps(payload,ensure_ascii=False))],max_output_tokens=limits.fact_extraction_output_tokens,stream=True)
    summary=dict(model='gpt-6.1-sol',instructions=COMPACTION_INSTRUCTIONS,input=manager.materialize_messages(manager.active_messages[:cut]),max_output_tokens=limits.summary_output_tokens,stream=True)
    return cut,extract,summary

def prepare(out):
    if out.exists():raise RuntimeError('禁止覆盖批次')
    out.mkdir(parents=True)
    production=file_hashes(ROOT/'hexgent');assets=fingerprints();rows=[]
    for identity in IDS:
        directory=out/identity;engine,state,built,tools,fixture,oracle,source_map=setup(directory,identity)
        cut,extract,summary=requests(engine,state,built,tools)
        before=estimate_request_tokens(built.instructions,engine.context_manager.model_messages(),tools)
        bodies=[m['text'] for m in fixture['messages']] if identity.startswith('F') else [str(m.get('output','')) for r in fixture['rounds'] for m in r['messages'] if isinstance(m,dict) and m.get('type')=='function_call_output']
        text='\n'.join(bodies);lines=[line.strip() for line in text.splitlines() if len(line.strip())>=12]
        counts=Counter(lines);repeated=sum((n-1)*len(s) for s,n in counts.items());line_chars=sum(map(len,lines))
        required=[f for f in oracle['facts'] if f['channel']=='SUMMARY_REQUIRED']
        source_byid={s['message_id']:s['message'] for s in engine.context_manager.prefix_sources(len(engine.context_manager.active_messages))}
        mapping=[]
        for f in oracle['facts']:
            ref=source_map.get(f.get('source_message_id',f.get('source_call_id')))
            source=source_byid.get(ref,{})
            located=f['quote'] in str(source.get('output',source.get('content','')))
            mapping.append(dict(fact_id=f['fact_id'],original_source=f.get('source_message_id',f.get('source_call_id')),runtime_source=ref,quote_located=located,channel=f['channel'],in_prefix=ref in {s['message_id'] for s in engine.context_manager.prefix_sources(cut)}))
        ei=estimate_request_tokens(extract['instructions'],extract['input'],[]);si=estimate_request_tokens(summary['instructions'],summary['input'],[])
        fp,op=fixture_paths(identity)
        row=dict(id=identity,group=fixture.get('family','LONG'),title=fixture['title'],source_chars=len(text),source_estimated_tokens=estimate_value_tokens(text),required_fact_count=len(required),
            facts_per_1000_source_estimated_tokens=1000*len(required)/max(1,estimate_value_tokens(text)),repeated_line_char_ratio=repeated/max(1,line_chars),
            repetition_method='长度>=12的去空白完全相同行占比；代码样板重复不自动等于无意义填充',
            original_body_unchanged=True,oracle_quote_mapping=mapping,oracle_locator_valid=all(m['quote_located'] for m in mapping),
            padding_added=False,history_origin=fixture.get('evidence_type','SYNTHETIC_FIXED_HISTORY'),
            evidence_limit='短小事实型Fixture，强制切点不代表自然长历史压缩' if identity.startswith('F') else '冻结源码阅读+合成台账，非自然真实Agent轨迹',
            production_would_trigger=before>=engine.config.context.compaction_threshold_tokens,forced_compaction=True,estimated_before=before,
            recent_context_tokens=engine.config.context.recent_context_tokens,cutpoint=extract['input'] and json.loads(extract['input'][0]['content'])['as_of_message_id'],
            extractor_estimated_input=ei,summary_estimated_input=si,estimated_auxiliary_ceiling=ei+extract['max_output_tokens']+si+summary['max_output_tokens'],
            budget_fits=all(t+o+engine.config.context.safety_margin_tokens<=engine.config.context.auxiliary_context_window_tokens for t,o in [(ei,extract['max_output_tokens']),(si,summary['max_output_tokens'])]),
            fixture_sha256=sha256(fp.read_bytes()).hexdigest(),oracle_sha256=sha256(op.read_bytes()).hexdigest(),source_map=source_map)
        save(directory,'audit.json',row);save(directory,'config.json',asdict(engine.config));save(directory,'oracle.json',oracle)
        save(directory,'requests-frozen.json',dict(extraction=extract,summary=summary));save(directory,'initial-context.json',_context_snapshot(engine.context_manager))
        engine.session.store.release_write_ownership() if hasattr(engine.session.store,'release_write_ownership') else None
        rows.append(row)
    save(out,'audit.json',rows)
    save(out,'manifest.json',dict(created_at=datetime.now(timezone.utc).isoformat(),ids=IDS,production_hashes=production,historical_assets=assets,
        cases=rows,max_provider_calls=30,per_case_extraction_limit=1,per_case_summary_limit=1,retries=0,real_main_calls=0,
        oracle_rule='沿用原事实实体/值/状态/条件/否定，人工逐事实判断允许释义；不以关键词命中替代语义。',
        snapshot_gain='同一Summary条件下的保留增量：Summary vs Snapshot+Summary，不是独立Summary-only算法对照。另报含Recent Tail和Pinned最终请求覆盖。',
        total_estimated_auxiliary_ceiling=sum(r['estimated_auxiliary_ceiling'] for r in rows),capacity='USER_ASSERTED_CAPACITY_128K_UNVERIFIED'))
    print(json.dumps(dict(output=str(out),samples=15,budget_ceiling=sum(r['estimated_auxiliary_ceiling'] for r in rows),audit_issues=[r['id'] for r in rows if not r['oracle_locator_valid'] or not r['budget_fits']]),ensure_ascii=False))

class BatchAudit(Audit):
    def create(self,**request):
        stage='EXTRACTOR' if request.get('instructions')==EXTRACTION_INSTRUCTIONS else 'SUMMARY' if request.get('instructions')==COMPACTION_INSTRUCTIONS else 'FORBIDDEN'
        idx=len(self.shared['rows'])
        if idx>=2 or stage!=['EXTRACTOR','SUMMARY'][idx]:raise ExperimentStop('BATCH_CALL_ORDER_OR_LIMIT')
        expected=read(self.out/'requests-frozen.json')['extraction' if idx==0 else 'summary']
        if request!=expected:raise ExperimentStop('FROZEN_REQUEST_MISMATCH')
        limits=self.config.context;estimate=estimate_request_tokens(request['instructions'],request['input'],[])
        if estimate+request['max_output_tokens']+limits.safety_margin_tokens>limits.auxiliary_context_window_tokens:raise ExperimentStop('BUDGET_EXCEEDED')
        save(self.out,f'{stage}/actual-request.json',request)
        row=dict(stage=stage,started_at=datetime.now(timezone.utc).isoformat(),status='STARTED',input_tokens=None,output_tokens=None,estimated_input_tokens=estimate,retries=0)
        self.shared['rows'].append(row);save(self.out,'provider-ledger.json',self.shared['rows'])
        return self.stream(request,row)
    def with_options(self,**options):
        if options.get('max_retries',0)!=0:raise ExperimentStop('SDK_RETRY_FORBIDDEN')
        return BatchAudit(self.client.with_options(**options),self.out,self.config,self.shared)

def execute_case(out,identity,base):
    directory=out/identity;manifest=read(out/'manifest.json');audited=read(directory/'audit.json')
    # prepared workspace不是前次模型修改的工作区；仅本case执行一次。
    if (directory/'started.json').exists():raise RuntimeError('禁止重复样本')
    save(directory,'started.json',dict(started_at=datetime.now(timezone.utc).isoformat()))
    if not audited['budget_fits'] or not audited['oracle_locator_valid']:
        result=dict(id=identity,status='AUDIT_OR_BUDGET_BLOCKED',provider_calls=0);save(directory,'result.json',result);return result
    # 克隆冻结初始durable上下文；不重播工具Handler，不制造历史执行证据。
    from hexgent.context import ContextManager
    from hexgent.session import SessionStore, Session, SessionSettings
    from hexgent.session.session_restore import SessionRestorer
    config=load_config(ROOT);config=replace(config,context=replace(config.context,**{k:v for k,v in read(directory/'config.json')['context'].items()}),
        context_extraction=replace(config.context_extraction,enabled=False),context_retrieval=replace(config.context_retrieval,enabled=False),memory_retrieval=replace(config.memory_retrieval,enabled=False))
    engine=AgentEngine.restore(config,directory/'workspace',identity.lower())
    manager=engine.context_manager;state=LoopState(manager,engine.tool_context)
    built=build_context_details(engine.tool_context,(),(),None,config.context.runtime_injection_tokens,pinned_constraints=engine.list_pinned_constraints(),pinned_constraints_tokens=config.context.pinned_constraints_tokens)
    tools=_available_tools_for_state(state);audit=BatchAudit(base,directory,config)
    try:
        def commit(boundary):
            boundary['resume_state']=replace(state,transition='context_compacted').to_snapshot().to_dict()
            return engine.session.record_compaction(boundary)
        outcome=drain(compact_context_if_needed(manager,audit,'gpt-6.1-sol',built.instructions,tools,config.context,force=True,
            instruction_budget=built.instruction_tokens,commit_boundary=commit,output_allowance=config.loop.max_output_tokens_per_round))
        save(directory,'outcome.json',asdict(outcome))
        success=outcome.status in {'compacted','partial'}
        if success:
            cp=engine.session.create_checkpoint(context_manager=manager,plan=None,reason='context_compaction_completed',loop_state=replace(state,transition='context_compacted'),run_status='active')
            save(directory,'checkpoint.json',asdict(cp));save(directory,'committed-context.json',_context_snapshot(manager))
            request=dict(model='gpt-6.1-sol',instructions=built.instructions,input=manager.model_messages(),tools=tools,max_output_tokens=config.loop.max_output_tokens_per_round)
            tokens=estimate_request_tokens(request['instructions'],request['input'],tools)
            assert tokens+request['max_output_tokens']+config.context.safety_margin_tokens<=config.context.context_window_tokens
            assert sum('<key-fact-snapshot>' in str(m) for m in request['input'])==1
            save(directory,'injection-request-not-dispatched.json',request);save(directory,'transcript.json',engine.session.store.read_transcript())
        result=dict(id=identity,status='COMPACTION_COMMITTED' if success else 'CANDIDATE_REJECTED',compaction_status=outcome.status,reason=outcome.message,
            provider_calls=len(audit.shared['rows']),before_tokens=outcome.before_tokens,after_tokens=outcome.after_tokens,
            reduction_pct=100*(1-outcome.after_tokens/outcome.before_tokens) if success else 0,main_dispatched=False)
    except BaseException as error:
        result=dict(id=identity,status='INFRA_OR_AUDIT_FAILURE',reason=str(error),provider_calls=len(audit.shared['rows']),main_dispatched=False)
    save(directory,'result.json',result)
    print(json.dumps(result,ensure_ascii=False),flush=True);return result

def execute(out):
    manifest=read(out/'manifest.json')
    if (out/'started.json').exists():raise RuntimeError('禁止重跑批次')
    assert file_hashes(ROOT/'hexgent')==manifest['production_hashes'] and fingerprints()==manifest['historical_assets']
    from hexgent.client import get_client,get_model
    client=get_client().with_options(max_retries=0);assert client.max_retries==0 and get_model()=='gpt-6.1-sol'
    assert urlsplit(str(client.base_url)).hostname in {'rightapi.ai','www.rightapi.ai'}
    save(out,'started.json',dict(started_at=datetime.now(timezone.utc).isoformat(),concurrency=3))
    results=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs={pool.submit(execute_case,out,identity,client):identity for identity in manifest['ids']}
        for job in as_completed(jobs):
            try:results.append(job.result())
            except BaseException as error:results.append(dict(id=jobs[job],status='EVAL_FAILURE',reason=str(error)))
            save(out,'execution-results.json',results)
    save(out,'integrity.json',dict(production_unchanged=file_hashes(ROOT/'hexgent')==manifest['production_hashes'],historical_unchanged=fingerprints()==manifest['historical_assets'],model_main_calls=0))
    print('BATCH_COMPLETE_AWAITING_INDEPENDENT_FACT_REVIEW',flush=True)

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--prepare',action='store_true');p.add_argument('--execute',action='store_true');a=p.parse_args()
    if a.prepare:prepare(a.output.resolve())
    if a.execute:execute(a.output.resolve())

if __name__=='__main__':main()
