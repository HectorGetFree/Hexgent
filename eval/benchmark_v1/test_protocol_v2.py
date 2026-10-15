"""执行协议离线反例；不连接模型、Provider、Docker或业务服务。"""
from contextlib import ExitStack
from copy import deepcopy
from hashlib import sha256
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from .artifacts import write_json
from .contracts import ArtifactReference,SessionArtifacts,TrialManifest
from .environment import dump
from .business_cases import load_case
from .protocol_v2 import TrialBudget,BudgetExhausted,classify,strict_jsonl,replacement,cleanup
from .evidence_v2 import seal,verify
from .eval_runtime import install
from .trial_finalize import fixture_cleanup


class ProtocolV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.identity=dict(suite_id='suite',case_id='case',trial_id='trial',contract_sha256='a'*64)
        self.limits=dict(max_tool_calls=2,max_provider_tokens=100,max_wall_time_seconds=600)
        self.budget=TrialBudget(self.root/'budget.jsonl',self.identity,self.limits)

    def test_two_runs_share_dispatch_budget(self):
        for run in ['r1','r2']:self.budget.dispatch(dict(session_id='s',run_id=run),run,'read',1)
        with self.assertRaises(BudgetExhausted):self.budget.dispatch(dict(session_id='s',run_id='r2'),'third','read',1)
        self.assertEqual(sum(r['event']=='actual_dispatch' for r in strict_jsonl(self.budget.path)),2)

    def test_two_sessions_share_dispatch_budget(self):
        for sid in ['s1','s2']:self.budget.dispatch(dict(session_id=sid,run_id='r'),sid,'read',1)
        with self.assertRaises(BudgetExhausted):self.budget.dispatch(dict(session_id='s3',run_id='r'),'third','read',1)

    def test_gate_prevents_real_handler_before_side_effect(self):
        observation=importlib.import_module('hexgent.observation');clients=importlib.import_module('hexgent.client')
        loop=importlib.import_module('hexgent.loop.agent_loop')
        with ExitStack() as stack:
            stack.enter_context(patch.object(observation,'observed_handler',lambda f,a,c,*rest:f(a,c)))
            stack.enter_context(patch.object(clients,'get_client',Mock()))
            stack.enter_context(patch.object(loop,'get_client',Mock()))
            install(dict(protocol_version=2,ledger_path=str(self.root/'gate.jsonl'),identity=self.identity,budget=self.limits,started_at=__import__('time').time()))
            count=[]
            store=SimpleNamespace(session_id='s',read_transcript=lambda:[{'type':'agent_run_started','payload':{'run_id':'r'}}])
            ctx=SimpleNamespace(observation=SimpleNamespace(store=store))
            f=lambda a,c:count.append('actual')
            for cid in ['a','b','c']:observation.observed_handler(f,{},ctx,cid,1,'read')
            self.assertEqual(count,['actual','actual'])

    def test_provider_tokens_across_runs_clamped_and_unknown_not_zero(self):
        who=dict(session_id='s',run_id='a');rid,out=self.budget.request(who,10,90)
        self.budget.response(who,rid,{'input_tokens':30,'output_tokens':40})
        rid,out=self.budget.request(dict(session_id='s',run_id='b'),10,90);self.assertEqual(out,20)
        self.budget.response(who,rid,None,reason='provider_429')
        self.assertIsNone(strict_jsonl(self.budget.path)[-1].get('tokens'))
        with self.assertRaises(BudgetExhausted):self.budget.request(who,1,1)

    def test_expired_wall_time_blocks_before_dispatch(self):
        b=TrialBudget(self.root/'expired.jsonl',self.identity,self.limits,started_at=0)
        with self.assertRaises(BudgetExhausted):b.dispatch(dict(session_id='s',run_id='r'),'a','read',1)

    def test_agent_failure_and_budget_are_valid_fail(self):
        for reason in ['agent_failed','agent_stopped','trial_tool_dispatches','trial_wall_time','trial_provider_tokens']:
            value=classify(termination=reason,task='FAIL',safety=False,scenario_triggered=False)
            self.assertTrue(value['valid_task_sample']);self.assertEqual(value['task_status'],'FAIL');self.assertFalse(value['safety_violation'])

    def test_provider_network_injector_and_unsupported_classification(self):
        for reason in ['provider_429','provider_unavailable','fixture_failure']:
            self.assertEqual(classify(termination=reason)['task_status'],'INVALID')
        self.assertEqual(classify(task='PASS',injector_valid=False)['task_status'],'INVALID')
        self.assertEqual(classify(termination='unsupported')['protocol_status'],'BLOCKED')
        self.assertEqual(classify(task='PASS',evidence='MISSING')['task_status'],'INCONCLUSIVE')

    def test_untriggered_fault_preserves_known_agent_fail(self):
        self.assertTrue(classify(task='FAIL',scenario_triggered=False)['valid_task_sample'])
        self.assertEqual(classify(task='FAIL',injector_valid=False)['task_status'],'INVALID')

    def test_goal_already_satisfied_not_failed_only_by_termination(self):
        self.assertEqual(classify(task='PASS',termination='trial_tool_dispatches')['task_status'],'PASS')

    def sample(self,status='success'):
        c,_=load_case('T01',revision='f1');identity=dict(suite_id=c.suite_id,case_id=c.case_id,trial_id='trial',contract_sha256=c.digest())
        root=self.root/'sample';root.mkdir();write_json(root/'contract.json',c)
        path='sessions/s/transcript.jsonl'
        events=[('session_started',{'session_id':'s'}),('agent_run_started',{'run_id':'r'}),('tool_execution_started',{'call_id':'call','tool_name':'read_file','arguments':'{}'})]
        if status=='success':events.append(('tool_execution_dispatched',{'call_id':'call','tool_name':'read_file'}))
        events.extend([('tool_execution_finished',{'call_id':'call','tool_name':'read_file','status':status}),('agent_run_finished',{'run_id':'r','status':'completed'})])
        rows=[dict(schema_version=2,sequence=i,type=k,created_at='2026-10-14',payload=p) for i,(k,p) in enumerate(events,1)]
        p=root/path;p.parent.mkdir(parents=True);p.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
        calls=[dict(session_id='s',run_id='r',call_id='call',tool_name='read_file',arguments={},status=status,data=None,failure=None)]
        dump(root/'tool-trace.json',calls);dump(root/'provider-usage.json',{'records':[]});dump(root/'readiness.json',{'status':'PASS'});dump(root/'lifecycle.json',{'destroy':'PASS'})
        dump(root/'observations/business-evidence.json',{**identity,'calls':calls,'initial':{'documents':[]},
            'current':{'documents':[]},'stages':[],'versions':[],'mutations':[],'settled':True})
        budget=TrialBudget(root/'trial-budget.jsonl',identity,self.limits)
        dump(root/'runtime-protocol-v2.json',dict(protocol_version=2,identity=identity,budget=c.execution_budget.model_dump(),sdk_max_retries=0))
        if status=='success':budget.dispatch(dict(session_id='s',run_id='r'),'call','read_file',1)
        manifest=TrialManifest(schema_version=1,suite_id=c.suite_id,task_id=c.task_id,case_id=c.case_id,trial_id='trial',environment_kind=c.environment_kind,
            phase='protocol_smoke',baseline_id=c.baseline_id,verifier_version='2',task_contract_sha256=c.digest(),contract=ArtifactReference(path='contract.json'),
            run_config={'protocol_version':2},code_versions={'test':'offline'},sessions=(SessionArtifacts(session_id='s',transcript=ArtifactReference(path=path),run_reports=(),provider_usage=()),))
        seal(root,manifest,identity);return root,manifest,identity

    def reseal(self,root,manifest,identity):
        (root/'evidence-manifest-v2.json').unlink();seal(root,manifest,identity)

    def test_denied_invalid_vs_actual_dispatch_counts(self):
        root,m,i=self.sample('denied');v=verify(root,m);self.assertEqual(v['status'],'PASS');self.assertEqual(v['actual_dispatches'],0);self.assertEqual(v['denied_calls'],1)

    def test_damaged_jsonl_and_active_tail_only(self):
        p=self.root/'bad.jsonl';p.write_text('{}\n{broken')
        self.assertEqual(len(strict_jsonl(p,active=True)),1)
        with self.assertRaises(ValueError):strict_jsonl(p)
        p.write_text('{bad}\n{}\n')
        with self.assertRaises(ValueError):strict_jsonl(p,active=True)

    def test_missing_end_not_accepted_even_valid_hash(self):
        root,m,i=self.sample();p=root/m.sessions[0].transcript.path;rows=strict_jsonl(p);p.write_text('\n'.join(json.dumps(r) for r in rows[:-1])+'\n')
        self.reseal(root,m,i);self.assertEqual(verify(root,m)['status'],'INVALID')

    def test_unpaired_result_rejected(self):
        root,m,i=self.sample();p=root/m.sessions[0].transcript.path;rows=strict_jsonl(p);rows[-2]['payload']['call_id']='other';p.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
        self.reseal(root,m,i);self.assertEqual(verify(root,m)['status'],'INVALID')

    def test_cross_trial_evidence_rejected(self):
        root,m,i=self.sample();p=root/'observations/business-evidence.json';e=json.loads(p.read_text());e['trial_id']='other';dump(p,e)
        self.reseal(root,m,i);self.assertEqual(verify(root,m)['status'],'INVALID')

    def test_digest_mismatch_missing_and_manifest_reference(self):
        root,m,i=self.sample();(root/'readiness.json').write_text('{}')
        self.assertEqual(verify(root,m)['status'],'INVALID')
        self.reseal(root,m,i);bad=m.model_copy(update={'run_config':{'evidence_manifest_sha256':'0'*64}})
        self.assertEqual(verify(root,bad)['status'],'INVALID')
        (root/'readiness.json').unlink();self.assertEqual(verify(root,m)['status'],'MISSING')

    def test_artifact_reference_digest_and_unsealed_report_rejected(self):
        root,m,i=self.sample()
        bad=m.model_copy(update={'contract':ArtifactReference(path='contract.json',sha256='0'*64)})
        self.assertEqual(verify(root,bad)['status'],'INVALID')
        session=m.sessions[0].model_copy(update={'run_reports':(ArtifactReference(path='unsealed-report.json'),)})
        dump(root/'unsealed-report.json',{})
        bad=m.model_copy(update={'sessions':(session,)})
        self.assertEqual(verify(root,bad)['status'],'MISSING')

    def test_duplicate_sequence_and_normalized_forgery_rejected(self):
        root,m,i=self.sample();p=root/m.sessions[0].transcript.path;rows=strict_jsonl(p);rows[1]['sequence']=1;p.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
        self.reseal(root,m,i);self.assertEqual(verify(root,m)['status'],'INVALID')

    def test_legacy_not_relabelled_failed(self):
        root,m,i=self.sample();(root/'evidence-manifest-v2.json').unlink()
        self.assertEqual(verify(root,m)['status'],'LEGACY_NOT_AUDITABLE_UNDER_V2')

    def test_replacement_lineage_and_selection(self):
        old=dict(task_status='INVALID',trial_id='old',case_id='case',contract_sha256='a'*64,termination_reason='provider_429')
        v=replacement(old,'new',case_id='case',contract_sha256='a'*64,attempt=1);self.assertEqual(v['previous_trial_id'],'old')
        for change in ['fail','same','limit','revision']:
            p=deepcopy(old);new='new';ordinal=1;digest='a'*64
            if change=='fail':p['task_status']='FAIL'
            if change=='same':new='old'
            if change=='limit':ordinal=2
            if change=='revision':digest='b'*64
            with self.assertRaises(ValueError):replacement(p,new,case_id='case',contract_sha256=digest,attempt=ordinal)

    def test_all_cleanup_actions_run_after_evidence_and_verifier_errors(self):
        for failed in ['observe','archive','verify','initialize']:
            done=[]
            def action(name):
                done.append(name)
                if name==failed:raise RuntimeError(name)
            errors=cleanup([(n,lambda n=n:action(n)) for n in ['initialize','observe','archive','verify','close','destroy']])
            self.assertEqual(done[-2:],['close','destroy']);self.assertIn(failed,errors)

    def test_fixture_cleanup_when_observation_fails(self):
        (self.root/'private').mkdir();f=Mock();f.observe.side_effect=ValueError('bad observation')
        child=Mock();child.poll.return_value=None
        archive=Mock(side_effect=ValueError('bad verifier'))
        errors=fixture_cleanup(self.root,f,child,archive)
        f.close.assert_called_once();child.kill.assert_called_once();child.wait.assert_called_once()
        self.assertFalse((self.root/'private').exists());self.assertIn('archive',errors)

    def test_workspace_cleanup_after_archive_and_evidence_errors(self):
        from .trial_finalize import finalize_workspace
        c,_=load_case('T01',revision='f1');manager=Mock()
        manager.observe.side_effect=ValueError('observe unavailable');manager.archive.side_effect=ValueError('archive unavailable')
        root=self.root/'workspace';root.mkdir();env=SimpleNamespace(artifact_root=root,workdir=root/'workdir',trial_id='trial')
        env.workdir.mkdir()
        finalize_workspace(manager,env,c,{},[])
        manager.destroy.assert_called_once_with(env)
        self.assertEqual(json.loads((root/'protocol-result-v2.json').read_text())['task_status'],'INVALID')

    def test_partial_fixture_start_failure_closes_socket(self):
        from .business_fixture import BusinessFixture
        from .remaining_cases import load_remaining
        _,seed=load_remaining('t04-eligible')
        server=Mock();server.server_port=9999
        with patch('eval.benchmark_v1.business_fixture.ThreadingHTTPServer',return_value=server),patch('threading.Thread.start',side_effect=RuntimeError('start failed')):
            with self.assertRaises(RuntimeError):BusinessFixture(self.root/'partial',seed)
        server.server_close.assert_called_once();self.assertFalse((self.root/'partial').exists())

    def test_f1_runner_cannot_fallback_to_legacy_smoke(self):
        from .baseline import inventory
        with self.assertRaisesRegex(ValueError,'不得回退'):inventory('f1')

    def test_final_v2_verifier_requires_integrity_manifest(self):
        from .test_verifier_v2 import VerifierV2Tests
        from .remaining_verifier import RemainingVerifier
        h=VerifierV2Tests();self.addCleanup(h.doCleanups);c,e=h.fixture('t07-denied');inputs=h.inputs(c)
        e.update(suite_id=c.suite_id,case_id=c.case_id,trial_id=inputs.trial.trial_id,contract_sha256=c.digest())
        dump(inputs.artifact_root/'observations/remaining-evidence.json',e)
        strict=inputs.model_copy(update={'trial':inputs.trial.model_copy(update={'run_config':{'protocol_version':2}})})
        self.assertEqual(RemainingVerifier().verify(strict).status,'INCONCLUSIVE')

    def test_seal_then_formal_score_and_trial_record(self):
        from .test_verifier_v2 import VerifierV2Tests
        from .evidence_v2 import finish
        h=VerifierV2Tests();self.addCleanup(h.doCleanups);c,e=h.fixture('t07-denied');inputs=h.inputs(c);root=inputs.artifact_root
        identity=dict(suite_id=c.suite_id,case_id=c.case_id,trial_id=inputs.trial.trial_id,contract_sha256=c.digest())
        e.update(identity);rows=[]
        def event(kind,payload):rows.append(dict(schema_version=2,sequence=len(rows)+1,type=kind,created_at='2026-10-14',payload=payload))
        event('session_started',{'session_id':'offline'});event('agent_run_started',{'run_id':'r'})
        budget=TrialBudget(root/'trial-budget.jsonl',identity,c.execution_budget.model_dump())
        for call in e['calls']:
            event('tool_execution_started',dict(call_id=call['call_id'],tool_name=call['tool_name'],arguments=json.dumps(call['arguments'])))
            if call['status']!='denied':
                event('tool_execution_dispatched',dict(call_id=call['call_id'],tool_name=call['tool_name']))
                budget.dispatch(dict(session_id='offline',run_id='r'),call['call_id'],call['tool_name'],1)
            event('tool_execution_finished',{**call,'data':call.get('data'),'failure':call.get('failure')})
            call.update(session_id='offline',run_id='r',data=call.get('data'),failure=call.get('failure'))
        event('agent_run_finished',{'run_id':'r','status':'completed'})
        (root/inputs.trial.sessions[0].transcript.path).write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
        dump(root/'tool-trace.json',e['calls']);dump(root/'provider-usage.json',{'records':[]})
        dump(root/'lifecycle.json',{'destroy':'PASS'});dump(root/'observations/remaining-evidence.json',e)
        dump(root/'runtime-protocol-v2.json',dict(protocol_version=2,identity=identity,budget=c.execution_budget.model_dump(),sdk_max_retries=0))
        manifest=inputs.trial.model_copy(update={'run_config':{'protocol_version':2}})
        result=finish(root,manifest,{'environment_valid':True},None)
        self.assertEqual(result['task_status'],'PASS');self.assertTrue(result['valid_task_sample'])
        saved=TrialManifest.model_validate_json((root/'trial.json').read_text())
        self.assertEqual(verify(root,saved)['status'],'PASS')
        self.assertEqual(saved.run_config['evidence_manifest_sha256'],result['evidence_manifest_sha256'])

    def test_replacement_registry_does_not_touch_original(self):
        from .protocol_v2 import reserve_replacement
        c,_=load_case('T01',revision='f1');old=self.root/'history';old.mkdir()
        dump(old/'protocol-result-v2.json',dict(identity=dict(suite_id=c.suite_id,case_id=c.case_id,trial_id='old',contract_sha256=c.digest()),
            task_status='INVALID',termination_reason='provider_429'))
        before=(old/'protocol-result-v2.json').read_bytes()
        reserve_replacement(self.root/'new-campaign'/'one',old,c,'new1')
        with self.assertRaises(ValueError):reserve_replacement(self.root/'new-campaign'/'two',old,c,'new2')
        self.assertEqual(before,(old/'protocol-result-v2.json').read_bytes())

    def test_invalid_arguments_are_agent_failure_not_corrupt_event(self):
        root,m,i=self.sample('invalid');p=root/m.sessions[0].transcript.path;rows=strict_jsonl(p)
        next(r for r in rows if r['type']=='tool_execution_started')['payload']['arguments']='{bad argument'
        p.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
        for target in [root/'tool-trace.json',root/'observations/business-evidence.json']:
            value=json.loads(target.read_text());calls=value if isinstance(value,list) else value['calls']
            calls[0]['arguments']={'_invalid_json':'{bad argument'};dump(target,value)
        self.reseal(root,m,i);self.assertEqual(verify(root,m)['status'],'PASS')
        self.assertEqual(verify(root,m)['actual_dispatches'],0)

    def test_budget_clock_cannot_reset_on_next_process(self):
        observer=importlib.import_module('hexgent.observation');clients=importlib.import_module('hexgent.client');loop=importlib.import_module('hexgent.loop.agent_loop')
        cfg=dict(protocol_version=2,ledger_path=str(self.root/'shared.jsonl'),identity=self.identity,budget=self.limits,started_at=100)
        with ExitStack() as stack:
            for module,name in [(observer,'observed_handler'),(clients,'get_client'),(loop,'get_client')]:stack.enter_context(patch.object(module,name,Mock()))
            install(cfg)
            with self.assertRaises(ValueError):install({**cfg,'started_at':200})


if __name__=='__main__':unittest.main()
