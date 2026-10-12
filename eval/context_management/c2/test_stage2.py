"""Stage 2专属门禁测试；没有真实SDK create。"""
from unittest import TestCase
from unittest.mock import patch
from openai import APIConnectionError
from eval.context_management.c2.stage2_live import ORDER, Stage2Guard
from eval.context_management.c2_dispatch_guard_repair.guard import DispatchGuard
from eval.context_management.c2_prep_3.budget import TokenBudget, require_stage_approval
from eval.context_management.c2_prep_3.runner import policy,load_rounds
from eval.context_management.c2_prep_3.prepare import ROOT,read_json


class Stage2Tests(TestCase):
    def setUp(self):
        self.manifest=read_json(ROOT/'smoke_manifest.json')
        self.policy=policy(self.manifest,2)
        self.budget=TokenBudget(self.policy)
    def test_order_and_input_hashes(self):
        entries=[e for e in self.manifest['fixtures'] if e['stage']==2]
        self.assertEqual([e['fixture_id'] for e in entries],ORDER)
        for e in entries:self.assertTrue(load_rounds(e)[0])
    def test_independent_stage2_budget(self):
        self.assertEqual((self.policy.max_compaction_calls,self.policy.max_total_reserved_input,self.policy.max_total_reserved_output),(20,524288,40000))
        self.assertEqual((self.budget.calls,self.budget.inputs,self.budget.outputs),(0,0,0))
    def test_approval_requires_actual_prior_run(self):
        with self.assertRaises(Exception):require_stage_approval(2)
        with self.assertRaises(Exception):require_stage_approval(2,dict(approved=True,source_type='HUMAN_REVIEW',source_reference='trusted',stage1_run_id='wrong'),dict(run_id='real',audit_valid=True))
    def test_authorization_error_sets_shared_stop(self):
        guard=Stage2Guard(self.budget)
        with patch.object(DispatchGuard,'authorize',side_effect=OSError('persist failed')):
            with self.assertRaises(OSError):guard.authorize(None,{})
        self.assertEqual(self.budget.halted_reason,'GUARD_AUTHORIZATION_FAILURE')
    def test_consumed_persist_failure_still_stops(self):
        guard=Stage2Guard(self.budget)
        with patch.object(DispatchGuard,'dispatch',side_effect=OSError('persist failed')):
            with self.assertRaises(OSError):guard.dispatch(None,{'reservation':{'consumed':True}}, {})
        self.assertEqual(self.budget.halted_reason,'GUARD_DISPATCH_FAILURE')
    def test_provider_error_keeps_production_retry_contract(self):
        guard=Stage2Guard(self.budget)
        with patch.object(DispatchGuard,'dispatch',side_effect=APIConnectionError(request=None)):
            with self.assertRaises(APIConnectionError):guard.dispatch(None,{}, {})
        self.assertIsNone(self.budget.halted_reason)
