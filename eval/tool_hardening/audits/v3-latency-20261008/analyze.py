"""仅读取现有 Artifact；不导入 Runtime、不调用模型、不覆盖报告。"""
from pathlib import Path
from hashlib import sha256
from datetime import datetime
import json
import re

OUT = Path(__file__).resolve().parent
ROOT = Path('/Users/hector/dev/hexgent')
RUNS = ROOT / 'eval/tool_hardening/runs'
SOURCES = {'ON': RUNS/'v3-on-off-replay-20261008-r1/smoke/tool_pipeline/1/ON',
           'OFF': RUNS/'v3-manual-20261008T135408Z/tool_pipeline/pair-1/OFF'}

def save(name, value):
    with (OUT/name).open('x') as file:
        file.write(json.dumps(value,ensure_ascii=False,indent=2))

def normalize(value, side):
    text=json.dumps(value,ensure_ascii=False,sort_keys=True).replace(str(SOURCES[side]/'workspace'),'<WORKSPACE>')
    return re.sub('20261008-(214932|215414)-6adaeaa0','<SESSION>',text)

def digest(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def interval(a,b):
    return (datetime.fromisoformat(b)-datetime.fromisoformat(a)).total_seconds()*1000

data={}
protected={}
for side,path in SOURCES.items():
    transcript=next((path/'workspace/.hexgent/sessions').glob('*/transcript.jsonl'))
    for p in (path/'artifact.json',path/'provider-calls.json',transcript):
        protected[str(p)]=sha256(p.read_bytes()).hexdigest()
    artifact=json.loads((path/'artifact.json').read_text())
    requests=json.loads((path/'provider-calls.json').read_text())
    records=[json.loads(line) for line in transcript.read_text().splitlines()]
    main=[r for r in requests if r['request_type']=='MAIN_AGENT']
    call_turn={c['call_id']:(i,r) for i,r in enumerate(main,1) for c in r['tool_calls']}
    events=[]
    def add(kind,**fields):
        item=dict(event_index=len(events),event_type=kind,start_timestamp='UNOBSERVED',end_timestamp='UNOBSERVED',
            duration_ms='UNOBSERVED',agent_turn_index=None,provider_request_id=None,provider_model=None,
            tool_call_id=None,tool_name=None,tool_status=None,context_tokens='UNOBSERVED',input_tokens=None,
            output_tokens=None,artifact_reference=None)
        item.update(fields)
        events.append(item)
    for i,r in enumerate(requests):
        turn=next((j for j,m in enumerate(main,1) if m['turn_id']==r['turn_id']),None)
        add('Main Provider Request' if turn else 'Auxiliary Provider Request',start_timestamp=r['start_timestamp'],
            end_timestamp=r['end_timestamp'],duration_ms=r['provider_latency_ms'],agent_turn_index=turn,
            provider_request_id=r['response_id'],provider_model=r['model'],input_tokens=r['input_tokens'],
            output_tokens=r['output_tokens'],status=r['status'],failure_reason=r['failure_reason'],
            wall_timestamp_duration_ms=interval(r['start_timestamp'],r['end_timestamp']),
            artifact_reference=f'{path}/provider-calls.json#/{i}')
        if r['tool_calls']:
            add('Tool Pipeline Active Aggregate',duration_ms=r['tool_batch_latency_ms'],agent_turn_index=turn,
                artifact_reference=f'{path}/provider-calls.json#/{i}/tool_batch_latency_ms',
                scope='profiled generator resume segments; not independent handler wall span')
    for i,r in enumerate(records):
        p=r['payload'];cid=p.get('call_id');turn,req=call_turn.get(cid,(None,{}));t=r['type']
        kind=('Tool Dispatch' if t=='tool_execution_dispatched' else 'Tool Result Delivery' if
            t=='context_messages_appended' and p.get('source')=='tool_results' else 'Retry / Timeout / Cancellation' if
            t=='context_record_extraction_failed' else 'Session / Transcript Persistence')
        add(kind,start_timestamp=r['created_at'],agent_turn_index=turn,provider_request_id=req.get('response_id'),
            provider_model=req.get('model'),tool_call_id=cid,tool_name=p.get('tool_name'),tool_status=p.get('status'),
            transcript_event_type=t,transcript_sequence=r['sequence'],
            artifact_reference=f'{transcript} line {i+1}; sequence {r["sequence"]}',
            limitation='durable timestamp is an event point, not persistence or handler duration')
    for kind in ('Tool Handler Execution','Context Build / Compaction','Other Agent Runtime Overhead'):
        add(kind,artifact_reference=str(path/'artifact.json'),limitation='independent timing UNOBSERVED')
    events.sort(key=lambda e:datetime.fromisoformat(e['start_timestamp']).timestamp() if e['start_timestamp']!='UNOBSERVED' else float('inf'))
    for i,e in enumerate(events):e['event_index']=i
    spans=sorted((datetime.fromisoformat(r['start_timestamp']).timestamp(),datetime.fromisoformat(r['end_timestamp']).timestamp()) for r in requests)
    merged=[]
    for a,b in spans:
        if merged and a<=merged[-1][1]:merged[-1][1]=max(b,merged[-1][1])
        else:merged.append([a,b])
    summary=dict(total_agent_ms=artifact['total_agent_latency_ms'],main_provider_ms=sum(r['provider_latency_ms'] for r in main),
        auxiliary_provider_ms=sum(r['provider_latency_ms'] for r in requests if r['request_type']!='MAIN_AGENT'),
        tool_profiled_ms=artifact['total_tool_latency_ms'],provider_timestamp_union_ms=sum(b-a for a,b in merged)*1000,
        main_requests=len(main),tool_calls=sum(len(r['tool_calls']) for r in main),
        main_input_tokens=sum(r['input_tokens'] for r in main),main_output_tokens=sum(r['output_tokens'] for r in main),
        auxiliary_usage='UNAVAILABLE: cancelled',compaction_events=sum('compaction' in r['type'] for r in records),
        main_requests_detail=[dict(main_turn=i,observed_turn=r['turn_id'],response_id=r['response_id'],latency_ms=r['provider_latency_ms'],
            input_tokens=r['input_tokens'],output_tokens=r['output_tokens'],tool_call_count=len(r['tool_calls']),
            tool_calls=r['tool_calls'],artifact_reference=f'{path}/provider-calls.json#/{requests.index(r)}') for i,r in enumerate(main,1)],
        extraction_events=[r for r in records if r['type'] in ('context_record_extraction_started','context_record_extraction_failed')])
    summary['accounting_residual_ms']=summary['total_agent_ms']-summary['main_provider_ms']-summary['auxiliary_provider_ms']-summary['tool_profiled_ms']
    summary['outside_provider_timestamp_ms']=summary['total_agent_ms']-summary['provider_timestamp_union_ms']
    save(side+'_TIMELINE.json',dict(summary=summary,events=events,
        limitation='Not a complete additive wall partition; accounting residual is not measured runtime overhead'))
    data[side]=dict(summary=summary,main=main,records=records,artifact=artifact)

aligned=[]
for i in range(14):
    a=data['ON']['main'][i] if i<10 else None;b=data['OFF']['main'][i]
    row=dict(main_turn=i+1,ON_response_id=a and a['response_id'],OFF_response_id=b['response_id'],
        ON_calls=a and a['tool_calls'],OFF_calls=b['tool_calls'],OFF_latency_ms=b['provider_latency_ms'])
    if a:
        row.update(ON_latency_ms=a['provider_latency_ms'],captured_request_equal=a['request']==b['request'],
            normalized_request_equal=normalize(a['request'],'ON')==normalize(b['request'],'OFF'),
            ON_request_hash=digest(a['request']),OFF_request_hash=digest(b['request']),
            differing_fields=[k for k in a['request'] if a['request'][k]!=b['request'].get(k)],
            normalized_output_equal=normalize(a['response']['output'],'ON')==normalize(b['response']['output'],'OFF'),
            request_field_comparison={k:dict(captured_equal=a['request'][k]==b['request'].get(k),
                normalized_equal=normalize(a['request'][k],'ON')==normalize(b['request'].get(k),'OFF')) for k in a['request']},
            tool_semantic_equal=[(c['name'],json.loads(c['arguments'])) for c in a['tool_calls']]==[(c['name'],json.loads(c['arguments'])) for c in b['tool_calls']])
    row['limitation']='ordinal alignment, not equal planning state after first response'
    aligned.append(row)
save('ROUND_ALIGNMENT.json',aligned)

def tools(side):
    d=data[side];facts={r['payload']['call_id']:r for r in d['records'] if r['type']=='tool_execution_finished'}
    result=[]
    for i,r in enumerate(d['main'],1):
        for c in r['tool_calls']:
            f=facts[c['call_id']];p=f['payload'];body=(SOURCES[side]/'workspace'/p['persisted_output']).read_text() if p.get('persisted_output') else p.get('output')
            permissions=[{k:v for k,v in t['payload'].items() if k!='call_id'} for t in d['records'] if
                t['type'] in ('permission_evaluated','permission_resolved') and t['payload'].get('call_id')==c['call_id']]
            result.append(dict(turn=i,call_id=c['call_id'],name=c['name'],arguments=normalize(json.loads(c['arguments']),side),
                body=body,status=p['status'],permissions=permissions,sequence=f['sequence']))
    return result
matched=[]
for a in tools('ON'):
    for b in tools('OFF'):
        if a['name']==b['name'] and a['arguments']==b['arguments']:
            matched.append(dict(ON_call=a['call_id'],OFF_call=b['call_id'],ON_turn=a['turn'],OFF_turn=b['turn'],
                name=a['name'],arguments=a['arguments'],raw_body_equal=a['body']==b['body'],
                normalized_body_equal=normalize(a['body'],'ON')==normalize(b['body'],'OFF'),
                status_equal=a['status']==b['status'],permission_equal=a['permissions']==b['permissions'],
                ON_sequence=a['sequence'],OFF_sequence=b['sequence'],
                match_policy='same name/normalized arguments; repeated calls not forced into unique pairing'))
save('MATCHED_TOOL_RESULTS.json',matched)
delta={k:data['OFF']['summary'][k]-data['ON']['summary'][k] for k in
    ('total_agent_ms','main_provider_ms','auxiliary_provider_ms','tool_profiled_ms','accounting_residual_ms')}
save('EVIDENCE_MATRIX.json',dict(source_hashes=protected,delta=delta,
    main_provider_delta_share=delta['main_provider_ms']/delta['total_agent_ms'],evidence=[
    dict(explanation='fixed trace scheduler performance',grade='CONFIRMED DIRECT EFFECT',references=[str(RUNS/'v3-cross-trace-20261008-r1/report.json')],value='both traces slightly slower in parallel'),
    dict(explanation='main provider request cost difference',grade='CONFIRMED OBSERVATIONAL CONTRIBUTION',references=['ON_TIMELINE.json','OFF_TIMELINE.json']),
    dict(explanation='provider nondeterminism/route/load',grade='PLAUSIBLE MECHANISM',references=['ROUND_ALIGNMENT.json /0'],value='exact internal cause UNDETERMINED'),
    dict(explanation='scheduler caused first choice divergence or 4.12x E2E gain',grade='UNSUPPORTED / RULED OUT',references=['ROUND_ALIGNMENT.json /0'],value='first divergence before dispatch'),
    dict(explanation='extra compaction/new record injection',grade='UNSUPPORTED / RULED OUT',value='zero compaction, both extraction cancelled'),
    dict(explanation='SDK retry/queue/rate limit/close delay cause',grade='PLAUSIBLE MECHANISM',value='no per-attempt or close span evidence: UNDETERMINED')]))
for path,expected in protected.items():assert sha256(Path(path).read_bytes()).hexdigest()==expected
print(json.dumps(dict(delta=delta,share=delta['main_provider_ms']/delta['total_agent_ms'],matched_count=len(matched),
    matched_normalized_equal=sum(m['normalized_body_equal'] for m in matched),first_input_equal=aligned[0]['captured_request_equal']),indent=2))
