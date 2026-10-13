"""第一版批次独立人工判定的记录与汇总；不调用模型或改评分器。"""
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
import statistics
import subprocess

from .batch_v2 import IDS, fixture_paths
from .offline import ROOT, fingerprints
from .factorial_pilot import file_hashes
from hexgent.context.context_compaction import _extract_summary
from hexgent.context.key_fact_snapshot import validate_snapshot

OUT=ROOT/'eval/context_remediation_v1/runs/multiscenario-v2-20261012-v1'

def read(p):return json.loads(p.read_text())
def save(name,value):
    p=OUT/name
    if p.exists():raise RuntimeError('禁止覆盖复核结果')
    p.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def text(r):return ''.join(c.get('text','') for o in r.get('output',[]) for c in o.get('content',[]))

def main():
    manifest=read(OUT/'manifest.json');rows=[];judgments=[];ledgers=[]
    for identity in IDS:
        d=OUT/identity;oracle=read(d/'oracle.json');audit=read(d/'audit.json');result=read(d/'result.json')
        ex=text(read(d/'EXTRACTOR/response.json'));summary=text(read(d/'SUMMARY/response.json'))
        request=read(d/'EXTRACTOR/actual-request.json');payload=json.loads(request['input'][0]['content'])
        raw=json.loads(ex)
        valid=validate_snapshot(raw,cutpoint=payload['as_of_message_id'],sources=payload['history_prefix'],previous=None,budget_tokens=read(d/'config.json')['context']['key_fact_snapshot_tokens'])
        summary_valid=_extract_summary(summary) is not None
        assert file_hashes(ROOT/'hexgent')==manifest['production_hashes']
        maps={m['fact_id']:m for m in audit['oracle_quote_mapping']}
        required=[f for f in oracle['facts'] if f['channel']=='SUMMARY_REQUIRED']
        case_j=[]
        for fact in required:
            eligible=maps[fact['fact_id']]['in_prefix']
            # 这些判定来自本轮逐份阅读原始回答与原Oracle；不执行关键词匹配评分。
            # L2 Summary 完全转向最近测试文件；Snapshot保留前缀4项，第5项实际位于Tail。
            snapshot_present=eligible
            summary_present=identity!='L2'
            item=dict(fixture_id=identity,fact_id=fact['fact_id'],ground_truth=fact['canonical_fact'],quote=fact['quote'],runtime_source=maps[fact['fact_id']]['runtime_source'],
                source_in_compacted_prefix=eligible,original_oracle_channel=fact['channel'],actual_source_channel='PREFIX' if eligible else 'RECENT_TAIL',
                snapshot='PRESENT_CORRECT' if snapshot_present else 'NOT_IN_EXTRACTION_INPUT',
                raw_summary='PRESENT_CORRECT' if summary_present else 'MISSING',
                valid_summary=summary_present and summary_valid,raw_union=snapshot_present or summary_present,
                manual_basis='原实体/值/单位/状态/条件及否定逐项对照，允许释义；L2 Sparse与RRF两个原子记录合计保留复合事实。',
                snapshot_evidence=ex,summary_evidence=summary,
                pipeline_committed=result['status']=='COMPACTION_COMMITTED',
                injected_after_commit=(snapshot_present or summary_present or not eligible) if result['status']=='COMPACTION_COMMITTED' else None,
                extra_fact_semantic_quality='NOT_FULLY_AUDITED')
            case_j.append(item);judgments.append(item)
        ledger=read(d/'provider-ledger.json')
        ledgers.extend([dict(fixture_id=identity,**r) for r in ledger])
        success=result['status']=='COMPACTION_COMMITTED'
        row=dict(id=identity,group=audit['group'],status=result['status'],reason=result.get('reason'),natural_trigger=audit['production_would_trigger'],
            source_chars=audit['source_chars'],source_tokens=audit['source_estimated_tokens'],repeated_line_char_ratio=audit['repeated_line_char_ratio'],
            before_tokens=result['before_tokens'],published_after_tokens=result['after_tokens'],applied_reduction_pct=result['reduction_pct'],
            required_facts=len(required),prefix_eligible=sum(j['source_in_compacted_prefix'] for j in case_j),snapshot_retained=sum(j['snapshot']=='PRESENT_CORRECT' for j in case_j),
            raw_summary_retained=sum(j['raw_summary']=='PRESENT_CORRECT' for j in case_j),valid_summary_retained=sum(j['valid_summary'] for j in case_j),
            summary_format_valid=summary_valid,raw_union_retained=sum(j['raw_union'] for j in case_j),
            successful_final_request_retained=len(required) if success else None,
            provider_requests=len(ledger),provider_input_tokens=sum(r['input_tokens'] for r in ledger),provider_output_tokens=sum(r['output_tokens'] for r in ledger),
            provider_latency_ms=sum(r['latency_ms'] for r in ledger),usage_complete=all(r['usage_available'] for r in ledger),candidate_snapshot_schema_valid=True)
        if success:
            req=read(d/'injection-request-not-dispatched.json')
            assert sum('<key-fact-snapshot>' in str(m) for m in req['input'])==1
            assert req['input'][0]['content'].count('<key-fact-snapshot>')==1
            assert len([r for r in read(d/'transcript.json') if r['type']=='context_compaction_applied'])==1
            row['injection_request_snapshot_once']=True
        rows.append(row)
    def aggregate(values):
        n=len(values);before=sum(r['before_tokens'] for r in values);after=sum(r['published_after_tokens'] for r in values)
        return dict(total=n,committed=sum(r['status']=='COMPACTION_COMMITTED' for r in values),success_rate=sum(r['status']=='COMPACTION_COMMITTED' for r in values)/n,
            applied_equal_weight_reduction_pct=statistics.mean(r['applied_reduction_pct'] for r in values),applied_token_weighted_reduction_pct=100*(1-after/before),
            required_facts=sum(r['required_facts'] for r in values),prefix_eligible=sum(r['prefix_eligible'] for r in values),snapshot_retained=sum(r['snapshot_retained'] for r in values),
            raw_summary_retained=sum(r['raw_summary_retained'] for r in values),valid_summary_retained=sum(r['valid_summary_retained'] for r in values),
            raw_union_retained=sum(r['raw_union_retained'] for r in values),provider_input_tokens=sum(r['provider_input_tokens'] for r in values),provider_output_tokens=sum(r['provider_output_tokens'] for r in values))
    groups={g:aggregate([r for r in rows if r['group']==g]) for g in ['F1','F2','F3','F4','LONG']}
    groups['ALL']=aggregate(rows);groups['SHORT']=aggregate(rows[:12])
    allmetrics=groups['ALL'];long=groups['LONG']
    save('manual-fact-judgments.json',dict(required_fact_judgments=judgments,reviewer='本轮独立工程审阅',count=len(judgments),
        field_contract='raw表示候选文本的语义，不代表格式合格或已发布。source eligibility来自调用前audit冻结字段。',
        obsolete_required_values_incorrectly_reactivated=0,extra_fact_quality='NOT_FULLY_AUDITED',main_behavior='NOT_EVALUATED'))
    save('per-case-results.json',rows);save('provider-usage.json',dict(rows=ledgers,actual_requests=len(ledgers),usage_complete=sum(r['usage_available'] for r in ledgers),
        input_tokens=sum(r['input_tokens'] for r in ledgers),output_tokens=sum(r['output_tokens'] for r in ledgers),sdk_retries=0,main_calls=0))
    gain=dict(method='同一真实Summary的离线表示消融；不重新生成Summary，不调用MAIN，非独立Summary-only算法因果收益。',
        all_prefix_eligible=allmetrics['prefix_eligible'],snapshot_prefix_retained=allmetrics['snapshot_retained'],raw_summary_prefix_retained=allmetrics['raw_summary_retained'],
        raw_union_prefix_retained=allmetrics['raw_union_retained'],additional_prefix_facts=allmetrics['raw_union_retained']-allmetrics['raw_summary_retained'],
        prefix_gain_pp=100*(allmetrics['raw_union_retained']-allmetrics['raw_summary_retained'])/allmetrics['prefix_eligible'],
        long_prefix_required=long['prefix_eligible'],long_summary_prefix_retained=long['raw_summary_retained'],long_union_prefix_retained=long['raw_union_retained'],
        long_prefix_gain_pp=100*(long['raw_union_retained']-long['raw_summary_retained'])/long['prefix_eligible'],
        long_final_request_gold=long['required_facts'],long_summary_plus_tail_retained=long['raw_summary_retained']+1,long_snapshot_summary_tail_retained=long['required_facts'],
        long_final_request_gain_pp=100*(long['required_facts']-long['raw_summary_retained']-1)/long['required_facts'],
        warning='2例Summary格式无效不算可发布Summary；Snapshot不能绕过原子提交失败，不能把其候选覆盖包装为实际救回。')
    save('snapshot-gain.json',gain)
    save('metrics.json',dict(groups=groups,original_gold_count=75,oracle_channel_drift=[dict(fact='L2:fact:7',original='SUMMARY_REQUIRED',actual='RECENT_TAIL',source='msg-75',cut='msg-72',old_oracle_unchanged=True)],
        all_original_gold_snapshot_recall=74/75,all_prefix_snapshot_recall=74/74,all_raw_summary_original_gold_recall=70/75,
        all_raw_summary_prefix_recall=70/74,successful_final_request_recall=15/15,
        interpretation='全体强制提交率20%不是线上失败率。12短样本不触发自然Production压缩；只能用于事实和保护性拒绝诊断。'))
    save('failure-records.json',[r for r in rows if r['status']!='COMPACTION_COMMITTED'])
    diff=subprocess.run(['git','diff','--check'],cwd=ROOT,capture_output=True,text=True)
    assert fingerprints()==manifest['historical_assets'] and file_hashes(ROOT/'hexgent')==manifest['production_hashes']
    save('review-integrity.json',dict(production_unchanged=True,historical_unchanged=True,raw_fixtures_oracles_unchanged=True,network_calls_during_review=0,
        total_actual_provider_calls=30,main_calls=0,git_diff_check=dict(exit_code=diff.returncode,output=diff.stdout+diff.stderr)))
    table='\n'.join(f"|{r['id']}|{r['status']}|{r['before_tokens']}→{r['published_after_tokens']}|{r['applied_reduction_pct']:.2f}%|{r['snapshot_retained']}/{r['prefix_eligible']}|{r['raw_summary_retained']}/{r['prefix_eligible']}|" for r in rows)
    report=f'''# Production v2 + Snapshot — 12+3第一版

## 执行与审计

15个原始Fixture各1次Extractor及Summary，实际30请求、Usage30/30完整，无重试、无真实MAIN。默认Production v2，无请求Prompt覆盖。
短12例536–703字、约508–651估算Tokens，完全重复长行比例为0，无额外填充；属于高事实密度的合成资料。长3例约21–24万字符、6.7–6.9万源正文估算Tokens，由真实冻结源码阅读加合成台账组成，不是自然真实Agent轨迹。长行重复比例13.4%–22.6%，包含代码样板和阅读说明，不等于全部无意义填充。
保留Fixture全文、角色与Oracle内容；短样本按原有来源包装为工具call/output或用户消息，使用旧评测已有recent=100的切点配置，long使用24000。未添加历史答案、未重放真实Handler。每样本max_compaction_attempts=1，仅本次Eval限制调用数。

## 核心结果

全15例提交{allmetrics['committed']}/15（{100*allmetrics['success_rate']:.2f}%）；短样本0/12，长样本3/3。短样本默认不会触发压缩，强制实验被拒绝不等于Production线上故障率。
短样本10例因无收益拒绝，2例F3-03/F4-02因未返回summary标签拒绝。所有失败保留；Candidate未发布。
长样本缩减率61.95%、62.96%、62.73%；等权均值{long['applied_equal_weight_reduction_pct']:.2f}%，Token加权{long['applied_token_weighted_reduction_pct']:.2f}%。
包括未提交样本（保留原活动上下文、缩减为0）的全15例等权均值{allmetrics['applied_equal_weight_reduction_pct']:.2f}%，Token加权{allmetrics['applied_token_weighted_reduction_pct']:.2f}%。不得只报成功子集而隐去失败。
原75个目标事实：Snapshot候选74/75；但L2一项源在Tail，不属于提取输入。按调用前冻结的实际前缀责任，Snapshot74/74，原始Summary70/74。成功提交的3个长样本最终请求15/15目标事实可用。
2例格式错误Summary虽然原文保留目标事实，不能计入合格可发布Summary；valid_summary字段单独统计。

## Snapshot增益

长样本中，同一份Summary对前缀14项保留10项，加入Snapshot后14/14，额外4项，增加{gain['long_prefix_gain_pp']:.2f}个百分点。缺失来自L2的查询ID、文档ID、Dense以及Sparse/RRF状态；L1/L3的Summary已完整保留，没有新增覆盖。
含相同Tail的最终请求消融：Summary+Tail11/15，Snapshot+Summary+Tail15/15，增加{gain['long_final_request_gain_pp']:.2f}个百分点。L2最终第8名在Tail，不归因Snapshot。
全体前缀表示增量4/74，即{gain['prefix_gain_pp']:.2f}个百分点。它是本次表示覆盖消融，不是独立Summary-only算法提升或MAIN任务成功收益。短样本未发布，Snapshot不能“救回”失败提交。

## 逐样本

Snapshot及原始Summary列的分母均为实际输入前缀所含必需事实。缩减使用已发布状态，不代表被拒绝候选大小。

|ID|提交结果|估算请求Token|已应用缩减|Snapshot候选|原始Summary|
|---|---|---|---|---|---|
{table}

## 成本与限制

Provider实际输入{allmetrics['provider_input_tokens']}、输出{allmetrics['provider_output_tokens']} Tokens；逐调用Usage和耗时在provider-usage.json，完整请求原文在各case目录。估算缩减不是计费节省率，辅助提取/摘要成本已计入，没有无压缩Baseline，净效率未证明。
旧Oracle的L2:fact:7渠道漂移单独记录，未修改旧Fixture/Oracle/Gate；原75分母与实际74前缀分母同时公布，不在结果后删掉失败事实。
所有必需事实的状态/单位/否定人工审核，未发现必需旧值误激活；未对全部额外Fact作全面引用语义认证，也不宣称普遍可靠。强制短样本与默认长样本配置不同，不混作统一线上样本分布。
本轮仅1次/样本，MAIN行为未评测，128K仍USER_ASSERTED_CAPACITY_128K_UNVERIFIED。原子提交和注入工程正确不代表实际任务完成。
重现命令：.venv/bin/python -m eval.context_remediation_v1.batch_v2 --prepare --output <全新目录>；需另行授权真实调用后才能加--execute。现有目录禁止重跑；模型具有随机性，不能保证输出数值相同。
Production、冻结资产哈希不变，git diff --check通过；不commit/tag/push，不再扩大样本。
'''
    (OUT/'REPORT.md').write_text(report)
    print(json.dumps(dict(groups=groups,usage_requests=len(ledgers),snapshot_gain=gain),ensure_ascii=False))

if __name__=='__main__':main()
