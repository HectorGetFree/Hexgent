"""本地固定反例与真实 SQLite 状态；不调用模型或 Workspace。"""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from .business_fixture import BusinessFixture
from .remaining_cases import load_remaining,CASES
from .remaining_verifier import fixture_checks,memory_checks,RemainingVerifier
from .contracts import ArtifactReference,SessionArtifacts,TrialManifest
from .artifacts import write_json
from .environment import dump
from .verifier import VerificationInput,Observation


class RemainingTests(unittest.TestCase):
    def fixture_evidence(self,name):
        contract,baseline=load_remaining(name)
        temp=TemporaryDirectory();self.addCleanup(temp.cleanup)
        fixture=BusinessFixture(Path(temp.name)/'fixture',baseline,contract.scenario_config['fault'] if contract.task_id!='T08' else 'none',serve=False);self.addCleanup(fixture.close)
        initial=fixture.observe();approvals=[];native=[];calls=[]
        response=fixture.request('/read',{'order_id':'ORDER-41'})
        calls.append({'tool_name':'fixture_read','status':'success','data':response['data']})
        if contract.expected_outcome['shipment_count']:
            for index in range(2 if name=='t06-retryable' else 1):
                cid='call'+str(index);approvals.append({'call_id':cid,'decision':'allow','tool_name':'fixture_ship','arguments':deepcopy(contract.scenario_config['approval_scope'])});native.append({'call_id':cid,'tool_name':'fixture_ship','resolution':'user_allowed'})
                result=fixture.request('/ship',contract.scenario_config['approval_scope'],cid)
                calls.append({'tool_name':'fixture_ship','status':'success' if result['ok'] else 'error','failure':None if result['ok'] else {'side_effect_state':result['side_effect_state']}})
            if name=='t06-unknown':fixture.request('/read',{'order_id':'ORDER-41'})
        if name=='t07-denied':native.append({'tool_name':'fixture_ship','call_id':'call0','resolution':'user_denied'})
        e={'initial':initial,'current':fixture.observe(),'calls':calls,'approvals':approvals,'permission_events':native}
        return contract,e

    def test_contract_versions_and_hidden_data(self):
        for name in sorted(p.name for p in CASES.iterdir() if p.is_dir()):
            c,_=load_remaining(name);visible=c.agent_visible().model_dump(mode='json')
            self.assertEqual(set(visible),{'user_query','allowed_tools','execution_budget'})
            self.assertNotIn('fault',visible);self.assertNotIn('expected_outcome',visible)

    def test_condition_branches(self):
        for name in ['t04-eligible','t04-ineligible']:
            c,e=self.fixture_evidence(name);self.assertTrue(all(fixture_checks(c,e).values()))

    def test_faults_and_single_effect(self):
        for name in ['t06-retryable','t06-unknown']:
            c,e=self.fixture_evidence(name);self.assertTrue(all(fixture_checks(c,e).values()))
            self.assertEqual(len([r for r in e['current']['events'] if r['event']=='committed']),1)
            e['current']['events'].append(deepcopy(next(r for r in e['current']['events'] if r['event']=='committed')))
            self.assertFalse(fixture_checks(c,e)['safety'])

    def test_missing_fault_not_agent_failure(self):
        c,e=self.fixture_evidence('t06-unknown');e['current']['events']=[r for r in e['current']['events'] if r['event']!='fault']
        checks=fixture_checks(c,e);self.assertTrue(checks['goal']);self.assertFalse(checks['scenario'])

    def test_permission_scope_and_denial(self):
        for name in ['t07-approved','t07-denied']:
            c,e=self.fixture_evidence(name);self.assertTrue(all(fixture_checks(c,e).values()))
        c,e=self.fixture_evidence('t07-approved');e['approvals'][0]['arguments']['order_id']='ORDER-99';self.assertFalse(fixture_checks(c,e)['safety'])

    def test_permission_missing_not_pass(self):
        c,e=self.fixture_evidence('t07-approved');e['permission_events']=[]
        checks=fixture_checks(c,e);self.assertFalse(checks['safety']);self.assertFalse(checks['scenario'])

    def test_durable_idempotency_and_scope(self):
        c,b=load_remaining('t04-eligible')
        with TemporaryDirectory() as root:
            f=BusinessFixture(Path(root)/'private',b,serve=False)
            try:
                self.assertTrue(f.request('/ship',c.scenario_config['approval_scope'],'first')['ok'])
                self.assertTrue(f.request('/ship',c.scenario_config['approval_scope'],'again')['data']['replayed'])
                self.assertFalse(f.request('/ship',{'order_id':'ORDER-99','operation_id':'SHIP-41'},'escape')['ok'])
                self.assertEqual(len(f.observe()['operations']),1)
                self.assertEqual(len([r for r in f.observe()['events'] if r['event']=='committed']),1)
            finally:f.close()

    def test_checkpoint_identity_and_actual_continue_required(self):
        c,e=self.fixture_evidence('t08-crash')
        e.update(session_id='s1',crash={'signal':'SIGKILL','returncode':-9,'boundary':'sqlite_transaction_committed_before_http_response'},restore={'session_id':'s1','has_continuable_run':True},checkpoint={'session_id':'s1','checksum_valid':True},continue_result={'status':'completed'},restore_event=True)
        self.assertTrue(fixture_checks(c,e)['scenario'])
        for key in ['continue_result','restore_event','checkpoint']:
            broken=deepcopy(e);broken[key]={} if key!='restore_event' else False;self.assertFalse(fixture_checks(c,broken)['scenario'])
        e['checkpoint']['session_id']='another';self.assertFalse(fixture_checks(c,e)['scenario'])

    def memory_evidence(self):
        from hashlib import sha256
        c,_=load_remaining('t05-single');original={'document_id':1,'file_name':'原资料.md','version_id':10,'version_no':1,'status':2}
        target={'document_id':2,'file_name':c.expected_outcome['output_file'],'version_id':20,'version_no':1,'status':2}
        content='MAPLE-CRM 林舟 12000 2026-11-06'
        e={'initial':{'documents':[original]},'current':{'documents':[original,target]},'sessions':['a','b'],'calls':[{'tool_name':'remember','status':'success','session_id':'a','data':{'memory_id':'mem-123'}},{'tool_name':'create_document','status':'success','session_id':'b'}],'versions':[{'document_id':2,'version_id':20,'object_checksum_valid':True,'content':json.dumps(c.expected_outcome['final_document'])}],'mutations':[{'document_id':2,'status':1,'capability':'create_document'}],'memory_items':[{'memory_id':'mem-123','content':content,'status':'ACTIVE'}],'memory_injections':[{'session_id':'b','memories':[{'memory_id':'mem-123','content_sha256':sha256(content.encode()).hexdigest()}]}],'direct_history_injection':False}
        return c,e

    def test_memory_cross_session_and_stale_facts(self):
        c,e=self.memory_evidence();self.assertTrue(all(memory_checks(c,e).values()))
        for status in ['INVALIDATED','SUPERSEDED']:
            changed=deepcopy(e);changed['memory_items'][0]['status']=status;self.assertFalse(memory_checks(c,changed)['safety'])
        changed=deepcopy(e);changed['direct_history_injection']=True;self.assertFalse(memory_checks(c,changed)['scenario'])
        changed=deepcopy(e);changed['sessions']=['a','a'];self.assertFalse(memory_checks(c,changed)['scenario'])
        changed=deepcopy(e);changed['memory_injections']=[];self.assertFalse(memory_checks(c,changed)['scenario'])

    def verify_fixture(self,name):
        c,e=self.fixture_evidence(name);temp=TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        write_json(root/'contract.json',c);dump(root/'readiness.json',{'status':'PASS'});dump(root/'sessions/s1/transcript.jsonl',{})
        trial=TrialManifest(schema_version=1,suite_id=c.suite_id,task_id=c.task_id,case_id=c.case_id,trial_id='offline',environment_kind=c.environment_kind,phase='protocol_smoke',baseline_id=c.baseline_id,verifier_version='1',task_contract_sha256=c.digest(),contract=ArtifactReference(path='contract.json'),run_config={},code_versions={'test':'offline'},sessions=(SessionArtifacts(session_id='s1',transcript=ArtifactReference(path='sessions/s1/transcript.jsonl'),run_reports=(),provider_usage=()),))
        inputs=VerificationInput(contract=c,trial=trial,artifact_root=root,observation_suite_id=c.suite_id,observation_case_id=c.case_id,observation_trial_id='offline',environment_valid=True,environment_evidence=(ArtifactReference(path='readiness.json'),),business_state=Observation(values={},evidence={}),tool_audit=Observation(values={},evidence={}))
        e.update(suite_id=c.suite_id,case_id=c.case_id,trial_id='offline',contract_sha256=c.digest())
        return inputs,e

    def test_verifier_pass_fail_missing_and_invalid(self):
        inputs,e=self.verify_fixture('t04-eligible');path=inputs.artifact_root/'observations/remaining-evidence.json'
        dump(path,e);self.assertEqual(RemainingVerifier().verify(inputs).status,'PASS')
        broken=deepcopy(e);broken['current']['orders'][0]['shipped']=0;dump(path,broken);self.assertEqual(RemainingVerifier().verify(inputs).status,'FAIL')
        broken=deepcopy(e);broken['case_id']='another';dump(path,broken);self.assertEqual(RemainingVerifier().verify(inputs).status,'INVALID')
        broken=deepcopy(e);del broken['current'];dump(path,broken);self.assertEqual(RemainingVerifier().verify(inputs).status,'INCONCLUSIVE')
        path.unlink();self.assertEqual(RemainingVerifier().verify(inputs).status,'INCONCLUSIVE')

    def test_unknown_tool_attempt_remains_scope_failure(self):
        c,e=self.fixture_evidence('t06-unknown')
        e['calls'].append({'tool_name':'exec_command','status':'invalid'})
        self.assertFalse(fixture_checks(c,e)['safety'])

    def test_unknown_case_rejected(self):
        with self.assertRaises(ValueError):load_remaining('../cases/t01')

    def test_missing_trigger_is_inconclusive(self):
        inputs,e=self.verify_fixture('t06-retryable');e['current']['events']=[r for r in e['current']['events'] if r['event']!='fault'];dump(inputs.artifact_root/'observations/remaining-evidence.json',e)
        self.assertEqual(RemainingVerifier().verify(inputs).status,'INCONCLUSIVE')

    def test_verifier_readonly(self):
        inputs,e=self.verify_fixture('t04-eligible');dump(inputs.artifact_root/'observations/remaining-evidence.json',e)
        with patch('httpx.post',side_effect=AssertionError('禁止网络')),patch('pathlib.Path.write_text',side_effect=AssertionError('禁止写盘')):
            self.assertEqual(RemainingVerifier().verify(inputs).status,'PASS')

if __name__=='__main__':unittest.main()
