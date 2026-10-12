"""Eval 独立调用账本及保守额度预留，不改变 Production Provider。"""
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from threading import RLock
from time import perf_counter
from uuid import uuid4
from urllib.parse import urlsplit

from hexgent.observation import current_observation
from hexgent.context.context_compaction import COMPACTION_INSTRUCTIONS
from eval.context_management.c1_independent import plain, save


class BudgetDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class Policy:
    max_provider_calls: int
    max_input_tokens_per_call: int
    max_output_tokens_per_call: int
    max_total_input_tokens: int
    max_total_output_tokens: int
    max_estimated_cost: str
    input_price_per_million: str | None
    output_price_per_million: str | None
    price_source: str
    confirmed: bool = False
    dry_only: bool = True


class Budget:
    """1. 派发前预留；在途、失败与 Usage 缺失均不释放额度。"""
    def __init__(self, policy, input_bound=None):
        self.policy, self.input_bound = policy, input_bound
        self.lock = RLock()
        self.calls = self.inputs = self.outputs = 0
        self.cost = Decimal('0')
        self.reservations, self.denials = [], []
        self.halted_reason = None

    def reserve(self, request):
        with self.lock:
            p = self.policy
            reason = None
            if self.halted_reason:
                reason = self.halted_reason
            elif not p.confirmed or p.input_price_per_million is None or p.output_price_per_million is None:
                reason = 'COST_CONFIGURATION_UNCONFIRMED'
            elif self.input_bound is None:
                reason = 'INPUT_TOKEN_BOUND_UNKNOWN'
            if reason:
                self.denials.append(reason)
                raise BudgetDenied(reason)
            rates = [Decimal(p.input_price_per_million), Decimal(p.output_price_per_million), Decimal(p.max_estimated_cost)]
            if any(not value.is_finite() or value < 0 for value in rates):
                raise BudgetDenied('INVALID_PRICE_CONFIGURATION')
            i = self.input_bound(request)
            o = request.get('max_output_tokens')
            if not isinstance(i, int) or i < 0 or not isinstance(o, int) or o < 1:
                reason = 'TOKEN_BOUND_UNKNOWN'
            elif i > p.max_input_tokens_per_call:
                reason = 'PER_CALL_INPUT_LIMIT'
            elif o > p.max_output_tokens_per_call:
                reason = 'PER_CALL_OUTPUT_LIMIT'
            elif self.calls + 1 > p.max_provider_calls:
                reason = 'PROVIDER_CALL_LIMIT'
            elif self.inputs + i > p.max_total_input_tokens:
                reason = 'TOTAL_INPUT_LIMIT'
            elif self.outputs + o > p.max_total_output_tokens:
                reason = 'TOTAL_OUTPUT_LIMIT'
            else:
                cost = (Decimal(i) * Decimal(p.input_price_per_million) + Decimal(o) * Decimal(p.output_price_per_million)) / Decimal(1000000)
                if self.cost + cost > Decimal(p.max_estimated_cost):
                    reason = 'TOTAL_COST_LIMIT'
            if reason:
                self.denials.append(reason)
                raise BudgetDenied(reason)
            self.calls += 1
            self.inputs += i
            self.outputs += o
            self.cost += cost
            row = dict(reservation_id=uuid4().hex, input_upper_bound=i, output_upper_bound=o,
                       reserved_cost=str(cost), status='IN_FLIGHT')
            self.reservations.append(row)
            return row


class TraceStream:
    def __init__(self, raw, trace, row, started):
        self.raw, self.trace, self.row, self.started = raw, trace, row, started
        self.iterator = iter(raw)
    def __getattr__(self, name):
        return getattr(self.raw, name)
    def __iter__(self):
        return self
    def __next__(self):
        try:
            event = next(self.iterator)
        except StopIteration:
            if self.row['outcome'] == 'IN_FLIGHT':
                self.trace.finish(self.row, self.started, 'ERROR', 'STREAM_NO_TERMINAL')
            raise
        except BaseException as error:
            self.trace.fail(self.row, self.started, error)
            raise
        self.row['stream_events'].append(plain(event))
        kind = getattr(event, 'type', '')
        if kind in {'response.completed', 'response.failed', 'response.incomplete'}:
            self.trace.capture_response(self.row, event.response)
            self.trace.finish(self.row, self.started, 'SUCCESS' if kind == 'response.completed' else 'ERROR', kind)
        return event
    def close(self):
        try:
            close = getattr(self.raw, 'close', None)
            if close:
                close()
        finally:
            if self.row['outcome'] == 'IN_FLIGHT':
                self.trace.finish(self.row, self.started, 'CANCELLED', 'CONSUMER_CLOSE')
    def __enter__(self):
        self.raw.__enter__()
        return self
    def __exit__(self, *args):
        try:
            return self.raw.__exit__(*args)
        finally:
            if self.row['outcome'] == 'IN_FLIGHT':
                self.trace.finish(self.row, self.started, 'CANCELLED', 'CONTEXT_EXIT')


class Trace:
    """2. create 紧贴 SDK 调用边界；生产 span 直接从当前观测日志获取。"""
    def __init__(self, delegate, destination, budget, *, eval_run_id, fixture_id, session=None, state=None):
        self.delegate, self.destination, self.budget = delegate, Path(destination), budget
        self.eval_run_id, self.fixture_id, self.session = eval_run_id, fixture_id, session
        self.state = state if state is not None else {'calls': [], 'denied': [], 'lock': RLock()}
        self.responses = TraceResponses(self)
    def __getattr__(self, name):
        return getattr(self.delegate, name)
    def with_options(self, *args, **kwargs):
        return Trace(self.delegate.with_options(*args, **kwargs), self.destination, self.budget,
                     eval_run_id=self.eval_run_id, fixture_id=self.fixture_id, session=self.session, state=self.state)
    def copy(self, *args, **kwargs):
        return Trace(self.delegate.copy(*args, **kwargs), self.destination, self.budget,
                     eval_run_id=self.eval_run_id, fixture_id=self.fixture_id, session=self.session, state=self.state)
    def persist(self):
        save(self.destination, {'calls': self.state['calls'], 'denied': self.state['denied']})
    def capture_response(self, row, value):
        value = plain(value)
        row['response'] = value
        usage = value.get('usage') or {}
        row['usage'] = usage or None
        row['input_tokens'], row['output_tokens'] = usage.get('input_tokens'), usage.get('output_tokens')
        row['usage_complete'] = row['input_tokens'] is not None and row['output_tokens'] is not None
        row['usage_status'] = 'COMPLETE' if row['usage_complete'] else 'MISSING'
    def finish(self, row, started, outcome, reason=None):
        with self.state['lock']:
            if row['outcome'] != 'IN_FLIGHT':
                return
            row.update(outcome=outcome, latency_ms=(perf_counter()-started)*1000, reason=reason)
            row['reservation']['status'] = 'CONSERVATIVELY_CHARGED'
            if not self.budget.policy.dry_only:
                if not row['usage_complete']:
                    self.budget.halted_reason = 'COST_OBSERVABILITY_BLOCKED'
                elif row['input_tokens'] > row['reservation']['input_upper_bound'] or row['output_tokens'] > row['reservation']['output_upper_bound']:
                    self.budget.halted_reason = 'PROVIDER_USAGE_EXCEEDED_BOUND'
            self.persist()
    def fail(self, row, started, error):
        outcome = 'TIMEOUT' if isinstance(error, TimeoutError) or 'timeout' in type(error).__name__.lower() else 'CANCELLED' if isinstance(error, (KeyboardInterrupt, GeneratorExit)) else 'ERROR'
        self.finish(row, started, outcome, dict(type=type(error).__name__, message=str(error)))


class TraceResponses:
    def __init__(self, trace):
        self.trace = trace
    def __getattr__(self, name):
        return getattr(self.trace.delegate.responses, name)
    def create(self, *args, **request):
        t = self.trace
        obs = current_observation.get()
        starts = []
        if obs:
            path = obs.store.session_dir / 'observation.jsonl'
            if path.exists():
                starts = [json.loads(line) for line in path.read_text().splitlines()
                          if json.loads(line).get('event') == 'provider_started'
                          and json.loads(line).get('execution_id') == obs.execution_id]
        native = starts[-1] if starts else {}
        # 3. SDK 内部重试不可见；实时客户端必须 Retry=0，未知上界拒绝派发。
        if not t.budget.policy.dry_only and getattr(t.delegate, 'max_retries', None) != 0:
            raise BudgetDenied('SDK_RETRY_NOT_DISABLED')
        try:
            reservation = t.budget.reserve(request)
        except BudgetDenied as error:
            t.state['denied'].append(dict(reason=str(error), request=plain(request),span_id=native.get('span_id','NOT_AVAILABLE')))
            t.persist()
            raise
        kind = 'COMPACTION' if request.get('instructions', '').startswith(COMPACTION_INSTRUCTIONS) else 'MAIN'
        request_plain = plain(request)
        with t.state['lock']:
            row = dict(eval_run_id=t.eval_run_id, fixture_id=t.fixture_id,
                session_id=getattr(getattr(t.session, 'store', None), 'session_id', 'NOT_AVAILABLE'),
                agent_run_id=getattr(t.session, '_active_run_id', None) or 'NOT_AVAILABLE',
                execution_id=getattr(obs, 'execution_id', 'NOT_AVAILABLE'),
                span_id=native.get('span_id', 'NOT_AVAILABLE'),
                compaction_attempt_id=native.get('span_id', 'NOT_AVAILABLE') if kind == 'COMPACTION' else None,
                provider_call_id=uuid4().hex, call_type=kind, provider=urlsplit(str(getattr(t.delegate, 'base_url', ''))).hostname or 'OFFLINE',
                model=request.get('model'), retry_index=native.get('attempt', 1)-1,
                request_hash=sha256(json.dumps(request_plain, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                request=request_plain, response=None, stream_events=[], input_tokens=None, output_tokens=None,
                usage_complete=False, usage_status='MISSING', latency_ms=None, outcome='IN_FLIGHT', reservation=reservation)
            t.state['calls'].append(row)
        started = perf_counter()
        try:
            raw = t.delegate.responses.create(*args, **request)
        except BaseException as error:
            t.fail(row, started, error)
            raise
        if request.get('stream'):
            return TraceStream(raw, t, row, started)
        t.capture_response(row, raw)
        t.finish(row, started, 'SUCCESS')
        return raw


def reconcile(trace, events):
    """4. 未实际派发的预算拒绝 span 与已派发调用分开，不伪造 Provider Call。"""
    starts = {e['span_id']: e for e in events if e['event'] == 'provider_started'}
    finishes = {e['span_id']: e for e in events if e['event'] == 'provider_finished'}
    calls = trace.state['calls']
    ids = [c['span_id'] for c in calls]
    valid = len(ids) == len(set(ids)) and all(i in starts and i in finishes for i in ids)
    valid &= all(starts[c['span_id']].get('request_type') == ('COMPACTION' if c['call_type']=='COMPACTION' else 'MAIN_AGENT') for c in calls if c['span_id'] in starts)
    extra = [s for s in starts if s not in ids]
    denial_spans={row.get('span_id') for row in trace.state['denied']}
    unexplained = [s for s in extra if s not in denial_spans and not any(
        marker in str(finishes.get(s,{})) for marker in ('CONTEXT_BUDGET_EXCEEDED','PINNED_CONSTRAINTS_LIMIT_EXCEEDED'))]
    valid &= not unexplained
    return dict(pass_=valid, matched_calls=len(calls), non_dispatch_spans=extra,
                unexplained_spans=unexplained,
                totals={k: dict(calls=len([c for c in calls if c['call_type']==k]),
                    input_tokens=sum(c['input_tokens'] for c in calls if c['call_type']==k) if all(c['usage_complete'] for c in calls if c['call_type']==k) else None,
                    output_tokens=sum(c['output_tokens'] for c in calls if c['call_type']==k) if all(c['usage_complete'] for c in calls if c['call_type']==k) else None)
                    for k in ('MAIN', 'COMPACTION')})
