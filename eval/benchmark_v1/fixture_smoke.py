"""一个固定 Fixture Case 的真实 Smoke；不执行 Baseline 批次。"""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlparse
from dotenv import dotenv_values
from .artifacts import write_json
from .business_fixture import BusinessFixture
from .business_smoke import audit, known_tokens, records
from .contracts import ArtifactReference, SessionArtifacts, TrialManifest
from .environment import dump
from .remaining_cases import load_remaining, CASE_NAMES
from .remaining_verifier import RemainingVerifier
from .verifier import Observation, VerificationInput


def archive_manifest(root,workdir,contract,trial_id,run_config):
    environment=SimpleNamespace(workdir=workdir)
    trace=audit(environment);sessions=[]
    for sid,_ in records(environment):
        source=workdir/'.hexgent/sessions'/sid;target=root/'sessions'/sid
        shutil.copytree(source,target,dirs_exist_ok=True)
        dump(target/'provider-usage.json',[u for u in trace['usage'] if u['session_id']==sid])
        sessions.append(SessionArtifacts(session_id=sid,transcript=ArtifactReference(path=f'sessions/{sid}/transcript.jsonl'),run_reports=tuple(ArtifactReference(path=str(p.relative_to(root))) for p in sorted((target/'reports').glob('run-*.json'))),provider_usage=(ArtifactReference(path=f'sessions/{sid}/provider-usage.json'),)))
    dump(root/'tool-trace.json',trace['calls'])
    dump(root/'provider-usage.json',{'records':trace['usage'],'known_tokens':known_tokens(trace['usage']),'harness_recorded_usage_complete':bool(trace['usage']) and all(u.get('usage') and u['usage'].get('input_tokens') is not None and u['usage'].get('output_tokens') is not None for u in trace['usage']),'unreported_failed_or_sdk_retry_usage':'UNKNOWN'})
    if not sessions:return None,trace
    production=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    manifest=TrialManifest(schema_version=1,suite_id=contract.suite_id,task_id=contract.task_id,case_id=contract.case_id,trial_id=trial_id,environment_kind=contract.environment_kind,phase=run_config.get('phase','protocol_smoke'),baseline_id=contract.baseline_id,verifier_version=contract.verifier_spec.verifier_version,task_contract_sha256=contract.digest(),contract=ArtifactReference(path='contract.json'),run_config=run_config,code_versions={'hexgent_commit':production,'fixture_adapter_sha256':run_config.get('eval_sources_at_start',{}).get('fixture_agent.py',sha256(Path(__file__).with_name('fixture_agent.py').read_bytes()).hexdigest()),'fixture_server_sha256':sha256(Path(__file__).with_name('business_fixture.py').read_bytes()).hexdigest(),'verifier_sha256':sha256(Path(__file__).with_name('remaining_verifier.py').read_bytes()).hexdigest(),'smoke_sha256':sha256(Path(__file__).read_bytes()).hexdigest()},sessions=tuple(sessions))
    if contract.verifier_spec.verifier_version=='2':
        manifest=manifest.model_copy(update={'code_versions':{**manifest.code_versions,
            'verifier_v2_sha256':sha256(Path(__file__).with_name('verifier_v2.py').read_bytes()).hexdigest()}})
    return manifest,trace


def verify_archive(root,contract,manifest,valid):
    inputs=VerificationInput(contract=contract,trial=manifest,artifact_root=root,observation_suite_id=contract.suite_id,observation_case_id=contract.case_id,observation_trial_id=manifest.trial_id,environment_valid=valid,environment_evidence=(ArtifactReference(path='readiness.json'),),business_state=Observation(values={},evidence={}),tool_audit=Observation(values={},evidence={}))
    result=RemainingVerifier().verify(inputs)
    write_json(root/'verifier-result.json',result)
    write_json(root/'trial.json',manifest.model_copy(update={'verifier_result':result}))
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--replacement-of',type=Path,default=None);p.add_argument('--revision',choices=['f1'],default=None);p.add_argument('--phase',choices=['protocol_smoke','baseline'],default='protocol_smoke');p.add_argument('--case',choices=[n for n in CASE_NAMES if not n.startswith('t05-')],required=True);p.add_argument('--trial-id',required=True);p.add_argument('--root',type=Path,required=True);p.add_argument('--provider-env-file',type=Path,required=True);args=p.parse_args()
    contract,baseline=load_remaining(args.case,revision=args.revision)
    if contract.environment_kind!='business_fixture':raise ValueError('此入口仅允许固定 Fixture Case')
    # IDs 与根目录先验证，绝不覆盖已有 Trial。
    from .contracts import Identifier
    from pydantic import TypeAdapter
    TypeAdapter(Identifier).validate_python(args.trial_id)
    root=args.root.resolve();root.mkdir(parents=True,exist_ok=False)
    if args.replacement_of:
        if contract.verifier_spec.verifier_version!='2':raise ValueError('仅协议v2可登记补测')
        from .protocol_v2 import reserve_replacement
        reserve_replacement(root,args.replacement_of,contract,args.trial_id)
    workdir=root/'private/workdir';workdir.mkdir(parents=True);(root/'private').chmod(0o700)
    fixture=None;child=None;started=time.monotonic();deadline=started+contract.execution_budget.max_wall_time_seconds
    protocol={'protocol_status':'BLOCKED','task_status':'INVALID','scenario_triggered':False,'trial_id':args.trial_id,'case_id':contract.case_id}
    write_json(root/'contract.json',contract);dump(root/'baseline-frozen.json',baseline)
    provider={k:v for k,v in dotenv_values(args.provider_env_file).items() if k in {'OPENAI_API_KEY','OPENAI_BASE_URL','MODEL_ID'}}
    if not all(provider.values()) or len(provider)!=3:raise ValueError('Provider 配置缺失')
    run_config={'phase':args.phase,'model':provider['MODEL_ID'],'provider_host':urlparse(provider['OPENAI_BASE_URL']).hostname,'permission_mode':'default','tool_retry':'production-default-disabled','tool_parallel':'production-default-disabled','entry':'AgentEngine.run; eval HTTP adapter; Production loop unchanged','budget_scope':'Trial wall + known completed Provider usage; unreported requests unknown'}
    run_config['eval_sources_at_start']={name:sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in ['fixture_smoke.py','fixture_agent.py','business_fixture.py','remaining_verifier.py']}
    dump(root/'run-config.json',run_config)
    env={**os.environ,**provider,'NO_PROXY':'localhost,127.0.0.1','PYTHONUNBUFFERED':'1'}
    try:
        fixture=BusinessFixture(root/'private/fixture',baseline,contract.scenario_config['fault'],
            evidence_revision=contract.scenario_config.get('evidence_revision',1))
        initial=fixture.observe();dump(root/'observations/initial.json',initial)
        cfg={'phase':args.phase,'trial_id':args.trial_id,'endpoint':fixture.endpoint,'token':fixture.token,'workdir':str(workdir),'observation_root':str(root),'user_query':contract.user_query,'allowed_tools':list(contract.allowed_tools),'budget':contract.execution_budget.model_dump(),'approve':contract.scenario_config['approval'],'approval_scope':contract.scenario_config['approval_scope']}
        if contract.verifier_spec.verifier_version=='2':
            control=dict(protocol_version=2,identity=dict(suite_id=contract.suite_id,case_id=contract.case_id,
                trial_id=args.trial_id,contract_sha256=contract.digest()),started_at=time.time()-(time.monotonic()-started),
                budget=contract.execution_budget.model_dump(),ledger_path=str(root/'trial-budget.jsonl'))
            cfg.update(protocol_version=2,trial_control=control);run_config['protocol_version']=2
            dump(workdir/'.benchmark-control.json',control)
        config_path=root/'private/agent-config.json';dump(config_path,cfg);config_path.chmod(0o600)
        dump(root/'readiness.json',{'status':'PASS','storage':'SQLite synchronous=FULL','endpoint_binding':'127.0.0.1','initial_read_observed':True,'separate_agent_process':True})
        def launch(restore=False):
            return subprocess.Popen([sys.executable,'-m','eval.benchmark_v1.fixture_agent',str(config_path)]+(['--restore'] if restore else []),cwd=Path(__file__).resolve().parents[2],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        child=launch();crash={};checkpoint={}
        while child.poll() is None and time.monotonic()<deadline:
            if fixture.cut.is_set() and not crash:
                # 正在等待 HTTP 回执的进程独占此 Trial；SIGKILL 不执行 Python finally。
                snapshot=fixture.observe()
                child.kill();child.wait(10)
                sid=json.loads((root/'agent-session.json').read_text())['session_id']
                cps=sorted((workdir/'.hexgent/sessions'/sid/'checkpoints').glob('*.json'))
                if cps:
                    cp=json.loads(cps[-1].read_text());digest=cp.pop('checksum')
                    from hexgent.session.checkpoint import _checksum
                    checkpoint={'session_id':cp['session_id'],'checkpoint_id':cp['checkpoint_id'],'checksum_valid':_checksum(cp)==digest,'source':str(cps[-1].relative_to(workdir))}
                crash={'signal':'SIGKILL','returncode':child.returncode,'pid':child.pid,'session_id':sid,'boundary':'sqlite_transaction_committed_before_http_response','committed_count_at_cut':len(snapshot['operations'])}
                dump(root/'crash-boundary.json',crash);dump(root/'observations/cut-state.json',snapshot)
                fixture.release.set();child=launch(True)
            if known_tokens(audit(SimpleNamespace(workdir=workdir))['usage'])>contract.execution_budget.max_provider_tokens:
                raise TimeoutError('已知 Provider Token 超出预算')
            time.sleep(.2)
        if child.poll() is None:raise TimeoutError('Trial 墙钟预算到期')
        protocol['agent_exit_code']=child.returncode
        if child.returncode != 0: protocol.update(protocol_status='FAIL',reason='Agent subprocess exited before valid completion')
        current=fixture.observe()
        manifest,trace=archive_manifest(root,workdir,contract,args.trial_id,run_config)
        rows=[r for _,rs in records(SimpleNamespace(workdir=workdir)) for r in rs]
        approvals=[json.loads(l) for l in (root/'approval-decisions.jsonl').read_text().splitlines()] if (root/'approval-decisions.jsonl').exists() else []
        restore=json.loads((root/'restore.json').read_text()) if (root/'restore.json').exists() else {}
        continuation=json.loads((root/'continue-result.json').read_text()) if (root/'continue-result.json').exists() else {}
        e={'suite_id':contract.suite_id,'case_id':contract.case_id,'trial_id':args.trial_id,'contract_sha256':contract.digest(),'initial':initial,'current':current,'calls':trace['calls'],'approvals':approvals,'permission_events':[r['payload'] for r in rows if r['type']=='permission_resolved'],'session_id':manifest.sessions[0].session_id if manifest else None,'crash':crash,'checkpoint':checkpoint,'restore':restore,'continue_result':continuation,'restore_event':any(r['type']=='session_recovered' for r in rows)}
        if contract.verifier_spec.verifier_version=='2':e['evidence_revision']=2
        dump(root/'observations/remaining-evidence.json',e)
        if manifest:
            result=verify_archive(root,contract,manifest,True)
            scenario=next((a.status=='PASS' for a in result.assertions if a.assertion_id=='scenario'),False)
            protocol.update(task_status=result.status,scenario_triggered=scenario,protocol_status='PASS' if child.returncode==0 and scenario and trace['usage'] and result.status in {'PASS','FAIL'} else 'BLOCKED' if contract.task_id=='T08' and not restore.get('has_continuable_run') else 'FAIL',known_provider_tokens=known_tokens(trace['usage']),reason='Production Restore 无可继续的安全 Checkpoint；不使用新 Query 冒充 Continue' if contract.task_id=='T08' and not restore.get('has_continuable_run') else None)
    except Exception as exc:
        protocol.update(protocol_status='BLOCKED' if isinstance(exc,TimeoutError) else 'FAIL',reason=type(exc).__name__)
    finally:
        if contract.verifier_spec.verifier_version=='2':
            from .trial_finalize import fixture_cleanup,complete
            def final_archive():
                nonlocal manifest
                manifest,trace=archive_manifest(root,workdir,contract,args.trial_id,{**run_config,'protocol_version':2})
                if fixture:
                    rows=[r for _,rs in records(SimpleNamespace(workdir=workdir)) for r in rs]
                    current=json.loads((root/'observations/final-authoritative-state.json').read_text())
                    approvals=[json.loads(l) for l in (root/'approval-decisions.jsonl').read_text().splitlines()] if (root/'approval-decisions.jsonl').exists() else []
                    dump(root/'observations/remaining-evidence.json',dict(suite_id=contract.suite_id,case_id=contract.case_id,
                        trial_id=args.trial_id,contract_sha256=contract.digest(),initial=json.loads((root/'observations/initial.json').read_text()),
                        current=current,calls=trace['calls'],approvals=approvals,evidence_revision=2,
                        permission_events=[r['payload'] for r in rows if r['type']=='permission_resolved']))
                    if contract.task_id=='T06':
                        relevant=[r for r in current['events'] if r['event']=='write_attempt' and
                            {k:r['body'].get(k) for k in contract.scenario_config['approval_scope']}==contract.scenario_config['approval_scope']]
                        kind=contract.scenario_config['fault'];side='unknown' if kind=='unknown' else 'not_started'
                        protocol['injector_valid']=not relevant or any(r['event']=='fault' and
                            r['body'].get('kind')==kind and r['body'].get('side_effect_state')==side and
                            (kind!='unknown' or r['body'].get('boundary')=='sqlite_transaction_committed_before_http_response')
                            for r in current['events'])
            manifest=None
            was_active=child is not None and child.poll() is None
            if was_active:protocol['termination_reason']='trial_wall_time'
            elif child is not None and child.returncode!=0:protocol['termination_reason']='agent_failed'
            errors=fixture_cleanup(root,fixture,child,final_archive)
            stopped=child is None or child.poll() is not None
            if was_active and stopped:dump(root/'supervisor-stop.json',dict(trial_id=args.trial_id,process_stopped=True,
                termination_reason=protocol['termination_reason'],source='Runner child.kill/wait'))
            dump(root/'lifecycle.json',dict(destroy='PASS' if not any(k in errors for k in ['agent_stop','fixture_close','private_remove']) else 'FAIL',
                archive='archive' not in errors,errors=errors,agent_process_stopped=stopped))
            complete(root,manifest,protocol,errors)
            return
        if child and child.poll() is None:child.kill();child.wait(10)
        if fixture:
            dump(root/'observations/final-authoritative-state.json',fixture.observe())
            fixture.close()
        # 只清理本 Trial 私有文件；原生 Transcript、Report、Checkpoint 已复制。
        if not (root/'trial.json').exists():
            manifest,_=archive_manifest(root,workdir,contract,args.trial_id,run_config)
            if manifest:
                result=verify_archive(root,contract,manifest,False);protocol['task_status']=result.status
        shutil.rmtree(root/'private')
        protocol.update(destroy='PASS',wall_time_seconds=time.monotonic()-started,wall_time_scope='includes initialization, Agent, archive, destroy')
        dump(root/'lifecycle.json',{'archive':(root/'sessions').exists(),'destroy':'PASS','private_files_removed':True,'fixture_server_closed':True,'agent_process_stopped':child is None or child.poll() is not None})
        dump(root/'protocol-result.json',protocol);print(json.dumps({**protocol,'artifact_root':str(root)},ensure_ascii=False))

if __name__=='__main__':main()
