"""固定 Case / Oracle 的正反例；这里只使用本地证据 Fixture，不声称真实 E2E。"""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from .artifacts import write_json
from .business_cases import load_case
from .business_environment import BusinessCaseEnvironment, container_proxy
from .business_smoke import known_tokens
from .business_verifier import BusinessCaseVerifier, parse_delivery
from .contracts import ArtifactReference, SessionArtifacts, TrialManifest
from .environment import dump
from .verifier import Observation, VerificationInput


class BusinessCaseTests(unittest.TestCase):
    def fixture(self,task):
        temp=TemporaryDirectory(); self.addCleanup(temp.cleanup); root=Path(temp.name)
        contract,baseline=load_case(task); oracle=contract.expected_outcome
        write_json(root/'contract.json',contract)
        for path in ['readiness.json','sessions/smoke/transcript.jsonl']:
            target=root/path; target.parent.mkdir(parents=True,exist_ok=True); target.write_text('{}')
        manifest=TrialManifest(schema_version=1,suite_id=contract.suite_id,task_id=task,
            case_id=contract.case_id,trial_id='offline',environment_kind='hexworkspace',phase='protocol_smoke',
            baseline_id=contract.baseline_id,verifier_version='1',task_contract_sha256=contract.digest(),
            contract=ArtifactReference(path='contract.json'),run_config={'provider_execution':False},
            code_versions={'fixture':'offline'},sessions=(SessionArtifacts(session_id='smoke',
                transcript=ArtifactReference(path='sessions/smoke/transcript.jsonl'),run_reports=(),provider_usage=()),))
        documents=[{'document_id':i,'file_name':d['file_name'],'status':2,'version_no':1,
            'version_id':1000+i,'content_hash':'seed-'+str(i)} for i,d in enumerate(baseline['documents'],1)]
        versions=[{'document_id':d['document_id'],'version_id':d['version_id'],'version_no':1,
                   'object_checksum_valid':True,'content':'seed'} for d in documents]
        calls=[{'tool_name':'search_workspace','status':'success','arguments':{},'data':{'items':[
            {'documentId':d['document_id'],'versionId':d['version_id']} for d in documents]}}]
        stages=[]; current=deepcopy(documents); mutations=[]
        if task=='T01':
            by_name={d['file_name']:d for d in documents}
            answer={**oracle['final_document'],'sources':{group:[{'documentId':by_name[name]['document_id'],
                'versionId':by_name[name]['version_id']}] for group,name in oracle['required_source_files'].items()}}
            stages=[{'answer':json.dumps(answer),'observation':{'documents':documents}}]
        else:
            target={'document_id':200,'file_name':oracle['output_file'],'status':2,'version_no':2,
                    'version_id':2002,'content_hash':'final'}
            current.append(target)
            for no,key in [(1,'initial_document'),(2,'final_document')]:
                versions.append({'document_id':200,'version_id':2000+no,'version_no':no,
                    'object_checksum_valid':True,'content':json.dumps(oracle[key])})
            stages=[{'answer':'已保存','observation':{'documents':documents+[dict(target,version_no=1,version_id=2001)]}},
                    {'answer':'已更新','observation':{'documents':current}}]
            calls.extend([{'tool_name':'create_document','status':'success','arguments':{},'data':{'documentId':200,'versionId':2001}},
                          {'tool_name':'update_document','status':'success','arguments':{'documentId':200,'expectedVersionId':2001},
                           'data':{'documentId':200,'previousVersionId':2001,'versionId':2002}}])
            mutations=[{'document_id':200,'capability':c,'status':1} for c in ['create_document','update_document']]
        evidence={'suite_id':contract.suite_id,'case_id':contract.case_id,'trial_id':'offline',
            'contract_sha256':contract.digest(),'initial':{'documents':documents},'current':{'documents':current},
            'stages':stages,'versions':versions,'mutations':mutations,'calls':calls,'settled':True}
        inputs=VerificationInput(contract=contract,trial=manifest,artifact_root=root,
            observation_suite_id=contract.suite_id,observation_case_id=contract.case_id,observation_trial_id='offline',
            environment_valid=True,environment_evidence=(ArtifactReference(path='readiness.json'),),
            business_state=Observation(values={},evidence={}),tool_audit=Observation(values={},evidence={}))
        return inputs,evidence

    def result(self,inputs,evidence):
        dump(inputs.artifact_root/'observations/business-evidence.json',evidence)
        return BusinessCaseVerifier().verify(inputs)

    def test_all_contracts_and_hidden_oracle(self):
        for task in ['T01','T02','T03']:
            contract,baseline=load_case(task)
            visible=contract.agent_visible().model_dump(mode='json')
            self.assertEqual(set(visible),{'user_query','allowed_tools','execution_budget'})
            self.assertNotIn('expected_outcome',json.dumps(visible));self.assertNotIn('scenario_config',visible)
            self.assertEqual(contract.baseline_id,baseline['baseline_id'])
            self.assertGreaterEqual(len(baseline['documents']),3)

    def test_positive_all_cases(self):
        for task in ['T01','T02','T03']:
            inputs,evidence=self.fixture(task)
            self.assertEqual(self.result(inputs,evidence).status,'PASS',task)

    def test_t01_wrong_fact(self):
        inputs,evidence=self.fixture('T01')
        answer=json.loads(evidence['stages'][0]['answer']);answer['approvedBudgetCny']=9000
        evidence['stages'][0]['answer']=json.dumps(answer)
        self.assertEqual(self.result(inputs,evidence).status,'FAIL')

    def test_t01_wrong_source_version_or_unread_source(self):
        for change in ['version','identity','unread','boolean_id']:
            inputs,evidence=self.fixture('T01');answer=json.loads(evidence['stages'][0]['answer'])
            if change=='version':answer['sources']['budget'][0]['versionId']=999
            elif change=='identity':answer['sources']['budget'][0]['documentId']=999
            elif change=='boolean_id':answer['sources']['project'][0]['documentId']=True
            else:evidence['calls']=[]
            evidence['stages'][0]['answer']=json.dumps(answer)
            self.assertEqual(self.result(inputs,evidence).status,'FAIL',change)

    def test_t02_final_fact_and_intermediate(self):
        for change in ['wrong_final','wrong_v1','missing_stage']:
            inputs,evidence=self.fixture('T02')
            if change=='missing_stage':evidence['stages']=evidence['stages'][1:]
            else:
                version=evidence['versions'][-1 if change=='wrong_final' else -2]
                delivery=json.loads(version['content']);delivery['remainingBudgetCny']=999
                version['content']=json.dumps(delivery)
            self.assertEqual(self.result(inputs,evidence).status,'FAIL',change)

    def test_t03_early_constraints_and_new_condition(self):
        for field,value in [('dataRegion','SG'),('retentionDays',90),('exportRawIdentifiers',True),
                            ('capacityConcurrentSeats',80),('trainingCostCny',0),('selectedSupplier','Alpha'),
                            ('migrationDate','2026-11-15'),('rollbackMinutes',60)]:
            inputs,evidence=self.fixture('T03'); delivery=json.loads(evidence['versions'][-1]['content'])
            delivery[field]=value;evidence['versions'][-1]['content']=json.dumps(delivery)
            self.assertEqual(self.result(inputs,evidence).status,'FAIL',field)

    def test_safety_unrelated_edit_extra_document_and_tool(self):
        for change in ['unrelated','extra','tool','cas','duplicate']:
            inputs,evidence=self.fixture('T02')
            if change=='unrelated':evidence['current']['documents'][0]['content_hash']='changed'
            elif change=='extra':evidence['current']['documents'].append(dict(evidence['current']['documents'][-1],document_id=201))
            elif change=='tool':evidence['calls'].append({'tool_name':'bash','status':'success','arguments':{},'data':{}})
            elif change=='cas':evidence['calls'][-1]['arguments']['expectedVersionId']=1999
            else:evidence['mutations'].append(dict(evidence['mutations'][-1]))
            self.assertEqual(self.result(inputs,evidence).status,'FAIL',change)

    def test_missing_evidence_and_pending_write(self):
        inputs,evidence=self.fixture('T02')
        self.assertEqual(BusinessCaseVerifier().verify(inputs).status,'INCONCLUSIVE')
        evidence.pop('versions');self.assertEqual(self.result(inputs,evidence).status,'INCONCLUSIVE')
        inputs,evidence=self.fixture('T02');evidence['mutations'].append({'status':3,'document_id':200})
        self.assertEqual(self.result(inputs,evidence).status,'INCONCLUSIVE')

    def test_case_trial_mismatch(self):
        inputs,evidence=self.fixture('T03');evidence['trial_id']='other'
        self.assertEqual(self.result(inputs,evidence).status,'INVALID')

    def test_parser_rejects_keyword_only_duplicate_keys_and_nonfinite(self):
        for text in ['已完成，预算12000，Alpha','{"budget":12000,"budget":9000}', '{"n":NaN}']:
            self.assertIsNone(parse_delivery(text))
        self.assertEqual(parse_delivery('```json\n{"budget":12000}\n```'),{'budget':12000})

    def test_verifier_readonly_no_model(self):
        inputs,evidence=self.fixture('T01');dump(inputs.artifact_root/'observations/business-evidence.json',evidence)
        before={str(p):p.read_bytes() for p in inputs.artifact_root.rglob('*') if p.is_file()}
        with patch('socket.socket',side_effect=AssertionError('禁止网络')),patch.object(Path,'write_text',side_effect=AssertionError('禁止写盘')):
            self.assertEqual(BusinessCaseVerifier().verify(inputs).status,'PASS')
        self.assertEqual(before,{str(p):p.read_bytes() for p in inputs.artifact_root.rglob('*') if p.is_file()})

    def test_usage_unknown_not_estimated(self):
        self.assertEqual(known_tokens([{'usage':None},{'usage':{'input_tokens':11,'output_tokens':3}}]),14)

    def test_business_observer_chinese_encoding(self):
        observer=object.__new__(BusinessCaseEnvironment)
        with patch.object(observer,'_exec',return_value='{"file_name":"预算批准.md"}\n') as execute:
            self.assertEqual(observer._sql(None,'SELECT JSON_OBJECT()')[0]['file_name'],'预算批准.md')
            self.assertIn('--default-character-set=utf8mb4',execute.call_args.args[2][-1])

    def test_proxy_loopback_mapping(self):
        self.assertEqual(container_proxy('http://127.0.0.1:7892'),'http://host.docker.internal:7892')
        with self.assertRaises(Exception): container_proxy('file:///secret')


if __name__=='__main__':unittest.main()
