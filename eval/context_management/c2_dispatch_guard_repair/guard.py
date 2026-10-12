"""Eval 派发授权；不更改 Production Span、请求或状态转换。"""
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from uuid import uuid4
from urllib.parse import urlsplit
from unittest.mock import patch
import json

from hexgent import observation
from hexgent.context.context_compaction import COMPACTION_INSTRUCTIONS
from eval.context_management.c1_independent import plain
from eval.context_management.c2_prep_2.control import Trace, TraceStream, BudgetDenied


@dataclass(frozen=True)
class Binding:
    span_id: str
    execution_id: str
    session_id: str
    request_type: str
    logical_attempt: int
    physical_attempt: int = 1


def request_hash(request):
    return sha256(json.dumps(plain(request),ensure_ascii=False,sort_keys=True).encode()).hexdigest()


class DispatchGuard:
    """1. Span 生命周期、额度预留与一次性授权共享同一锁。"""
    def __init__(self,budget):
        self.budget=budget; self.lock=budget.lock
        self.binding=ContextVar('eval_provider_binding_'+uuid4().hex,default=None)
        self.spans={}; self.claims={}; self.authorizations={}; self.consumed=set()

    def begin(self,kind,**fields):
        handle=observation.begin_span(kind,**fields)
        if kind=='provider':
            obs=handle[0]
            if obs is None: raise BudgetDenied('SPAN_OBSERVER_MISSING')
            attempt=fields.get('attempt',1)
            if not isinstance(attempt,int) or attempt<1: raise BudgetDenied('ATTEMPT_INVALID')
            b=Binding(handle[2],obs.execution_id,obs.store.session_id,fields.get('request_type'),attempt)
            with self.lock:
                self.spans[b.span_id]={'binding':b,'observer':obs,'open':True}
            self.binding.set(b)
        return handle

    def end(self,handle,**fields):
        with self.lock:
            if handle[2] in self.spans: self.spans[handle[2]]['open']=False
        observation.end_span(handle,**fields)
        b=self.binding.get()
        if b and b.span_id==handle[2]: self.binding.set(None)

    @contextmanager
    def instrument(self):
        # 2. 仅包裹实际 begin/end 入口；底层 Production 观测仍原样执行。
        with ExitStack() as stack:
            for module in ('hexgent.context.context_compaction','hexgent.loop.call_model'):
                stack.enter_context(patch(module+'.begin_span',self.begin))
                stack.enter_context(patch(module+'.end_span',self.end))
            yield

    @contextmanager
    def new_physical_attempt(self):
        """显式 Eval 重试许可；只允许上次终态失败，非 SDK 隐藏重试。"""
        with self.lock:
            b=self.binding.get()
            if b is None: raise BudgetDenied('SPAN_BINDING_MISSING')
            previous=self.claims.get((b.span_id,b.physical_attempt))
            if previous is None or previous['outcome'] not in ('ERROR','TIMEOUT','CANCELLED'):
                raise BudgetDenied('RETRY_NOT_AUTHORIZED')
            token=self.binding.set(replace(b,physical_attempt=b.physical_attempt+1))
        try: yield
        finally: self.binding.reset(token)

    def validate(self,trace,request,b):
        obs=observation.current_observation.get()
        if b is None: raise BudgetDenied('SPAN_BINDING_MISSING')
        span=self.spans.get(b.span_id)
        if span is None: raise BudgetDenied('SPAN_UNKNOWN')
        if not span['open']: raise BudgetDenied('SPAN_ENDED')
        if obs is not span['observer'] or obs.execution_id!=b.execution_id:
            raise BudgetDenied('EXECUTION_MISMATCH')
        if not obs.available: raise BudgetDenied('OBSERVATION_UNAVAILABLE')
        if trace.session is None or obs.store is not trace.session.store or b.session_id!=trace.session.store.session_id:
            raise BudgetDenied('SESSION_MISMATCH')
        if not getattr(trace.session,'_active_run_id',None): raise BudgetDenied('RUN_BINDING_MISSING')
        original=span['binding']
        if replace(b,physical_attempt=1)!=original: raise BudgetDenied('ATTEMPT_OR_BINDING_MISMATCH')
        kind='COMPACTION' if request.get('instructions','').startswith(COMPACTION_INSTRUCTIONS) else 'MAIN_AGENT'
        if b.request_type!=kind: raise BudgetDenied('REQUEST_TYPE_MISMATCH')
        if request.get('model')!=trace.model: raise BudgetDenied('MODEL_MISMATCH')
        if trace.native.max_retries!=0: raise BudgetDenied('SDK_RETRY_NOT_DISABLED')
        if b.physical_attempt>1:
            prior=self.claims.get((b.span_id,b.physical_attempt-1))
            if prior is None or prior['outcome'] not in ('ERROR','TIMEOUT','CANCELLED'):
                raise BudgetDenied('RETRY_NOT_AUTHORIZED')

    def authorize(self,trace,request):
        with self.lock:
            b=self.binding.get(); self.validate(trace,request,b)
            key=(b.span_id,b.physical_attempt)
            if key in self.claims: raise BudgetDenied('ATTEMPT_ALREADY_CLAIMED')
            reservation=trace.budget.reserve(request)
            call_id=uuid4().hex; attempt_id=uuid4().hex
            reservation.update(provider_call_id=call_id,span_id=b.span_id,execution_id=b.execution_id,
                session_id=b.session_id,logical_attempt=b.logical_attempt,physical_attempt=b.physical_attempt,
                provider_attempt_id=attempt_id,request_hash=request_hash(request),consumed=False)
            kind='COMPACTION' if b.request_type=='COMPACTION' else 'MAIN'
            row=dict(eval_run_id=trace.eval_run_id,fixture_id=trace.fixture_id,session_id=b.session_id,
                agent_run_id=trace.session._active_run_id,execution_id=b.execution_id,span_id=b.span_id,
                logical_attempt=b.logical_attempt,physical_attempt=b.physical_attempt,provider_attempt_id=attempt_id,
                compaction_attempt_id=b.span_id if kind=='COMPACTION' else None,
                provider_call_id=call_id,call_type=kind,retry_index=b.logical_attempt-1,
                provider=urlsplit(str(trace.native.base_url)).hostname,model=request['model'],
                request=plain(request),request_hash=request_hash(request),reservation=reservation,
                response=None,stream_events=[],usage=None,input_tokens=None,output_tokens=None,
                usage_complete=False,usage_status='MISSING',outcome='IN_FLIGHT',latency_ms=None,
                is_real_compaction_call=trace.real_mode and kind=='COMPACTION',
                transport_mode=trace.transport_mode,actual_network_dispatch=False,
                monetary_cost_status='MONETARY_COST_NOT_EVALUATED')
            self.claims[key]=row
            self.authorizations[reservation['reservation_id']]=(trace,b,row)
            trace.state['calls'].append(row)
            trace.persist()
            return row

    def dispatch(self,trace,row,request):
        # 3. 显式传递当前 Call 授权，不从 calls[-1] 获取其他请求许可。
        with self.lock:
            rid=row['reservation']['reservation_id']
            auth=self.authorizations.get(rid)
            if auth is None or auth[0] is not trace or auth[2] is not row:
                raise BudgetDenied('RESERVATION_OWNER_MISMATCH')
            b=auth[1]; self.validate(trace,request,b)
            if self.binding.get()!=b: raise BudgetDenied('CURRENT_ATTEMPT_MISMATCH')
            if row['request_hash']!=request_hash(request): raise BudgetDenied('RESERVATION_REQUEST_MISMATCH')
            if rid in self.consumed: raise BudgetDenied('RESERVATION_ALREADY_CONSUMED')
            self.consumed.add(rid)
            row['reservation']['consumed']=True
            row['actual_network_dispatch']=trace.real_mode and trace.transport_mode=='NETWORK' and row['call_type']=='COMPACTION'
            trace.persist()  # 持久化失败不得触及底层；预留仍保守保留。
        delegate=trace.native.responses if row['call_type']=='COMPACTION' else trace.main_fake
        return delegate.create(**request)


class GuardedTrace(Trace):
    def __init__(self,delegate,destination,budget,*,eval_run_id,fixture_id,session=None,state=None,main_fake=None,
                 guard=None,real_mode=False,transport_mode='FAKE',model='gpt-6.1-sol'):
        self.native=delegate; self.main_fake=main_fake
        self.guard=guard or DispatchGuard(budget.owner)
        self.real_mode=real_mode; self.transport_mode=transport_mode; self.model=model
        super().__init__(delegate,destination,budget,eval_run_id=eval_run_id,fixture_id=fixture_id,session=session,state=state)
        self.responses=GuardedCreate(self)
    def with_options(self,*args,**kwargs): return self._clone(self.native.with_options(*args,**kwargs))
    def copy(self,*args,**kwargs): return self._clone(self.native.copy(*args,**kwargs))
    def _clone(self,native):
        return GuardedTrace(native,self.destination,self.budget,eval_run_id=self.eval_run_id,fixture_id=self.fixture_id,
            session=self.session,state=self.state,main_fake=self.main_fake,guard=self.guard,real_mode=self.real_mode,
            transport_mode=self.transport_mode,model=self.model)
    def persist(self):
        with self.guard.lock: super().persist()
    def finish(self,row,started,outcome,reason=None):
        with self.guard.lock:
            if row['outcome']!='IN_FLIGHT': return
            row.update(outcome=outcome,latency_ms=(perf_counter()-started)*1000,reason=reason)
            row['reservation']['status']='RETAINED_CONSERVATIVE_RESERVATION'
            self.persist()


class GuardedCreate:
    def __init__(self,trace): self.trace=trace
    def create(self,**request):
        t=self.trace; row=None; started=perf_counter()
        try:
            row=t.guard.authorize(t,request)
            raw=t.guard.dispatch(t,row,request)
        except BaseException as error:
            if row is not None: t.fail(row,started,error)
            with t.guard.lock:
                t.state['denied'].append(dict(reason=str(error),type=type(error).__name__,request=plain(request)))
                t.persist()
            raise
        if request.get('stream'): return TraceStream(raw,t,row,started)
        t.capture_response(row,raw); t.finish(row,started,'SUCCESS')
        return raw
