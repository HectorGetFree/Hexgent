"""Eval-only压缩消融与任务权限，不改变Production实现。"""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import shutil
from unittest.mock import patch

from hexgent.config import load_config
from hexgent.context.context_compaction import CompactionOutcome
from hexgent.context.context_builder import build_context_details
from hexgent.loop.loop_state import LoopState
from hexgent.loop.agent_turn import _available_tools_for_state
from hexgent.tool.tool_permission_pipeline import PermissionRule, DENY_RULES
from hexgent.tool.tool_registry import TOOL_REGISTRY
from hexgent.utils.token_estimator import estimate_request_tokens
from hexgent.events import CheckpointCommittedEvent

ALLOWED={'read_file','glob','list_directory','write_file','edit_file','create_plan','update_plan'}
FIXTURE=Path(__file__).parent/'fixtures/vendor-review'

def manifest(root):
    return {str(p.relative_to(root)):sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}

def config(workdir):
    base=load_config(Path(__file__).resolve().parents[2])
    return replace(base,context_extraction=replace(base.context_extraction,enabled=False),
        context_retrieval=replace(base.context_retrieval,enabled=False),memory_retrieval=replace(base.memory_retrieval,enabled=False))

def policy_violation(args,context):
    value=args.get('path',args.get('pattern','.'))
    if not isinstance(value,str):return True
    root=context.workdir.resolve();target=Path(value).expanduser()
    if not target.is_absolute():target=root/target
    target=target.resolve()
    return not target.is_relative_to(root) or '.hexgent' in target.relative_to(root).parts

def illegal_write(args,context):
    if policy_violation(args,context):return True
    value=args.get('path','');root=context.workdir.resolve();target=Path(value).expanduser()
    if not target.is_absolute():target=root/target
    return not target.resolve().is_relative_to(root/'outputs')

def compaction_off(manager,client,model,instructions,tools,limits,*,force=False,instruction_budget=None,output_allowance=None,**kwargs):
    """只关闭压缩；主动/应急入口一致，完整硬预算不变。"""
    if False:yield None
    before=estimate_request_tokens(instructions,manager.model_messages(),tools)
    output=limits.reserved_output_tokens if output_allowance is None else output_allowance
    if force or before+output+limits.safety_margin_tokens>limits.context_window_tokens:
        return CompactionOutcome('blocked',before,before,message='OFF_CONTEXT_LIMIT：不调用辅助模型，保留硬预算')
    return CompactionOutcome('not_needed',before,before)

@contextmanager
def profile(variant):
    """仅在独立工作进程使用；同进程不得并行ON/OFF。"""
    if variant not in {'ON','OFF'}:raise ValueError('未知variant')
    import hexgent.loop.agent_turn as turn
    rules=(PermissionRule('eval_tool_not_allowed','deny',tuple(sorted(set(TOOL_REGISTRY)-ALLOWED)),lambda a,c:True,lambda a,c:'此任务不允许该工具'),
        PermissionRule('eval_fixture_boundary','deny',('read_file','glob','list_directory'),policy_violation,lambda a,c:'禁止访问工作区外或Runtime工件'),
        PermissionRule('eval_inputs_readonly','deny',('write_file','edit_file'),illegal_write,lambda a,c:'仅outputs目录允许写入'))
    with patch('hexgent.tool.tool_permission_pipeline.DENY_RULES',rules+DENY_RULES),patch.object(turn,'TOOLS',[t for t in turn.TOOLS if t['name'] in ALLOWED]):
        if variant=='OFF':
            with patch.object(turn,'compact_context_if_needed',compaction_off):yield
        else:yield

def next_request_tokens(engine):
    built=build_context_details(engine.tool_context,(),(),None,engine.config.context.runtime_injection_tokens,
        pinned_constraints=engine.list_pinned_constraints(),pinned_constraints_tokens=engine.config.context.pinned_constraints_tokens)
    state=LoopState(engine.context_manager,engine.tool_context)
    return estimate_request_tokens(built.instructions,engine.context_manager.model_messages(),_available_tools_for_state(state))

def capture_before_compaction(engine,stream,destination):
    """只截取已完成真实工具批次的安全Checkpoint，不制造历史。"""
    for event in stream:
        if isinstance(event,CheckpointCommittedEvent) and event.reason=='tool_batch_completed':
            tokens=next_request_tokens(engine);limits=engine.config.context
            if limits.compaction_threshold_tokens<=tokens and tokens+engine.config.loop.max_output_tokens_per_round+limits.safety_margin_tokens<=limits.context_window_tokens:
                stream.close()
                if destination.exists():raise RuntimeError('禁止覆盖快照')
                shutil.copytree(engine.workdir,destination)
                return dict(status='SNAPSHOT_CAPTURED',session_id=engine.session.store.session_id,checkpoint_id=event.checkpoint_id,
                    next_request_estimated_tokens=tokens,hashes=manifest(destination),same_original_session=True)
    return dict(status='NO_COMPACTION_OPPORTUNITY')

def clone_pair(snapshot,parent):
    if parent.exists():raise RuntimeError('禁止复用分支目录')
    parent.mkdir()
    for variant in ['ON','OFF']:shutil.copytree(snapshot,parent/variant)
    if manifest(parent/'ON')!=manifest(parent/'OFF'):raise RuntimeError('CLONE_MISMATCH')
    return parent/'ON',parent/'OFF'

class BudgetStop(BaseException):pass

class RequestBudget:
    """Future真实客户端前置硬限额；不提供Provider调用入口。"""
    def __init__(self,max_calls,max_estimated_tokens):self.max_calls=max_calls;self.max_tokens=max_estimated_tokens;self.calls=0;self.reserved=0
    def reserve(self,request):
        amount=estimate_request_tokens(request.get('instructions',''),request.get('input',[]),request.get('tools',[]))+request.get('max_output_tokens',0)
        if self.calls>=self.max_calls or self.reserved+amount>self.max_tokens:raise BudgetStop('BUDGET_STOP：不得追加收尾或重试请求')
        self.calls+=1;self.reserved+=amount
        return amount
