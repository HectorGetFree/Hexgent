"""B2 串行协议和指标单元测试；无Provider、Docker或外部服务。"""
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from .baseline import inventory
from .baseline_support import full_config,aggregate,metrics
from .environment import dump


class BaselineTests(unittest.TestCase):
    def test_complete_inventory_and_real_fail_eligible(self):
        rows=inventory();self.assertEqual(len(rows),12);self.assertEqual(sum(r['eligible'] for r in rows),10)
        unknown=next(r for r in rows if r['name']=='t06-unknown');self.assertTrue(unknown['eligible']);self.assertEqual(unknown['smoke_task'],'FAIL')
        self.assertEqual({r['name'] for r in rows if not r['eligible']},{'t05-multiidentity','t08-crash'})

    def test_uniform_full_config_preserves_budget(self):
        for row in inventory():
            config=asdict(full_config(row['budget']));self.assertEqual(config['loop']['max_tool_calls'],row['budget']['max_tool_calls'])
            self.assertEqual(config['loop']['max_model_rounds'],40);self.assertEqual(config['loop']['max_output_tokens_per_round'],4096)
            self.assertFalse(config['tool_parallel']['enabled']);self.assertTrue(config['memory_retrieval']['enabled'])
            self.assertEqual(config['context']['proactive_threshold_tokens'],64000)

    def test_invalid_and_inconclusive_not_denominator(self):
        item={'case_id':'c','task_id':'T06','eligible':True}
        rows=[{'case_id':'c','trial_id':str(i),'task_status':s,'valid_task_sample':s in {'PASS','FAIL'},'metrics':{'known_provider_tokens':10,'tool_calls':2,'agent_run_seconds_sum':1}} for i,s in enumerate(['PASS','FAIL','INVALID','INCONCLUSIVE'])]
        result=aggregate(rows,[item])[0];self.assertEqual((result['pass_rate_numerator'],result['pass_rate_denominator']),(1,2));self.assertEqual(result['excluded_attempts'],2)

    def test_aux_usage_not_double_counted_or_estimated(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)
            dump(root/'provider-usage.json',{'records':[{'kind':'main','usage':{'input_tokens':10,'output_tokens':5}},{'kind':'summary','usage':{'input_tokens':20,'output_tokens':3}}]})
            events=[{'event':'provider_finished','request_type':'MAIN_AGENT','status':'SUCCEEDED','input_tokens':10,'output_tokens':5},{'event':'provider_finished','request_type':'COMPACTION','status':'SUCCEEDED','input_tokens':20,'output_tokens':3}]
            path=root/'sessions/s/observation.jsonl';path.parent.mkdir(parents=True);path.write_text('\n'.join(json.dumps(r) for r in events))
            result=metrics(root);self.assertEqual(result['known_provider_tokens'],38);self.assertEqual(result['provider_failures'],[])
            self.assertEqual(result['provider_groups']['main']['cached_missing_records'],1)

    def test_native_rate_limit_is_invalid_and_blocks_future_queue(self):
        from types import SimpleNamespace
        from .baseline import collect
        case=next(c for c in inventory() if c['name']=='t07-denied')
        root=Path(__file__).parent/'runs/b2-full-20261014-v1'
        if not root.exists():self.skipTest('需要已有429原始证据；不访问外部服务')
        manifest=json.loads((root/'baseline-manifest.json').read_text())
        trial=next(t for t in manifest['trial_plan'] if t['name']=='t07-denied')
        from unittest.mock import patch
        with patch('eval.benchmark_v1.baseline.write_json'):
            r=collect(SimpleNamespace(root=root),case,trial,0,7.0)
        self.assertEqual(r['protocol_status'],'BLOCKED');self.assertEqual(r['task_status'],'INVALID')
        self.assertFalse(r['valid_task_sample']);self.assertIsNone(r['metrics']['known_provider_tokens'])

    def test_missing_usage_is_unknown(self):
        with TemporaryDirectory() as temp:
            root=Path(temp);dump(root/'provider-usage.json',{'records':[{'kind':'main','usage':None}]})
            result=metrics(root);self.assertFalse(result['recorded_usage_complete']);self.assertEqual(result['provider_groups']['main']['missing_usage_records'],1)

if __name__=='__main__':unittest.main()
