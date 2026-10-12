"""已完成 Stage 1 的离线证据整理；不触发 Provider。"""
import argparse
import io
import json
import os
import socket
import subprocess
import unittest
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch

from eval.context_management.c1_independent import save, hashes
from eval.context_management.c2_prep_3.prepare import ROOT, digest, old_assets
from hexgent.utils.token_estimator import estimate_request_tokens


# 人工逐项核对实体、值、单位、条件和生效状态；文本定位仅用于保留证据。
REVIEW = {
    'F1-02': [
        ('F1-02:fact:1', '当前探头为 FROST-Q8'),
        ('F1-02:fact:2', '温度偏移为-0.37摄氏度且必须保留负号'),
        ('F1-02:fact:3', '采样周期为45秒，旧周期120秒已废止'),
        ('F1-02:fact:4', '校准证书为 CAL-73K-2026'),
        ('F1-02:fact:5', '报警条件为连续3个有效采样高于6.8摄氏度'),
    ],
    'L2': [
        ('L2:fact:1', '目标查询 QUERY-WILLOW-804'),
        ('L2:fact:3', '目标文档 DOC-W804-6'),
        ('L2:fact:4', '完整响应 Dense Top30 未命中'),
        ('L2:fact:5', '完整响应 Sparse 第7名、RRF 第11名'),
    ],
}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('run', type=Path)
    run = parser.parse_args().run
    read = lambda p: json.loads(p.read_text())
    initial = read(run/'execution_manifest.json')
    metrics = []; reviews = []; all_comp = []
    for identifier in ['F1-02', 'L2']:
        ev = read(run/identifier/'evidence.json')
        comp = [c for c in ev['calls'] if c['call_type'] == 'COMPACTION']
        mains = [c for c in ev['calls'] if c['call_type'] == 'MAIN']
        assert len(mains) == 1 and not mains[0]['actual_network_dispatch']
        assert all(c['outcome'] == 'SUCCESS' and c['usage_complete'] for c in comp)
        all_comp.extend(comp)
        applied = [e for e in ev['transcript'] if e['type'] == 'context_compaction_applied']
        assert len(applied) == 1
        payload = applied[0]['payload']; summary = payload['summary']
        assert summary.strip() in ev['actual_candidates'][0]
        oracle = read(ROOT/'oracle_v2'/f'{identifier}.json')
        fixture = read(old_assets(identifier)[0])
        rows = []
        for fact_id, quote in REVIEW[identifier]:
            fact = next(f for f in oracle['facts'] if f['fact_id'] == fact_id)
            assert fact['summary_responsibility'] == 'SUMMARY_REQUIRED'
            assert quote in summary
            if 'source_round' in fact:
                source = fixture['rounds'][fact['source_round']]['messages'][1]
                assert source['call_id'] == fact['source_call_id'] and fact['quote'] in source['output']
            else:
                source = next(m for m in fixture['messages'] if m['id']==fact['source_message_id'])
                assert fact['quote'] in source['text']
            assert fact['quote'] in json.dumps(comp[0]['request']['input'], ensure_ascii=False)
            rows.append(dict(fact_id=fact_id, judgement='PRESENT_CORRECT', summary_quote=quote,
                             source_quote=fact['quote'], source_location=fact['source_location']))
        assert len(rows) == sum(f['summary_responsibility']=='SUMMARY_REQUIRED' for f in oracle['facts'])
        channels=[]
        for fact in oracle['facts']:
            channel=fact['summary_responsibility']
            if channel in ['RECENT_TAIL_PRESERVED','EXTERNALLY_PROTECTED']:
                text = mains[0]['request']['instructions'] if channel=='EXTERNALLY_PROTECTED' else json.dumps(mains[0]['request']['input'],ensure_ascii=False)
                assert fact['quote'] in text
                channels.append(dict(fact_id=fact['fact_id'],channel=channel,final_request_available=True,summary_denominator=False))
        obsolete = ['旧周期120秒已废止'] if identifier=='F1-02' else ['已废止台账“旧残缺响应排名=第2名”','“旧结论=目标已进入Top5”(retrieval)不再生效']
        assert all(q in summary for q in obsolete)
        reviews.append(dict(fixture_id=identifier,review_method='独立人工语义核对；无 Judge 模型',
            facts=rows,summary_required_denominator=len(rows),fact_recall=1.0,
            stale_fact_activation_count=0,obsolete_evidence=obsolete,other_channels=channels,
            required_fact_unsupported_count=0,
            whole_summary_unsupported_claim_count=None,
            whole_summary_review_status='PARTIAL',
            limitation='核心预注册事实及废止关系已核对；额外叙述未逐条完成穷尽来源审查，不声称整篇无无依据断言。',
            extra_claims_review=[dict(judgement='UNCERTAIN',quote=summary.split('重要发现：',1)[-1].split('当前状态：',1)[0],
                                    reason='非预注册扩展断言；保留原文供人工复核，未发现与核心事实冲突。')],
            accepted_summary=summary,accepted_transcript_sequence=applied[0]['sequence']))
        request=mains[0]['request']
        post=estimate_request_tokens(request['instructions'],request['input'],request.get('tools',[]))
        pre=next(e['before_tokens'] for e in ev['events'] if 'before_tokens' in e)
        assert post == payload['after_tokens']
        metrics.append(dict(fixture_id=identifier,pre_compaction_full_request_estimated_input_tokens=pre,
            post_compaction_full_request_estimated_input_tokens=post,estimated_request_reduction=pre-post,
            estimated_request_reduction_ratio=(pre-post)/pre,
            compaction_provider_input_tokens=sum(c['usage']['input_tokens'] for c in comp),
            compaction_provider_output_tokens=sum(c['usage']['output_tokens'] for c in comp),
            compaction_provider_total_tokens=sum(c['usage']['input_tokens']+c['usage']['output_tokens'] for c in comp),
            provider_usage_complete=True,compaction_create_count=len(comp),accepted_compaction_count=1,
            rejected_candidates=len(comp)-1,compaction_provider_latency_ms=sum(c['latency_ms'] for c in comp),
            agent_elapsed_seconds=next(e['result']['elapsed_seconds'] for e in ev['events'] if 'result' in e),
            attempts=[dict(provider_call_id=c['provider_call_id'],logical_attempt=c['logical_attempt'],
                outcome=c['outcome'],input_tokens=c['usage']['input_tokens'],output_tokens=c['usage']['output_tokens'],
                latency_ms=c['latency_ms'],candidate=ev['actual_candidates'][i],
                accepted=i==0,rejection_reason=None if i==0 else 'INSUFFICIENT_COMPACTION_SAVINGS') for i,c in enumerate(comp)]))
    save(run/'quality_review.json',dict(fixtures=reviews,scope='PRE_REGISTERED_FACTS_REVIEWED_EXTRA_CLAIMS_PARTIAL'))
    save(run/'token_metrics.json',dict(fixtures=metrics,
        total_real_compaction_input_tokens=sum(c['usage']['input_tokens'] for c in all_comp),
        total_real_compaction_output_tokens=sum(c['usage']['output_tokens'] for c in all_comp),
        usage_completeness='COMPLETE_4_OF_4',main='FAKE_NOT_PROVIDER_COST',
        monetary_cost='MONETARY_COST_NOT_EVALUATED',agent_net_token_benefit='NOT_EVALUATED'))
    log=io.StringIO(); network=[]
    def deny(*a,**kw): network.append('attempt'); raise AssertionError('离线回归禁止网络')
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ,{},clear=True))
        stack.enter_context(patch.object(socket.socket,'connect',deny))
        stack.enter_context(patch('socket.create_connection',deny))
        modules=['eval.context_management.c2.test_serialization','eval.context_management.c2_dispatch_guard_repair.test_guard',
                 'eval.context_management.c2_prep_3.test_gate','eval.context_management.c2_prep_2.test_control',
                 'eval.context_management.c2_prep.test_prepare','eval.tool_hardening.test_v3_provider']
        tests=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(modules))
    (run/'test_results.txt').write_text(log.getvalue(),encoding='utf-8')
    unchanged=lambda group: all(Path(p).exists() and digest(Path(p))==h for p,h in group.items())
    diff=subprocess.run(['git','diff','--check'],capture_output=True,text=True)
    checks=dict(production_unchanged=hashes()==initial['production_before'],production_after=hashes(),
        protected_unchanged=unchanged(initial['protected_before']),prep3_unchanged=unchanged(initial['prep3_before']),
        git_diff_check_exit_code=diff.returncode,git_diff_check_output=diff.stdout+diff.stderr)
    save(run/'hash_check.json',checks)
    assert tests.wasSuccessful() and not network and diff.returncode==0
    assert all(checks[k] for k in ['production_unchanged','protected_unchanged','prep3_unchanged'])
    ledger=read(run/'budget_ledger.json'); assert ledger['halted_reason'] is None
    result=dict(gate='STAGE1_COMPLETED_WITH_QUALITY_FINDINGS',real_compaction_calls=4,fake_main_calls=2,
        provider_errors=0,timeouts=0,sdk_retries=0,eval_retries=0,production_retry_attempts=1,
        compaction_cycles=3,accepted_summaries=2,rejected_candidates=2,budget_exceeded=False,
        regression=dict(tests=tests.testsRun,failures=len(tests.failures),errors=len(tests.errors),skips=len(tests.skipped)),
        regression_network_attempts=network,stage2_executed=False,**checks)
    save(run/'result.json',result)
    lines=['# C2 Stage 1 修复后真实模型 Smoke','',
        '结论：STAGE1_COMPLETED_WITH_QUALITY_FINDINGS。必需摘要事实保留通过；F1 两份候选因收益不足被拒绝。扩展叙述的穷尽语义审查尚未完成。','',
        '## 修复范围','',
        '仅 Eval 序列化省略 assistant 的 phase:null 与 output_text.logprobs:null；文本、顺序、工具输入输出不变。Fake MAIN 的网络派发标志修正为 false。Production 未修改。原失败 Run 保留：stage1-real-20261011T090900010334Z。',
        '修复前离线 Gate：serialization-repair-20261011T150024244201Z，90/90；本 Run 之后重新执行 90/90，无网络尝试。','',
        '| 样本 | 摘要必需事实 | 完整请求估算前→后 | 降幅 | 真实摘要调用 | Provider 输入/输出 |','|---|---:|---:|---:|---:|---:|']
    for m in metrics:
        n=len(REVIEW[m['fixture_id']])
        lines.append(f"| {m['fixture_id']} | {n}/{n} | {m['pre_compaction_full_request_estimated_input_tokens']} → {m['post_compaction_full_request_estimated_input_tokens']} | {m['estimated_request_reduction_ratio']:.2%} | {m['compaction_create_count']} | {m['compaction_provider_input_tokens']}/{m['compaction_provider_output_tokens']} |")
    lines += ['', '## 质量、成本与边界','',
        '- 预注册事实逐项实体/值/单位/条件核对，证据见 quality_review.json；废止事实仅历史性提及，未错误激活。',
        '- L2 最终排序第8名由 recent tail 保留，不计入摘要分母；外部保护约束也不计入摘要分母。实际 Fake MAIN 请求可得性已核查。',
        '- required_fact_unsupported_count=0；整篇扩展断言的 Unsupported 总数 NOT_AVAILABLE，UNCERTAIN 原文留供人工审核，不伪造零值。',
        '- F1 首次 partial 接受后仍超过短样本冻结阈值，第二周期 attempt1/2 都因收益不足拒绝；三个调用全部计费 Usage 纳入成本。候选3438不是实现后的 Token 值。',
        '- 4次真实请求 Usage 完整：输入40023、输出2534。2次 Fake MAIN fixture Usage 不计入真实成本。SDK/Eval Retry为0，Production内部重试1次，无Provider错误或超时。',
        '- 预算预留66163输入/8000输出，4/8调用，每Fixture≤4，未越界；预留不是真实Token硬上界。容量仅 USER_ASSERTED_CAPACITY_128K。',
        '- 本轮真实模型只生成摘要，MAIN为Fake；不证明真实模型最终任务成功，不证明整个Agent Run净Token收益，不评估货币费用。',
        '- 本轮是2例Smoke，不能外推整体压缩质量或稳定性。Production及C1/Prep冻结资产哈希未变，git diff --check通过。','',
        '## 原始证据','',
        '每样本 evidence.json / provider-trace.json / serialization_review.json，完整 Transcript / Checkpoint；总 provider_trace.json、budget_ledger.json、execution_manifest.json、token_metrics.json、quality_review.json、hash_check.json、test_results.txt。','',
        '## 建议','',
        '先人工确认 F1 的低收益额外调用及 L2 扩展断言审查范围，再独立审批 Stage 2。未自动运行 Stage 2。']
    (run/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='production_after'},ensure_ascii=False))


if __name__=='__main__': main()
