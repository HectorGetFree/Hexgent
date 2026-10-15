"""B2 固定清单串行执行，每 Case 三次；不重试任务失败或调参。"""
import argparse
from dataclasses import asdict
from datetime import datetime,timezone
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse
from dotenv import dotenv_values
from .artifacts import write_json
from .baseline_support import full_config,metrics,aggregate,raw_rows
from .business_cases import load_case
from .business_verifier import BusinessCaseVerifier
from .contracts import ArtifactReference,TaskContract,TrialManifest,VerifierResult,AssertionResult
from .environment import dump
from .remaining_cases import CASE_NAMES,load_remaining
from .remaining_verifier import RemainingVerifier
from .verifier import Observation,VerificationInput

ROOT=Path(__file__).resolve().parents[2]
MODULE=Path(__file__).parent
SMOKES={
 'T01':'b1c-t01-v1-r3','T02':'b1c-t02-v2','T03':'b1c-t03-v3',
 't04-eligible':'b1d-t04-eligible-v1-r2','t04-ineligible':'b1d-t04-ineligible-v1',
 't05-single':'b1d-t05-single-v1','t05-multiidentity':'b1d-t05-multiidentity-v1',
 't06-retryable':'b1d-t06-retryable-v1','t06-unknown':'b1d-t06-unknown-v1',
 't07-denied':'b1d-t07-denied-v1','t07-approved':'b1d-t07-approved-v1','t08-crash':'b1d-t08-crash-v1'}


def git(path,*args):return subprocess.check_output(['git','-C',str(path),*args],text=True).strip()
def digest(path):return sha256(path.read_bytes()).hexdigest()
def now():return datetime.now(timezone.utc).isoformat()


def verification(root,contract,manifest):
    valid=json.loads((root/'readiness.json').read_text()).get('status')=='PASS'
    inputs=VerificationInput(contract=contract,trial=manifest,artifact_root=root,observation_suite_id=contract.suite_id,observation_case_id=contract.case_id,observation_trial_id=manifest.trial_id,environment_valid=valid,environment_evidence=(ArtifactReference(path='readiness.json'),),business_state=Observation(values={},evidence={}),tool_audit=Observation(values={},evidence={}))
    return (BusinessCaseVerifier() if contract.task_id in {'T01','T02','T03'} else RemainingVerifier()).verify(inputs)


def inventory(revision=None):
    result=[]
    for name in ['T01','T02','T03',*CASE_NAMES]:
        selected=revision if name not in {'t05-multiidentity','t08-crash'} else None
        contract,baseline=(load_case(name,revision=selected) if name.startswith('T') else load_remaining(name,revision=selected))
        protocols=list((MODULE/'runs'/SMOKES[name]).rglob('protocol-result.json'))
        if selected=='f1':
            protocols=[p for p in (MODULE/'runs').rglob('protocol-result-v2.json') if
                json.loads(p.read_text()).get('identity',{}).get('case_id')==contract.case_id and
                (p.parent/'trial.json').exists() and json.loads((p.parent/'trial.json').read_text()).get('phase')=='protocol_smoke']
            if not protocols:raise ValueError('F1必须先有新版Protocol Smoke；不得回退到旧B2：'+name)
        if len(protocols)!=1:raise ValueError('Smoke Protocol 产物不唯一：'+name)
        source=protocols[0].parent;p=json.loads(protocols[0].read_text())
        if name not in {'t05-multiidentity','t08-crash'}:
            trial=TrialManifest.model_validate_json((source/'trial.json').read_text())
            checked=verification(source,contract,trial)
            if not trial.matches(contract) or checked.status!=p.get('verifier_status',p['task_status']) or p['protocol_status']!='PASS':raise ValueError('合同／Verifier／Smoke 不匹配：'+name)
        eligible=p['protocol_status']=='PASS' and name not in {'t05-multiidentity','t08-crash'}
        result.append({'name':name,'suite_id':contract.suite_id,'case_id':contract.case_id,'task_id':contract.task_id,'environment_kind':contract.environment_kind,'eligible':eligible,'smoke_protocol':p['protocol_status'],'smoke_task':p['task_status'],'smoke_artifact':str(source),'reason':p.get('reason'),'contract_sha256':contract.digest(),'baseline_sha256':sha256(json.dumps(baseline,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),'contract':contract.model_dump(mode='json'),'baseline':baseline,'budget':contract.execution_budget.model_dump(mode='json'),'full_config':asdict(full_config(contract.execution_budget.model_dump()))})
    return result


def source_hashes(workspace,metadata,provider,embedding):
    paths=[p for p in (ROOT/'hexgent').rglob('*.py')]
    paths += [p for p in MODULE.rglob('*') if p.is_file() and p.suffix in {'.py','.json','.md'} and 'runs' not in p.relative_to(MODULE).parts and '__pycache__' not in p.parts]
    paths += [p for p in workspace.rglob('*.java') if 'target' not in p.parts]
    paths += [workspace/'docs/devops/mysql/sql/001-init-hex-workspace.sql',metadata,provider,embedding]
    return {str(p.resolve()):digest(p) for p in paths}


def freeze(args,items):
    provider={k:v for k,v in dotenv_values(args.provider_env_file).items() if k in {'OPENAI_API_KEY','OPENAI_BASE_URL','MODEL_ID'}}
    if len(provider)!=3 or not all(provider.values()):raise ValueError('Provider 配置缺失')
    metadata=json.loads(args.build_metadata.read_text())
    if git(ROOT,'diff',metadata['runtime']['commit'],'--','hexgent'):raise ValueError('Runtime 镜像与当前 Production 源码不一致')
    from .fixture_agent import register_fixture
    from hexgent.tool.tool_registry import TOOL_REGISTRY
    from hexgent.prompt import TOOLS
    from hexgent.observation import source_fingerprint
    register_fixture('http://127.0.0.1:0','not-a-live-token')
    registry={name:{'function':tool.function.__module__+'.'+tool.function.__qualname__,'requires_context':tool.requires_context,'permission_capability':tool.permission_capability,'execution_domain':tool.execution_domain,'concurrency_safe':tool.concurrency_safe,'automatic_retry_safe':tool.automatic_retry_safe,'deduplication_policy':tool.deduplication_policy,'interrupt_behavior':tool.interrupt_behavior} for name,tool in TOOL_REGISTRY.items()}
    plan=[{'case_id':c['case_id'],'name':c['name'],'ordinal':i,'trial_id':'b2-'+c['case_id']+'-'+str(i)} for c in items if c['eligible'] for i in [1,2,3]]
    manifest={'schema_version':1,'protocol_version':2 if getattr(args,'revision',None)=='f1' else 1,'campaign_id':args.root.name,'frozen_at':now(),'execution':'serial; exactly three planned attempts per eligible Case; no automatic task retry',
        'inventory':items,'trial_plan':plan,'git':{'hexgent_head':git(ROOT,'rev-parse','HEAD'),'hexgent_branch':git(ROOT,'branch','--show-current'),'hexgent_status':git(ROOT,'status','--short','--untracked-files=all'),'workspace_head':git(args.workspace_source,'rev-parse','HEAD'),'workspace_branch':git(args.workspace_source,'branch','--show-current'),'workspace_status':git(args.workspace_source,'status','--short','--untracked-files=all')},
        'runtime_source_sha256':source_fingerprint(),'build_metadata':metadata,'source_hashes':source_hashes(args.workspace_source,args.build_metadata,args.provider_env_file,args.embedding_env_file),
        'provider':{'host':urlparse(provider['OPENAI_BASE_URL']).hostname,'route_sha256':sha256(provider['OPENAI_BASE_URL'].encode()).hexdigest(),'model':provider['MODEL_ID'],'temperature':'omitted / provider default','reasoning_effort':'omitted / provider default','sdk_max_retries':0 if getattr(args,'revision',None)=='f1' else 2,'max_output_tokens_per_round':4096,'finalization_output_tokens':2000,'server_model_identity':'not independently attested'},
        'harness':{'variant':'Full; no ablation','config_by_case':'inventory.full_config','max_model_rounds':40,'tool_parallel':False,'permission_mode':'default','compaction_threshold_tokens':64000,'snapshot':'production compaction Key Fact Snapshot enabled','memory':'production default enabled; T05 single identity','context_records':'production tracked default disabled; not an ablation','budgets':'unchanged frozen Case budgets'},
        'tool_registry':registry,'tool_schemas':TOOLS,'host':{'python':sys.version,'platform':platform.platform(),'timezone':'Asia/Shanghai','dependencies':{n:importlib.metadata.version(n) for n in ['openai','httpx','pydantic','python-dotenv']}},
        'protocol_adjustments':['phase baseline metadata added to existing entries','Fixture baseline config aligned to Workspace: rounds40/output4096/parallel false; Smoke defaults preserved','B1 Smoke outcomes not pooled with B2'],
        'usage_limitations':['failed or SDK retry usage unknown if unreported','Workspace embedding adapter usage unavailable'],'excluded_case_count':sum(not c['eligible'] for c in items)}
    path=args.root/'baseline-manifest.json'
    with path.open('x') as stream:json.dump(manifest,stream,ensure_ascii=False,sort_keys=True,indent=2)
    path.chmod(0o444)
    dump(args.root/'manifest-sha256.json',{'sha256':digest(path)})
    # 保存源码快照，使未提交Eval修改仍然可复现；凭证文件绝不复制。
    for p in MODULE.rglob('*'):
        if p.is_file() and p.suffix in {'.py','.json','.md'} and 'runs' not in p.relative_to(MODULE).parts and '__pycache__' not in p.parts:
            target=args.root/'source-snapshot'/p.relative_to(MODULE);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
    dump(args.root/'case-inventory.json',items)
    return manifest


def check_frozen(args,manifest):
    if digest(args.root/'baseline-manifest.json')!=json.loads((args.root/'manifest-sha256.json').read_text())['sha256']:raise ValueError('Manifest 被修改')
    for name,expected in manifest['source_hashes'].items():
        if digest(Path(name))!=expected:raise ValueError('冻结输入发生变化：'+name)
    if git(ROOT,'rev-parse','HEAD')!=manifest['git']['hexgent_head']:raise ValueError('Git HEAD 变化')


def command(args,case,trial):
    module='business_smoke' if case['name'].startswith('T') else 'memory_business_smoke' if case['name']=='t05-single' else 'fixture_smoke'
    cmd=[sys.executable,'-m','eval.benchmark_v1.'+module,'--phase','baseline','--case',case['name'],'--trial-id',trial['trial_id'],'--root',str(args.root/'trials'/trial['trial_id']),'--provider-env-file',str(args.provider_env_file)]
    if case.get('contract',{}).get('verifier_spec',{}).get('verifier_version')=='2':cmd+=['--revision','f1']
    if case['environment_kind']=='hexworkspace':
        cmd += ['--workspace-source',str(args.workspace_source),'--build-metadata',str(args.build_metadata),'--docker-socket',args.docker_socket,'--embedding-env-file',str(args.embedding_env_file)]
    return cmd


def collect(args,case,trial,returncode,elapsed):
    paths=list((args.root/'trials'/trial['trial_id']).rglob('protocol-result.json'))
    v2paths=list((args.root/'trials'/trial['trial_id']).rglob('protocol-result-v2.json'))
    if v2paths:
        paths=v2paths
        base=paths[0].parent
        from .evidence_v2 import verify
        manifest=TrialManifest.model_validate_json((base/'trial.json').read_text())
        integrity=verify(base,manifest);p=json.loads(paths[0].read_text())
        contract=TaskContract.model_validate_json((base/'contract.json').read_text())
        checked=verification(base,contract,manifest)
        from .protocol_v2 import classify
        safety=any(a.category=='safety' and a.status=='FAIL' for a in checked.assertions)
        expected=classify(termination=p['termination_reason'],task=checked.status,safety=safety,evidence=integrity['status'],
            environment=p.get('environment_valid',False),injector_valid=p.get('injector_valid',False),
            scenario_triggered=p.get('scenario_triggered',False))
        if p['verifier_status']!=checked.status or any(p[k]!=expected[k] for k in ['protocol_status','task_status','safety_violation']):
            raise ValueError('Protocol/Verifier与可信原始证据复核不一致')
        if manifest.case_id!=case['case_id'] or manifest.trial_id!=trial['trial_id'] or contract.digest()!=case['contract_sha256']:
            raise ValueError('Trial Record串Case/Revision')
        ledger=raw_rows(base/'trial-budget.jsonl');completed=[r for r in ledger if r['event']=='provider_finished']
        measured=metrics(base)
        if case.get('full_config') is not None:
            configs=measured['native_run_configurations']
            model=json.loads((args.root/'baseline-manifest.json').read_text())['provider']['model']
            if not configs or any(r['config']!=case['full_config'] or r['model']!=model for r in configs):
                raise ValueError('新版实际配置不符合冻结Baseline')
        measured.update(attempted_tool_calls=measured['tool_calls'],kernel_dispatch_candidates=measured['tool_dispatches'],
            tool_calls=integrity.get('actual_dispatches'),tool_dispatches=integrity.get('actual_dispatches'))
        groups={}
        for r in completed:
            g=groups.setdefault(r.get('request_type','UNKNOWN'),dict(requests=0,input_tokens=0,output_tokens=0,total_tokens=0,missing_usage_records=0))
            g['requests']+=1
            if r['usage_known']:
                g['input_tokens']+=r['input_tokens'];g['output_tokens']+=r['output_tokens'];g['total_tokens']+=r['tokens']
            else:g['missing_usage_records']+=1
        return {**trial,'task_id':case['task_id'],'suite_id':case['suite_id'],'protocol_status':p['protocol_status'],
            'task_status':p['task_status'],'termination_reason':p['termination_reason'],'safety_violation':p['safety_violation'],
            'valid_task_sample':p['valid_task_sample'] and integrity['status']=='PASS','artifact_root':str(base),
            'evidence_manifest_sha256':p['evidence_manifest_sha256'],'contract_sha256':manifest.task_contract_sha256,
            'total_wall_seconds':elapsed,'returncode':returncode,'reason':p['termination_reason'],
            'failure_category':'safety' if p['safety_violation'] else 'outcome' if p['task_status']=='FAIL' else None,
            'metrics':{**measured,'provider_groups':groups,'known_provider_tokens':integrity.get('known_tokens'),
                'usage_coverage':'COMPLETED_REQUESTS_ONLY; failed/unreported costs UNKNOWN','actual_tool_dispatches':integrity.get('actual_dispatches')}}
    base=paths[0].parent if len(paths)==1 else args.root/'trials'/trial['trial_id']
    protocol=json.loads(paths[0].read_text()) if len(paths)==1 else {'protocol_status':'FAIL','task_status':'INVALID','reason':'Protocol 产物缺失'}
    record={**trial,'task_id':case['task_id'],'suite_id':case['suite_id'],'protocol_status':protocol['protocol_status'],'task_status':protocol.get('task_status','INCONCLUSIVE'),'valid_task_sample':False,'returncode':returncode,'total_wall_seconds':elapsed,'artifact_root':str(base),'reason':protocol.get('reason'),'metrics':{}}
    if (base/'trial.json').exists():
        manifest=TrialManifest.model_validate_json((base/'trial.json').read_text());contract=TaskContract.model_validate_json((base/'contract.json').read_text())
        result=verification(base,contract,manifest);write_json(base/'baseline-independent-verifier-result.json',result)
        record['task_status']=result.status;record['failure_category']=result.failure_category
        record['assertions']=[a.model_dump(mode='json') for a in result.assertions]
        measure=metrics(base);record['metrics']=measure
        expected=case['full_config'];configs=measure.pop('native_run_configurations')
        same=bool(configs) and all(r['config']==expected and r['model']==json.loads((args.root/'baseline-manifest.json').read_text())['provider']['model'] and r['permission_mode']=='default' for r in configs)
        record['native_full_config_matches_manifest']=same
        record['valid_task_sample']=returncode==0 and protocol['protocol_status']=='PASS' and protocol.get('destroy')=='PASS' and result.status in {'PASS','FAIL'} and contract.digest()==case['contract_sha256'] and manifest.phase=='baseline' and same
        if not same:record.update(task_status='INVALID',reason='实际 Runtime 配置与冻结 Full 配置不一致')
        if any(f.get('failure_reason')=='RateLimitError' for f in measure['provider_failures']):
            record.update(original_protocol_status=record['protocol_status'],original_task_status=record['task_status'],protocol_status='BLOCKED',task_status='INVALID',failure_category='environment',reason='Provider RateLimitError; no automatic retry; usage unknown',valid_task_sample=False)
            measure['known_provider_tokens']=None
            refs=tuple(ArtifactReference(path=str(p.relative_to(base))) for p in base.glob('sessions/*/observation.jsonl'))
            invalid=VerifierResult(schema_version=1,suite_id=contract.suite_id,case_id=contract.case_id,trial_id=manifest.trial_id,status='INVALID',failure_category='environment',verifier_version='1',assertions=(AssertionResult(assertion_id='provider_environment',category='protocol',status='FAIL',evidence=refs,detail='Provider RateLimitError 阻止实验场景执行'),))
            write_json(base/'baseline-protocol-verifier-result.json',invalid)
        if not record['valid_task_sample'] and result.status in {'PASS','FAIL'}:record['exclusion_reason']='Task result exists but Protocol/configuration/cleanup validation failed'
    return record


def report(root,records,items,status):
    summary=aggregate(records,items)
    dump(root/'baseline-summary.json',{'stage_status':status,'generated_at':now(),'planned_trials':3*sum(c['eligible'] for c in items),'completed_attempts':len(records),'valid_trials':sum(r['valid_task_sample'] for r in records),'excluded_cases':sum(not c['eligible'] for c in items),'cases':summary,'trials':records,'statistical_significance':'not claimed; n=3 per Case'})
    lines=['# B2 Full Harness Baseline', '', '阶段状态：'+status+'。固定清单串行执行；没有消融、Oracle调整或任务失败重跑。', '', '| Case | 有效 | PASS/FAIL | INVALID/INCONCLUSIVE | 成功率 | 已知Token均值 | Agent秒均值 | Tool均值 |','|---|---:|---|---|---|---:|---:|---:|']
    for s in summary:
        def val(key):return str(round(s[key]['mean'],2)) if s[key] else '—'
        lines.append(f"| {s['case_id']} | {s['valid_trials']} | {s['pass']}/{s['fail']} | {s['invalid']}/{s['inconclusive']} | {s['pass_rate_numerator']}/{s['pass_rate_denominator']} | {val('known_provider_tokens')} | {val('agent_run_seconds_sum')} | {val('tool_calls')} |")
    lines += ['', '## 原始 Trial', '', '| Case / Trial | Protocol | Task | 已知Token | 总墙钟秒 | Agent秒 | Tools | Compaction / Snapshot / Memory写 / Recall请求 |','|---|---|---|---:|---:|---:|---:|---|']
    for r in records:
        m=r['metrics'];link=str(Path(r['artifact_root'])/'protocol-result.json')
        lines.append(f"| [{r['trial_id']}]({link}) | {r['protocol_status']} | {r['task_status']} | {m.get('known_provider_tokens','—')} | {r['total_wall_seconds']:.2f} | {m.get('agent_run_seconds_sum','—')} | {m.get('tool_calls','—')} | {m.get('compactions','—')} / {m.get('snapshot_events','—')} / {m.get('memory_writes','—')} / {m.get('memory_recall_requests','—')} |")
    lines += ['', '## 边界', '', '两个BLOCKED Case没有执行，不进入分母。INVALID、INCONCLUSIVE与Protocol/配置失效均排除并单列。Task FAIL保留为有效样本。', '', '总墙钟包含初始化、Readiness、运行、归档、销毁；Agent秒来自原生Run Report并跨Session求和，不等同模型推理时间。', '', 'Token来自实际Provider完成记录，MAIN与辅助调用明细在baseline-summary.json。未报告失败/SDK重试和Workspace Embedding成本未知，不称全部计费成本。Cached/Reasoning字段缺失不默认为零。', '', '仅三个样本，不宣称显著性，没有加权分数。本轮停止，不自动进入B3。']
    (root/'BASELINE_REPORT.md').write_text('\n'.join(lines)+'\n')


def main():
    p=argparse.ArgumentParser();p.add_argument('--revision',choices=['f1'],default=None);p.add_argument('--root',type=Path,required=True);p.add_argument('--workspace-source',type=Path,required=True);p.add_argument('--build-metadata',type=Path,required=True);p.add_argument('--docker-socket',required=True);p.add_argument('--provider-env-file',type=Path,required=True);p.add_argument('--embedding-env-file',type=Path,required=True);args=p.parse_args()
    for field in ['root','workspace_source','build_metadata','provider_env_file','embedding_env_file']:setattr(args,field,getattr(args,field).resolve())
    items=inventory(args.revision);args.root.mkdir(parents=True,exist_ok=False);manifest=freeze(args,items);records=[];status='PARTIAL'
    print(json.dumps({'event':'manifest_frozen','cases':sum(c['eligible'] for c in items),'trials':len(manifest['trial_plan']),'manifest':str(args.root/'baseline-manifest.json')},ensure_ascii=False),flush=True)
    try:
        for trial in manifest['trial_plan']:
            check_frozen(args,manifest)
            case=next(c for c in items if c['case_id']==trial['case_id'])
            dump(args.root/'progress.json',{'started_at':now(),'current_trial':trial,'completed_attempts':len(records)})
            print(json.dumps({'event':'trial_started',**trial},ensure_ascii=False),flush=True)
            start=time.monotonic()
            # 单个现有入口拥有生命周期和finally清理；父进程不杀环境管理器。
            child=subprocess.run(command(args,case,trial),cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            record=collect(args,case,trial,child.returncode,time.monotonic()-start)
            records.append(record)
            with (args.root/'trial-records.jsonl').open('a') as stream:stream.write(json.dumps(record,ensure_ascii=False)+'\n');stream.flush();os.fsync(stream.fileno())
            report(args.root,records,items,'PARTIAL')
            print(json.dumps({'event':'trial_finished','trial_id':trial['trial_id'],'protocol':record['protocol_status'],'task':record['task_status'],'valid':record['valid_task_sample'],'tokens':record['metrics'].get('known_provider_tokens'),'completed':len(records)},ensure_ascii=False),flush=True)
            if record['protocol_status']=='BLOCKED' and record['reason'] and record['task_status']=='INVALID':
                # 外部依赖/配额不可用时停止；不把剩余Case伪造成结果。
                dump(args.root/'stop-reason.json',{'trial_id':trial['trial_id'],'reason':record['reason']});break
            if record.get('termination_reason') in {'provider_429','provider_unavailable','usage_unavailable','fixture_failure'}:
                dump(args.root/'stop-reason.json',{'trial_id':trial['trial_id'],'reason':record['termination_reason']});break
        if len(records)==len(manifest['trial_plan']) and all(r['valid_task_sample'] for r in records):status='PASS'
    except Exception as exc:
        dump(args.root/'stop-reason.json',{'exception_type':type(exc).__name__,'reason':str(exc)})
    finally:
        report(args.root,records,items,status)
        dump(args.root/'progress.json',{'finished_at':now(),'stage_status':status,'completed_attempts':len(records)})
        print(json.dumps({'event':'baseline_finished','status':status,'completed':len(records),'root':str(args.root)},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
