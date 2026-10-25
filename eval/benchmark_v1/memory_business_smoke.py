"""T05 单身份真实 Workspace 跨独立 Conversation；多身份安全明确阻塞。"""
import argparse
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import time
from dotenv import dotenv_values
from .artifacts import write_json
from .business_environment import BusinessCaseEnvironment
from .business_smoke import audit, records, wait_stage, settle, known_tokens
from .environment import dump, LocalDocker, EnvironmentError
from .fixture_smoke import archive_manifest, verify_archive
from .remaining_cases import load_remaining
from .contracts import VerifierResult, AssertionResult, ArtifactReference


def main():
    from .variants import VARIANTS
    p=argparse.ArgumentParser();p.add_argument("--variant",choices=VARIANTS,default=None);p.add_argument('--replacement-of',type=Path,default=None);p.add_argument('--revision',choices=['f1'],default=None);p.add_argument('--phase',choices=['protocol_smoke','baseline'],default='protocol_smoke');p.add_argument('--case',choices=['t05-single','t05-multiidentity'],required=True);p.add_argument('--trial-id',required=True);p.add_argument('--root',type=Path,required=True);p.add_argument('--workspace-source',type=Path,required=True);p.add_argument('--build-metadata',type=Path,required=True);p.add_argument('--docker-socket',required=True);p.add_argument('--provider-env-file',type=Path,required=True);p.add_argument('--embedding-env-file',type=Path,required=True);args=p.parse_args()
    contract,baseline=load_remaining(args.case,revision=args.revision)
    if args.variant is not None:
        from .variants import require_applicable
        require_applicable(contract,args.variant)
    if args.case=='t05-multiidentity':
        args.root.mkdir(parents=True,exist_ok=False);write_json(args.root/'contract.json',contract)
        result={'protocol_status':'BLOCKED','task_status':'INCONCLUSIVE','scenario_triggered':False,'reason':'Production MemoryStore only workdir scope; no trusted userId/workspaceId namespace; no identity routing added','real_provider_requests':0}
        dump(args.root/'protocol-result.json',result)
        source=Path(__file__).resolve().parents[2]/'hexgent/memory/memory_store.py'
        dump(args.root/'memory-scope-audit.json',{'source':str(source),'sha256':sha256(source.read_bytes()).hexdigest(),'verified_scope':'workdir/.hexgent/memory','trusted_identity_namespace':'NOT_IMPLEMENTED'})
        write_json(args.root/'verifier-result.json',VerifierResult(schema_version=1,suite_id=contract.suite_id,case_id=contract.case_id,trial_id=args.trial_id,status='INCONCLUSIVE',failure_category='missing_evidence',verifier_version='1',assertions=(AssertionResult(assertion_id='trusted_identity_scope',category='trace',status='MISSING',evidence=(ArtifactReference(path='memory-scope-audit.json'),),detail='可信业务身份隔离缺失；未创建 Session 或调用模型'),)))
        print(json.dumps(result));return
    provider={k:v for k,v in dotenv_values(args.provider_env_file).items() if k in {'OPENAI_API_KEY','OPENAI_BASE_URL','MODEL_ID'}}
    for key in ['HTTP_PROXY','HTTPS_PROXY']:
        if os.environ.get(key):provider[key]=os.environ[key]
    metadata=json.loads(args.build_metadata.read_text())
    manager=BusinessCaseEnvironment(args.root,args.workspace_source,metadata['workspace']['image_id'],metadata['runtime']['image_id'],LocalDocker(args.docker_socket),contract=contract,baseline=baseline,provider=provider,variant_id=args.variant,embedding_key=dotenv_values(args.embedding_env_file).get('DASHSCOPE_API_KEY',''))
    env=None;started=time.monotonic();deadline=started+contract.execution_budget.max_wall_time_seconds;stages=[];decisions={}
    protocol={'case_id':contract.case_id,'trial_id':args.trial_id,'protocol_status':'BLOCKED','task_status':'INVALID','scenario_triggered':False,'identity_boundary':'single user + workspace; multiple identity NOT_SUPPORTED'}
    run_config={'protocol_version':2 if contract.verifier_spec.verifier_version=='2' else 1,'phase':args.phase,'model':provider['MODEL_ID'],'memory':'production-default','context_records':'disabled as tracked default','direct_history_injection':False,'identity_scope':'single trusted business identity','measurement_scope':contract.scenario_config.get('measurement_scope','legacy memory evidence; causal dependence not established'),'runtime_image_id':metadata['runtime']['image_id'],'workspace_image_id':metadata['workspace']['image_id']}
    if args.variant is not None:
        from .variants import metadata as variant_metadata
        run_config.update(variant_metadata(contract.execution_budget.model_dump(),args.variant))
        run_config["tool_parallel"]=args.variant=="tool_parallel_on"
        run_config["memory"]="retrieval-disabled; writes unchanged" if args.variant=="no_memory_retrieval" else "production-default"
    try:
        env=manager.provision(args.trial_id,contract.baseline_id);root=env.artifact_root
        dump(root/"run-config.json",run_config)
        if args.replacement_of:
            from .protocol_v2 import reserve_replacement
            reserve_replacement(root,args.replacement_of,contract,args.trial_id)
        write_json(root/'contract.json',contract);dump(root/'baseline-frozen.json',baseline);dump(root/'build-metadata.json',metadata)
        if manager.readiness(env,require_seed=False,require_vector=False,timeout_seconds=120)['status']!='PASS':raise EnvironmentError('基础环境未就绪')
        manager.seed(env,contract.baseline_id)
        if manager.readiness(env,timeout_seconds=180)['status']!='PASS':raise EnvironmentError('Seed 未就绪')
        initial=manager.observe(env);dump(root/"observations/initial-state-v2.json",initial);seed=json.loads((root/'seed.json').read_text())
        for query in [contract.user_query,contract.scenario_config['session_b_query']]:
            # 真实新建 Conversation，绝不把 A 的答案或历史作为 B 的输入。
            conversation=manager._api(env,'POST',f"/api/v1/workspaces/{seed['workspace_id']}/conversations",{'title':contract.case_id+'-'+str(len(stages))})['conversationId']
            run=manager.start_stage(env,conversation,query);stage=wait_stage(manager,env,run['runId'],deadline,decisions)
            stage['conversation_id']=conversation;stages.append(stage);dump(root/'stage-results.json',stages)
            if stage['runtime_status']=='failed':raise EnvironmentError('Production Agent 未完成阶段')
        current,valid=settle(manager,env,min(deadline,time.monotonic()+120));independent=manager.observe_versions(env);manager.archive(env)
        sessions=[s['session_id'] for s in stages];injections=[];history=[]
        for sid,rows in records(env):
            if sid==sessions[1]:history=[r['payload'] for r in rows if r['type']=='context_user_query_appended']
            path=env.workdir/'.hexgent/sessions'/sid/'observation.jsonl'
            if path.exists():
                for line in path.read_text().splitlines():
                    row=json.loads(line)
                    if row['event']=='memory_injected':injections.append({'session_id':sid,'sequence':row['sequence'],'memories':row['memories']})
        from hexgent.memory.memory_store import MemoryStore
        memory=MemoryStore(env.workdir)
        items=[asdict(m) for m in memory.items]
        dump(root/'memory-authority.json',{'items':items});dump(root/'memory-injections.json',injections)
        shutil.copytree(env.workdir/'.hexgent/memory',root/'memory',dirs_exist_ok=True)
        manifest,trace=archive_manifest(root,env.workdir,contract,args.trial_id,run_config)
        manifest=manifest.model_copy(update={'code_versions':{**manifest.code_versions,'runtime_image_commit':metadata['runtime']['commit'],'workspace_image_commit':metadata['workspace']['commit'],'memory_smoke_sha256':sha256(Path(__file__).read_bytes()).hexdigest()}})
        e={'suite_id':contract.suite_id,'case_id':contract.case_id,'trial_id':args.trial_id,'contract_sha256':contract.digest(),'initial':initial,'current':current,'versions':independent['versions'],'mutations':independent['mutations'],'calls':trace['calls'],'sessions':sessions,'memory_items':items,'memory_injections':injections,'direct_history_injection':len(history)!=1 or contract.user_query in json.dumps(history,ensure_ascii=False),'stages':stages}
        dump(root/'observations/remaining-evidence.json',e)
        result=verify_archive(root,contract,manifest,valid);trigger=next((a.status=='PASS' for a in result.assertions if a.assertion_id=='scenario'),False)
        protocol.update(protocol_status='PASS' if valid and trigger else 'FAIL',task_status=result.status,scenario_triggered=trigger,sessions=sessions,known_provider_tokens=known_tokens(trace['usage']),memory_gain='not a matched ablation; no causal cost/quality gain claim')
    except Exception as exc:protocol.update(protocol_status='BLOCKED' if isinstance(exc,EnvironmentError) else 'FAIL',reason=type(exc).__name__)
    finally:
        if contract.verifier_spec.verifier_version=='2':
            if env is None:env=manager.environments.get(args.trial_id)
            if env:
                from .trial_finalize import finalize_workspace
                if protocol.get('reason'):protocol['termination_reason']='agent_failed'
                finalize_workspace(manager,env,contract,{**protocol,'phase':args.phase},stages)
            return
        if env is None:env=manager.environments.get(args.trial_id)
        if env:
            try:manager.archive(env)
            finally:
                manager.destroy(env);protocol['destroy']='PASS'
            protocol['wall_time_seconds']=time.monotonic()-started
            dump(env.artifact_root/'protocol-result.json',protocol);print(json.dumps({**protocol,'artifact_root':str(env.artifact_root)},ensure_ascii=False))
        else:
            args.root.mkdir(parents=True,exist_ok=True);dump(args.root/'blocked.json',protocol);print(json.dumps(protocol))

if __name__=='__main__':main()
