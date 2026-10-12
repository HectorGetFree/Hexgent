"""Smoke Token额度：复用原Trace边界，只移除货币条件，不承诺精确Tokenizer上界。"""
from dataclasses import dataclass
from math import ceil
from threading import RLock
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

from eval.context_management.c2_prep_2.control import BudgetDenied, Trace, TraceResponses
from hexgent.context.context_compaction import COMPACTION_INSTRUCTIONS
from hexgent.utils.token_estimator import estimate_request_tokens


@dataclass(frozen=True)
class TokenPolicy:
    max_compaction_calls: int
    max_total_reserved_input: int
    max_total_reserved_output: int
    fixtures: dict
    input_margin_ratio: float = 1.25
    dry_only: bool = False  # 复用Trace时，Fake也必须核对Retry=0。


class TokenBudget:
    """1. 共享锁内预留，不按实际Usage返还失败或在途额度。"""
    def __init__(self, policy):
        self.policy=policy
        self.lock=RLock()
        self.calls=self.inputs=self.outputs=0
        self.by_fixture={}
        self.reservations=[]
        self.halted_reason=None
    def bind(self,fixture_id):
        return BoundBudget(self,fixture_id)
    def reserve(self,fixture_id,request):
        with self.lock:
            if self.halted_reason:
                raise BudgetDenied(self.halted_reason)
            if fixture_id not in self.policy.fixtures:
                raise BudgetDenied('FIXTURE_NOT_IN_MANIFEST')
            if not request.get('instructions','').startswith(COMPACTION_INSTRUCTIONS):
                return dict(reservation_id=uuid4().hex,status='FAKE_MAIN',
                    input_upper_bound=0,output_upper_bound=0,charged_compaction=False)
            p=self.policy; limits=p.fixtures[fixture_id]
            estimated=estimate_request_tokens(request.get('instructions',''),request.get('input',[]),request.get('tools',[]))
            reserved=ceil(estimated*p.input_margin_ratio)
            output=request.get('max_output_tokens')
            reason=None
            if not isinstance(output,int) or output<1 or output>limits['max_output_tokens_per_call']:
                reason='PER_CALL_OUTPUT_LIMIT'
            elif reserved>limits['max_reserved_input_per_call']:
                reason='PER_CALL_ESTIMATED_INPUT_LIMIT'
            elif self.by_fixture.get(fixture_id,0)>=limits['max_compaction_calls']:
                reason='FIXTURE_CALL_LIMIT'
            elif self.calls>=p.max_compaction_calls:
                reason='GLOBAL_CALL_LIMIT'
            elif self.inputs+reserved>p.max_total_reserved_input:
                reason='GLOBAL_INPUT_RESERVATION_LIMIT'
            elif self.outputs+output>p.max_total_reserved_output:
                reason='GLOBAL_OUTPUT_RESERVATION_LIMIT'
            if reason:
                self.halted_reason=reason
                raise BudgetDenied(reason)
            self.calls+=1; self.inputs+=reserved; self.outputs+=output
            self.by_fixture[fixture_id]=self.by_fixture.get(fixture_id,0)+1
            row=dict(reservation_id=uuid4().hex,fixture_id=fixture_id,status='IN_FLIGHT',
                input_estimated_tokens=estimated,input_reserved_tokens=reserved,
                output_reserved_tokens=output,input_upper_bound=reserved,output_upper_bound=output,
                bounds_semantics='LOCAL_ESTIMATE_WITH_MARGIN_NOT_PROVIDER_HARD_BOUND',charged_compaction=True)
            self.reservations.append(row)
            return row


class BoundBudget:
    def __init__(self,owner,fixture_id):
        self.owner,self.fixture_id=owner,fixture_id
    @property
    def policy(self): return self.owner.policy
    def reserve(self,request): return self.owner.reserve(self.fixture_id,request)


class GuardedResponses:
    """2. 派发前先持久化在途账本并校验归属；异常时禁止底层create。"""
    def __init__(self,native,trace): self.native,self.trace=native,trace
    def __getattr__(self,name): return getattr(self.native.responses,name)
    def create(self,*args,**request):
        t=self.trace; row=t.state['calls'][-1]
        required=('session_id','agent_run_id','execution_id','span_id','provider_call_id')
        if any(row.get(k) in (None,'NOT_AVAILABLE') for k in required):
            t.budget.owner.halted_reason='PROVIDER_ATTRIBUTION_MISSING'
            raise BudgetDenied('PROVIDER_ATTRIBUTION_MISSING')
        try: t.persist()
        except Exception:
            t.budget.owner.halted_reason='ARTIFACT_PERSIST_FAILED'
            raise
        # MAIN由受控Fake提供，不能让实际Provider意外执行主任务。
        if not request.get('instructions','').startswith(COMPACTION_INSTRUCTIONS):
            return t.main_fake.create(**request)
        return self.native.responses.create(*args,**request)


class TokenTrace(Trace):
    def __init__(self,delegate,destination,budget,*,eval_run_id,fixture_id,session=None,state=None,main_fake=None):
        self.native=delegate
        self.main_fake=main_fake
        proxy=SimpleNamespace(responses=GuardedResponses(delegate,self),base_url=getattr(delegate,'base_url','offline://fake'),
                              max_retries=getattr(delegate,'max_retries',None))
        super().__init__(proxy,destination,budget,eval_run_id=eval_run_id,fixture_id=fixture_id,session=session,state=state)
    def with_options(self,*args,**kwargs):
        return TokenTrace(self.native.with_options(*args,**kwargs),self.destination,self.budget,
            eval_run_id=self.eval_run_id,fixture_id=self.fixture_id,session=self.session,state=self.state,main_fake=self.main_fake)
    def copy(self,*args,**kwargs):
        return TokenTrace(self.native.copy(*args,**kwargs),self.destination,self.budget,
            eval_run_id=self.eval_run_id,fixture_id=self.fixture_id,session=self.session,state=self.state,main_fake=self.main_fake)
    def finish(self,row,started,outcome,reason=None):
        with self.state['lock']:
            if row['outcome']!='IN_FLIGHT': return
            row.update(outcome=outcome,latency_ms=(perf_counter()-started)*1000,reason=reason,
                monetary_cost_status='MONETARY_COST_NOT_EVALUATED',is_real_compaction_call=False)
            row['reservation']['status']='RETAINED_CONSERVATIVE_RESERVATION'
            # Usage缺失保持MISSING，不单独触发停止；派发保护仍采用已预留额度。
            self.persist()


def require_stage_approval(stage,approval=None,prior_stage=None,*,dry_run=False):
    if stage==1:
        return
    if stage!=2 or approval is None or not approval.get('approved'):
        raise BudgetDenied('STAGE_2_REQUIRES_HUMAN_APPROVAL')
    if not dry_run and (approval.get('source_type')!='HUMAN_REVIEW' or not approval.get('source_reference')):
        raise BudgetDenied('STAGE_2_REQUIRES_HUMAN_EVIDENCE')
    if not prior_stage or approval.get('stage1_run_id')!=prior_stage.get('run_id') or not prior_stage.get('audit_valid'):
        raise BudgetDenied('STAGE_1_REVIEW_NOT_BOUND')
