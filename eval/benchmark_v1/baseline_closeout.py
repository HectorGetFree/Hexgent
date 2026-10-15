"""B2 仅T07 denied补测；旧产物只读，最多三个新独立Trial。"""
import argparse
import ast
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from dotenv import dotenv_values
from .baseline import collect,ROOT,now
from .baseline_metrics_v2 import corrected_metrics
from .baseline_support import full_config,aggregate,raw_rows
from .contracts import TaskContract
from .environment import dump
from .remaining_cases import load_remaining


def file_hashes(root):
    return {str(p.relative_to(root)):sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


def config_ast(source):
    node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='full_config')
    return ast.dump(node,include_attributes=False)


def comparable(original):
    m=json.loads((original/'baseline-manifest.json').read_text())
    case=next(c for c in m['inventory'] if c['name']=='t07-denied')
    contract,baseline=load_remaining('t07-denied')
    if contract.digest()!=case['contract_sha256']:raise ValueError('T07合同变化，不可合并')
    if sha256(json.dumps(baseline,sort_keys=True,ensure_ascii=False).encode()).hexdigest()!=case['baseline_sha256']:raise ValueError('Seed变化')
    if asdict(full_config(contract.execution_budget.model_dump()))!=case['full_config']:raise ValueError('Full配置变化')
    support=Path(__file__).with_name('baseline_support.py')
    if config_ast(support.read_text())!=config_ast((original/'source-snapshot/baseline_support.py').read_text()):raise ValueError('Full配置函数变化')
    permitted={'baseline.py','baseline_support.py','test_baseline.py'};changes=[]
    for name,digest in m['source_hashes'].items():
        if sha256(Path(name).read_bytes()).hexdigest()!=digest:
            if Path(name).parent!=Path(__file__).parent or Path(name).name not in permitted:raise ValueError('冻结执行输入变化：'+name)
            changes.append(name)
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    if head!=m['git']['hexgent_head']:raise ValueError('Git HEAD变化')
    return m,case,changes


def main():
    p=argparse.ArgumentParser();p.add_argument('--original',type=Path,required=True);p.add_argument('--root',type=Path,required=True);p.add_argument('--provider-env-file',type=Path,required=True);args=p.parse_args()
    args.original=args.original.resolve();args.root=args.root.resolve();args.provider_env_file=args.provider_env_file.resolve()
    old_hashes=file_hashes(args.original);m,case,changes=comparable(args.original)
    provider=dotenv_values(args.provider_env_file)
    if provider.get('MODEL_ID')!=m['provider']['model'] or sha256(str(provider.get('OPENAI_BASE_URL')).encode()).hexdigest()!=m['provider']['route_sha256']:raise ValueError('模型或Provider路由变化')
    args.root.mkdir(parents=True,exist_ok=False)
    shutil.copy2(args.original/'baseline-manifest.json',args.root/'baseline-manifest.json')
    manifest={'schema_version':1,'frozen_at':now(),'original':str(args.original),'original_manifest_sha256':sha256((args.original/'baseline-manifest.json').read_bytes()).hexdigest(),'case_id':case['case_id'],'contract_sha256':case['contract_sha256'],'budget':case['budget'],'full_config':case['full_config'],'permitted_eval_differences':changes,'comparability':'Production, fixture client/server, oracle/verifiers, full_config AST and effective config unchanged; evaluator statistics/stop fixes only','attempts_max':3,'stop_on_rate_limit':True}
    path=args.root/'supplement-manifest.json';dump(path,manifest);path.chmod(0o444)
    dump(args.root/'original-artifact-hashes.json',old_hashes)
    records=[]
    try:
        for ordinal in [1,2,3]:
            comparable(args.original)
            trial={'case_id':case['case_id'],'name':'t07-denied','ordinal':ordinal,'trial_id':'b2-supp-t07-denied-v1-'+str(ordinal)}
            print(json.dumps({'event':'supplement_started',**trial}),flush=True)
            start=time.monotonic()
            child=subprocess.run([sys.executable,'-m','eval.benchmark_v1.fixture_smoke','--phase','baseline','--case','t07-denied','--trial-id',trial['trial_id'],'--root',str(args.root/'trials'/trial['trial_id']),'--provider-env-file',str(args.provider_env_file)],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            record=collect(args,case,trial,child.returncode,time.monotonic()-start)
            if (Path(record['artifact_root'])/'trial.json').exists():record['metrics']=corrected_metrics(Path(record['artifact_root']))
            records.append(record)
            with (args.root/'supplement-trial-records.jsonl').open('a') as stream:stream.write(json.dumps(record,ensure_ascii=False)+'\n')
            print(json.dumps({'event':'supplement_finished','trial_id':trial['trial_id'],'protocol':record['protocol_status'],'task':record['task_status'],'valid':record['valid_task_sample'],'tokens':record['metrics'].get('known_provider_tokens')}),flush=True)
            if any(f.get('failure_reason')=='RateLimitError' for f in record['metrics'].get('provider_failures',[])):
                dump(args.root/'stop-reason.json',{'reason':'Provider RateLimitError; no further attempts','trial_id':trial['trial_id']});break
            if record['protocol_status']=='BLOCKED':break
    finally:
        current=file_hashes(args.original)
        unchanged=current==old_hashes
        dump(args.root/'original-preservation-audit.json',{'status':'PASS' if unchanged else 'FAIL','files':len(old_hashes),'changed':[n for n,d in old_hashes.items() if current.get(n)!=d]})
        if not unchanged:raise ValueError('原始Artifact发生变化，不可合并')
        combined=raw_rows(args.original/'metrics-v2/trial-records.jsonl')+records
        status='PASS' if len(records)==3 and all(r['valid_task_sample'] for r in records) else 'PARTIAL'
        target=args.root/'merged-v3';target.mkdir()
        (target/'trial-records.jsonl').write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in combined)+'\n')
        dump(target/'baseline-summary.json',{'stage_status':status,'original_attempts':30,'supplement_attempts':len(records),'total_attempts':len(combined),'valid_trials':sum(r['valid_task_sample'] for r in combined),'cases':aggregate(combined,m['inventory']),'trials':combined,'original_manifest_sha256':manifest['original_manifest_sha256'],'original_artifacts_unchanged':True})
        print(json.dumps({'event':'supplement_done','status':status,'attempts':len(records),'root':str(args.root)}),flush=True)

if __name__=='__main__':main()
