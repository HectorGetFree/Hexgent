"""配置消融离线验收：不调用Provider、Docker或业务服务。"""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock
from hexgent.config import load_config
from .variants import VARIANTS,effective_config,metadata,config_toml,verify_actual,require_applicable
from .baseline_support import full_config
from .business_cases import load_case
from .remaining_cases import load_remaining
from .compare_variants import comparable,summarize

BUDGET=dict(max_tool_calls=24,max_provider_tokens=120000,max_wall_time_seconds=300.0)


class VariantTests(unittest.TestCase):
    def test_fixed_variant_delta_preserves_budgets_and_defaults(self):
        base=asdict(full_config(BUDGET));self.assertEqual(asdict(effective_config(BUDGET)),base)
        for variant,section,enabled in [('no_memory_retrieval','memory_retrieval',False),('tool_parallel_on','tool_parallel',True)]:
            expected=deepcopy(base);expected[section]['enabled']=enabled
            self.assertEqual(asdict(effective_config(BUDGET,variant)),expected)

    def test_toml_roundtrip_matches_fixture_configuration(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            for variant in VARIANTS:
                cfg=effective_config(BUDGET,variant);(root/'hexgent.toml').write_text(config_toml(cfg))
                self.assertEqual(load_config(root),cfg)

    def test_unknown_variants_and_legacy_rejected(self):
        for v in ['no_compaction','summary_only','invalid']:
            with self.assertRaises(ValueError):effective_config(BUDGET,v)
        old,_=load_case('T01')
        with self.assertRaises(ValueError):require_applicable(old,'baseline')

    def test_only_incompatible_memory_case_is_excluded(self):
        single,_=load_remaining('t05-single',revision='f1');t01,_=load_case('T01',revision='f1')
        with self.assertRaisesRegex(ValueError,'NOT_APPLICABLE'):require_applicable(single,'no_memory_retrieval')
        require_applicable(single,'tool_parallel_on');require_applicable(t01,'no_memory_retrieval')

    def test_digest_and_actual_config_fail_closed(self):
        meta=metadata(BUDGET,'tool_parallel_on');self.assertEqual(meta,metadata(BUDGET,'tool_parallel_on'))
        self.assertNotEqual(meta['effective_config_sha256'],metadata(BUDGET,'baseline')['effective_config_sha256'])
        good={'config':meta['effective_config'],'model':'fixed'}
        verify_actual([good,good],meta['effective_config'],'fixed')
        for rows in [[],[{'config':asdict(full_config(BUDGET)),'model':'fixed'}],[{**good,'model':'other'}]]:
            with self.assertRaises(ValueError):verify_actual(rows,meta['effective_config'],'fixed')

    def test_workspace_configuration_uses_same_effective_config(self):
        from .business_environment import BusinessCaseEnvironment
        c,_=load_case('T01',revision='f1')
        for variant in VARIANTS:
            with TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'workdir').mkdir();(root/'runtime.env').touch()
                obj=BusinessCaseEnvironment.__new__(BusinessCaseEnvironment)
                obj.contract=c;obj.protocol_started_at=1.0;obj.provider={'OPENAI_API_KEY':'local-test','OPENAI_BASE_URL':'http://unused','MODEL_ID':'fixed'};obj.variant_id=variant
                stub={'services':{'workspace':{'environment':{}},'runtime':{'volumes':[]}}}
                with patch('eval.benchmark_v1.environment.TrialEnvironmentManager._configuration',return_value=stub):
                    obj._configuration(root,'test','owner')
                self.assertEqual(load_config(root/'workdir'),effective_config(c.execution_budget.model_dump(),variant))

    def test_fixture_entry_uses_variant_even_for_smoke(self):
        from .fixture_agent import main
        for variant in VARIANTS:
            with TemporaryDirectory() as tmp:
                root=Path(tmp);p=root/'config.json';p.write_text(json.dumps(dict(workdir=tmp,observation_root=tmp,endpoint='unused',token='unused',allowed_tools=[],budget=BUDGET,phase='protocol_smoke',variant_id=variant,trial_id='test',user_query='offline')))
                engine=Mock();engine.session.store.session_id='s';engine.run.return_value=[SimpleNamespace(result=SimpleNamespace(status='completed',stop_reason='done'))]
                with patch('sys.argv',['fixture_agent',str(p)]),patch('eval.benchmark_v1.fixture_agent.register_fixture'),patch('eval.benchmark_v1.fixture_agent.AgentEngine.create',return_value=engine) as create:
                    main()
                self.assertEqual(create.call_args.args[0],effective_config(BUDGET,variant))

    def test_all_command_paths_receive_variant(self):
        from .baseline import command
        args=SimpleNamespace(root=Path('/tmp/offline'),provider_env_file=Path('/unused'),variant='tool_parallel_on',workspace_source=Path('/unused'),build_metadata=Path('/unused'),docker_socket='unused',embedding_env_file=Path('/unused'))
        for name,env in [('T01','hexworkspace'),('t05-single','hexworkspace'),('t04-eligible','business_fixture')]:
            cmd=command(args,dict(name=name,environment_kind=env,contract={'verifier_spec':{'verifier_version':'2'}}),{'trial_id':'test'})
            self.assertEqual(cmd[cmd.index('--variant')+1],args.variant)

    def test_variant_smoke_cannot_fallback_to_baseline(self):
        from .baseline import inventory
        with TemporaryDirectory() as tmp,patch('eval.benchmark_v1.baseline.MODULE',Path(tmp)):
            with self.assertRaisesRegex(ValueError,'不得回退'):inventory('f1','tool_parallel_on')

    def test_inventory_keeps_fail_and_excludes_t05_without_changing_contract(self):
        from .baseline import inventory,SMOKES
        from .contracts import TrialManifest,SessionArtifacts,ArtifactReference
        for variant in VARIANTS:
            with TemporaryDirectory() as tmp:
                root=Path(tmp)
                for name in SMOKES:
                    blocked=name in {'t05-multiidentity','t08-crash'}
                    c,_=(load_case(name,revision='f1') if name.startswith('T') else load_remaining(name,revision=None if blocked else 'f1'))
                    if blocked:
                        folder=root/'runs'/SMOKES[name];folder.mkdir(parents=True)
                        (folder/'protocol-result.json').write_text(json.dumps({'protocol_status':'BLOCKED','task_status':'INCONCLUSIVE'}));continue
                    if variant=='no_memory_retrieval' and c.task_id=='T05':continue
                    folder=root/'runs'/name;folder.mkdir(parents=True)
                    m=TrialManifest(schema_version=1,suite_id=c.suite_id,task_id=c.task_id,case_id=c.case_id,trial_id='smoke',environment_kind=c.environment_kind,phase='protocol_smoke',baseline_id=c.baseline_id,verifier_version='2',task_contract_sha256=c.digest(),contract=ArtifactReference(path='contract.json'),run_config={'variant_id':variant},code_versions={'test':'offline'},sessions=(SessionArtifacts(session_id='s',transcript=ArtifactReference(path='sessions/s/t.jsonl'),run_reports=(),provider_usage=()),))
                    (folder/'trial.json').write_text(m.model_dump_json())
                    (folder/'protocol-result-v2.json').write_text(json.dumps({'identity':{'case_id':c.case_id},'protocol_status':'PASS','task_status':'FAIL'}))
                with patch('eval.benchmark_v1.baseline.MODULE',root),patch('eval.benchmark_v1.baseline.verification',return_value=SimpleNamespace(status='FAIL')):
                    rows=inventory('f1',variant)
                self.assertEqual(sum(r['eligible'] for r in rows),9 if variant=='no_memory_retrieval' else 10)
                eligible=[r for r in rows if r['eligible']]
                self.assertTrue(all(r['smoke_task']=='FAIL' for r in eligible))
                if variant=='no_memory_retrieval':
                    item=next(r for r in rows if r['name']=='t05-single');self.assertEqual(item['smoke_protocol'],'NOT_APPLICABLE')
                    c,_=load_remaining('t05-single',revision='f1');self.assertEqual(item['contract_sha256'],c.digest())

    def test_campaign_comparison_rechecks_raw_records_and_preserves_partial(self):
        from .compare_variants import compare,load_campaign
        from hashlib import sha256
        with TemporaryDirectory() as tmp:
            roots=[];actual={}
            for variant in ['baseline','no_memory_retrieval']:
                root=Path(tmp)/variant;root.mkdir();roots.append(root)
                m=self.manifest(variant);m.update(campaign_id=variant,trial_plan=[dict(trial_id=variant,case_id='c')])
                p=root/'baseline-manifest.json';p.write_text(json.dumps(m));(root/'manifest-sha256.json').write_text(json.dumps({'sha256':sha256(p.read_bytes()).hexdigest()}))
                row=dict(trial_id=variant,case_id='c',variant_id=variant,task_status='PASS' if variant=='baseline' else 'FAIL',protocol_status='PASS',valid_task_sample=True,returncode=0,total_wall_seconds=1,metrics={'known_provider_tokens':10})
                actual[variant]=row;(root/'trial-records.jsonl').write_text(json.dumps(row)+'\n')
                folder=root/'trials'/variant;folder.mkdir(parents=True);(folder/'protocol-result-v2.json').write_text('{}')
            with patch('eval.benchmark_v1.compare_variants.collect',side_effect=lambda a,c,t,*rest:actual[t['trial_id']]):
                result=compare(roots);self.assertEqual(result['matched_case_ids'],['c']);self.assertEqual(result['groups'][1]['cases'][0]['failed'],1)
                with self.assertRaises(ValueError):compare([roots[0],roots[0]])
                (roots[1]/'trial-records.jsonl').write_text('')
                result=compare(roots);self.assertFalse(result['groups'][1]['complete']);self.assertIsNone(result['groups'][1]['cases'][0]['pass_rate'])
                (roots[1]/'trial-records.jsonl').write_text(json.dumps({**actual['no_memory_retrieval'],'task_status':'PASS'})+'\n')
                with self.assertRaises(ValueError):load_campaign(roots[1])

    def test_trial_ids_separate_campaign_variant_case_and_attempt(self):
        from .variants import trial_id
        values=[trial_id(root,v,c,i) for root in ['/tmp/experiment-a/baseline','/tmp/experiment-b/baseline'] for v in VARIANTS for c in ['c1','c2'] for i in [1,2,3]]
        self.assertEqual(len(values),len(set(values)))
        self.assertEqual(trial_id('/tmp/a','baseline','c',1),trial_id('/tmp/a','baseline','c',1))

    def manifest(self,variant):
        config=asdict(effective_config(BUDGET,variant))
        return dict(protocol_version=2,variant_id=variant,provider={'model':'fixed'},runtime_source_sha256='a',build_metadata={'image':'fixed'},source_hashes={'code':'fixed'},tool_registry={},tool_schemas=[],inventory=[dict(case_id='c',contract_sha256='contract',baseline_sha256='seed',budget=BUDGET,full_config=config,eligible=True)])

    def test_comparison_only_allows_defined_config_differences(self):
        a=self.manifest('baseline');b=self.manifest('no_memory_retrieval');self.assertEqual(comparable(a,b),['c'])
        for key in ['provider','build_metadata','source_hashes']:
            bad=deepcopy(b);bad[key]={}
            with self.assertRaises(ValueError):comparable(a,bad)
        bad=deepcopy(b);bad['inventory'][0]['full_config']['tool_retry']['enabled']=True
        with self.assertRaises(ValueError):comparable(a,bad)
        bad=deepcopy(b);bad['inventory'][0]['contract_sha256']='other'
        with self.assertRaises(ValueError):comparable(a,bad)

    def test_matched_cases_exclude_not_applicable(self):
        a=self.manifest('baseline');b=self.manifest('no_memory_retrieval');b['inventory'][0]['eligible']=False
        self.assertEqual(comparable(a,b),[])

    def test_valid_fail_stays_denominator_unknown_cost_visible(self):
        records=[dict(case_id='c',valid_task_sample=s in ('PASS','FAIL'),task_status=s,metrics={'known_provider_tokens':None}) for s in ['PASS','FAIL','INVALID','INCONCLUSIVE']]
        v=summarize(records,['c'])[0];self.assertEqual((v['valid'],v['failed'],v['excluded'],v['pass_rate']),(2,1,2,0.5));self.assertTrue(v['usage_unknown'])

    def test_evidence_rejects_variant_not_effective_and_digest_mismatch(self):
        from .test_protocol_v2 import ProtocolV2Tests
        from .evidence_v2 import seal,verify
        t=ProtocolV2Tests();t.setUp();self.addCleanup(t.doCleanups)
        root,m,identity=t.sample();c,_=load_case('T01',revision='f1')
        meta=metadata(c.execution_budget.model_dump(),'no_memory_retrieval');cfg={**m.run_config,**meta,'model':'fixed'}
        m=m.model_copy(update={'run_config':cfg});(root/'run-config.json').write_text(json.dumps(cfg))
        p=root/'sessions/s/observation.jsonl'
        def reseal(config):
            p.write_text(json.dumps({'event':'run_configuration','config':config,'model':'fixed'})+'\n')
            (root/'evidence-manifest-v2.json').unlink();seal(root,m,identity)
        reseal(meta['effective_config']);self.assertEqual(verify(root,m)['status'],'PASS',verify(root,m))
        reseal(asdict(full_config(c.execution_budget.model_dump())));self.assertEqual(verify(root,m)['status'],'INVALID')
        reseal(meta['effective_config']);bad=m.model_copy(update={'run_config':{**cfg,'effective_config_sha256':'0'*64}})
        self.assertEqual(verify(root,bad)['status'],'INVALID')


if __name__=='__main__':unittest.main()
