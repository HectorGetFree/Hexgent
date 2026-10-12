"""Stage 1 真实入口默认关闭；Mock 接线和七例 Fake Dry Run 不读取凭证。"""
from pathlib import Path
from unittest.mock import patch
import argparse
import json
import os
from copy import deepcopy
import httpx
from openai import OpenAI

from .guard import DispatchGuard, GuardedTrace
from eval.context_management.c2_prep_3 import runner as frozen
from eval.context_management.c2_prep_3.budget import TokenBudget, require_stage_approval
from eval.context_management.c2_prep_3.prepare import ROOT, read_json


def normalize_history(rounds):
    """仅去除合成 assistant 协议元数据的可选 null，不改工具正文或事实。"""
    normalized=deepcopy(rounds);changes=[]
    for i,turn in enumerate(normalized):
        for j,message in enumerate(turn['messages']):
            if message.get('type')!='message' or message.get('role')!='assistant':continue
            if 'phase' in message and message['phase'] is None:
                del message['phase'];changes.append(f'rounds[{i}].messages[{j}].phase')
            for k,content in enumerate(message.get('content',[])):
                if content.get('type')=='output_text' and 'logprobs' in content and content['logprobs'] is None:
                    del content['logprobs'];changes.append(f'rounds[{i}].messages[{j}].content[{k}].logprobs')
    return normalized,changes


def make_client(manifest,*,execute=False,mock_transport=None):
    if not execute: raise RuntimeError('REAL_ENTRY_DISABLED')
    if mock_transport is not None:
        key='OFFLINE-NOT-A-CREDENTIAL'
        http=httpx.Client(transport=mock_transport)
    else:
        # 1. 只有显式真实执行授权入口才允许读取配置凭证。
        key=os.environ['OPENAI_API_KEY']; http=None
    options=dict(api_key=key,base_url=manifest['provider']['base_url'],max_retries=0,
        organization='',project='',admin_api_key='',webhook_secret='',http_client=http)
    if mock_transport is not None:
        # SDK 的可选身份字段也不得回落到真实环境凭证。
        with patch.dict(os.environ,{},clear=True): return OpenAI(**options)
    return OpenAI(**options)


def execute(entry,output,budget,*,client=None,transport_mode='FAKE',summary_override=None):
    guard=DispatchGuard(budget)
    original_load=frozen.load_rounds;normalization={}
    def load_normalized(value):
        rounds,query=original_load(value);clean,changes=normalize_history(rounds)
        normalization.update(original_rounds=rounds,normalized_rounds=clean,removed_optional_null_paths=changes,
            rule='仅 assistant message.phase / output_text.logprobs 的 None 省略；不改文本、事实、顺序、调用或参数')
        return clean,query
    def factory(delegate,destination,bound,**kwargs):
        return GuardedTrace(client if client is not None else delegate,destination,bound,guard=guard,
            real_mode=client is not None,transport_mode=transport_mode,**kwargs)
    # 2. 仅替换 Eval 观测代理，真实 Loop、Builder、Compaction 和冻结输入不变。
    with guard.instrument(),patch.object(frozen,'TokenTrace',factory),patch.object(frozen,'load_rounds',load_normalized):
        result=frozen.execute(entry,output,budget,summary_override)
    path=output/entry['fixture_id']/'evidence.json'
    evidence=read_json(path)
    result['provider_usage_kind']='MOCK_SDK_PROTOCOL_USAGE_NOT_REAL_PROVIDER_COST' if transport_mode=='MOCK_TRANSPORT' else (
        'REAL_COMPACTION_FAKE_MAIN' if client is not None else 'FAKE_FIXTURE_PROTOCOL_USAGE_NOT_REAL_PROVIDER_COST')
    result['actual_network_calls']=sum(c.get('actual_network_dispatch',False) for c in evidence['calls'])
    # 旧执行器的 candidate 是 Fake 样例；真实入口不得误写为真实候选摘要。
    if client is not None:
        evidence['fake_candidate_not_sent_to_provider']=evidence.pop('candidate')
        evidence['actual_candidates']=[''.join(part.get('text','') for item in c['response'].get('output',[])
            for part in item.get('content',[]) if part.get('type')=='output_text') for c in evidence['calls']
            if c['call_type']=='COMPACTION' and c.get('response')]
        result['summary_quality']='QUALITY_REVIEW_PENDING' if transport_mode=='NETWORK' else 'NOT_EVALUATED_MOCK_RESPONSE'
    evidence['result']=result
    from eval.context_management.c1_independent import save
    save(output/entry['fixture_id']/'serialization_review.json',normalization)
    save(path,evidence)
    return result


def main():
    parser=argparse.ArgumentParser(description='默认禁用的 C2 Stage 1 Compaction 入口')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    if not args.execute: raise RuntimeError('REAL_ENTRY_DISABLED')
    # 3. 本入口只包含 Stage 1；Stage 2 不可由循环自动进入。
    manifest=read_json(ROOT/'smoke_manifest.json')
    frozen.validate_capacity(manifest,read_json(ROOT/'capacity_confirmation.json'))
    entries=[e for e in manifest['fixtures'] if e['stage']==1]
    if [e['fixture_id'] for e in entries]!=['F1-02','L2']: raise RuntimeError('STAGE1_ORDER_MISMATCH')
    for e in entries: frozen.load_rounds(e)
    if args.output_dir.exists(): raise RuntimeError('ARTIFACT_ALREADY_EXISTS')
    args.output_dir.mkdir(parents=True)
    budget=TokenBudget(frozen.policy(manifest,1)); client=make_client(manifest,execute=True)
    try:
        for e in entries:
            result=execute(e,args.output_dir,budget,client=client,transport_mode='NETWORK')
            if result['status']!='PASS' or budget.halted_reason: break
    finally: client.close()
    # 不进行质量自动 PASS，不尝试第二阶段，也不主动重试。
    print(json.dumps({'output':str(args.output_dir),'quality':'QUALITY_REVIEW_PENDING'},ensure_ascii=False))


if __name__=='__main__': main()
