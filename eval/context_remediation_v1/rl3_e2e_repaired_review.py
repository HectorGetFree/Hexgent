"""修复后单次结果的独立复核：不修改执行工件或追加请求。"""
import json
from pathlib import Path
from hashlib import sha256
from .offline import ROOT, fingerprints
from .factorial_pilot import file_hashes
from hexgent.context.context_manager import _request_message

OUT=ROOT/'eval/context_remediation_v1/runs/rl3-e2e-real-20261012-logprobs-fix'
OLD=ROOT/'eval/context_remediation_v1/runs/rl3-e2e-real-20261012-v1'

def read(root,name):return json.loads((root/name).read_text())
def save(name,obj):
    p=OUT/name
    if p.exists():raise RuntimeError('禁止覆盖结果')
    p.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')
def text(response):
    return ''.join(c.get('text','') for o in response.get('output',[]) for c in o.get('content',[]))

def main():
    ledger=read(OUT,'provider-ledger.json');manifest=read(OUT,'manifest.json')
    assert len(ledger)==4 and all(r['status']=='COMPLETED' and r['usage_available'] for r in ledger)
    outcome=read(OUT,'compaction-outcome.json');cp=read(OUT,'checkpoint.json');restore=read(OUT,'restore-evidence.json')
    a=read(OUT,'MAIN/actual-request.json');b=read(OUT,'RESTORE_MAIN/actual-request.json')
    assert a==b and restore['context_equal'] and restore['has_continuable_run']
    assert read(OLD,'EXTRACTOR/actual-request.json')==read(OUT,'EXTRACTOR/actual-request.json')
    assert _request_message(read(OLD,'SUMMARY/actual-request.json'))==read(OUT,'SUMMARY/actual-request.json')
    extraction=json.loads(text(read(OUT,'EXTRACTOR/response.json')))
    source_payload=json.loads(read(OUT,'EXTRACTOR/actual-request.json')['input'][0]['content'])
    facts={f['id']:f for f in extraction['facts']}
    sources={s['message_id']:s['message'] for s in source_payload['history_prefix']}
    quality=[]
    for stage in ['MAIN','RESTORE_MAIN']:
        answer=text(read(OUT,f'{stage}/response.json'))
        quality.append(dict(stage=stage,final_answer=answer,agent_terminal='completed',
            required_fact_review=[
                dict(fact='当前批次AUDIT-MAPLE-57',verdict='NOT_DEMONSTRATED',reason='回答未给出批次标识。'),
                dict(fact='参数与协议审查已完成',verdict='NOT_DEMONSTRATED',reason='回答提到协议校验实现，不等价于此审查里程碑已完成。'),
                dict(fact='失败恢复后的检索审查已完成',verdict='NOT_DEMONSTRATED',reason='回答区分恢复实现与运行证据，但未明确该检索审查已完成；不把需独立运行验证解释为否定审查完成。'),
                dict(fact='等待维护者确认两项权限契约',verdict='SUPPORTED',reason='回答明确保留维护者确认两处权限契约的阻塞。'),
                dict(fact='确认后生成交接报告且不得发布',verdict='PARTIALLY_DEMONSTRATED',reason='保留只读/不发布边界；未明确确认后生成交接报告这一条件动作。')],
            conclusion='NOT_FULLY_DEMONSTRATED',tool_calls=0,external_actions_executed=False,
            limitation='没有缺事实不等于模型忘记；当前原始Query是继续而非逐事实复述。不得临时增加复述Oracle后将未复述判定为确定性遗忘或状态错误。'))
    save('main-quality-review.json',quality)
    attribution=[]
    for fid,ref in zip(['f2','f3','f4','f5','f6'],['msg-3','msg-43','msg-83','msg-99','msg-107']):
        raw=sources[ref]['output'];quote=raw[raw.rfind('合成审查台账证据：'):]
        attribution.append(dict(fact_id=fid,snapshot_text=facts[fid]['content'],source_refs=facts[fid]['source_refs'],source_quote=quote,
            snapshot='SUPPORTED_AND_INJECTED',summary='SUPPORTED_AND_INJECTED',recent_tail='TARGET_FACT_NOT_PRESENT_IN_REVIEWED_TAIL',pinned='NOT_PRESENT',memory='DISABLED',context_record='DISABLED',
            attribution='两渠道均包含，不证明Snapshot独立收益。'))
    save('fact-channel-audit.json',dict(required_facts=attribution,main_request_equal_after_restore=True,
        snapshot_message=a['input'][0],summary_message=a['input'][1],recent_tail=a['input'][2:],
        extra_reference_warning=dict(fact_id='f10',reason='依赖范围及完整前缀缺少确认的复合句仍仅引用端点，记录范围性引用不足；不修改提取结果或评分合同。')))
    transcripts=read(OUT,'final-transcripts.json')
    commits={branch:[r for r in rows if r['type']=='context_compaction_applied'] for branch,rows in transcripts.items()}
    assert all(len(v)==1 for v in commits.values())
    usage=dict(input_tokens=sum(r['input_tokens'] for r in ledger),output_tokens=sum(r['output_tokens'] for r in ledger),
        usage_completeness='4/4',provider_latency_ms=sum(r['latency_ms'] for r in ledger),
        stage_rows=ledger,monetary_cost='NOT_AVAILABLE_PROVIDER_TARIFF_NOT_VERIFIED',estimated_before=outcome['before_tokens'],estimated_after=outcome['after_tokens'],
        estimated_reduction_pct=100*(1-outcome['after_tokens']/outcome['before_tokens']),
        interpretation='估算缩减不是Provider输入节省或Agent整体收益；实际成本包含两次辅助及两个恢复分支MAIN，没有无压缩对照。')
    save('cost-review.json',usage)
    result=dict(gate='C2_RL3_E2E_INCOMPLETE',engineering='PASS',task_quality='NOT_FULLY_DEMONSTRATED',efficiency='OBSERVATION_COMPLETE_NO_BASELINE',
        real_requests=4,retries=0,all_provider_usage_complete=True,summary_compatibility_fix_verified=True,
        required_facts_in_snapshot_and_summary='5/5',both_main_requests_equal=True,compaction_commits_per_branch=1,
        formal_continue_no_new_query=True,current_query_count=sum(r['type']=='context_user_query_appended' for r in transcripts['current']),restored_query_count=sum(r['type']=='context_user_query_appended' for r in transcripts['restored']),
        remaining_evidence_gap='两次自然继续回答未展示所有五事实，不能证明完整理解与使用；不是确认模型遗忘。',
        publication_metrics=outcome['boundary']['publication_metrics'])
    save('independent-review.json',result)
    integrity=read(OUT,'integrity.json')
    assert manifest['production_hashes']==file_hashes(ROOT/'hexgent') and manifest['frozen_hashes']==fingerprints()
    save('review-integrity.json',dict(production_unchanged_during_run=True,frozen_assets_unchanged=True,
        extractor_request_identical_to_failed_attempt=True,summary_diff_only_null_logprobs_to_array=True,
        original_failure_preserved=True,git_diff_check=integrity['git_diff_check']))
    report=f'''# R-L3 E2E 修复后重跑

Gate：C2_RL3_E2E_INCOMPLETE。工程PASS，任务质量NOT_FULLY_DEMONSTRATED，成本观测完整但没有收益对照。

## 修复

仅context_manager.py的请求物化边界将logprobs=null转换为[]，同时处理SDK对象和恢复后的字典。原活动历史、Transcript、Fixture、Oracle和Prompt不改。不是在Eval层删改历史答案。
新增4项回归；相关42/42、Runtime347/347通过，离线R-L3接线PASS，git diff --check PASS。日志见 ../rl3-e2e-offline-20261012-logprobs-fix/repair-regressions.json。

## 真实链路

四次请求4/4完成，隐藏/显式重试均无。Extractor实际仍是已冻结Eval v2，不宣称Production原生v2。Summary兼容错误已消失。
Snapshot+Summary权威原子提交、内存发布、Checkpoint完成；每个分支仅一个Compaction事件。MAIN通过正式Continue，恢复分支通过AgentEngine.restore再正式Continue，无新Query，恢复前后完整请求逐对象相等。安全快照分支恢复，不是完成终态后新Submit。
未执行工具或外部动作，没有旧状态作为有效事实复活或Snapshot重复注入。旧“全部审查完成”“跳过权限直接发布”保留为已废止历史，不等于有效复活。

## 质量与来源

五事实在真实Snapshot及Summary均完整出现，随后都进入两次MAIN请求；Recent Tail没有目标台账事实，Pinned未配置，Memory/ContextRecord关闭。
因此只能证明渠道可用，不能把回答结果单独归因于Snapshot。
两次MAIN均明确等待维护者确认两处权限契约，保留只读边界，恢复回答明确不发布。
但没有给出AUDIT-MAPLE-57及两项完成里程碑，也未明确确认后生成交接报告；协议实现/运行验证讨论不替代审查完成状态。
这不足以验收全部五事实被理解使用。原Query不是逐事实复述请求，不能据未复述断言模型遗忘，亦不临时加强Oracle制造FAIL。整体Gate因此INCOMPLETE，不追加问题或重跑。

## 成本

Provider实际输入{usage['input_tokens']}、输出{usage['output_tokens']} Tokens，Usage4/4完整。阶段耗时及请求见provider-ledger.json。
完整请求本地估算{outcome['before_tokens']}→{outcome['after_tokens']}，约减少{usage['estimated_reduction_pct']:.2f}%；这不是Provider实际节省率，也不是Agent效率提升。
没有货币成本依据或无压缩Baseline，不推断成本净收益。准备350011是容量估算，不是消耗。

## 边界与停止

额外Snapshot f10存在已知范围引用不足，单独记录，不修改Prompt/评分器。Production仅本次授权的序列化修复；真实运行期间哈希不变，历史失败工件及冻结C1/C2/M1/M2资产保留。
未commit/tag/push，不进入其他样本。不宣称C2稳定性或Production发布通过。本轮结束。
'''
    path=OUT/'REPORT.md'
    if path.exists():raise RuntimeError('禁止覆盖报告')
    path.write_text(report)
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
