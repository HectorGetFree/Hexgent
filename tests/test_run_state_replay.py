"""Run增量恢复与本地进程崩溃验收；不访问模型或外部业务服务。"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import json
import os
import subprocess
import sys
import unittest

from hexgent.agent_engine import AgentEngine,RunRequest
from hexgent.config import HexgentConfig
from hexgent.context import ContextManager
from hexgent.loop.loop_state import LoopState
from hexgent.plan import InMemoryPlanStore
from hexgent.session import Session,SessionRestorer
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_registry import TOOL_REGISTRY,RegisteredTool
from hexgent.tool.tool_validation import TOOL_SCHEMAS
from hexgent.session.checkpoint import CheckpointStore


class Call(SimpleNamespace):
    def model_dump(self,**_kwargs):return vars(self)


def response(name=None,cid=None,arguments=None):
    return SimpleNamespace(output=[Call(type='function_call',name=name,call_id=cid,arguments=json.dumps(arguments or {}))] if name else [],
                           output_text='verified',usage={'input_tokens':10,'output_tokens':2})


def scripted(responses):
    values=iter(responses)
    def call(*args,**kwargs):
        if False:yield
        return next(values)
    return call


def crash_child(root):
    root=Path(root);(root/'seed.txt').write_text('seed')
    def commit_then_crash():
        (root/'operation.txt').write_text('one submission')
        os._exit(73)  # 真实进程退出；不写finished，不运行Python finally。
    with patch.dict(TOOL_REGISTRY,{'replay_write':RegisteredTool(commit_then_crash,permission_capability='workspace_write')}),patch.dict(TOOL_SCHEMAS,{'replay_write':{'type':'object','properties':{},'required':[],'additionalProperties':False}}),patch('hexgent.loop.agent_turn.get_model',return_value='offline'),patch('hexgent.loop.agent_turn.call_model',scripted([response('read_file','read-1',{'path':'seed.txt'}),response('replay_write','write-1')])):
        engine=AgentEngine.create(HexgentConfig(),root);engine.session.initialize_runtime_session('replay-test')
        list(engine.run('execute and verify',client=object(),user_confirmation=lambda *a:'allow'))


class RunStateReplayTest(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.context=ContextManager();self.session=Session.persistent(self.root)
        self.session.record_user_query(self.context.append_user_query('original task'))
        self.state=LoopState(self.context,ToolExecutionContext(InMemoryPlanStore(),self.root),model_rounds=2,tool_calls=3,input_tokens=100,output_tokens=20)
        self.session.start_run(limits_enabled=True,plan=None,loop_state=self.state)
        self.checkpoint=self.session.create_checkpoint(context_manager=self.context,plan=None,reason='tool_batch_completed',loop_state=self.state,run_status='active')

    def model_commit(self,calls=None):
        calls=calls if calls is not None else [{'type':'function_call','name':'business_write','call_id':'c1','arguments':'{}'}]
        self.session.invalidate_run_state('main_model')
        self.state=replace(self.state,model_rounds=3,input_tokens=130,output_tokens=25)
        self.context.append_messages(calls,completes_round=not calls)
        self.session.record_context_messages(calls,completes_round=not calls,source='model_response',loop_state=self.state)

    def tool(self,kind,cid='c1',status=None,**extra):
        payload=dict(call_id=cid,tool_name='business_write',recovery_action='verify_before_retry',**extra)
        if status:payload.update(status=status,output='done')
        self.session.record_runtime_event('tool_execution_'+kind,payload)

    def restored(self):return SessionRestorer(self.root).restore(self.session.store.session_id)

    def test_replay_model_and_dispatched_unknown_tool_preserves_budget(self):
        self.model_commit();self.tool('started');self.tool('dispatched')
        restored=self.restored();s=restored.pending_run_state
        self.assertEqual((s.model_rounds,s.tool_calls,s.input_tokens,s.output_tokens),(3,4,130,25))
        self.assertGreaterEqual(s.elapsed_seconds,next(r['payload']['run_state']['elapsed_seconds'] for r in reversed(self.session.store.read_transcript()) if r['type']=='context_messages_appended' and 'run_state' in r['payload']))
        rows=restored.session.store.read_transcript();repair=next(r for r in rows if r['type']=='session_recovery_repaired')
        self.assertEqual(repair['payload']['repaired_calls'][0]['recovery_action'],'verify_before_retry')

    def test_parallel_dispatch_and_deduplicated_finish_count_once(self):
        self.model_commit([{'type':'function_call','name':'business_write','call_id':cid,'arguments':'{}'} for cid in ['c1','c2','c3']])
        for cid in ['c1','c2']:self.tool('started',cid);self.tool('dispatched',cid);self.tool('finished',cid,'success')
        self.tool('started','c3');self.tool('finished','c3','deduplicated')
        self.assertEqual(self.restored().pending_run_state.tool_calls,6)

    def test_denied_invalid_skipped_do_not_consume_tool_budget(self):
        self.model_commit([{'type':'function_call','name':'business_write','call_id':cid,'arguments':'{}'} for cid in ['c1','c2','c3']])
        for cid,status in zip(['c1','c2','c3'],['denied','invalid','skipped']):self.tool('started',cid);self.tool('finished',cid,status)
        self.assertEqual(self.restored().pending_run_state.tool_calls,3)

    def test_finished_output_repaired_without_dispatch_or_double_charge(self):
        self.model_commit();self.tool('started');self.tool('dispatched');self.tool('finished',status='success')
        first=self.restored();second=self.restored()
        self.assertEqual(first.pending_run_state.tool_calls,4);self.assertEqual(second.pending_run_state.tool_calls,4)
        outputs=[x for x in second.context_manager.model_messages() if isinstance(x,dict) and x.get('type')=='function_call_output']
        self.assertEqual(len(outputs),1);self.assertEqual(outputs[0]['output'],'done')

    def test_confirmed_interrupted_call_follows_existing_kernel_count(self):
        self.model_commit();self.tool('started');self.tool('dispatched');self.tool('finished',status='interrupted')
        output=[{'type':'function_call_output','call_id':'c1','output':'interrupted'}]
        self.context.append_messages(output,completes_round=True)
        self.session.record_context_messages(output,completes_round=True,source='tool_results',loop_state=self.state)
        self.assertEqual(self.restored().pending_run_state.tool_calls,3)

    def test_tool_results_anchor_does_not_double_count_prior_delta(self):
        self.model_commit();self.tool('started');self.tool('dispatched');self.tool('finished',status='success')
        self.state=replace(self.state,tool_calls=4,transition='tool_results_ready')
        output=[{'type':'function_call_output','call_id':'c1','output':'done'}]
        self.context.append_messages(output,completes_round=True);self.session.record_context_messages(output,completes_round=True,source='tool_results',loop_state=self.state)
        self.assertEqual(self.restored().pending_run_state.tool_calls,4)

    def test_inflight_model_or_auxiliary_without_committed_usage_refuses(self):
        from hexgent.session.session_restore import _pending_run_state
        self.session.invalidate_run_state('main_model');self.assertIsNone(_pending_run_state(self.checkpoint,self.session.store.read_transcript()))
        self.session.record_run_state(self.state);self.session.invalidate_run_state('context_record_extraction')
        self.assertIsNone(self.restored().pending_run_state)

    def test_auxiliary_complete_state_recovers_even_checkpoint_failed(self):
        self.session.invalidate_run_state('context_record_extraction')
        self.state=replace(self.state,input_tokens=150,output_tokens=30)
        self.session.record_run_state(self.state)
        self.session.record_runtime_event('checkpoint_failed',{'message':'offline injected'})
        self.assertEqual(self.restored().pending_run_state.input_tokens,150)

    def test_unknown_schema_cross_run_and_decreasing_counters_refuse(self):
        from hexgent.session.session_restore import _pending_run_state
        rows=self.session.store.read_transcript()
        for change in [{'run_state_version':2},{'run_id':'other'},{'run_state':replace(self.state,input_tokens=1).to_snapshot().to_dict()}]:
            with self.subTest(change=change):
                r=dict(sequence=rows[-1]['sequence']+1,type='run_state_committed',created_at=rows[-1]['created_at'],payload={**self.session._run_state_payload(self.state),**change})
                self.assertIsNone(_pending_run_state(self.checkpoint,rows+[r]))

    def test_duplicate_dispatch_or_result_refuses(self):
        self.model_commit();self.tool('started');self.tool('dispatched');self.tool('dispatched')
        self.assertIsNone(self.restored().pending_run_state)

    def test_uncommitted_no_tool_response_not_silently_restarted(self):
        from hexgent.session.session_restore import _pending_run_state
        self.model_commit([]);self.assertIsNone(_pending_run_state(self.checkpoint,self.session.store.read_transcript()))
        self.session.record_run_state(self.state,run_status='terminal');self.assertIsNone(self.restored().pending_run_state)

    def test_new_run_not_bound_to_previous_terminal_checkpoint(self):
        self.session.finish_run(status='completed',stop_reason=None,elapsed_seconds=1,output_text='done',plan=None,context_manager=self.context,loop_state=self.state)
        self.session.record_user_query(self.context.append_user_query('next task'))
        new=replace(self.state,model_rounds=0,tool_calls=0,input_tokens=0,output_tokens=0)
        self.session.start_run(limits_enabled=True,plan=None,loop_state=new)
        self.assertEqual(self.restored().pending_run_state.model_rounds,0)

    def test_legacy_stateful_suffix_stays_refused(self):
        from hexgent.session.session_restore import _pending_run_state
        rows=self.session.store.read_transcript()
        for r in rows:r['payload'].pop('run_state_version',None)
        rows.append(dict(sequence=rows[-1]['sequence']+1,type='model_response_completed',payload={}))
        self.assertIsNone(_pending_run_state(self.checkpoint,rows))

    def test_corrupted_checkpoint_falls_back_to_new_journal(self):
        self.model_commit();self.tool('started');self.tool('dispatched')
        store=CheckpointStore(self.session.store.session_dir)
        (store.directory/(self.checkpoint.checkpoint_id+'.json')).write_text('broken')
        self.assertEqual(self.restored().pending_run_state.tool_calls,4)

    def test_paused_checkpoint_does_not_charge_restore_downtime(self):
        from hexgent.session.session_restore import _pending_run_state
        cp=replace(self.checkpoint,run_status='continuable');rows=self.session.store.read_transcript()
        diagnostic=dict(sequence=rows[-1]['sequence']+1,type='session_recovered',payload={},created_at='2099-01-01T00:00:00+00:00')
        state=_pending_run_state(cp,rows+[diagnostic])
        self.assertEqual(state.elapsed_seconds,cp.run_state['elapsed_seconds'])

    def test_frozen_control_state_and_nonfinite_elapsed(self):
        from hexgent.loop.loop_state import RunStateSnapshot
        self.state=replace(self.state,mode='finalizing',transition='budget_finalization',stop_reason='max_tool_calls',plan_completion_nudges=1,reactive_compaction_attempted=True)
        self.session.record_run_state(self.state)
        restored=self.restored().pending_run_state
        self.assertEqual((restored.mode,restored.transition,restored.stop_reason,restored.plan_completion_nudges,restored.reactive_compaction_attempted),('finalizing','budget_finalization','max_tool_calls',1,True))
        for bad in [float('nan'),float('inf'),-1]:
            with self.assertRaises(ValueError):RunStateSnapshot.from_dict({**self.state.to_snapshot().to_dict(),'elapsed_seconds':bad})

    def test_child_crash_budget_is_not_refilled_by_continue(self):
        from hexgent.loop.limits import LoopLimits
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            child=subprocess.run([sys.executable,'-m','tests.test_run_state_replay','--crash-child',tmp],cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=20)
            self.assertEqual(child.returncode,73,child.stderr.decode())
            engine=AgentEngine.restore(HexgentConfig(loop=LoopLimits(max_tool_calls=2)),root,'replay-test')
            requests=[]
            def finalization(*args,**kwargs):
                requests.append(kwargs)
                if False:yield
                return response()
            with patch('hexgent.loop.agent_turn.get_model',return_value='offline'),patch('hexgent.loop.agent_turn.call_model',finalization):
                list(engine.run(RunRequest.continue_run(),client=object()))
            self.assertEqual(len(requests),1);self.assertNotIn('tools',requests[0])
            records=engine.session.store.read_transcript()
            finished=[r for r in records if r['type']=='agent_run_finished'][-1]
            self.assertEqual(finished['payload']['stop_reason'],'max_tool_calls')
            self.assertEqual((root/'operation.txt').read_text(),'one submission')

    def test_actual_child_exit_after_commit_then_restore_continue(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            child=subprocess.run([sys.executable,'-m','tests.test_run_state_replay','--crash-child',tmp],cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=20)
            self.assertEqual(child.returncode,73,child.stderr.decode())
            self.assertEqual((root/'operation.txt').read_text(),'one submission')
            engine=AgentEngine.restore(HexgentConfig(),root,'replay-test')
            self.assertTrue(engine.has_continuable_run)
            s=engine._pending_run_state
            self.assertEqual((s.model_rounds,s.tool_calls,s.input_tokens,s.output_tokens),(2,2,20,4))
            outputs=[m for m in engine.context_manager.model_messages() if isinstance(m,dict) and m.get('call_id')=='write-1' and m.get('type')=='function_call_output']
            self.assertEqual(len(outputs),1);self.assertIn('核验',outputs[0]['output'])
            with patch('hexgent.loop.agent_turn.get_model',return_value='offline'),patch('hexgent.loop.agent_turn.call_model',scripted([response('read_file','verify-1',{'path':'operation.txt'}),response()])):
                list(engine.run(RunRequest.continue_run(),client=object(),user_confirmation=lambda *a:'allow'))
            self.assertEqual((root/'operation.txt').read_text(),'one submission')
            rows=engine.session.store.read_transcript()
            self.assertEqual(sum(r['type']=='tool_execution_dispatched' and r['payload']['tool_name']=='replay_write' for r in rows),1)
            self.assertFalse(AgentEngine.restore(HexgentConfig(),root,'replay-test').has_continuable_run)


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--crash-child':crash_child(sys.argv[2])
    else:unittest.main()
