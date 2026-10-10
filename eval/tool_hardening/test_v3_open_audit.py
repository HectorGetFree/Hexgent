"""简短自然任务的口径与保留规则；不请求Provider。"""

from pathlib import Path
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from .v3_open_audit import QUERY, manual_evidence, summarize, trajectory, select_attempt, run_counted
from .v2_runner import digest


class OpenAuditTests(unittest.TestCase):
    def test_production_profile_uses_default_budgets_and_auto_approve(self):
        from dataclasses import asdict
        from hexgent.config import HexgentConfig
        from .v3_smoke import production_audit_config
        on, off = production_audit_config(True), production_audit_config(False)
        self.assertEqual(on.loop, HexgentConfig().loop)
        self.assertEqual(on.permission.default_mode, 'auto_approve')
        left, right = asdict(on), asdict(off)
        self.assertTrue(left['tool_parallel'].pop('enabled'))
        self.assertFalse(right['tool_parallel'].pop('enabled'))
        self.assertEqual(left, right)

    def test_counted_mode_continues_failures_and_never_exceeds_five_per_side(self):
        with TemporaryDirectory() as temp:
            output = Path(temp)
            manifest = output/'manifest.json'
            manifest.write_text('{}')
            def complete_failure(argv, **kwargs):
                directory = Path(argv[argv.index('--worker-output')+1])
                variant = argv[argv.index('--worker-variant')+1]
                directory.mkdir()
                metric = directory/'metrics.json'
                metric.write_text(json.dumps({'variant': variant, 'total_agent_latency_ms': 100,
                    'stop_reasons': ['PROVIDER_FAILURE', 'AGENT_NOT_COMPLETED']}))
                (directory/'evidence-index.json').write_text(json.dumps({'files': {'metrics.json': digest(metric)}}))
                return SimpleNamespace(returncode=0)
            with patch('eval.tool_hardening.v3_open_audit.validate'), patch(
                'eval.tool_hardening.v3_open_audit.subprocess.run', side_effect=complete_failure) as execute:
                run_counted(output, manifest, {}, 5)
                self.assertEqual(execute.call_count, 10)
                run_counted(output, manifest, {}, 5)
                self.assertEqual(execute.call_count, 10)
            report = json.loads((output/'counted-report-manifest.json').read_text())
            for variant in ('ON', 'OFF'):
                self.assertEqual(report['summary'][variant]['attempt_count'], 5)
                self.assertEqual(report['summary'][variant]['failure_count'], 5)
            self.assertEqual(report['status'], 'BUDGET_COMPLETED_MANUAL_REVIEW_PENDING')
    def make_attempt(self, directory, failed):
        directory.mkdir()
        metric = directory/'metrics.json'
        metric.write_text(json.dumps({'stop_reasons': ['PROVIDER_FAILURE'] if failed else []}))
        (directory/'evidence-index.json').write_text(json.dumps({'files': {'metrics.json': digest(metric)}}))

    def test_explicit_retry_preserves_original_and_allocates_new_attempt(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)/'ON'
            self.make_attempt(base, True)
            before = (base/'metrics.json').read_bytes()
            selected, history = select_attempt(base, resume=True, retry_failed=True)
            self.assertEqual(selected.name, 'ON-attempt-2')
            self.assertFalse(selected.exists())
            self.assertEqual(len(history), 1)
            self.assertEqual((base/'metrics.json').read_bytes(), before)
            self.assertEqual(select_attempt(base, resume=True, retry_failed=False)[0], base)

    def test_successful_retry_is_reused_not_executed_again(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)/'ON'
            self.make_attempt(base, True)
            retry = base.with_name('ON-attempt-2')
            self.make_attempt(retry, False)
            selected, history = select_attempt(base, resume=True, retry_failed=True)
            self.assertEqual(selected, retry)
            self.assertEqual(len(history), 2)

    def test_incomplete_attempt_and_changed_evidence_stop(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)/'ON'
            base.mkdir()
            with self.assertRaisesRegex(RuntimeError, 'INCOMPLETE_RUN'):
                select_attempt(base, resume=True, retry_failed=True)
        with TemporaryDirectory() as temp:
            base = Path(temp)/'OFF'
            self.make_attempt(base, True)
            (base/'metrics.json').write_text('{}')
            with self.assertRaisesRegex(RuntimeError, 'SAVED_EVIDENCE_CHANGED'):
                select_attempt(base, resume=True, retry_failed=True)

    def test_query_is_exactly_user_contract(self):
        self.assertEqual(QUERY, '请帮我审计一下 `hexgent/tool` 模块的设计')

    def test_freeform_answer_is_not_reported_as_json_oracle_failure_or_pass(self):
        evidence = manual_evidence({}, [], '设计主要分为调度与执行。', Path('/tmp'))
        self.assertIsNone(evidence['mechanical_pass'])
        self.assertIsNone(evidence['task_oracle_pass'])
        self.assertEqual(evidence['evaluation_mode'], 'MANUAL_REVIEW')
        self.assertEqual(evidence['errors'], [])

    def test_failed_and_incomplete_pairs_are_retained(self):
        def metric(stop):
            return {'metrics': {'stop_reasons': stop, 'total_agent_latency_ms': 100,
                               'provider_failures': [], 'usage_completeness': 'INCOMPLETE', 'provider_usage': {},
                               'main_provider_latency_ms': 80, 'auxiliary_provider_latency_ms': 8,
                               'total_tool_execution_latency_ms': 1, 'main_provider_requests': 1,
                               'tool_calls_total': 2, 'parallel_eligible_batch_count': 1,
                               'actual_parallel_batch_count': 1, 'tool_calls_per_response': [2]}}
        result = summarize([{'pair': 1, 'runs': {'ON': metric([]), 'OFF': metric(['PROVIDER_FAILURE'])}},
                            {'pair': 2, 'runs': {'ON': metric([])}}])
        self.assertEqual(result['completed_pairs'], 1)
        self.assertEqual(result['valid_pairs'], 0)
        self.assertEqual(len(result['excluded_pairs']), 2)
        self.assertEqual(result['ON']['observed_runs'], 2)
        self.assertEqual(result['OFF']['observed_runs'], 1)
        self.assertNotIn('median_off_over_on_agent_latency', result)

    def test_trajectory_does_not_merge_response_boundaries(self):
        calls = [{'name': 'read_file', 'arguments': '{"path":"a"}'},
                 {'name': 'glob', 'arguments': '{"pattern":"*"}'}]
        split = {'turns': [{'request_type': 'MAIN_AGENT', 'tool_calls': [c]} for c in calls]}
        merged = {'turns': [{'request_type': 'MAIN_AGENT', 'tool_calls': calls}]}
        self.assertNotEqual(trajectory(split, Path('/tmp')), trajectory(merged, Path('/tmp')))
