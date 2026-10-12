"""独立Oracle v2和七例Smoke合同；只读取冻结历史，不生成新Fixture。"""
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from math import ceil
from pathlib import Path
import json

from eval.context_management.c1_independent import save
from hexgent.context.context_compaction import COMPACTION_INSTRUCTIONS
from hexgent.utils.token_estimator import estimate_request_tokens

ROOT=Path(__file__).resolve().parent
OLD=ROOT.parent/'c2_prep'
SECOND=ROOT.parent/'c2_prep_2'
ORDER=['F1-02','L2','F2-01','F3-02','F4-03','L1','L3']


def digest(path): return sha256(path.read_bytes()).hexdigest()


def freeze(path,value):
    body=json.dumps(value,ensure_ascii=False,indent=2)
    if path.exists() and path.read_text()!=body: raise RuntimeError(f'禁止覆盖新冻结资产：{path}')
    if not path.exists(): save(path,value)


def read_json(path): return json.loads(path.read_text())


def old_assets(identifier):
    if identifier.startswith('F'):
        return OLD/'fixtures'/f'{identifier}.json',OLD/'oracles'/f'{identifier}.json'
    return SECOND/'long_fixtures'/f'{identifier}.json',SECOND/'long_oracles'/f'{identifier}.json'


def snapshot():
    folders=[OLD,SECOND,ROOT.parent/'runs'/'c1-independent-20261011T065644251729Z']
    return {str(p):digest(p) for folder in folders for p in folder.rglob('*') if p.is_file() and '__pycache__' not in str(p)}


def quote_in(value,quote):
    return quote in json.dumps(value,ensure_ascii=False)


def review_source(fact,fixture):
    if 'source_round' in fact:
        message=fixture['rounds'][fact['source_round']]['messages'][1]
        if message['call_id']!=fact['source_call_id']: raise RuntimeError('来源Call ID不一致')
        if fact['quote'] not in message['output']: raise RuntimeError('来源Quote不一致')
        return dict(round=fact['source_round'],call_id=message['call_id'],field='output')
    message=next(m for m in fixture['messages'] if m['id']==fact['source_message_id'])
    if fact['quote'] not in message['text']: raise RuntimeError('来源Quote不一致')
    return dict(message_id=message['id'],field='text')


def main():
    reviewed=read_json(SECOND/'reviewed_dry_run.json')
    run=Path(reviewed['report']['output'])
    all_counts=Counter(); changes=[]; checked=0; migration=[]
    for item in reviewed['report']['results']:
        identifier=item['id']; fp,op=old_assets(identifier)
        fixture,original=read_json(fp),read_json(op)
        evidence=read_json(run/identifier/'evidence.json')
        comp=[c for c in evidence['calls'] if c['call_type']=='COMPACTION'][0]
        maincall=[c for c in evidence['calls'] if c['call_type']=='MAIN'][0]
        tail=[m for m in maincall['request']['input'] if not
              (m.get('role')=='developer' and str(m.get('content','')).startswith('以下是此前会话的压缩摘要'))]
        facts=[]
        for f in original['facts']:
            source=review_source(f,fixture)
            summary=quote_in(comp['request']['input'],f['quote'])
            recent=quote_in(tail,f['quote'])
            pinned=f['quote'] in maincall['request']['instructions']
            final=quote_in(maincall['request'],f['quote'])
            seq=[e['sequence'] for e in evidence['transcript'] if quote_in(e.get('payload',{}),f['quote'])]
            if not seq: raise RuntimeError('事实不能定位到Production Transcript')
            responsibility=f['channel']
            if responsibility=='SUMMARY_REQUIRED' and recent:
                responsibility='RECENT_TAIL_PRESERVED'
            elif responsibility=='SUMMARY_REQUIRED' and not summary:
                raise RuntimeError(f"事实既不在摘要输入也不在可靠渠道：{f['fact_id']}")
            if responsibility=='EXTERNALLY_PROTECTED' and not pinned:
                raise RuntimeError(f"未核验实际外部渠道：{f['fact_id']}")
            changed=responsibility!=f['channel']
            if changed: changes.append(dict(fact_id=f['fact_id'],from_=f['channel'],to=responsibility))
            facts.append(dict(f,source_location=dict(source,transcript_sequences=seq),
                summary_input_present=summary,recent_tail_present=recent,final_request_present=final,
                pinned_present=pinned,summary_responsibility=responsibility,
                evidence_reference=str(run/identifier/'evidence.json')))
            all_counts[responsibility]+=1; checked+=1
        freeze(ROOT/'oracle_v2'/f'{identifier}.json',dict(schema_version=2,fixture_id=identifier,
            original_oracle=str(op),original_oracle_hash=digest(op),fixture_hash=digest(fp),
            facts=facts,scope='SOURCE_RESPONSIBILITY_AUDIT_NOT_QUALITY_RESULT'))
        migration.append(dict(id=identifier,facts=len(facts),original_hash=digest(op),v2_hash=digest(ROOT/'oracle_v2'/f'{identifier}.json')))
    freeze(ROOT/'oracle_migration.json',dict(checked=checked,changes=changes,counts=dict(all_counts),fixtures=migration))
    entries=[]
    retry_extra='\n上一次摘要格式或压缩收益不合格。必须显著缩短内容，并严格只输出 summary 区块。'
    for index,identifier in enumerate(ORDER):
        fp,op=old_assets(identifier)
        evidence=read_json(run/identifier/'evidence.json')
        c=next(c for c in evidence['calls'] if c['call_type']=='COMPACTION')
        request=dict(c['request'],instructions=COMPACTION_INSTRUCTIONS+retry_extra)
        estimate=estimate_request_tokens(request['instructions'],request['input'],request.get('tools',[]))
        cap=ceil(ceil(estimate*1.25)/1024)*1024
        config=read_json(run/identifier/'frozen-state.json')['config']
        limits=config['context']; maxcalls=limits['max_compaction_attempts']*(limits['max_partial_compactions']+1)
        entries.append(dict(fixture_id=identifier,fixture_path=str(fp),fixture_hash=digest(fp),
            oracle_v2_path=str(ROOT/'oracle_v2'/f'{identifier}.json'),oracle_v2_hash=digest(ROOT/'oracle_v2'/f'{identifier}.json'),
            source='SYNTHETIC_LONG_HISTORY' if identifier.startswith('L') else 'SYNTHETIC_FIXED_HISTORY',
            category='C2-B' if identifier.startswith('L') else 'C2-A',execution_order=index+1,stage=1 if index<2 else 2,
            frozen_config=config,compaction_parameters=limits,
            observed_retry_candidate_estimated_input=estimate,max_reserved_input_per_call=cap,
            max_output_tokens_per_call=limits['summary_output_tokens'],max_compaction_calls=maxcalls,
            artifact_subdirectory=identifier))
    stages=[]
    for stage in (1,2):
        subset=[e for e in entries if e['stage']==stage]
        stages.append(dict(stage=stage,fixture_ids=[e['fixture_id'] for e in subset],
            max_compaction_calls=sum(e['max_compaction_calls'] for e in subset),
            max_total_reserved_input=sum(e['max_compaction_calls']*e['max_reserved_input_per_call'] for e in subset),
            max_total_reserved_output=sum(e['max_compaction_calls']*e['max_output_tokens_per_call'] for e in subset),
            human_approval_required=True))
    manifest=dict(schema_version=1,fixtures=entries,stages=stages,
        provider=dict(host='www.rightapi.ai',base_url='https://www.rightapi.ai/v1',model='gpt-6.1-sol',sdk_max_retries=0,
            protocol='RESPONSES_STREAMING',parameters_source='Production Compaction请求，无新增采样参数'),
        policy=dict(input_margin_ratio=1.25,max_compaction_calls=sum(s['max_compaction_calls'] for s in stages),
            max_total_reserved_input=sum(s['max_total_reserved_input'] for s in stages),
            max_total_reserved_output=sum(s['max_total_reserved_output'] for s in stages),
            max_reserved_input_per_call=max(e['max_reserved_input_per_call'] for e in entries),
            max_output_tokens_per_call=max(e['max_output_tokens_per_call'] for e in entries)),
        monetary_cost='MONETARY_COST_NOT_EVALUATED',eval_runner_retries=0,main_provider='FAKE_TERMINAL_ONLY',
        capacity_evidence=dict(configured_context_window_tokens=128000,configured_source='hexgent/context/context_budget.py',
            provider_route_capacity='UNCONFIRMED',confirmed=False,reason='本地配置不是Provider路由容量证明'),
        stops=['TOKEN_OR_CALL_BUDGET_EXHAUSTED','SDK_RETRY_NOT_DISABLED','ATTRIBUTION_MISSING',
            'ARTIFACT_PERSIST_FAILED','CAPACITY_NOT_CONFIRMED','STAGE_2_REQUIRES_HUMAN_APPROVAL'],
        output_directory='eval/context_management/c2_prep_3/runs/smoke-<unique-run-id>',
        production_hash_source=str(SECOND/'reviewed_dry_run.json'))
    freeze(ROOT/'smoke_manifest.json',manifest)


if __name__=='__main__': main()
