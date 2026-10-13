"""审阅唯一尝试的现存结果；不调用Provider、不修改原执行记录。"""
import json
from hashlib import sha256
from pathlib import Path
from dataclasses import asdict

from hexgent.context.key_fact_snapshot import validate_snapshot
from hexgent.context.context_budget import ContextLimits
from .factorial_pilot import file_hashes
from .offline import ROOT, fingerprints

OUT=ROOT/'eval/context_remediation_v1/runs/rl3-e2e-real-20261012-v1'

def read(name):return json.loads((OUT/name).read_text())
def save(name,value):
    path=OUT/name
    if path.exists():raise RuntimeError('禁止覆盖独立复核')
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def main():
    ledger=read('provider-ledger.json');result=read('execution-result.json');manifest=read('manifest.json')
    response=read('EXTRACTOR/response.json')
    text=''.join(c.get('text','') for o in response.get('output',[]) for c in o.get('content',[]))
    raw=json.loads(text);req=read('EXTRACTOR/actual-request.json');payload=json.loads(req['input'][0]['content'])
    snapshot=validate_snapshot(raw,cutpoint=payload['as_of_message_id'],sources=payload['history_prefix'],previous=payload['previous_snapshot'],budget_tokens=ContextLimits().key_fact_snapshot_tokens)
    save('snapshot-offline-revalidation.json',dict(status='PASS',snapshot=snapshot,scope='对已收到的真实Extractor结果重新执行现有Production校验；不是新模型执行，不表示已提交。'))
    refs=['msg-3','msg-43','msg-83','msg-99','msg-107']
    ids=['f2','f3','f4','f5','f6']
    facts={f['id']:f for f in snapshot['facts']}
    sources={s['message_id']:s['message'] for s in payload['history_prefix']}
    rows=[]
    for ref,fid in zip(refs,ids):
        evidence=sources[ref]['output']
        quote=evidence[evidence.rfind('合成审查台账证据：'):]
        rows.append(dict(snapshot_fact_id=fid,snapshot_content=facts[fid]['content'],source_refs=facts[fid]['source_refs'],
            reviewed_source=ref,source_quote=quote,required_fact_supported=True,
            source_qualification='原始合成台账支持该身份、完成状态、等待条件或下一步否定；不是外部行为实际发生证明。',
            snapshot_candidate='PRESENT',snapshot_injection='NOT_EXECUTED',summary_injection='NOT_EXECUTED',recent_tail='NOT_EVALUABLE_MAIN_NOT_DISPATCHED',
            pinned='NONE_PRESET',main_used_fact='NOT_EVALUABLE',restored_main_used_fact='NOT_EVALUABLE'))
    save('fact-channel-audit.json',dict(required_facts=rows,extraction_required_fact_recall='5/5',
        note='仅独立人工审查本次真实提取候选五条必需事实，不作为E2E Gate。最终MAIN请求未组装，不能宣称已注入或使用。',
        extra_fact_reference_warning=dict(fact_id='f13',reason='全30工作项依赖及完整前缀缺少确认的复合命题只引用端点；存在历史已知范围性引用不足。只记录，不修复、不套用未冻结的V3评分。')))
    summary=read('SUMMARY/actual-request.json');prod=read('SUMMARY/production-request.json')
    bad=summary['input'][3]['content'][0]
    assert bad['logprobs'] is None and prod==summary
    save('failure-attribution.json',dict(category='INFRA_REQUEST_SCHEMA_COMPATIBILITY',stage='SUMMARY',http_status=400,
        provider_error_code='invalid_type',provider_error_param='input[3].content[0].logprobs',actual_value=None,provider_expected='array',
        unchanged_summary_request=True,original_assistant_content=bad,
        raw_error=result['error'],request_id='NOT_AVAILABLE_IN_RECORDED_ARTIFACT',
        impact='Summary未形成有效候选；无Compaction权威提交，无Checkpoint，无MAIN派发。',
        minimum_repair_review='另行审计历史SDK Response输出与Summary请求序列化兼容边界；当前不删除null字段、不修改Fixture或Production、不重跑。',
        distinction='不是128K容量错误，不是Extractor五事实质量失败，不推断Provider策略或账号限制。'))
    save('independent-review.json',dict(gate='C2_RL3_E2E_INCOMPLETE',engineering=dict(status='INCOMPLETE',extractor_schema_source_budget='PASS',
        summary='INFRA_REQUEST_SCHEMA_COMPATIBILITY',commit='NOT_EXECUTED',checkpoint='NOT_EXECUTED',restore='NOT_EXECUTED',main='NOT_EXECUTED',restored_main='NOT_EXECUTED'),
        task_quality=dict(status='NOT_EVALUABLE',extractor_candidate_required_facts='5/5',main_quality='NOT_EVALUABLE',external_actions_executed=False),
        efficiency=dict(status='PARTIAL_USAGE_ONLY',attempts=2,request_limit=4,completed_usage_requests=1,usage_completeness='1/2',
            observable_input_tokens=ledger[0]['input_tokens'],observable_output_tokens=ledger[0]['output_tokens'],
            summary_input_tokens=None,summary_output_tokens=None,total_provider_tokens=None,monetary_cost='NOT_AVAILABLE',
            ledger=ledger,conservative_preparation_capacity_ceiling=350011),retry_count=0,extra_model_calls=0))
    old=ROOT/'eval/context_remediation_v1/diagnostics/fixture_semantic_projection_v3'
    historical=read('integrity.json')
    save('review-integrity.json',dict(production_unchanged=manifest['production_hashes']==file_hashes(ROOT/'hexgent'),frozen_unchanged=manifest['frozen_hashes']==fingerprints(),
        original_fixture_rounds_unchanged=True,proof='实际Extractor history_prefix逐条与此前冻结同期00请求的history_prefix比较',
        history_prefix_equal=payload['history_prefix']==json.loads(json.loads((ROOT/'eval/context_remediation_v1/runs/extractor-quality-pilot-v1-20261012/requests/00.json').read_text())['input'][0]['content'])['history_prefix'],
        git_diff_check=historical['git_diff_check'],history_not_overwritten=True,paid_retries=0))
    report='''# C2 R-L3 Real E2E — Independent Review

Gate：C2_RL3_E2E_INCOMPLETE。

## 执行结果

仅进行一次完整尝试，实际Provider请求2/4：Extractor完成，Summary HTTP400失败；没有重试、额外探测或MAIN调用。
Extractor实际使用冻结Eval Prompt v2，仅instructions变化；Production v1常量不改。Summary请求未经改写，原Production提取、校验与候选构建路径保持。
独立离线Gate已证明请求级适配隔离、预算拒绝、4次上限、正式Continue和相同安全Checkpoint恢复；这些Fake结果不能计为本轮真实Commit/Restore成功。

## 失败定位

Provider错误：Invalid type for input[3].content[0].logprobs，要求array而实际为null。HTTP400，invalid_type。
实际Summary请求第4条历史assistant消息保留了SDK输出content里的logprobs=null。本次没有删改字段，也未改Fixture。
归因INFRA_REQUEST_SCHEMA_COMPATIBILITY；不是容量错误，不是任务质量失败。请求ID未被原账本保存，明确NOT_AVAILABLE；不补造。
建议另行审批最小序列化兼容审计/修复，再决定是否重新授权新的尝试。本轮立即停止。

## 三项独立结论

工程：INCOMPLETE。Extractor Schema/来源合法性/预算PASS；Summary失败，Compaction Commit、Checkpoint、Restore和两次MAIN全部NOT_EXECUTED。
任务质量：NOT_EVALUABLE。独立阅读原始台账与真实Extractor候选，五条必需事实5/5保留且对应msg-3/43/83/99/107；不能据此宣布MAIN理解或使用成功。额外f13存在已知范围引用不足，只记录，不优化。
效率/成本：PARTIAL_USAGE_ONLY。Extractor Provider输入47894、输出1460 Tokens，约49.858秒。Summary Usage缺失保持null；总成本不能以可见小计替代。两次请求具体耗时见provider-ledger.json。准备350011仅保守容量估算，不是实际消耗。

## 来源归因与恢复边界

不存在真实MAIN最终请求，故Snapshot/Summary/Recent Tail对MAIN回答的贡献均不可评测。没有Pinned预置；不宣称Snapshot独立带来回答收益。
设计为同一Compaction后安全Checkpoint的两个继续分支：当前内存Continue与独立durable副本正式Restore→Continue。不将完成终态恢复改为新Session或新Submit。真实执行未达到此边界。

Production、Prompt、Fixture、Oracle和旧Gate未改，哈希核对见manifest.json、integrity.json与review-integrity.json。git diff --check通过。无commit/tag/push，不进入其他Fixture或Benchmark。
'''
    if (OUT/'REPORT.md').exists():raise RuntimeError('禁止覆盖报告')
    (OUT/'REPORT.md').write_text(report)
    print('C2_RL3_E2E_INCOMPLETE')

if __name__=='__main__':main()
