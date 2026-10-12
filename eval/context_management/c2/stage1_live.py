"""获批的 Stage 1 执行与证据收集；只运行 F1-02、L2，不自动重试。"""
from datetime import datetime,timezone
from pathlib import Path
import json
import os
import traceback
from dotenv import load_dotenv

from eval.context_management.c2_dispatch_guard_repair.entry import execute,make_client
from eval.context_management.c2_prep_3.prepare import ROOT,read_json,digest,snapshot
from eval.context_management.c2_prep_3.runner import policy,validate_capacity,load_rounds
from eval.context_management.c2_prep_3.budget import TokenBudget
from eval.context_management.c1_independent import hashes,save


def main():
    manifest=read_json(ROOT/'smoke_manifest.json');confirmation=read_json(ROOT/'capacity_confirmation.json')
    validate_capacity(manifest,confirmation)
    repair=read_json(ROOT.parent/'c2_dispatch_guard_repair/production_hash_check.json')
    if hashes()!=repair['production_after']:raise RuntimeError('PRODUCTION_HASH_DRIFT')
    for p,h in repair['prep3_before'].items():
        if digest(Path(p))!=h:raise RuntimeError('FROZEN_PREP3_HASH_DRIFT')
    entries=[e for e in manifest['fixtures'] if e['stage']==1]
    if [e['fixture_id'] for e in entries]!=['F1-02','L2']:raise RuntimeError('STAGE1_ORDER_MISMATCH')
    for e in entries:load_rounds(e)
    before=hashes();protected=snapshot();frozen={str(p):digest(p) for p in ROOT.rglob('*') if p.is_file() and '__pycache__' not in str(p)}
    output=ROOT.parent/'c2/runs'/('stage1-real-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True)
    save(output/'execution_manifest.json',dict(stage=1,entries=entries,provider=manifest['provider'],
        capacity_status='USER_ASSERTED_CAPACITY_128K',budget=manifest['stages'][0],
        approval='本轮用户明确批准真实模型 Stage 1；不包括 Stage 2',
        production_before=before,protected_before=protected,prep3_before=frozen,
        serialization_revision='OPTIONAL_ASSISTANT_NULL_METADATA_OMISSION_V1',
        eval_source_hashes={str(p):digest(p) for p in [Path(__file__),ROOT.parent/'c2_dispatch_guard_repair/entry.py',
            ROOT.parent/'c2_dispatch_guard_repair/guard.py']}))
    budget=TokenBudget(policy(manifest,1));results=[];error=None;client=None
    try:
        # 凭证仅用于本轮已授权的冻结 RightAPI 路由，不写入 Artifact。
        load_dotenv(Path(__file__).resolve().parents[3]/'.env',override=True)
        actual=os.environ.get('OPENAI_BASE_URL','').rstrip('/')
        if actual!=manifest['provider']['base_url'].rstrip('/'):raise RuntimeError('PROVIDER_BASE_URL_MISMATCH')
        client=make_client(manifest,execute=True)
        print(json.dumps({'output':str(output),'status':'START'},ensure_ascii=False),flush=True)
        for e in entries:
            print('START '+e['fixture_id'],flush=True)
            result=execute(e,output,budget,client=client,transport_mode='NETWORK')
            results.append(result);print(json.dumps(result,ensure_ascii=False),flush=True)
            if result['status']!='PASS' or budget.halted_reason:break
    except Exception as exc:
        error={'type':type(exc).__name__,'message':str(exc)}
        save(output/'execution_error.json',error)
    finally:
        if client is not None:client.close()
        traces=[]
        for e in entries:
            p=output/e['fixture_id']/'provider-trace.json'
            if p.exists():traces.append(dict(fixture_id=e['fixture_id'],**read_json(p)))
        save(output/'provider_trace.json',dict(fixtures=traces))
        save(output/'budget_ledger.json',dict(calls=budget.calls,input_reserved=budget.inputs,output_reserved=budget.outputs,
            reservations=budget.reservations,halted_reason=budget.halted_reason))
        final=dict(results=results,error=error,quality='QUALITY_REVIEW_PENDING',output=str(output),
            production_unchanged=before==hashes(),protected_unchanged=protected==snapshot(),
            prep3_unchanged=all(Path(p).exists() and digest(Path(p))==h for p,h in frozen.items()),
            real_compaction_calls=sum(c['call_type']=='COMPACTION' and c.get('actual_network_dispatch',False)
                for t in traces for c in t['calls']),stage2_executed=False)
        save(output/'execution_result.json',final);print(json.dumps(final,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
