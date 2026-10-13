"""单 Case 真实 Protocol Smoke；不做批量 Baseline 或 Ablation 调度。"""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from urllib.parse import urlparse

from dotenv import dotenv_values
import httpx

from .artifacts import write_json
from .business_cases import load_case, BUSINESS_TOOLS
from .business_environment import BusinessCaseEnvironment
from .business_verifier import BusinessCaseVerifier
from .contracts import ArtifactReference, AssertionResult, SessionArtifacts, TrialManifest, VerifierResult
from .environment import EnvironmentError, LocalDocker, dump
from .verifier import Observation, VerificationInput


def records(environment):
    result=[]
    for path in (environment.workdir/'.hexgent/sessions').glob('*/transcript.jsonl'):
        # 并发追加期间忽略尚未完整写入的最后一行；终态读取必须全部完整。
        rows=[]
        for line in path.read_text().splitlines():
            try: rows.append(json.loads(line))
            except ValueError: continue
        result.append((path.parent.name,rows))
    return result


def audit(environment):
    calls=[]; usage=[]; compactions=[]; offloaded=[]; consumption=[]; errors=[]
    for sid,rows in records(environment):
        started={}; run_id=None
        for row in rows:
            payload=row['payload']; kind=row['type']
            if kind=='agent_run_started': run_id=payload.get('workspace_run_id')
            if kind=='tool_execution_started': started[payload['call_id']]=payload
            if kind=='tool_execution_finished':
                source=started.get(payload['call_id'],{})
                calls.append({'session_id':sid,'run_id':run_id,'call_id':payload['call_id'],
                    'tool_name':payload['tool_name'],'arguments':json.loads(source.get('arguments','{}')),
                    'status':payload['status'],'data':payload.get('data'), 'failure':payload.get('failure')})
                if payload['tool_name']=='read_file':
                    consumption.append({'call_id':payload['call_id'],'status':payload['status'],
                                        'arguments':json.loads(source.get('arguments','{}'))})
            if kind=='model_response_completed':
                usage.append({'session_id':sid,'sequence':row['sequence'],'kind':'main',
                              'usage':payload.get('usage')})
            if kind=='context_compaction_applied':
                compactions.append({'session_id':sid,'sequence':row['sequence'],'payload':payload})
                for component,ledger in payload.get('cost_ledger',{}).items():
                    if isinstance(ledger,list):
                        for entry in ledger:
                            usage.append({'session_id':sid,'sequence':row['sequence'],
                                          'kind':component,'usage':entry})
            if kind=='context_record_extraction_completed':
                usage.append({'session_id':sid,'sequence':row['sequence'],'kind':'context_record',
                              'usage':payload.get('usage')})
            if kind=='tool_result_persisted': offloaded.append({'session_id':sid,**payload})
            if kind in {'model_response_failed','context_record_extraction_failed','context_compaction_failed'}:
                errors.append({'session_id':sid,'sequence':row['sequence'],'type':kind,
                               'code':payload.get('code'),'reason':payload.get('reason')})
    return {'calls':calls,'usage':usage,'compactions':compactions,'offloaded':offloaded,
            'consumption':consumption,'errors':errors}


def known_tokens(usage):
    return sum((entry.get('usage') or {}).get('input_tokens',0) or 0 for entry in usage) + sum(
        (entry.get('usage') or {}).get('output_tokens',0) or 0 for entry in usage)


def wait_stage(manager, environment, run_id, deadline, decisions):
    while time.monotonic()<deadline:
        for sid,rows in records(environment):
            finished=[r['payload'] for r in rows if r['type']=='agent_run_finished'
                      and r['payload'].get('workspace_run_id')==run_id]
            if finished:
                # Report 与 Web Worker 的 finally 可能稍后落盘。
                time.sleep(.3)
                return {'session_id':sid,'run_id':run_id,'runtime_status':finished[-1]['status'],
                        'answer':finished[-1].get('output_text','')}
            for row in rows:
                if row['type']!='permission_requested': continue
                payload=row['payload']; request_id=payload['request_id']
                if request_id in decisions: continue
                decision='allow' if payload['tool_name'] in manager.contract.allowed_tools else 'deny'
                response=httpx.post(environment.endpoints['runtime']+
                    f'/internal/v1/runtime/runs/{run_id}/permissions/{request_id}',
                    headers={'Authorization':'Bearer '+manager._credentials(environment)['INTEGRATION_TOKEN']},
                    json={'responseId':'smoke-'+request_id,'decision':decision},timeout=10,trust_env=False)
                if response.status_code==200 and response.json().get('accepted'):
                    decisions[request_id]=decision
        usage=audit(environment)['usage']
        if known_tokens(usage)>manager.contract.execution_budget.max_provider_tokens:
            raise EnvironmentError('已知 Provider Token 超出冻结预算')
        time.sleep(.5)
    raise EnvironmentError('Trial 墙钟预算到期')


def settle(manager,environment,deadline):
    while True:
        observation=manager.observe(environment)
        docs=observation['documents']
        valid=bool(docs) and all(d['status']==2 and d['version_id'] and d['source_object_valid']
            and d['parse_ready'] and d['parse_object_valid'] and d['index_ready'] and d['chunk_object_valid']
            and d['qdrant_point_count']==d['chunk_count'] for d in docs) and not any(observation['pending_tasks'].values())
        if valid or time.monotonic()>=deadline: return observation,valid
        time.sleep(1)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--case',choices=['T01','T02','T03'],required=True)
    parser.add_argument('--trial-id',required=True)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--workspace-source',type=Path,required=True)
    parser.add_argument('--build-metadata',type=Path,required=True)
    parser.add_argument('--docker-socket',required=True)
    parser.add_argument('--provider-env-file',type=Path,required=True)
    parser.add_argument('--embedding-env-file',type=Path,required=True)
    args=parser.parse_args()
    contract,baseline=load_case(args.case)
    provider={k:v for k,v in dotenv_values(args.provider_env_file).items()
              if k in {'OPENAI_API_KEY','OPENAI_BASE_URL','MODEL_ID'}}
    for key in ['HTTP_PROXY','HTTPS_PROXY']:
        if os.environ.get(key): provider[key]=os.environ[key]
    metadata=json.loads(args.build_metadata.read_text())
    manager=BusinessCaseEnvironment(args.root,args.workspace_source,metadata['workspace']['image_id'],
        metadata['runtime']['image_id'],LocalDocker(args.docker_socket),contract=contract,baseline=baseline,
        provider=provider,embedding_key=dotenv_values(args.embedding_env_file).get('DASHSCOPE_API_KEY',''))
    environment=None; started=time.monotonic(); decisions={}; stages=[]
    protocol={'schema_version':1,'suite_id':contract.suite_id,'case_id':contract.case_id,
              'trial_id':args.trial_id,'protocol_status':'BLOCKED','task_status':'INVALID','reason':None}
    deadline=started+contract.execution_budget.max_wall_time_seconds
    try:
        environment=manager.provision(args.trial_id,contract.baseline_id)
        write_json(environment.artifact_root/'contract.json',contract)
        dump(environment.artifact_root/'baseline-frozen.json',baseline)
        dump(environment.artifact_root/'build-metadata.json',metadata)
        # 仅模型名和路由主机进入公开配置，凭证与完整 Provider URL 不进入。
        run_config={'variant':'full-protocol-smoke','model':provider['MODEL_ID'],
            'provider_host':urlparse(provider['OPENAI_BASE_URL']).hostname,'tool_parallel':False,
            'provider_egress_proxy_enabled':bool(provider.get('HTTPS_PROXY')),
            'context_record_extraction':False,'context_record_retrieval':False,
            'compaction_threshold_tokens':64000,'memory':'production-default',
            'budget_scope':'Trial across stages; known Harness usage monitored; embedding usage unavailable'}
        dump(environment.artifact_root/'run-config.json',run_config)
        base=manager.readiness(environment,require_seed=False,require_vector=False,timeout_seconds=120)
        if base['status']!='PASS': raise EnvironmentError('基础环境未就绪')
        manager.seed(environment,contract.baseline_id)
        ready=manager.readiness(environment,timeout_seconds=180)
        if ready['status']!='PASS': raise EnvironmentError('Seed 版本／解析／索引未就绪')
        initial=manager.observe(environment)
        seed=json.loads((environment.artifact_root/'seed.json').read_text())
        conversation=manager._api(environment,'POST',f"/api/v1/workspaces/{seed['workspace_id']}/conversations",
                                   {'title':contract.case_id})['conversationId']
        for query in [contract.user_query]+[s['user_query'] for s in contract.scenario_config['stages']]:
            run=manager.start_stage(environment,conversation,query)
            stage=wait_stage(manager,environment,run['runId'],deadline,decisions)
            stage['observation']=manager.observe(environment)
            stages.append(stage)
            dump(environment.artifact_root/'stage-results.json',stages)
            if stage['runtime_status']=='failed':
                raise EnvironmentError('Production Agent 失败；保留 Provider/Runtime 原始证据，不自动重试')
        current,settled=settle(manager,environment,min(deadline,time.monotonic()+120))
        independent=manager.observe_versions(environment)
        trace=audit(environment)
        payload={'schema_version':1,'suite_id':contract.suite_id,'case_id':contract.case_id,
            'trial_id':args.trial_id,'contract_sha256':contract.digest(),'initial':initial,'current':current,
            'stages':stages,'versions':independent['versions'],'mutations':independent['mutations'],
            'calls':trace['calls'],'settled':settled}
        dump(environment.artifact_root/'observations/business-evidence.json',payload)
        dump(environment.artifact_root/'tool-trace.json',trace['calls'])
        dump(environment.artifact_root/'execution-observation.json',{
            'compaction_count':len(trace['compactions']),
            'snapshot_generated':any(c['payload'].get('key_fact_snapshot') for c in trace['compactions']),
            'summary_generated':any(c['payload'].get('summary') for c in trace['compactions']),
            'compaction_gain_sample_eligible':False,
            'reason':'B1-C Protocol Smoke is not a matched ablation sample',
            'compactions':trace['compactions'],'offloads':trace['offloaded'],'artifact_reads':trace['consumption'],
            'provider_errors':trace['errors'], 'known_provider_tokens':known_tokens(trace['usage']),
            'early_constraints_used_in_final_result':'Determined by business facts assertion; no inference from summary alone'})
        # 保留逐请求来源，不用 Run Report 的聚合值冒充所有 Provider 成本。
        usage_complete=bool(trace['usage']) and all(e['usage'] and e['usage'].get('input_tokens') is not None and
                           e['usage'].get('output_tokens') is not None for e in trace['usage'])
        dump(environment.artifact_root/'provider-usage.json',{'records':trace['usage'],
            'known_tokens':known_tokens(trace['usage']),'harness_recorded_usage_complete':usage_complete,
            'embedding_usage':'NOT_AVAILABLE_FROM_PRODUCTION_ADAPTER','failed_request_usage':'UNKNOWN_IF_UNREPORTED'})
        manager.archive(environment)
        sessions=[]
        for sid,rows in records(environment):
            folder=environment.artifact_root/'sessions'/sid
            reports=tuple(ArtifactReference(path=str(p.relative_to(environment.artifact_root)))
                          for p in sorted((folder/'reports').glob('run-*.json')))
            usage_path=folder/'provider-usage.json'
            dump(usage_path,[entry for entry in trace['usage'] if entry['session_id']==sid])
            sessions.append(SessionArtifacts(session_id=sid,
                transcript=ArtifactReference(path=f'sessions/{sid}/transcript.jsonl'),run_reports=reports,
                provider_usage=(ArtifactReference(path=f'sessions/{sid}/provider-usage.json'),)))
        manifest=TrialManifest(schema_version=1,suite_id=contract.suite_id,task_id=contract.task_id,
            case_id=contract.case_id,trial_id=args.trial_id,environment_kind='hexworkspace',phase='protocol_smoke',
            baseline_id=contract.baseline_id,verifier_version='1',task_contract_sha256=contract.digest(),
            contract=ArtifactReference(path='contract.json'),run_config=run_config,
            code_versions={'runtime':metadata['runtime']['commit'],'workspace':metadata['workspace']['commit'],
                'business_cases_sha256':sha256((Path(__file__).with_name('cases')/args.case.lower()/'contract.json').read_bytes()).hexdigest(),
                'smoke_code_sha256':sha256(Path(__file__).read_bytes()).hexdigest()},sessions=tuple(sessions))
        result=BusinessCaseVerifier().verify(VerificationInput(contract=contract,trial=manifest,
            artifact_root=environment.artifact_root,observation_suite_id=contract.suite_id,
            observation_case_id=contract.case_id,observation_trial_id=args.trial_id,
            environment_valid=settled,environment_evidence=(ArtifactReference(path='readiness.json'),),
            business_state=Observation(values={},evidence={}),tool_audit=Observation(values={},evidence={})))
        write_json(environment.artifact_root/'verifier-result.json',result)
        write_json(environment.artifact_root/'trial.json',manifest.model_copy(update={'verifier_result':result}))
        business_calls=[c for c in trace['calls'] if c['tool_name'] in BUSINESS_TOOLS]
        protocol.update(task_status=result.status,protocol_status='PASS' if (business_calls and settled and
            trace['usage'] and sessions and result.status in {'PASS','FAIL'} and
            len(stages)==1+len(contract.scenario_config['stages'])) else 'FAIL',
            business_tools={tool:sum(c['tool_name']==tool for c in business_calls) for tool in sorted(BUSINESS_TOOLS)},
            known_provider_tokens=known_tokens(trace['usage']),stages=len(stages),sessions=len(sessions))
    except EnvironmentError as exc:
        protocol.update(protocol_status='BLOCKED',reason=str(exc))
    except Exception as exc:
        protocol.update(protocol_status='FAIL',reason=type(exc).__name__)
    finally:
        if environment is None: environment=manager.environments.get(args.trial_id)
        if environment:
            # 超时／失败不能留下正在付费的 Agent；Destroy 隔离 Runtime 中止其所有请求。
            try:
                manager.archive(environment)
                trace=audit(environment)
                if not (environment.artifact_root/'tool-trace.json').exists():
                    dump(environment.artifact_root/'tool-trace.json',trace['calls'])
                    dump(environment.artifact_root/'provider-usage.json',{'records':trace['usage'],'known_tokens':known_tokens(trace['usage'])})
                if not (environment.artifact_root/'contract.json').exists():
                    write_json(environment.artifact_root/'contract.json',contract)
                if not (environment.artifact_root/'verifier-result.json').exists():
                    invalid=VerifierResult(schema_version=1,suite_id=contract.suite_id,case_id=contract.case_id,
                        trial_id=args.trial_id,status='INVALID',failure_category='environment' if protocol['protocol_status']=='BLOCKED' else 'protocol',
                        verifier_version='1',assertions=(AssertionResult(assertion_id='execution_validity',category='protocol',
                            status='FAIL',evidence=(ArtifactReference(path='lifecycle.jsonl'),),
                            detail=protocol.get('reason') or '执行协议未完成'),))
                    write_json(environment.artifact_root/'verifier-result.json',invalid)
                if not (environment.artifact_root/'trial.json').exists():
                    partial=[]
                    for sid,_ in records(environment):
                        folder=environment.artifact_root/'sessions'/sid
                        usage_path=folder/'provider-usage.json'
                        dump(usage_path,[entry for entry in trace['usage'] if entry['session_id']==sid])
                        partial.append(SessionArtifacts(session_id=sid,
                            transcript=ArtifactReference(path=f'sessions/{sid}/transcript.jsonl'),
                            run_reports=tuple(ArtifactReference(path=str(p.relative_to(environment.artifact_root)))
                                              for p in sorted((folder/'reports').glob('run-*.json'))),
                            provider_usage=(ArtifactReference(path=f'sessions/{sid}/provider-usage.json'),)))
                    if partial:
                        invalid=VerifierResult.model_validate_json((environment.artifact_root/'verifier-result.json').read_text())
                        failed_manifest=TrialManifest(schema_version=1,suite_id=contract.suite_id,task_id=contract.task_id,
                            case_id=contract.case_id,trial_id=args.trial_id,environment_kind='hexworkspace',phase='protocol_smoke',
                            baseline_id=contract.baseline_id,verifier_version='1',task_contract_sha256=contract.digest(),
                            contract=ArtifactReference(path='contract.json'),run_config={'model':provider['MODEL_ID'],'execution_complete':False},
                            code_versions={'runtime':metadata['runtime']['commit'],'workspace':metadata['workspace']['commit']},
                            sessions=tuple(partial),verifier_result=invalid)
                        write_json(environment.artifact_root/'trial.json',failed_manifest)
            except Exception as exc:
                protocol.update(protocol_status='FAIL',archive_error=type(exc).__name__)
            try:
                manager.destroy(environment); protocol['destroy']='PASS'
            except Exception as exc:
                protocol.update(protocol_status='FAIL',destroy='FAIL',destroy_error=type(exc).__name__)
            protocol['wall_time_seconds']=time.monotonic()-started
            dump(environment.artifact_root/'protocol-result.json',protocol)
            print(json.dumps({**protocol,'artifact_root':str(environment.artifact_root)},ensure_ascii=False))
        else:
            args.root.mkdir(parents=True,exist_ok=True)
            dump(args.root/'blocked.json',protocol); print(json.dumps(protocol,ensure_ascii=False))


if __name__=='__main__':
    main()
