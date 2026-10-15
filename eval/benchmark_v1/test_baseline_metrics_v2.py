"""原生结束事件不含类型时的回归；不执行模型或外部服务。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from .baseline_metrics_v2 import corrected_metrics
from .environment import dump

class MetricsV2Tests(unittest.TestCase):
    def test_join_native_spans_no_double_main(self):
        with TemporaryDirectory() as temp:
            root=Path(temp);dump(root/'provider-usage.json',{'records':[{'kind':'main','usage':{'input_tokens':10,'output_tokens':2,'input_tokens_details':{'cached_tokens':4}}}]})
            events=[{'event':'provider_started','span_id':'main','request_type':'MAIN_AGENT'}, {'event':'provider_finished','span_id':'main','status':'SUCCEEDED','input_tokens':10,'output_tokens':2}, {'event':'provider_started','span_id':'fact','request_type':'FACT_EXTRACTION'}, {'event':'provider_finished','span_id':'fact','status':'SUCCEEDED','input_tokens':20,'output_tokens':3}]
            path=root/'sessions/s/observation.jsonl';path.parent.mkdir(parents=True);path.write_text('\n'.join(json.dumps(r) for r in events))
            r=corrected_metrics(root);self.assertEqual(r['known_provider_tokens'],35);self.assertEqual(set(r['provider_groups']),{'main','FACT_EXTRACTION'})
            self.assertEqual(r['provider_groups']['main']['cached_tokens_known'],4);self.assertEqual(r['provider_groups']['main']['reasoning_missing_records'],1)

    def test_unknown_type_not_added_to_main(self):
        with TemporaryDirectory() as temp:
            root=Path(temp);dump(root/'provider-usage.json',{'records':[{'kind':'main','usage':{'input_tokens':10,'output_tokens':2}}]})
            path=root/'sessions/s/observation.jsonl';path.parent.mkdir(parents=True);path.write_text(json.dumps({'event':'provider_finished','span_id':'unknown','input_tokens':10,'output_tokens':2,'status':'SUCCEEDED'}))
            r=corrected_metrics(root);self.assertEqual(r['known_provider_tokens'],12);self.assertFalse(r['recorded_usage_complete']);self.assertEqual(len(r['provider_span_unattributed']),1)

if __name__=='__main__':unittest.main()
