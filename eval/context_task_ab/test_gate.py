"""P0离线Gate：Fake仅替代Provider，工具与恢复走Production。"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.context.context_compaction import COMPACTION_INSTRUCTIONS
from hexgent.context.key_fact_extractor import EXTRACTION_INSTRUCTIONS
from hexgent.tool.tool_execution import execute_tool_call
from eval.support.agent_contracts import text_response, tool_call
from eval.context_management.c1_independent import response, plain
from .harness import FIXTURE, config, profile, manifest, clone_pair, capture_before_compaction, next_request_tokens, RequestBudget, BudgetStop
from hexgent.utils.token_estimator import estimate_value_tokens
from .private_oracle.oracle import FACTS, SOURCE_FILES, expected_cost, assess
from .task import PROMPT

EVIDENCE=[]

class Model:
    def __init__(self,main):self.main=list(main);self.requests=[];self.responses=self
    def with_options(self,**kwargs):return self
    def create(self,**request):
        self.requests.append(plain(deepcopy(request)))
        if request['instructions']==EXTRACTION_INSTRUCTIONS:
            payload=json.loads(request['input'][0]['content'])
            source=payload['history_prefix'][0]['message_id']
            result=text_response(json.dumps(dict(as_of_message_id=payload['as_of_message_id'],
                facts=[dict(id='fixture-fact',content='当前采购项目RIDGE-31',status='active',source_refs=[source])],dispositions=[])))
        elif request['instructions'].startswith(COMPACTION_INSTRUCTIONS):
            result=response('<summary>已查阅部分采购资料，继续核对报价。</summary>')
        else:
            if not self.main:raise AssertionError('非预注册额外请求')
            result=self.main.pop(0)
        return iter([SimpleNamespace(type='response.completed',response=result)])

class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.work=self.root/'work';shutil.copytree(FIXTURE,self.work)
        self.cfg=config(self.work)

    def good_outputs(self):
        rows=[]
        for ident,fact in FACTS.items():
            refs=[]
            for f in SOURCE_FILES:
                p=f'vendors/{ident}/{f}';lines=(FIXTURE/p).read_text().splitlines()
                refs.append(dict(path=p,quote='\n'.join(lines[1:4])))
            rows.append(dict(id=ident,total_2y_cny=expected_cost(fact),eligible=not fact[-1],delivery_weeks=fact[-2],blockers=fact[-1],evidence=refs))
        data=dict(project_id='RIDGE-31',horizon_years=2,annual_requests=3600000,recommended='aster',fallback='birch',vendors=rows)
        (self.work/'outputs').mkdir(exist_ok=True)
        (self.work/'outputs/comparison.json').write_text(json.dumps(data,ensure_ascii=False))
        (self.work/'outputs/recommendation.md').write_text('\n'.join('## '+s+'\naster / birch；评审不执行采购部署。' for s in ['推荐','备选','成本与条件','待核验','来源']))
        return data

    def test_source_baseline_and_clone(self):
        self.assertFalse((FIXTURE/'.hexgent').exists())
        self.assertEqual(len(list((FIXTURE/'vendors').rglob('*.md'))),20)
        on,off=clone_pair(self.work,self.root/'pair')
        self.assertEqual(manifest(on),manifest(off));self.assertEqual(manifest(on),manifest(FIXTURE))
        (on/'README.md').write_text('变化')
        self.assertNotEqual(manifest(on),manifest(off));self.assertEqual(manifest(off),manifest(FIXTURE))

    def test_oracle_positive_and_adversarial(self):
        data=self.good_outputs();self.assertTrue(assess(self.work)['oracle_pass'])
        path=self.work/'outputs/comparison.json'
        for key,value in [('total_2y_cny',True),('total_2y_cny',280000),('eligible',False),('delivery_weeks',9)]:
            wrong=deepcopy(data);wrong['vendors'][0][key]=value;path.write_text(json.dumps(wrong))
            self.assertFalse(assess(self.work)['oracle_pass'],key)
        wrong=deepcopy(data);wrong['vendors'][0]['evidence'][0]['quote']='不在原文的造假证据';path.write_text(json.dumps(wrong))
        self.assertFalse(assess(self.work)['oracle_pass'])
        wrong=deepcopy(data);wrong['vendors'][0]['evidence'][0]['path']='../../private_oracle/oracle.py';path.write_text(json.dumps(wrong))
        self.assertFalse(assess(self.work)['oracle_pass'])
        path.write_text(json.dumps(data));(self.work/'buyer/brief.md').write_text('篡改')
        self.assertFalse(assess(self.work)['oracle_pass'])

    def test_real_pipeline_permission_boundary(self):
        engine=AgentEngine(self.cfg,self.work);calls=[];denials=[]
        with profile('ON'):
            for name,args in [('read_file',{'path':'../private_oracle/oracle.py'}),('write_file',{'path':'buyer/brief.md','content':'篡改'}),('read_file',{'path':'.hexgent/secret'}),('bash',{'command':'echo hello'})]:
                result=execute_tool_call(name,json.dumps(args),context=engine.tool_context,user_confirmation=lambda *a:'allow',call_id=name,execution_notifier=lambda n:calls.append(n))
                self.assertNotEqual(result.status,'success')
                denials.append(dict(tool_name=name,arguments=args,result=plain(result)))
            self.assertEqual(calls,[])
            result=execute_tool_call('read_file',json.dumps({'path':'buyer/brief.md'}),context=engine.tool_context,user_confirmation=lambda *a:'allow',call_id='allowed-read')
            self.assertEqual(result.status,'success');self.assertIn('RIDGE-31',str(result))
            written=execute_tool_call('write_file',json.dumps({'path':'outputs/check.txt','content':'允许的交付目录'}),
                context=engine.tool_context,user_confirmation=lambda *a:'allow',call_id='allowed-write')
            self.assertEqual(written.status,'success')
            self.assertEqual((self.work/'outputs/check.txt').read_text(),'允许的交付目录')
        EVIDENCE.append(dict(test=self._testMethodName,unauthorized_dispatches=len(calls),denials=denials,
            read_result=plain(result),write_result=plain(written)))

    def test_empty_session_real_tools_then_formal_pair_continue(self):
        # 仅测试捕获时机，缩小主动阈值；不是正式工作负载性能配置。
        engine=AgentEngine(self.cfg,self.work)
        with profile('ON'):threshold=next_request_tokens(engine)+estimate_value_tokens(PROMPT)+50
        cfg=replace(self.cfg,context=replace(self.cfg.context,proactive_threshold_tokens=threshold,
            recent_context_tokens=100,reactive_recent_context_tokens=100))
        engine.config=cfg
        model=Model([response(calls=[tool_call('read-buyer','read_file','{"path":"buyer/brief.md"}')])])
        with profile('ON'):
            captured=capture_before_compaction(engine,engine.run(PROMPT,client=model,user_confirmation=lambda *a:'allow'),self.root/'snapshot')
        self.assertEqual(captured['status'],'SNAPSHOT_CAPTURED')
        self.assertEqual(len(model.requests),1);self.assertIsNone(engine.context_manager.key_fact_snapshot)
        self.assertEqual(engine.context_manager.compaction_count,0)
        on,off=clone_pair(self.root/'snapshot',self.root/'pair')
        requests=[];results=[]
        for variant,root in [('ON',on),('OFF',off)]:
            # 无压缩对照验证恢复隔离；硬预算及原配置保持相同。
            restored=AgentEngine.restore(self.cfg,root,captured['session_id'])
            self.assertTrue(restored.has_continuable_run)
            before=[r for r in restored.session.store.read_transcript() if r['type']=='context_user_query_appended']
            client=Model([response('继续核对，不宣称采购完成')])
            with profile(variant):events=list(restored.run(RunRequest.continue_run(),client=client,user_confirmation=lambda *a:'allow'))
            self.assertEqual(events[-1].result.status,'completed')
            after=[r for r in restored.session.store.read_transcript() if r['type']=='context_user_query_appended']
            self.assertEqual(before,after);self.assertEqual(restored.session.store.session_id,captured['session_id'])
            self.assertIn('RIDGE-31',json.dumps(client.requests[0],ensure_ascii=False))
            requests.append(client.requests[0]);results.append(plain(events[-1].result))
        self.assertEqual(requests[0],requests[1])
        EVIDENCE.append(dict(test=self._testMethodName,captured=captured,requests=requests,results=results,
            threshold='SCALED_CAPTURE_TIMING_TEST_ONLY',real_task_success='NOT_EVALUATED'))

    def test_off_does_not_dispatch_auxiliary_or_bypass_budget(self):
        # 同样的真实资料读入，OFF在第二轮拒绝完整预算超限，不调用摘要。
        cfg=replace(self.cfg,context=replace(self.cfg.context,context_window_tokens=22000,
            proactive_threshold_tokens=20000,reserved_output_tokens=8000,safety_margin_tokens=8000,
            recent_context_tokens=100,reactive_recent_context_tokens=100))
        engine=AgentEngine(cfg,self.work)
        engine.context_manager.append_user_query('资料'+('预算检查'*8000))
        client=Model([response()])
        with profile('OFF'):events=list(engine.run('继续',client=client))
        self.assertNotEqual(events[-1].result.status,'completed')
        self.assertEqual(client.requests,[])
        EVIDENCE.append(dict(test=self._testMethodName,requests=client.requests,result=plain(events[-1].result),input='SYNTHETIC_BUDGET_PROBE_NOT_TASK_HISTORY'))

    def test_attempt_budget(self):
        budget=RequestBudget(1,10000);req=dict(instructions='检查',input=[],tools=[],max_output_tokens=10)
        budget.reserve(req)
        with self.assertRaises(BudgetStop):budget.reserve(req)
        self.assertEqual(budget.calls,1)
        with self.assertRaises(BudgetStop):RequestBudget(1,1).reserve(req)

    def test_cross_process_scaled_compaction_pair(self):
        cfg=replace(self.cfg,context=replace(self.cfg.context,proactive_threshold_tokens=10000,
            recent_context_tokens=100,reactive_recent_context_tokens=100,
            min_compaction_savings_tokens=1,min_compaction_savings_ratio=.001))
        engine=AgentEngine(cfg,self.work)
        batches=[[tool_call('buyer-'+str(i),'read_file',json.dumps({'path':str(p.relative_to(FIXTURE))}))
                  for i,p in enumerate(sorted((FIXTURE/'buyer').glob('*.md')))],
                 [tool_call('vendor-'+str(i),'read_file',json.dumps({'path':str(p.relative_to(FIXTURE))}))
                  for i,p in enumerate(sorted((FIXTURE/'vendors').rglob('*.md')))]]
        model=Model([response(calls=c) for c in batches])
        with profile('ON'):
            captured=capture_before_compaction(engine,engine.run(PROMPT,client=model,user_confirmation=lambda *a:'allow'),self.root/'snapshot')
        self.assertEqual(captured['status'],'SNAPSHOT_CAPTURED')
        self.assertEqual(len(model.requests),2)
        on,off=clone_pair(self.root/'snapshot',self.root/'pair')
        results=[]
        for variant,root in [('ON',on),('OFF',off)]:
            output=self.root/(variant+'.json')
            proc=subprocess.run([sys.executable,'-m','eval.context_task_ab.offline_worker','--work',str(root),
                '--session',captured['session_id'],'--variant',variant,'--scaled-threshold','10000','--output',str(output)],
                cwd=Path(__file__).resolve().parents[2],capture_output=True,text=True,timeout=45)
            self.assertEqual(proc.returncode,0,proc.stderr)
            results.append(json.loads(output.read_text()))
        self.assertEqual(results[0]['status'],'completed');self.assertEqual(results[1]['status'],'completed')
        self.assertGreater(results[0]['compaction_count'],0);self.assertEqual(results[1]['compaction_count'],0)
        self.assertEqual(results[0]['pre_restore_transcript'],results[1]['pre_restore_transcript'])
        for r in results:
            self.assertEqual(len([e for e in r['before_transcript'] if e['type']=='context_user_query_appended']),
                             len([e for e in r['after_transcript'] if e['type']=='context_user_query_appended']))
        self.assertTrue(any(q['instructions']==EXTRACTION_INSTRUCTIONS for q in results[0]['requests']))
        self.assertFalse(any(q['instructions']==EXTRACTION_INSTRUCTIONS for q in results[1]['requests']))
        EVIDENCE.append(dict(test=self._testMethodName,stage_a_requests=model.requests,capture=captured,
            branches=results,real_task_success='NOT_EVALUATED',threshold='SCALED_10K_NOT_REAL_BENCHMARK'))

if __name__=='__main__':unittest.main()
