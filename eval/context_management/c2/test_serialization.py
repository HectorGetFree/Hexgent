"""已知 Provider Schema 拒绝的离线回归，不用真实请求试错。"""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
import httpx
from unittest.mock import patch

from eval.context_management.c2_dispatch_guard_repair.entry import normalize_history,execute,make_client
from eval.context_management.c2_dispatch_guard_repair.test_guard import mock_transport,Rig
from eval.context_management.c2_prep_3.runner import load_rounds,policy
from eval.context_management.c2_prep_3.prepare import ROOT,read_json
from eval.context_management.c2_prep_3.budget import TokenBudget


class SerializationTests(unittest.TestCase):
    def test_only_optional_metadata_null_removed(self):
        rounds=[{'query':'事实=null','messages':[{'type':'message','role':'assistant','phase':None,'content':[
            {'type':'output_text','text':'事实=null','logprobs':None}]},
            {'type':'function_call_output','output':'{"fact": null}','call_id':'C'}]}]
        before=deepcopy(rounds);clean,paths=normalize_history(rounds)
        self.assertEqual(rounds,before);self.assertEqual(len(paths),2)
        self.assertEqual(clean[0]['messages'][0]['content'][0]['text'],'事实=null')
        self.assertEqual(clean[0]['messages'][1],before[0]['messages'][1])
    def test_non_null_metadata_preserved(self):
        rounds=[{'messages':[{'type':'message','role':'assistant','phase':'final_answer',
            'content':[{'type':'output_text','text':'事实','logprobs':[]}]}]}]
        self.assertEqual(normalize_history(rounds),(rounds,[]))
    def test_mock_rejects_old_null_and_accepts_normalized_stage1(self):
        manifest=read_json(ROOT/'smoke_manifest.json');wires=[];budget=TokenBudget(policy(manifest,1))
        for e in manifest['fixtures'][:2]:
            raw,_=load_rounds(e)
            self.assertTrue(any('logprobs' in c and c['logprobs'] is None for r in raw for m in r['messages']
                for c in m.get('content',[])))
        def checked_handler(request):
            body=json.loads(request.content)
            for m in body['input']:
                if not isinstance(m,dict):continue
                if 'phase' in m and m['phase'] is None:raise AssertionError('非法可选 phase null')
                for c in m.get('content',[]) if isinstance(m.get('content'),list) else []:
                    if 'logprobs' in c:self.assertIsInstance(c['logprobs'],list)
            wires.append(body)
            e=manifest['fixtures'][len(wires)-1];o=read_json(Path(e['oracle_v2_path']))
            summary='<summary>'+'\n'.join(f['quote'] for f in o['facts'] if f['summary_responsibility']=='SUMMARY_REQUIRED')+'</summary>'
            return mock_transport(summary,[]).handle_request(request)
        with TemporaryDirectory() as tmp:
            client=make_client(manifest,execute=True,mock_transport=httpx.MockTransport(checked_handler))
            try:
                for e in manifest['fixtures'][:2]:
                    self.assertEqual(execute(e,Path(tmp),budget,client=client,transport_mode='MOCK_TRANSPORT')['status'],'PASS')
            finally:client.close()
        self.assertEqual(len(wires),2)
    def test_fake_main_not_network_dispatch(self):
        with TemporaryDirectory() as tmp:
            r=Rig(Path(tmp));r.trace.real_mode=True;r.trace.transport_mode='NETWORK'
            r.request['instructions']='SYSTEM'
            with r.observed():
                r.guard.begin('provider',request_type='MAIN_AGENT',attempt=1);r.call()
            self.assertFalse(r.trace.state['calls'][0]['actual_network_dispatch'])
            self.assertFalse(r.trace.state['calls'][0]['is_real_compaction_call'])


if __name__=='__main__':unittest.main()
