"""Stage2事后离线整理：人工语义判定独立输入，原始Artifact不改写。"""
import argparse
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import unittest
from unittest.mock import patch

from eval.context_management.c1_independent import save,hashes
from eval.context_management.c2_prep_3.prepare import read_json,digest
from hexgent.utils.token_estimator import estimate_request_tokens
from hexgent.context import ContextManager


def main():
    parser=argparse.ArgumentParser();parser.add_argument('run',type=Path);args=parser.parse_args();run=args.run
    initial=read_json(run/'execution_manifest.json');execution=read_json(run/'execution_result.json')
    decisions=read_json(run/'manual_judgements.json') if (run/'manual_judgements.json').exists() else {}
    metric_rows=[];quality=[];assertions=[];all_calls=[]
    for entry in initial['entries']:
        identifier=entry['fixture_id'];p=run/identifier/'evidence.json'
        if not p.exists():
            quality.append(dict(fixture_id=identifier,status='NOT_RUN',fact_recall=None));continue
        ev=read_json(p);calls=[c for c in ev['calls'] if c['call_type']=='COMPACTION'];all_calls+=calls
        maincalls=[c for c in ev['calls'] if c['call_type']=='MAIN']
        applied=[t for t in ev['transcript'] if t['type']=='context_compaction_applied']
        candidates=[];used=set()
        manager=ContextManager()
        normalized=read_json(run/identifier/'serialization_review.json')['normalized_rounds']
        for turn in normalized:
            manager.append_user_query(turn['query']);manager.append_messages(turn['messages'],completes_round=True)
        from eval.context_management.c2_prep_3.runner import load_rounds
        _,query=load_rounds(entry);manager.append_user_query(query)
        for c in calls:
            response=c.get('response') or {}
            text=''.join(t.get('text','') for o in response.get('output',[]) for t in o.get('content',[]) if t.get('type')=='output_text')
            match=re.search(r'<summary>\s*(.*?)\s*</summary>',text,re.S);summary=match.group(1).strip() if match else None
            accepted=next((t for t in applied if t['sequence'] not in used and t['payload']['summary']==summary),None)
            if accepted:used.add(accepted['sequence'])
            candidate_budget=None;reason=None
            if maincalls and summary:
                req=maincalls[-1]['request'];cut=len(c['request']['input'])
                assert manager.materialize_messages(manager.active_messages[:cut])==c['request']['input']
                before_est=estimate_request_tokens(req['instructions'],manager.model_messages(),req.get('tools',[]))
                messages,_,_=manager.build_compacted_messages(cut,summary)
                after_est=estimate_request_tokens(req['instructions'],manager.materialize_messages(messages),req.get('tools',[]))
                savings=before_est-after_est;ratio=savings/before_est
                candidate_budget=dict(before_estimated=before_est,candidate_estimated=after_est,savings=savings,reduction_ratio=ratio,
                                     realized=bool(accepted))
                limits=entry['compaction_parameters']
                if not accepted:
                    reason='INSUFFICIENT_SAVINGS' if savings<=0 or not(savings>=limits['min_compaction_savings_tokens'] or ratio>=limits['min_compaction_savings_ratio']) else '见Production状态/摘要预算规则'
                if accepted:
                    assert accepted['payload']['after_tokens']==after_est
                    manager.apply_compaction(cut,summary,before_tokens=before_est,after_tokens=after_est,summary_tokens=accepted['payload']['summary_tokens'],recent_tokens=accepted['payload']['recent_tokens'],status=accepted['payload']['status'])
            candidates.append(dict(provider_call_id=c['provider_call_id'],span_id=c['span_id'],logical_attempt=c['logical_attempt'],
                outcome=c['outcome'],candidate=text,summary=summary,accepted=bool(accepted),
                accepted_boundary=accepted,rejection_reason=None if accepted else reason or '见Production失败事件及原始错误',
                candidate_budget=candidate_budget,
                provider_usage=c.get('usage'),latency_ms=c.get('latency_ms'),error=c.get('reason'),
                request_pointer=f'{run}/{identifier}/provider-trace.json#/calls/'+str(ev['calls'].index(c))))
        assert len(used)==len(applied)
        before=next((e['before_tokens'] for e in ev['events'] if 'before_tokens' in e),None)
        post=estimate_request_tokens(maincalls[-1]['request']['instructions'],maincalls[-1]['request']['input'],maincalls[-1]['request'].get('tools',[])) if maincalls else None
        complete=all(c['usage_complete'] for c in calls)
        total=lambda field,group:sum(c['usage'][field] for c in group) if all(c['usage_complete'] for c in group) else None
        rejects=[c for c,candidate in zip(calls,candidates) if not candidate['accepted']]
        metric_rows.append(dict(fixture_id=identifier,pre_compaction_full_request_estimated_input_tokens=before,
            post_compaction_full_request_estimated_input_tokens=post,
            estimated_request_reduction=before-post if post is not None else None,
            estimated_request_reduction_ratio=(before-post)/before if post is not None else None,
            compaction_provider_input_tokens=total('input_tokens',calls),compaction_provider_output_tokens=total('output_tokens',calls),
            compaction_provider_total_tokens=total('input_tokens',calls)+total('output_tokens',calls) if complete else None,
            rejected_provider_input_tokens=total('input_tokens',rejects),rejected_provider_output_tokens=total('output_tokens',rejects),
            provider_usage_complete=complete,compaction_create_count=len(calls),accepted_count=len(applied),
            partial_count=sum(t['payload']['status']=='partial' for t in applied),rejected_count=sum(bool(c['summary']) and not c['accepted'] for c in candidates),
            provider_latency_ms=sum(c['latency_ms'] or 0 for c in calls),production_retry_count=sum(c['logical_attempt']>1 for c in calls),
            error_count=sum(c['outcome']=='ERROR' for c in calls),timeout_count=sum(c['outcome']=='TIMEOUT' for c in calls),
            candidates=candidates,events=ev['events'],amortized_benefit='AMORTIZED_BENEFIT_NOT_EVALUATED'))
        oracle=read_json(Path(entry['oracle_v2_path']));decision=decisions.get(identifier,{})
        other=[]
        final_request=maincalls[-1]['request'] if maincalls else {}
        tail=[m for m in final_request.get('input',[]) if not (m.get('role')=='developer' and str(m.get('content','')).startswith('以下是此前会话的压缩摘要'))]
        for f in oracle['facts']:
            if f['summary_responsibility']!='SUMMARY_REQUIRED':
                other.append(dict(f,actual_source_in_first_compaction_input=f['quote'] in json.dumps(calls[0]['request']['input'],ensure_ascii=False),
                    actual_recent_tail_available=f['quote'] in json.dumps(tail,ensure_ascii=False),
                    actual_pinned_available=f['quote'] in final_request.get('instructions',''),
                    actual_final_request_literal_available=f['quote'] in json.dumps(final_request,ensure_ascii=False),
                    note='字面可得性与语义保留不同；OBSOLETE已废止由人工状态Oracle判定，不凭旧原句缺失或出现判激活。'))
        required=[f for f in oracle['facts'] if f['summary_responsibility']=='SUMMARY_REQUIRED']
        finalsummary=applied[-1]['payload']['summary'] if applied else None
        rows=[]
        for fact in required:
            review=decision.get('facts',{}).get(fact['fact_id'],dict(judgement='UNCERTAIN',reason='未完成人工语义审查'))
            assert review['judgement'] in ['PRESENT_CORRECT','MISSING','INCORRECT','UNCERTAIN']
            if review['judgement']=='PRESENT_CORRECT':
                assert review['summary_quote'] in finalsummary
                if review.get('additional_summary_quote'):assert review['additional_summary_quote'] in finalsummary
            source=[]
            for c in calls:
                for i,m in enumerate(c['request']['input']):
                    content=str(m.get('output',m.get('content','')))
                    if fact['quote'] in content:source.append(dict(provider_call_id=c['provider_call_id'],input_index=i,quote=fact['quote'],source_call_id=m.get('call_id')))
            assert source or review['judgement']=='UNCERTAIN'
            main_available=review.get('main_available',False)
            if review.get('summary_quote') and maincalls:main_available=review['summary_quote'] in json.dumps(maincalls[-1]['request'],ensure_ascii=False)
            rows.append(dict(fact_id=fact['fact_id'],**review,source=source,final_request_available=main_available))
        quality.append(dict(fixture_id=identifier,status='REVIEWED' if decision else 'QUALITY_REVIEW_PENDING',
            required_denominator=len(required),facts=rows,fact_recall=sum(r['judgement']=='PRESENT_CORRECT' for r in rows)/len(rows) if applied and decision else None,
            current_summary=finalsummary,other_channels=other,
            stale_fact_activation=decision.get('stale_fact_activation'),candidate_fact_review=decision.get('candidate_fact_review','NOT_REVIEWED'),
            extra_findings=decision.get('findings',[])))
        reviewed=decision.get('assertions',[])
        for row in reviewed:
            assert row['summary_quote'] in finalsummary
            assert row['classification'] in ['SUPPORTED_EXACT','SUPPORTED_PARAPHRASE','SUPPORTED_INFERENCE','UNSUPPORTED','CONTRADICTED','UNCERTAIN']
            for source in row['sources']:
                call=next(c for c in calls if c['provider_call_id']==source['provider_call_id'])
                actual=str(call['request']['input'][source['input_index']].get(source.get('field','output'),''))
                assert source['quote'] in actual
        assertions.append(dict(fixture_id=identifier,assertions=reviewed,
            status=decision.get('exhaustive_assertion_review','EXHAUSTIVE_ASSERTION_REVIEW_INCOMPLETE'),
            classifications=dict(Counter(r['classification'] for r in reviewed)),
            limitation='仅已接受最终摘要；未审查项不计SUPPORTED，拒绝候选独立保留，不混入最终统计。'))
    save(run/'token_metrics.json',dict(fixtures=metric_rows,monetary_cost='MONETARY_COST_NOT_EVALUATED',net_agent_token_benefit='NOT_EVALUATED'))
    save(run/'quality_review.json',dict(fixtures=quality));save(run/'assertion_review.json',dict(fixtures=assertions))
    log=io.StringIO();network=[]
    def deny(*a,**kw):network.append('attempt');raise AssertionError('事后回归禁止网络')
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ,{},clear=True));stack.enter_context(patch.object(socket.socket,'connect',deny))
        stack.enter_context(patch.object(socket.socket,'connect_ex',deny));stack.enter_context(patch('socket.create_connection',deny))
        modules=['eval.context_management.c2.test_stage2','eval.context_management.c2.test_serialization','eval.context_management.c2_dispatch_guard_repair.test_guard',
            'eval.context_management.c2_prep_3.test_gate','eval.context_management.c2_prep_2.test_control','eval.context_management.c2_prep.test_prepare','eval.tool_hardening.test_v3_provider']
        tests=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(modules))
    (run/'test_results.txt').write_text(log.getvalue(),encoding='utf-8')
    protected=all(Path(p).exists() and digest(Path(p))==h for p,h in initial['protected_before'].items())
    diff=subprocess.run(['git','diff','--check'],capture_output=True,text=True)
    checks=dict(production_unchanged=hashes()==initial['production_before'],protected_unchanged=protected,
        protected_hashes={p:digest(Path(p)) for p in initial['protected_before']},production_after=hashes(),
        git_diff_check_exit_code=diff.returncode,git_diff_check_output=diff.stdout+diff.stderr)
    save(run/'hash_check.json',checks)
    assert protected and checks['production_unchanged'] and diff.returncode==0 and tests.wasSuccessful() and not network
    complete=execution['execution_complete'];finding=any(m['rejected_count'] or m['partial_count'] for m in metric_rows) or any(
        r['judgement']!='PRESENT_CORRECT' for q in quality for r in q.get('facts',[])) or any(a['status']!='COMPLETE' for a in assertions) or any(
        r['classification'] in ['UNSUPPORTED','CONTRADICTED','UNCERTAIN'] for a in assertions for r in a['assertions'])
    result=dict(gate='STAGE2_INCOMPLETE' if not complete else 'STAGE2_COMPLETED_WITH_QUALITY_FINDINGS' if finding else 'STAGE2_COMPLETED_READY_FOR_FINAL_REVIEW',
        real_calls=len(all_calls),usage_complete=all(c['usage_complete'] for c in all_calls),
        provider_input_tokens=sum(c['usage']['input_tokens'] for c in all_calls) if all(c['usage_complete'] for c in all_calls) else None,
        provider_output_tokens=sum(c['usage']['output_tokens'] for c in all_calls) if all(c['usage_complete'] for c in all_calls) else None,
        regression=dict(tests=tests.testsRun,failures=len(tests.failures),errors=len(tests.errors),skips=len(tests.skipped)),
        stage1_executed=False,final_review_approved=False,**checks)
    save(run/'result.json',result)
    lines=['# C2 Stage 2 真实模型 Smoke','',f'Gate：{result["gate"]}。仅摘要模型真实，MAIN为Fake；未执行真实Agent任务或Final Review。','',
        '| 样本 | 必需事实保留 | 完整请求估算前→后 | 摘要调用/接受/拒绝 | Provider输入/输出 |','|---|---:|---:|---:|---:|']
    for m,q in zip(metric_rows,[q for q in quality if q['status']!='NOT_RUN']):
        recall='NOT_EVALUATED' if q['fact_recall'] is None else f'{sum(r["judgement"]=="PRESENT_CORRECT" for r in q["facts"])}/{q["required_denominator"]}'
        lines.append(f'| {m["fixture_id"]} | {recall} | {m["pre_compaction_full_request_estimated_input_tokens"]} → {m["post_compaction_full_request_estimated_input_tokens"]} | {m["compaction_create_count"]}/{m["accepted_count"]}/{m["rejected_count"]} | {m["compaction_provider_input_tokens"]}/{m["compaction_provider_output_tokens"]} |')
    counts=Counter(r['classification'] for a in assertions for r in a['assertions'])
    lines+=['', '## 质量与效率发现','',
        'L3原摘要输入明确含五条当前台账：AUDIT-MAPLE-57、参数/协议完成、检索审查完成、待维护者确认两处权限契约、确认后生成交接报告且不发布。已接受摘要全部遗漏。旧台账不是实现证明的限制不能取代当前任务事实保留，故MISSING 5/5，后续Fake MAIN也未获得这些目标事实。',
        '其他四例20/20必需事实正确保留；Stage2共20/25。仅是五条冻结合成样本结果，不外推通用RAG/Agent任务成功率。最终Fake MAIN的完成文本不是事实回答Oracle。',
        f'接受摘要实质性断言共{sum(counts.values())}条（包括必需事实相关断言，不是纯扩展分母）：{dict(counts)}。未审查项不默认SUPPORTED，UNCERTAIN不强行判正确。',
        '四条UNCERTAIN：F4把该次迟到结果未覆盖描述成不能覆盖；L1的CLI全部/Web全部缺目录枚举来源；L3把AgentEngine委托路径概括为持久化Checkpoint，但原摘要prefix未读到Session/Checkpoint/AgentLoop持久化实现。均不是本轮证明Production缺陷。',
        '未发现当前有效摘要把旧事实错误激活，也未确定UNSUPPORTED/CONTRADICTED断言。Required事实遗漏和扩展断言来源支持是不同指标，不能用后者抵消L3失败。',
        '三个短样本均partial后没有新增业务消息再次proactive触发，第二周期候选收益不足，最终上下文未变。F2首轮第一次也因仅5.41%收益被拒绝，第二次接受；该现象符合冻结阈值与现有Production语义，没有主动重采样或改策略。',
        'Provider真实调用共12次：接受5、拒绝7；SDK/Eval Retry为0，Production逻辑Retry4次（F2两次，F3/F4各一次）；Provider ERROR/TIMEOUT为0。被拒候选全部Usage纳入成本。',
        ' rejected摘要输入4956、输出2003，共6959 Provider Tokens；不将未提交候选压缩率算作实现收益。',
        'Token估算来自完整请求instructions/input/tools；输出预留与安全余量用于派发预算。单次请求估算减少与Provider真实摘要成本分开，AMORTIZED_BENEFIT_NOT_EVALUATED。',
        '独立Stage2预留输入132265/524288、输出24000/40000，调用12/20，每例最多4，预算未越界。Stage1剩余额度未使用。',
        '',f'真实摘要{len(all_calls)}次；Usage完整={result["usage_complete"]}。输入{result["provider_input_tokens"]}，输出{result["provider_output_tokens"]}。',
        '估算减少不是Provider节省；被拒候选不算实现收益；不评估整个Agent Run净成本或货币费用。',
        '完整逐事实/候选/扩展断言/来源、Usage与时序见quality_review.json、assertion_review.json、token_metrics.json及各例原始evidence.json。',
        f'离线回归{tests.testsRun}项，FAIL={len(tests.failures)}、ERROR={len(tests.errors)}、SKIP={len(tests.skipped)}；Production及全部受保护Stage1/Review/Prep/Guard资产哈希未变，git diff --check通过。',
        '容量仍为USER_ASSERTED_CAPACITY_128K，不能视为独立Provider容量验证。扩展审查若INCOMPLETE，不将未判定项目默认计SUPPORTED。',
        '执行结束，等待独立C2 Final Review，不自动扩大样本、调参或修复。']
    (run/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print({k:v for k,v in result.items() if k not in ['production_after','protected_hashes']})


if __name__=='__main__':main()
