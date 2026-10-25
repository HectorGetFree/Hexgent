"""T08新Revision与计划内崩溃证据的纯离线反例。"""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from .remaining_cases import load_remaining
from .verifier_v2 import fixture_checks_v2
from .evidence_v2 import inspect_events
from .recovery_evidence import planned_crash


class T08RecoveryTests(unittest.TestCase):
    def evidence(self):
        from .test_verifier_v2 import VerifierV2Tests
        h=VerifierV2Tests();self.addCleanup(h.doCleanups)
        _,e=h.fixture('t06-unknown');c,_=load_remaining('t08-crash',revision='f1')
        for r in e['current']['events']:
            if r['event']=='fault':r['body']['kind']='crash'
        for call in e['calls']:
            if call['tool_name']=='fixture_ship':call['status']='interrupted'
        e.update(session_id='s',crash=dict(signal='SIGKILL',returncode=-9,boundary='sqlite_transaction_committed_before_http_response',committed_count_at_cut=1),checkpoint=dict(checksum_valid=True,session_id='s',run_state=dict(model_rounds=1,tool_calls=1,input_tokens=10,output_tokens=2)),restore=dict(session_id='s',has_continuable_run=True,continue_attempted=True,restored_run_state=dict(model_rounds=2,tool_calls=2,input_tokens=20,output_tokens=4)),continue_result=dict(session_id='s',status='completed'),restore_event=True)
        return c,e

    def test_positive_and_historical_revision_isolation(self):
        c,e=self.evidence();self.assertTrue(all(fixture_checks_v2(c,e).values()))
        old,_=load_remaining('t08-crash');self.assertEqual(old.case_id,'t08-crash-v1');self.assertEqual(old.verifier_spec.verifier_version,'1')
        self.assertEqual(c.execution_budget,old.execution_budget);self.assertEqual(c.user_query,old.user_query)

    def test_failed_continue_is_not_success_even_if_business_committed(self):
        c,e=self.evidence();e['continue_result']['status']='failed';v=fixture_checks_v2(c,e)
        self.assertTrue(v['goal']);self.assertFalse(v['recovery'])

    def test_budget_reset_or_missing_post_read_cannot_pass(self):
        c,e=self.evidence();bad=deepcopy(e);bad['restore']['restored_run_state']['tool_calls']=0
        self.assertFalse(fixture_checks_v2(c,bad)['recovery'])
        e['calls']=[c for c in e['calls'] if c['tool_name']!='fixture_read' or c['data']['order']['shipped']==0]
        e['current']['events']=[r for r in e['current']['events'] if r['event']!='read' or r['body']['data']['order']['shipped']==0]
        self.assertFalse(fixture_checks_v2(c,e)['recovery'])

    def test_planned_crash_only_closes_the_proven_run(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);c,_=load_remaining('t08-crash',revision='f1');(root/'contract.json').write_text(c.model_dump_json())
            m=SimpleNamespace(task_id='T08',trial_id='trial',contract=SimpleNamespace(path='contract.json'),sessions=[SimpleNamespace(session_id='s',transcript=SimpleNamespace(path='sessions/s/transcript.jsonl'),run_reports=(),provider_usage=())])
            proof=dict(trial_id='trial',session_id='s',run_id='r1',transcript_sequence=4,returncode=-9,process_stopped=True,source='Runner child.kill/wait; fixture cut',boundary='sqlite_transaction_committed_before_http_response')
            crash=dict(session_id='s',signal='SIGKILL',returncode=-9,boundary=proof['boundary'],committed_count_at_cut=1)
            (root/'supervisor-crash.json').write_text(json.dumps(proof));(root/'crash-boundary.json').write_text(json.dumps(crash))
            events=[('session_started',{'session_id':'s'}),('agent_run_started',{'run_id':'r1'}),('tool_execution_started',{'call_id':'c1','tool_name':'fixture_ship','arguments':'{}'}),('tool_execution_dispatched',{'call_id':'c1'}),('session_recovery_repaired',{'repaired_calls':[{'call_id':'c1','tool_name':'fixture_ship','recovery_action':'verify_before_retry'}]}),('agent_run_started',{'run_id':'r2'}),('agent_run_finished',{'run_id':'r2','status':'completed'})]
            p=root/m.sessions[0].transcript.path;p.parent.mkdir(parents=True)
            def write(rows):p.write_text(''.join(json.dumps(dict(schema_version=2,sequence=i,type=k,payload=v,created_at='2026-10-20T00:00:00+00:00'))+'\n' for i,(k,v) in enumerate(rows,1)))
            write(events);calls,bindings,native=inspect_events(root,m)
            self.assertEqual(calls[0]['status'],'interrupted');self.assertEqual(calls[0]['run_id'],'r1');self.assertEqual(len(bindings),2)
            write(events[:-1])
            with self.assertRaisesRegex(ValueError,'缺少Run结束'):inspect_events(root,m)
            proof['returncode']=0;(root/'supervisor-crash.json').write_text(json.dumps(proof))
            with self.assertRaises(ValueError):planned_crash(root,m)

    def test_same_run_resume_requires_recovery_and_only_once(self):
        from unittest.mock import patch
        with TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'transcript.jsonl'
            session=SimpleNamespace(session_id='s',transcript=SimpleNamespace(path=path.name),run_reports=(),provider_usage=())
            manifest=SimpleNamespace(trial_id='trial',sessions=[session])
            proof=dict(session_id='s',run_id='r1',transcript_sequence=4)
            rows=[('session_started',{'session_id':'s'}),('agent_run_started',{'run_id':'r1'}),
                ('tool_execution_started',{'call_id':'c','tool_name':'fixture_ship','arguments':'{}'}),
                ('tool_execution_dispatched',{'call_id':'c'}),
                ('session_recovery_repaired',{'repaired_calls':[dict(call_id='c',tool_name='fixture_ship',recovery_action='verify_before_retry')]}),
                ('session_recovered',{}),('agent_run_started',{'run_id':'r1'}),
                ('agent_run_finished',{'run_id':'r1','status':'completed'})]
            def write(values):
                path.write_text(''.join(json.dumps(dict(schema_version=2,sequence=i,type=k,payload=v,created_at='2026-10-20T00:00:00+00:00'))+'\n' for i,(k,v) in enumerate(values,1)))
            with patch('eval.benchmark_v1.recovery_evidence.planned_crash',return_value=proof):
                write(rows);calls,bindings,_=inspect_events(root,manifest)
                self.assertEqual(bindings,{('s','r1')});self.assertEqual(calls[0]['status'],'interrupted')
                write([r for r in rows if r[0]!='session_recovered'])
                with self.assertRaisesRegex(ValueError,'Run身份重复'):inspect_events(root,manifest)
                write(rows+[('agent_run_started',{'run_id':'r1'})])
                with self.assertRaisesRegex(ValueError,'Run身份重复'):inspect_events(root,manifest)
            write(rows)
            with patch('eval.benchmark_v1.recovery_evidence.planned_crash',return_value=None):
                with self.assertRaises(ValueError):inspect_events(root,manifest)

    def test_recovery_requires_explicit_opt_in_and_own_smoke(self):
        from .baseline import inventory
        from unittest.mock import patch
        with TemporaryDirectory() as tmp,patch('eval.benchmark_v1.baseline.MODULE',Path(tmp)):
            with self.assertRaisesRegex(ValueError,'不得回退'):inventory('f1','baseline',['t08-crash'],True)


if __name__=='__main__':unittest.main()
