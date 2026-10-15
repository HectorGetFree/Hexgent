"""仅 Eval 适配器：真实 HTTP 工具注册到既有 Production 管线与真实 Provider。"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import httpx
from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.config import HexgentConfig
from hexgent.loop.limits import LoopLimits
from hexgent.prompt import TOOLS
from hexgent.tool.tool_registry import TOOL_REGISTRY, RegisteredTool
from hexgent.tool.tool_validation import TOOL_SCHEMAS
from hexgent.tool.tool_result import ToolHandlerResult
from .environment import dump


def register_fixture(endpoint, token):
    # 1. 只注册 Eval HTTP 适配工具，不替换执行、权限、重试或恢复实现。
    for name,path,write in [('fixture_read','/read',False),('fixture_ship','/ship',True)]:
        def handler(order_id,operation_id=None,context=None,_path=path):
            body={'order_id':order_id}
            if operation_id is not None: body['operation_id']=operation_id
            try:
                response=httpx.post(endpoint+_path,headers={'Authorization':'Bearer '+token,'X-Tool-Call':context.integration_metadata.get('tool_call_id','')},json=body,timeout=150,trust_env=False)
                response.raise_for_status();result=response.json()
            except httpx.HTTPError:
                return ToolHandlerResult.failed('HTTP_UNCERTAIN','业务服务响应不可确定，请先查询操作状态',side_effect_state='unknown' if _path=='/ship' else 'none')
            if result['ok']: return ToolHandlerResult.success(json.dumps(result['data'],ensure_ascii=False),result['data'])
            return ToolHandlerResult.failed(result['code'],result['message'],retryable=result.get('retryable',False),side_effect_state=result['side_effect_state'])
        properties={'order_id':{'type':'string'}}
        if write: properties['operation_id']={'type':'string'}
        schema={'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
        TOOL_REGISTRY[name]=RegisteredTool(handler,requires_context=True,permission_capability='workspace_write' if write else 'workspace_read',automatic_retry_safe=False,interrupt_behavior='block' if write else 'cancel')
        TOOL_SCHEMAS[name]=schema
        TOOLS.append({'type':'function','name':name,'description':'提交幂等发货，返回真实操作身份；不确定失败必须核验状态。' if write else '查询订单、库存、政策和已提交业务操作。','parameters':schema})


def main():
    parser=argparse.ArgumentParser();parser.add_argument('config',type=Path);parser.add_argument('--restore',action='store_true');args=parser.parse_args()
    cfg=json.loads(args.config.read_text());workdir=Path(cfg['workdir']);out=Path(cfg['observation_root'])
    register_fixture(cfg['endpoint'],cfg['token'])
    if cfg.get('protocol_version')==2:
        from .eval_runtime import install
        install(cfg['trial_control'])
    # 外部业务系统仅通过此适配器暴露；工具 Schema 选择不改变 Kernel 规则。
    TOOLS[:]=[t for t in TOOLS if t['name'] in cfg['allowed_tools']]
    config=HexgentConfig(loop=LoopLimits(max_model_rounds=20,max_tool_calls=cfg['budget']['max_tool_calls'],max_total_tokens=cfg['budget']['max_provider_tokens'],max_elapsed_seconds=cfg['budget']['max_wall_time_seconds'],max_output_tokens_per_round=3000))
    if cfg.get('phase')=='baseline':
        from .baseline_support import full_config
        config=full_config(cfg['budget'])
    config=replace(config,context_extraction=replace(config.context_extraction,enabled=False),context_retrieval=replace(config.context_retrieval,enabled=False))
    if args.restore:
        sid=json.loads((out/'agent-session.json').read_text())['session_id']
        engine=AgentEngine.restore(config,workdir,sid)
        dump(out/'restore.json',{'session_id':sid,'has_continuable_run':engine.has_continuable_run,'mechanism':'AgentEngine.restore + RunRequest.continue_run','pid':__import__('os').getpid(),'continue_attempted':True})
        request=RunRequest.continue_run()
    else:
        engine=AgentEngine.create(config,workdir)
        engine.session.initialize_runtime_session('session_'+cfg['trial_id'])
        dump(out/'agent-session.json',{'session_id':engine.session.store.session_id,'pid':__import__('os').getpid(),'continue_attempted':True})
        request=RunRequest.submit(cfg['user_query'])

    def confirmation(tool_name,message,call_id):
        # 2. Production ask 已发生后，按这一调用的真实参数给予一次性范围批准。
        rows=engine.session.store.read_transcript()
        started=next((r['payload'] for r in reversed(rows) if r['type']=='tool_execution_started' and r['payload']['call_id']==call_id),{})
        arguments=json.loads(started.get('arguments','{}'))
        decision='allow' if (tool_name in cfg['allowed_tools'] and (tool_name!='fixture_ship' or (cfg['approve'] and arguments==cfg['approval_scope']))) else 'deny'
        with (out/'approval-decisions.jsonl').open('a') as stream:
            stream.write(json.dumps({'session_id':engine.session.store.session_id,'call_id':call_id,'tool_name':tool_name,'arguments':arguments,'decision':decision})+'\n')
        return decision
    try:
        last=None
        for event in engine.run(request,user_confirmation=confirmation): last=event
        dump(out/('continue-result.json' if args.restore else 'agent-result.json'),{'session_id':engine.session.store.session_id,'status':last.result.status,'stop_reason':last.result.stop_reason})
    except Exception as exc:
        dump(out/'agent-error.json',{'type':type(exc).__name__})
        raise

if __name__=='__main__': main()
