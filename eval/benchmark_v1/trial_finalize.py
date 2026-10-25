"""仅新版协议的终态收口；独立停止、观察、归档、评分、销毁。"""
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from .protocol_v2 import cleanup,strict_jsonl
from .environment import dump


def workspace_evidence(manager,env,contract,stages):
    from .business_smoke import audit,records
    current=manager.observe(env);versions=manager.observe_versions(env);trace=audit(env)
    root=env.artifact_root
    if not (root/'contract.json').exists():
        from .artifacts import write_json
        write_json(root/'contract.json',contract)
    initial=json.loads((root/'observations/initial-state-v2.json').read_text())
    if contract.task_id=='T05':
        from dataclasses import asdict
        from hexgent.memory.memory_store import MemoryStore
        raw=records(env);sessions=[s['session_id'] for s in stages]
        sessions.extend(sid for sid,_ in raw if sid not in sessions);injections=[]
        for sid in sessions:
            p=env.workdir/'.hexgent/sessions'/sid/'observation.jsonl'
            if p.exists():
                for row in strict_jsonl(p):
                    if row['event']=='memory_injected':injections.append(dict(session_id=sid,memories=row['memories']))
        history=next((rs for sid,rs in raw if len(sessions)>1 and sid==sessions[1]),[])
        queries=[r['payload'] for r in history if r['type']=='context_user_query_appended']
        direct=len(sessions)>1 and (len(queries)!=1 or contract.user_query in json.dumps(queries,ensure_ascii=False))
        dump(root/'observations/remaining-evidence.json',dict(suite_id=contract.suite_id,case_id=contract.case_id,
            trial_id=env.trial_id,contract_sha256=contract.digest(),initial=initial,current=current,
            versions=versions['versions'],mutations=versions['mutations'],calls=trace['calls'],sessions=sessions,
            memory_items=[asdict(m) for m in MemoryStore(env.workdir).items],memory_injections=injections,direct_history_injection=direct))
        return
    docs=current['documents']
    settled=bool(docs) and all(d['status']==2 and d['version_id'] and d['source_object_valid'] and
        d['parse_ready'] and d['parse_object_valid'] and d['index_ready'] and d['chunk_object_valid'] and
        d['qdrant_point_count']==d['chunk_count'] for d in docs) and not any(current['pending_tasks'].values())
    dump(root/'observations/business-evidence.json',dict(suite_id=contract.suite_id,case_id=contract.case_id,
        trial_id=env.trial_id,contract_sha256=contract.digest(),initial=initial,current=current,stages=stages,
        versions=versions['versions'],mutations=versions['mutations'],calls=trace['calls'],settled=settled))


def finalize_workspace(manager,env,contract,protocol,stages):
    from .business_smoke import audit
    from .fixture_smoke import archive_manifest
    root=env.artifact_root
    protocol={**protocol,'identity':dict(suite_id=contract.suite_id,case_id=contract.case_id,trial_id=env.trial_id,contract_sha256=contract.digest())}
    # 停止Runtime时其他Trial服务仍在，独立业务观察可继续；然后才销毁全部资源。
    stopped=[];manifest=None
    def stop():
        manager._compose(env,'stop','runtime',timeout=30);stopped.append(True)
    def supervisor():
        if not stopped:return
        dump(root/'supervisor-stop.json',dict(trial_id=env.trial_id,process_stopped=True,
            termination_reason=protocol.get('termination_reason','agent_failed'),source='owned Trial Compose stop runtime'))
    def copy_ledger():
        ledger=env.workdir/'.benchmark/trial-budget.jsonl'
        if ledger.exists():shutil.copy2(ledger,root/'trial-budget.jsonl')
        meta=ledger.parent/'runtime-protocol-v2.json'
        if meta.exists():shutil.copy2(meta,root/'runtime-protocol-v2.json')
        failed=ledger.parent/'provider-setup-error-v2.json'
        if failed.exists():shutil.copy2(failed,root/'provider-setup-error-v2.json')
    def archive():
        nonlocal manifest
        saved=json.loads((root/'run-config.json').read_text()) if (root/'run-config.json').exists() else {}
        manifest,_=archive_manifest(root,env.workdir,contract,env.trial_id,{**saved,'phase':protocol.get('phase','protocol_smoke'),'protocol_version':2})
    def contract_artifact():
        if not (root/'contract.json').exists():
            from .artifacts import write_json
            write_json(root/'contract.json',contract)
    errors=cleanup([('runtime_stop',stop),('contract',contract_artifact),('authority_observation',lambda:workspace_evidence(manager,env,contract,stages)),
        ('archive',lambda:manager.archive(env)),('supervisor',supervisor),('ledger_copy',copy_ledger),
        ('manifest',archive),('destroy',lambda:manager.destroy(env))])
    dump(root/'lifecycle.json',dict(destroy='FAIL' if 'destroy' in errors else 'PASS',errors=errors,
        archive='archive' not in errors,runtime_stopped='runtime_stop' not in errors))
    complete(root,manifest,protocol,errors)


def complete(root,manifest,protocol,errors):
    from .evidence_v2 import finish
    if manifest is None:
        from .protocol_v2 import classify
        ready=root/'readiness.json'
        environment=ready.exists() and json.loads(ready.read_text()).get('status')=='PASS'
        value=classify(termination=protocol.get('termination_reason','agent_failed'),evidence='MISSING',environment=environment)
        from .contracts import TaskContract
        if (root/'contract.json').exists():
            c=TaskContract.model_validate_json((root/'contract.json').read_text())
            value['identity']=dict(suite_id=c.suite_id,case_id=c.case_id,trial_id=protocol.get('trial_id',root.name),contract_sha256=c.digest())
        else:value['identity']=protocol.get('identity')
        dump(root/'protocol-result-v2.json',value);return
    try:
        value=finish(root,manifest,{**protocol,'environment_valid':json.loads((root/'readiness.json').read_text())['status']=='PASS'},None)
        protocol.update(value)
    except Exception as exc:
        from .protocol_v2 import classify
        value=classify(termination=protocol.get('termination_reason','agent_failed'),evidence='MISSING')
        value.update(finalize_error=type(exc).__name__);dump(root/'protocol-result-v2.json',value)
    if errors and any(k in errors for k in ['runtime_stop','destroy']):
        value.update(protocol_status='INVALID',task_status='INVALID',valid_task_sample=False)
        dump(root/'protocol-result-v2.json',value)


def fixture_cleanup(root,fixture,child,archive):
    def stop():
        if child and child.poll() is None:child.kill();child.wait(10)
    actions=[('agent_stop',stop)]
    if fixture:
        actions.extend([('fixture_quiesce',fixture.quiesce),('authority_observation',lambda:dump(root/'observations/final-authoritative-state.json',fixture.observe())),
            ('fixture_close',fixture.close)])
    actions.extend([('archive',archive),('private_remove',lambda:shutil.rmtree(root/'private'))])
    return cleanup(actions)
