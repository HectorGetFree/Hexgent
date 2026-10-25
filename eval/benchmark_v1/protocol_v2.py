"""执行协议v2：Trial预算、终止分类、有限补测；不改Production。"""
from contextlib import contextmanager
from hashlib import sha256
import fcntl
import json
from pathlib import Path
import time
from uuid import uuid4


class BudgetExhausted(RuntimeError):
    pass


def strict_jsonl(path, *, active=False):
    raw=Path(path).read_text();lines=raw.splitlines();rows=[]
    for i,line in enumerate(lines):
        try:
            row=json.loads(line)
            if not isinstance(row,dict):raise ValueError('JSONL记录必须为对象')
        except ValueError:
            if active and i==len(lines)-1 and not raw.endswith('\n'):break
            raise ValueError('JSONL损坏或截断：'+str(path))
        rows.append(row)
    if raw and not raw.endswith('\n') and not active:raise ValueError('终态JSONL未闭合')
    return rows


class TrialBudget:
    """共享文件锁保护的Eval权威账本；所有Run/Session共用，写失败即停止派发。"""
    def __init__(self, path, identity, limits, *, started_at=None):
        self.path=Path(path);self.identity=identity;self.limits=limits
        self.started_at=started_at if started_at is not None else time.time()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.touch(exist_ok=True)

    @contextmanager
    def locked(self):
        with self.path.open('a+') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            stream.seek(0);rows=strict_jsonl(self.path)
            if any(r['identity']!=self.identity for r in rows):raise ValueError('预算账本串Trial')
            def append(event,**fields):
                row=dict(schema_version=2,sequence=len(rows)+1,identity=self.identity,event=event,
                    timestamp=time.time(),**fields)
                stream.seek(0,2);stream.write(json.dumps(row,ensure_ascii=False)+'\n');stream.flush()
                __import__('os').fsync(stream.fileno());rows.append(row)
                return row
            yield rows,append
            fcntl.flock(stream,fcntl.LOCK_UN)

    def _reason(self,rows,*,dispatch=False):
        if any(r['event']=='budget_blocked' for r in rows):return next(r['reason'] for r in rows if r['event']=='budget_blocked')
        failed=next((r for r in rows if r['event']=='provider_finished' and r.get('failure_reason')),None)
        if failed:return failed['failure_reason']
        if time.time()-self.started_at>=self.limits['max_wall_time_seconds']:return 'trial_wall_time'
        if sum(r.get('tokens') or 0 for r in rows if r['event']=='provider_finished')>=self.limits['max_provider_tokens']:return 'trial_provider_tokens'
        if dispatch and sum(r['event']=='actual_dispatch' for r in rows)>=self.limits['max_tool_calls']:return 'trial_tool_dispatches'
        return None

    def dispatch(self,binding,call_id,tool_name,attempt):
        with self.locked() as (rows,append):
            reason=self._reason(rows,dispatch=True)
            if reason:
                append('budget_blocked',reason=reason,**binding,call_id=call_id,tool_name=tool_name)
                raise BudgetExhausted(reason)
            append('actual_dispatch',**binding,call_id=call_id,tool_name=tool_name,attempt=attempt)

    def request(self,binding,estimated_input,max_output):
        with self.locked() as (rows,append):
            reason=self._reason(rows)
            done={r['request_id'] for r in rows if r['event']=='provider_finished'}
            reserved=sum(r['estimated_input_tokens']+r['max_output_tokens'] for r in rows if r['event']=='provider_started' and r['request_id'] not in done)
            remaining=self.limits['max_provider_tokens']-sum(r.get('tokens') or 0 for r in rows if r['event']=='provider_finished')-reserved
            output=min(max_output,remaining-estimated_input)
            if reason or output<=0:
                reason=reason or 'trial_provider_tokens'
                append('budget_blocked',reason=reason,**binding)
                raise BudgetExhausted(reason)
            rid=uuid4().hex;append('provider_started',**binding,request_id=rid,estimated_input_tokens=estimated_input,max_output_tokens=output)
            return rid,output

    def response(self,binding,rid,usage,*,reason=None):
        with self.locked() as (rows,append):
            usage=usage or {};a=usage.get('input_tokens');b=usage.get('output_tokens')
            known=type(a) is int and type(b) is int and a>=0 and b>=0
            if not known and reason is None:reason='usage_unavailable'
            append('provider_finished',**binding,request_id=rid,input_tokens=a,output_tokens=b,
                tokens=a+b if known else None,usage_known=known,usage=usage,failure_reason=reason)
            if self._reason(rows):append('budget_blocked',reason=self._reason(rows),**binding)


def classify(*, termination='completed',task='INCONCLUSIVE',safety=None,evidence='PASS',
             environment=True,injector_valid=True,scenario_triggered=True):
    """有效失败保留；刺激是否触发与实验条件是否可信独立。"""
    if termination=='unsupported':p,t='BLOCKED','INCONCLUSIVE'
    elif termination=='usage_unavailable':p,t='INCONCLUSIVE','INCONCLUSIVE'
    elif not environment or not injector_valid or termination in {'provider_429','provider_unavailable','fixture_failure'}:p,t='INVALID','INVALID'
    elif evidence=='MISSING':p,t='INCONCLUSIVE','INCONCLUSIVE'
    elif evidence!='PASS':p,t='INVALID','INVALID'
    elif task=='INVALID':p,t='INVALID','INVALID'
    elif termination.startswith('trial_') or termination in {'agent_failed','agent_stopped'}:p,t='PASS','PASS' if task=='PASS' else 'FAIL'
    else:p,t=('PASS',task) if task in {'PASS','FAIL'} else ('INCONCLUSIVE','INCONCLUSIVE')
    return dict(protocol_version=2,protocol_status=p,task_status=t,termination_reason=termination,
        safety_violation=safety,valid_task_sample=p=='PASS' and t in {'PASS','FAIL'},scenario_triggered=scenario_triggered,
        environment_valid=environment,injector_valid=injector_valid)


def replacement(previous,new_trial_id,*,case_id,contract_sha256,attempt,max_replacements=1):
    """只有INVALID可补测；FAIL永不替补，429之后必须外部恢复再另轮。"""
    if previous['task_status']!='INVALID':raise ValueError('禁止替补有效FAIL或非INVALID')
    if previous['trial_id']==new_trial_id:raise ValueError('补测必须新Trial ID')
    if previous['case_id']!=case_id or previous['contract_sha256']!=contract_sha256:raise ValueError('补测Case/Revision不一致')
    if not 1<=attempt<=max_replacements:raise ValueError('超过补测上限')
    return dict(previous_trial_id=previous['trial_id'],trial_id=new_trial_id,case_id=case_id,
        contract_sha256=contract_sha256,replacement_reason=previous['termination_reason'],replacement_ordinal=attempt,
        max_replacements=max_replacements,stop_on_provider_unavailable=True)


def reserve_replacement(root,previous_root,contract,trial_id):
    """新campaign中登记最多一次替补；不向原Trial写任何内容，不自动执行重试。"""
    root=Path(root).resolve();previous_root=Path(previous_root).resolve()
    if root.is_relative_to(previous_root) or root.parent.is_relative_to(previous_root):raise ValueError('补测目录不得位于历史Trial内')
    p=json.loads((previous_root/'protocol-result-v2.json').read_text())
    prior={**p['identity'],'task_status':p['task_status'],'termination_reason':p['termination_reason']}
    if (previous_root/'replacement-manifest-v2.json').exists():raise ValueError('替补已用尽，不得形成无限补测链')
    lineage=replacement(prior,trial_id,case_id=contract.case_id,contract_sha256=contract.digest(),attempt=1)
    claims=root.parent/'replacement-claims-v2.jsonl';claims.parent.mkdir(parents=True,exist_ok=True)
    with claims.open('a+') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX);stream.seek(0);rows=[json.loads(s) for s in stream if s.strip()]
        if any(r['previous_trial_id']==prior['trial_id'] for r in rows):raise ValueError('本campaign已经登记过该INVALID的替补')
        lineage.update(previous_root=str(previous_root),previous_protocol_sha256=sha256((previous_root/'protocol-result-v2.json').read_bytes()).hexdigest())
        stream.seek(0,2);stream.write(json.dumps(lineage)+'\n');stream.flush();__import__('os').fsync(stream.fileno())
    root.mkdir(parents=True,exist_ok=True)
    (root/'replacement-manifest-v2.json').write_text(json.dumps(lineage,ensure_ascii=False,indent=2)+'\n')
    return lineage


def cleanup(actions):
    """每项清理均执行，归档/观察错误不能跳过资源关闭；保留错误类型。"""
    errors={}
    for name,action in actions:
        try:action()
        except Exception as exc:errors[name]=type(exc).__name__
    return errors


def recovered_call(source, action):
    """规范化Production修复的未闭合调用；不伪造tool_execution_finished。"""
    if action not in {'not_started','safe_to_retry','verify_before_retry'}:raise ValueError('未知恢复动作')
    return {**source,'status':'interrupted','data':None,'failure':{
        'code':'PROCESS_INTERRUPTED','side_effect_state':'unknown' if action=='verify_before_retry' else 'none',
        'recovery_action':action}}
