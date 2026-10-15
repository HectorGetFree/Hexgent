"""仅Benchmark进程安装预算闸门，使用既有Hexgent Web/Fixture入口。"""
import argparse
from dataclasses import replace
import importlib
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from .protocol_v2 import BudgetExhausted,TrialBudget


def binding(observer=None):
    from hexgent.observation import current_observation
    observer=observer or current_observation.get()
    if observer is None or observer.store is None:raise RuntimeError('预算边界缺少Run/Session来源')
    started=next(r for r in reversed(observer.store.read_transcript()) if r['type']=='agent_run_started')
    who=dict(session_id=observer.store.session_id,run_id=started['payload']['run_id'])
    p=getattr(observer.store,'session_dir',None)
    p=p/'observation.jsonl' if p is not None else None
    if p is not None and p.exists():
        from .protocol_v2 import strict_jsonl
        provider=next((r for r in reversed(strict_jsonl(p,active=True)) if r.get('event')=='provider_started'),{})
        who['request_type']=provider.get('request_type','UNKNOWN')
    return who


class Responses:
    def __init__(self,source,budget,fallback=None):self.source=source;self.budget=budget;self.last_binding=fallback
    def create(self,**request):
        from hexgent.utils.token_estimator import estimate_request_tokens
        try:who=binding()
        except (RuntimeError,StopIteration):
            if self.last_binding is None:raise
            who=self.last_binding
        self.last_binding=who
        estimate=estimate_request_tokens(request.get('instructions',''),request.get('input',[]),request.get('tools',[]))
        rid,output=self.budget.request(who,estimate,request.get('max_output_tokens',4096))
        request['max_output_tokens']=output
        try:response=self.source.create(**request)
        except Exception as exc:
            reason='provider_429' if getattr(exc,'status_code',None)==429 else 'provider_unavailable'
            self.budget.response(who,rid,None,reason=reason);raise
        if request.get('stream'):
            return Stream(response,self.budget,who,rid)
        usage=getattr(response,'usage',None)
        usage=usage.model_dump() if hasattr(usage,'model_dump') else usage
        self.budget.response(who,rid,usage,reason=None if usage is not None else 'usage_unavailable');return response


class Stream:
    def __init__(self,source,budget,who,rid):self.source=source;self.budget=budget;self.who=who;self.rid=rid;self.finished=False
    def __iter__(self):
        try:
            for event in self.source:
                if event.type=='response.completed':
                    usage=getattr(event.response,'usage',None)
                    self.budget.response(self.who,self.rid,usage.model_dump() if hasattr(usage,'model_dump') else usage,
                        reason=None if usage is not None else 'usage_unavailable')
                    self.finished=True
                elif event.type in {'error','response.failed'}:
                    error=getattr(event,'error',None) or getattr(getattr(event,'response',None),'error',None) or event
                    code=str(getattr(error,'code',''))
                    reason='provider_429' if code in {'model_cooldown','rate_limit_exceeded'} else 'provider_unavailable'
                    self.budget.response(self.who,self.rid,None,reason=reason);self.finished=True
                yield event
        except Exception as exc:
            if not self.finished:self.budget.response(self.who,self.rid,None,reason='provider_429' if getattr(exc,'status_code',None)==429 else 'provider_unavailable');self.finished=True
            raise
    def close(self):
        if not self.finished:self.budget.response(self.who,self.rid,None,reason='provider_unavailable');self.finished=True
        close=getattr(self.source,'close',None)
        if close:close()


class Client:
    def __init__(self,source,budget,fallback=None):self.source=source;self.budget=budget;self.responses=Responses(source.responses,budget,fallback)
    def with_options(self,**options):
        try:who=binding()
        except (RuntimeError,StopIteration):who=self.responses.last_binding
        return Client(self.source.with_options(**options),self.budget,who)
    def __getattr__(self,name):return getattr(self.source,name)


def install(config):
    if config.get('protocol_version')!=2:raise ValueError('F1执行必须显式protocol_version=2')
    budget=TrialBudget(config['ledger_path'],config['identity'],config['budget'],started_at=config['started_at'])
    from .environment import dump
    metadata=dict(protocol_version=2,
        identity=config['identity'],budget=config['budget'],started_at=config['started_at'],sdk_max_retries=0,
        code_sha256={n:sha256(Path(__file__).with_name(n).read_bytes()).hexdigest() for n in ['eval_runtime.py','protocol_v2.py']})
    meta_path=Path(config['ledger_path']).parent/'runtime-protocol-v2.json'
    if meta_path.exists():
        previous=json.loads(meta_path.read_text())
        if any(previous[k]!=metadata[k] for k in ['identity','budget','started_at']):raise ValueError('不得重置Trial预算或起始时间')
    else:dump(meta_path,metadata)
    import hexgent.observation as observation
    original=observation.observed_handler
    def guarded(function,arguments,context,call_id,attempt,tool_name):
        try:budget.dispatch(binding(context.observation),call_id,tool_name,attempt)
        except BudgetExhausted as exc:
            from hexgent.tool.tool_result import ToolHandlerResult
            return ToolHandlerResult.failed('TRIAL_BUDGET_EXHAUSTED',str(exc),side_effect_state='not_started')
        return original(function,arguments,context,call_id,attempt,tool_name)
    observation.observed_handler=guarded
    clients=importlib.import_module('hexgent.client');original_client=clients.get_client
    def get_client():
        try:return Client(original_client().with_options(max_retries=0),budget)
        except Exception as exc:
            dump(Path(config['ledger_path']).parent/'provider-setup-error-v2.json',dict(identity=config['identity'],
                termination_reason='provider_unavailable',error_type=type(exc).__name__,source='Eval SDK construction boundary'))
            raise
    clients.get_client=get_client
    importlib.import_module('hexgent.loop.agent_loop').get_client=get_client
    return budget


def main():
    p=argparse.ArgumentParser();p.add_argument('--workdir',type=Path,required=True);args=p.parse_args()
    install(json.loads((args.workdir/'.benchmark-control.json').read_text()))
    import uvicorn
    from hexgent.adapters.web.app import create_app
    uvicorn.run(create_app(workdir=args.workdir),host='0.0.0.0',port=8000)


if __name__=='__main__':main()
