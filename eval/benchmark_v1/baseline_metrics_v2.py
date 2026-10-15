"""B2 指标修正：按原生 span 关联类型，不改变冻结执行、预算或评分。"""
from hashlib import sha256
import json
from pathlib import Path
import shutil
from .baseline import report
from .baseline_support import metrics,raw_rows
from .environment import dump
from .artifacts import write_json
from .contracts import VerifierResult, AssertionResult, ArtifactReference


def corrected_metrics(root):
    base=metrics(root)
    usage=json.loads((root/'provider-usage.json').read_text()) if (root/'provider-usage.json').exists() else {'records':[]}
    observations=[r for p in root.glob('sessions/*/observation.jsonl') for r in raw_rows(p)]
    started={r['span_id']:r for r in observations if r['event']=='provider_started'}
    records=[r for r in usage['records'] if r['kind']=='main']
    unattributed=[]
    for row in observations:
        if row['event']!='provider_finished':continue
        kind=started.get(row['span_id'],{}).get('request_type',row.get('request_type'))
        if kind=='MAIN_AGENT':continue
        if kind is None:
            unattributed.append(row);continue
        records.append({'kind':kind,'usage':{'input_tokens':row.get('input_tokens'),'output_tokens':row.get('output_tokens')}})
    groups={}
    for record in records:
        g=groups.setdefault(record['kind'],{'requests':0,'input_tokens':0,'output_tokens':0,'cached_tokens_known':0,'reasoning_tokens_known':0,'missing_usage_records':0,'cached_missing_records':0,'reasoning_missing_records':0})
        g['requests']+=1;u=record.get('usage') or {}
        for f in ['input_tokens','output_tokens']:
            if type(u.get(f)) is int:g[f]+=u[f]
        if any(u.get(f) is None for f in ['input_tokens','output_tokens']):g['missing_usage_records']+=1
        for field,nested in [('cached_tokens','input_tokens_details'),('reasoning_tokens','output_tokens_details')]:
            value=(u.get(nested) or {}).get(field)
            if type(value) is int:g[field+'_known']+=value
            else:g[field.split('_')[0]+'_missing_records']+=1
    for g in groups.values():g['total_tokens']=g['input_tokens']+g['output_tokens']
    base.update(provider_groups=groups,known_provider_tokens=sum(g['total_tokens'] for g in groups.values()) if records else None,recorded_usage_complete=bool(records) and not unattributed and not any(g['missing_usage_records'] for g in groups.values()),metrics_version='2',provider_span_unattributed=unattributed)
    calls=json.loads((root/'tool-trace.json').read_text()) if (root/'tool-trace.json').exists() else []
    failed=set();reissued=0
    for call in calls:
        signature=json.dumps([call.get('session_id'),call['tool_name'],call.get('arguments')],sort_keys=True)
        if signature in failed: reissued+=1
        if call['status']=='error': failed.add(signature)
        elif call['status']=='success': failed.discard(signature)
    base['same_argument_reissues_after_error']=reissued
    base.pop('native_run_configurations',None)
    return base


def regenerate(campaign):
    records=raw_rows(campaign/'trial-records.jsonl');manifest=json.loads((campaign/'baseline-manifest.json').read_text())
    original=json.loads((campaign/'baseline-summary.json').read_text())
    for r in records:
        root=Path(r['artifact_root']);r['metrics']=corrected_metrics(root)
        if any(f.get('failure_reason')=='RateLimitError' for f in r['metrics']['provider_failures']):
            r.update(original_protocol_status=r['protocol_status'],original_task_status=r['task_status'],protocol_status='BLOCKED',task_status='INVALID',failure_category='environment',reason='Provider HTTP429 model_cooldown; approval scenario not executed; usage unknown',valid_task_sample=False)
            references=tuple(ArtifactReference(path=str(p.relative_to(root))) for p in sorted(root.glob('sessions/*/observation.jsonl')))
            verdict=VerifierResult(schema_version=1,suite_id=r['suite_id'],case_id=r['case_id'],trial_id=r['trial_id'],status='INVALID',failure_category='environment',verifier_version='1',assertions=(AssertionResult(assertion_id='provider_environment',category='protocol',status='FAIL',evidence=references,detail='原生Provider span明确RateLimitError，模型与审批场景未执行'),))
            write_json(root/'baseline-protocol-verifier-result.json',verdict)
    target=campaign/'metrics-v2';target.mkdir(exist_ok=True)
    dump(target/'revision.json',{'metrics_version':'2','reason':'provider_finished omits request_type; associate provider_started using span_id; remove duplicated MAIN usage','external_environment_classification':'RateLimitError is INVALID/BLOCKED; original business INCONCLUSIVE retained; no Outcome or Safety relaxation','executed_trials_unchanged':True,'verifier_unchanged':True,'budgets_unchanged':True,'original_summary_preserved':str(campaign/'baseline-summary.json'),'postprocessor_sha256':sha256(Path(__file__).read_bytes()).hexdigest(),'manifest_sha256':sha256((campaign/'baseline-manifest.json').read_bytes()).hexdigest()})
    shutil.copy2(Path(__file__),target/Path(__file__).name)
    (target/'trial-records.jsonl').write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in records)+'\n')
    report(target,records,manifest['inventory'],original['stage_status'])
    return records

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('campaign',type=Path);args=p.parse_args();regenerate(args.campaign.resolve())
