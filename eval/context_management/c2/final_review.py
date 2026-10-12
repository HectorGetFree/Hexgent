"""C2独立离线最终审查：只读原始Run，冻结结果与Finding而非认证Production。"""
from collections import Counter
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import socket
import subprocess


REPO=Path(__file__).resolve().parents[3]
BASE=REPO/'eval/context_management'
RUNS=BASE/'c2/runs'
S1=RUNS/'stage1-real-20261011T150121165142Z'
S2=RUNS/'stage2-real-20261012T020350788417Z'
POST=BASE/'c2/reviews/stage1-post-smoke-20261011T152803062442Z'
HISTORY=[RUNS/'stage2-real-20261011T153707035984Z',RUNS/'stage2-real-20261012T015500663297Z']
SCHEMA=RUNS/'stage1-real-20261011T090900010334Z'


def read(p):return json.loads(p.read_text(encoding='utf-8'))
def digest(p):return sha256(p.read_bytes()).hexdigest()
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def snapshot(roots):
    return {str(p):digest(p) for root in roots for p in root.rglob('*') if p.is_file() and '__pycache__' not in str(p)}
def text(response):
    return ''.join(c.get('text','') for o in (response or {}).get('output',[]) for c in o.get('content',[]) if c.get('type')=='output_text')
def usage(calls):
    complete=all(c.get('usage_complete') for c in calls)
    return dict(input_tokens=sum(c['usage']['input_tokens'] for c in calls) if complete else None,
                output_tokens=sum(c['usage']['output_tokens'] for c in calls) if complete else None,
                total_tokens=sum(c['usage']['input_tokens']+c['usage']['output_tokens'] for c in calls) if complete else None,
                complete=complete,call_count=len(calls))


F1_ASSERTIONS='''
核对资料来源 F1-02:source:0、source:1、source:2|已核对|SUPPORTED_INFERENCE|三组工具返回及确认在合成历史中存在，非本轮真实工具执行。
仅核对资料|任务是核对交接条件|SUPPORTED_PARAPHRASE|任务授权仅核对资料。
不连接设备|不连接设备|SUPPORTED_EXACT|来源明确。
不执行配置|不包含设备写入指令|SUPPORTED_INFERENCE|资料核对与设备写入分离，不新增执行授权。
校准证书原件只读，不得修改|不得修改校准证书原件|SUPPORTED_PARAPHRASE|明确证书保护。
区分已核对参数与待人工下发事项|分离已核对的参数和待人工下发事项|SUPPORTED_PARAPHRASE|保留已完成/待完成边界。
已读取并核对三个来源|已核对|SUPPORTED_INFERENCE|可见合成阅读历史，不是本轮Agent工作负载成功率。
当前探头为 FROST-Q8|当前探头=FROST-Q8|SUPPORTED_PARAPHRASE|实体与值一致。
采样周期为45秒|采样周期=45秒|SUPPORTED_PARAPHRASE|值/单位一致。
旧周期120秒已废止|旧采样周期=120秒|SUPPORTED_PARAPHRASE|原输出明确该历史项废止。
连续3个有效采样高于6.8摄氏度|报警条件=连续3个有效采样高于6.8摄氏度|SUPPORTED_PARAPHRASE|次数/有效采样/严格高于/单位均一致。
单个缺失采样不立即触发|单个缺失采样不会立即触发温度报警|SUPPORTED_PARAPHRASE|不改变缺失采样规则。
温度偏移为-0.37摄氏度|温度偏移=-0.37摄氏度|SUPPORTED_PARAPHRASE|保留负号与单位。
必须保留负号|负偏移必须保留符号|SUPPORTED_PARAPHRASE|方向未丢失。
校准证书为 CAL-73K-2026|校准证书=CAL-73K-2026|SUPPORTED_PARAPHRASE|证书编号一致。
校准仅适用于冷藏段|本次校准只适用于冷藏段|SUPPORTED_PARAPHRASE|适用范围不扩大。
不适用于冷冻段|不适用于冷冻段|SUPPORTED_EXACT|来源明确排除范围。
证书环境温度不可当作报警阈值|证书上的环境温度不能误当作报警阈值|SUPPORTED_PARAPHRASE|两种温度语义不同。
短暂离线不等于永久退役|短暂离线也不是设备永久退役|SUPPORTED_PARAPHRASE|临时/永久状态正确。
过期周检提醒仅为历史记录|过期的周检提醒，它是历史记录而非当前采样规则|SUPPORTED_PARAPHRASE|不激活过期规则。
资料满足交接核对条件|资料满足交接|SUPPORTED_PARAPHRASE|原始巡检资料明确资料满足交接，不等于上线。
没有证据表明配置已下发|没有证明设备已经下发配置|SUPPORTED_PARAPHRASE|保持未证明状态。
设备已上线|不能称上线完成|SUPPORTED_PARAPHRASE|处于没有证据的否定语境，不断言已上线。
附件不是可执行脚本|校准附件不是可执行脚本|SUPPORTED_PARAPHRASE|材料类型一致。
不含设备写入指令|不包含设备写入指令|SUPPORTED_PARAPHRASE|没有扩大执行范围。
探头编号、偏移单位和证书号需分别引用其独立出处|探头编号、偏移单位和证书号都有独立出处|SUPPORTED_INFERENCE|有独立出处且后续不能只说校准正常，引用建议可推导。
应分列已核对参数、适用范围及待人工下发/确认事项|分离已核对的参数和待人工下发事项|SUPPORTED_PARAPHRASE|结合适用范围说明的交接建议，不当作已完成行为。
并注明旧采样周期已废止|最终交接要注明旧周期已废止|SUPPORTED_PARAPHRASE|未来动作来源明确。
错误与恢复：无|已核对|SUPPORTED_INFERENCE|可见历史无失败状态；不推断真实设备无错误。
'''


def main():
    def deny(*a,**kw):raise AssertionError('最终审查禁止网络')
    socket.socket.connect=deny;socket.socket.connect_ex=deny;socket.create_connection=deny
    frozen=BASE/'c2_prep_3';manifest=read(frozen/'smoke_manifest.json')
    groups=[REPO/'hexgent',BASE/'c2_prep',BASE/'c2_prep_2',frozen,BASE/'c2_dispatch_guard_repair',
            BASE/'runs/c1-independent-20261011T065644251729Z',S1,S2,POST,SCHEMA,*HISTORY]
    before=snapshot(groups)
    output=BASE/'c2/reviews'/('final-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True)
    executions=[];facts=[];efficiency=[];selected=[];accepted_calls=[];rejected_calls=[];evidence={}
    from hexgent.context.context_compaction import COMPACTION_INSTRUCTIONS
    from hexgent.utils.token_estimator import estimate_request_tokens
    for run in [S1,S2]:
        recorded=read(run/'execution_manifest.json');top=read(run/'provider_trace.json');ledger=read(run/'budget_ledger.json')
        metrics=read(run/'token_metrics.json')['fixtures'];quality=read(run/'quality_review.json')['fixtures']
        entries=recorded['entries'];expected=[e for e in manifest['fixtures'] if e['stage']==recorded['stage']]
        assert entries==expected
        stage_calls=[];stage_main=[];config_valid=True
        for entry in entries:
            identifier=entry['fixture_id'];ev=read(run/identifier/'evidence.json');evidence[identifier]=(run,ev,entry)
            assert digest(Path(entry['fixture_path']))==entry['fixture_hash']
            assert digest(Path(entry['oracle_v2_path']))==entry['oracle_v2_hash']
            assert next(t['calls'] for t in top['fixtures'] if t['fixture_id']==identifier)==ev['calls']
            calls=[c for c in ev['calls'] if c['call_type']=='COMPACTION'];mains=[c for c in ev['calls'] if c['call_type']=='MAIN']
            stage_calls+=calls;stage_main+=mains;selected+=calls
            assert len(mains)==1 and not mains[0]['actual_network_dispatch']
            config=ev['observations'][0]['config']['context']
            assert all(config[k]==v for k,v in entry['compaction_parameters'].items())
            for c in calls:
                expected_prompt=COMPACTION_INSTRUCTIONS+('' if c['logical_attempt']==1 else '\n上一次摘要格式或压缩收益不合格。必须显著缩短内容，并严格只输出 summary 区块。')
                assert c['request']['instructions']==expected_prompt
                assert c['model']=='gpt-6.1-sol' and c['provider']=='www.rightapi.ai'
                assert c['request']['max_output_tokens']==2000 and c['request']['stream'] is True
                assert 'tools' not in c['request'] and c['physical_attempt']==1
                assert c['actual_network_dispatch'] and c['usage_complete'] and c['outcome']=='SUCCESS'
                assert c['reservation']['consumed']
                obs=[o for o in ev['observations'] if o.get('span_id')==c['span_id']]
                assert [o['event'] for o in obs]==['provider_started','provider_finished']
                assert obs[0]['execution_id']==c['execution_id'] and obs[0]['attempt']==c['logical_attempt']
            boundaries=[t for t in ev['transcript'] if t['type']=='context_compaction_applied'];assert len(boundaries)==1
            accepted_summary=boundaries[0]['payload']['summary']
            matched=[]
            for c in calls:
                match=re.search(r'<summary>\s*(.*?)\s*</summary>',text(c['response']),re.S)
                if match and match.group(1).strip()==accepted_summary and not matched:
                    accepted_calls.append(c);matched.append(c)
                else:rejected_calls.append(c)
            assert len(matched)==1
            oracle=read(Path(entry['oracle_v2_path']));q=next(x for x in quality if x['fixture_id']==identifier)
            old_rows=q['facts'];required=[f for f in oracle['facts'] if f['summary_responsibility']=='SUMMARY_REQUIRED']
            rows=[]
            for f in required:
                old=next(x for x in old_rows if x['fact_id']==f['fact_id'])
                review=old['judgement'];quote=old.get('summary_quote')
                if review=='PRESENT_CORRECT':
                    assert quote in accepted_summary
                    if old.get('additional_summary_quote'):assert old['additional_summary_quote'] in accepted_summary
                else:assert identifier=='L3' and review=='MISSING'
                source=[]
                for i,m in enumerate(calls[0]['request']['input']):
                    raw=str(m.get('output',m.get('content','')))
                    if f['quote'] in raw:
                        at=raw.index(f['quote']);source.append(dict(input_index=i,source_call_id=m.get('call_id'),
                            field='output' if 'output' in m else 'content',quote=f['quote'],excerpt=raw[max(0,at-120):at+len(f['quote'])+100]))
                assert source
                rows.append(dict(fact_id=f['fact_id'],canonical_fact=f['quote'],source_status=f['status'],
                    responsibility=f['summary_responsibility'],judgement=review,summary_quote=quote,
                    independently_checked=True,source=source,provider_call_id=calls[0]['provider_call_id'],
                    accepted_transcript_sequence=boundaries[0]['sequence'],
                    raw_request_reference=f'{run}/{identifier}/evidence.json#/calls/0/request/input',
                    note='实体/值/条件/时序人工核对；F3权限事实同时核对完成与不得扩大；L3逐读全文确实不存在完整目标事实。'))
            facts.append(dict(fixture_id=identifier,correct=sum(r['judgement']=='PRESENT_CORRECT' for r in rows),required=len(rows),
                fact_recall=sum(r['judgement']=='PRESENT_CORRECT' for r in rows)/len(rows),facts=rows,summary=accepted_summary))
            m=next(x for x in metrics if x['fixture_id']==identifier)
            final=mains[0]['request'];post=estimate_request_tokens(final['instructions'],final['input'],final.get('tools',[]))
            assert post==m['post_compaction_full_request_estimated_input_tokens']==boundaries[0]['payload']['after_tokens']
            assert m['pre_compaction_full_request_estimated_input_tokens']==ev['events'][0]['before_tokens']
            again=len([o for o in ev['events'] if 'before_tokens' in o and 'after_tokens' not in o])>1
            efficiency.append(dict(fixture_id=identifier,before=m['pre_compaction_full_request_estimated_input_tokens'],after=post,
                savings=m['pre_compaction_full_request_estimated_input_tokens']-post,
                ratio=(m['pre_compaction_full_request_estimated_input_tokens']-post)/m['pre_compaction_full_request_estimated_input_tokens'],
                actual_summary=accepted_summary,provider_calls=len(calls),provider_usage=usage(calls),
                accepted_usage=usage(matched),rejected_usage=usage([c for c in calls if c not in matched]),
                production_retries=sum(c['logical_attempt']>1 for c in calls),
                repeated_proactive_without_business_messages=again,
                attribution=['CONFIGURATION_INDUCED','EXPECTED_BY_CURRENT_POLICY','SUSPECTED_POLICY_INEFFICIENCY'] if again else ['EXPECTED_BY_CURRENT_POLICY'],
                candidate_counterfactuals=read(POST/'f1_call_timeline.json')['candidates'] if identifier=='F1-02' else m.get('candidates',[]),
                elapsed_ms=sum(c['latency_ms'] for c in calls),timer_scope='Eval create→stream终态；不等同于Provider纯计算或Agent E2E',
                source_reference=f'{run}/token_metrics.json'))
        assert len(stage_calls)==ledger['calls']
        assert {c['provider_call_id'] for c in stage_calls}=={r['provider_call_id'] for r in ledger['reservations']}
        assert len({c['span_id'] for c in stage_calls})==len(stage_calls)
        assert len({c['reservation']['reservation_id'] for c in stage_calls})==len(stage_calls)
        limits=next(s for s in manifest['stages'] if s['stage']==recorded['stage'])
        assert ledger['calls']<=limits['max_compaction_calls'] and ledger['input_reserved']<=limits['max_total_reserved_input'] and ledger['output_reserved']<=limits['max_total_reserved_output']
        assert ledger['halted_reason'] is None
        executions.append(dict(run=str(run),stage=recorded['stage'],frozen_fixture_order=[e['fixture_id'] for e in entries],
            fixture_hashes_match=True,oracle_hashes_match=True,configuration_matches=True,prompt_matches=True,
            actual_provider=recorded['provider'],real_calls=len(stage_calls),fake_main=len(stage_main),
            unique_call_span_reservation=True,consumed_reservations=True,usage=usage(stage_calls),
            sdk_retries=0,eval_retries=0,production_retries=sum(c['logical_attempt']>1 for c in stage_calls),
            ledger_input=ledger['input_reserved'],ledger_output=ledger['output_reserved'],run_budget_within_limits=True,
            approval=recorded.get('approval'),trace='预派发源码+前序离线门禁+实际Span/Reservation；非以事后reconcile代替门禁',
            evidence_reference=str(run/'execution_manifest.json')))
    assert len(selected)==16 and len(accepted_calls)==7 and len(rejected_calls)==9
    assert usage(selected)['total_tokens']==129986
    save(output/'execution_audit.json',dict(selected_successful_runs=executions,
        contract_scope='本次完整两阶段七例；历史重新授权Run另列，不混入选定样本分母或声称全部历史调用≤阶段20',
        source_locations=['context_compaction.py:217-313','c2_dispatch_guard_repair/guard.py:82-150','c2_dispatch_guard_repair/entry.py:27-74']))
    save(output/'fact_retention_matrix.json',dict(fixtures=facts,total_correct=29,total_required=34,recall=29/34,
        sample_pass=6,sample_count=7,short=dict(correct=20,required=20,passed=4,samples=4),
        long=dict(correct=9,required=14,passed=2,samples=3),worst='L3 0/5，完整任务台账事实丢失',
        exclusions=['EXTERNALLY_PROTECTED','RECENT_TAIL_PRESERVED','OBSOLETE'],
        e2e_task_effectiveness='NOT_EVALUATED_FAKE_MAIN'))
    # 历史失败单独保留；不把无Usage当零，也不忽略额外未完成Run中的成功请求。
    historical=[]
    for run in HISTORY+[SCHEMA]:
        top=read(run/'provider_trace.json');cs=[c for f in top['fixtures'] for c in f['calls'] if c['call_type']=='COMPACTION']
        known=[c for c in cs if c.get('usage_complete')];unknown=[c for c in cs if not c.get('usage_complete')]
        ledger=read(run/'budget_ledger.json');recorded=read(run/'execution_manifest.json')
        historical.append(dict(run=str(run),category='SCHEMA_REQUEST_REJECTION' if run==SCHEMA else 'INCOMPLETE_CONNECTION_RUN',
            sdk_create_attempts=len(cs),confirmed_usage_call_count=len(known),observed_usage_subtotal=usage(known) if known else dict(
                input_tokens=None,output_tokens=None,total_tokens=None,complete=False,call_count=0,status='NO_REPORTED_USAGE'),
            complete_run_cost=None if unknown else usage(cs),unknown_usage_call_count=len(unknown),
            failures=[dict(provider_call_id=c['provider_call_id'],span_id=c['span_id'],error=c.get('reason'),
                usage=None,upstream_model_receive_status='UNKNOWN',billing_status='UNKNOWN',response=c.get('response')) for c in unknown],
            approval=recorded.get('approval'),budget_reservations=dict(calls=ledger['calls'],input=ledger['input_reserved'],output=ledger['output_reserved']),
            raw_error=read(run/'execution_error.json') if (run/'execution_error.json').exists() else None,
            exclusion='不进入七例16次成功摘要/129986Token的分母；Usage未知不补零',
            required_note='denied列表包含下游APIConnectionError，不能仅据该列表判Guard授权失败。'))
    connection=[r for r in historical if r['category']=='INCOMPLETE_CONNECTION_RUN']
    assert sum(r['sdk_create_attempts'] for r in connection)==9
    assert sum(r['unknown_usage_call_count'] for r in connection)==3
    history_known=dict(input_tokens=sum(r['observed_usage_subtotal']['input_tokens'] for r in connection if r['confirmed_usage_call_count']),
                       output_tokens=sum(r['observed_usage_subtotal']['output_tokens'] for r in connection if r['confirmed_usage_call_count']))
    assert history_known==dict(input_tokens=4563,output_tokens=1729)
    save(output/'historical_failure_accounting.json',dict(runs=historical,connection_create_attempts=9,
        connection_known_usage=history_known,connection_known_total=6292,connection_unknown_attempts=3,
        unexpected_additional_run=str(HISTORY[1]),
        finding='Prompt仅提一条历史连接失败Run；原始目录另有7次尝试的未完成Run，不能默默丢弃。',
        aggregate_stage2_attempts_across_authorizations=21,per_run_scope='2+7+12；每个Run账本≤20，不能据此声称跨审批累计≤20。后续必须显式绑定预算续用/刷新，不推断该21次都是同一未刷新授权。',
        unknown_billing='UNKNOWN，完整历史货币/Token成本不可重建；已观测Subtotal不是总成本'))
    save(output/'provider_usage_reconciliation.json',dict(selected_successful_runs=executions,
        total=usage(selected),accepted=usage(accepted_calls),rejected=usage(rejected_calls),
        historical_separate_accounting='historical_failure_accounting.json',
        all_observed_input_subtotal=123070+4563,all_observed_output_subtotal=6916+1729,
        all_observed_total_subtotal=129986+6292,unknown_usage_requests=5,
        note='所列历史连接3次+Stage1 schema拒绝2次未知Usage；136278是可观测小计，不是所有请求真实账单。'))
    save(output/'efficiency_review.json',dict(fixtures=efficiency,accepted=usage(accepted_calls),rejected=usage(rejected_calls),
        production_retries=5,sdk_retries=0,eval_retries=0,
        late_no_business_message_recompaction=['F1-02','F2-01','F3-02','F4-03'],
        confirmed_defect=False,classification='短样本冻结低阈值诱发、现有策略预期、疑似低收益重复调用，不外推默认64K普遍无效',
        compaction_model_cost='ALL_SELECTED_ATTEMPTS_INCLUDED',run_report_usage='KNOWN_OBSERVABILITY_GAP_MAIN_ONLY',
        amortized='AMORTIZED_BENEFIT_NOT_EVALUATED',monetary='MONETARY_COST_NOT_EVALUATED',
        capacity='USER_ASSERTED_CAPACITY_128K',net_agent_run_benefit='NOT_EVALUATED'))
    # F1曾未穷尽审查，本轮独立补齐；不回写旧质量文件。
    _,ev,_=evidence['F1-02'];summary=facts[0]['summary'];input_=ev['calls'][0]['request']['input'];f1_assertions=[]
    for line in F1_ASSERTIONS.strip().splitlines():
        q,source,classification,reason=line.split('|');assert q in summary
        witnesses=[dict(input_index=i,field=k,quote=source) for i,m in enumerate(input_) for k in ['output','content'] if source in str(m.get(k,''))]
        assert witnesses
        f1_assertions.append(dict(summary_quote=q,classification=classification,reason=reason,source=witnesses,
            pointer=str(S1/'F1-02/evidence.json')+'#/calls/0/request/input'))
    l2=read(POST/'l2_assertion_review.json');s2_assert=read(S2/'assertion_review.json')
    # 独立复核已有逐项来源引用真实位于摘要输入，不借助Gold或MAIN反向证明。
    for f in s2_assert['fixtures']:
        _,ev,_=evidence[f['fixture_id']];calls=ev['calls']
        for a in f['assertions']:
            assert a['summary_quote'] in next(t['payload']['summary'] for t in ev['transcript'] if t['type']=='context_compaction_applied')
            for s in a['sources']:
                c=next(c for c in calls if c['provider_call_id']==s['provider_call_id'])
                assert s['quote'] in str(c['request']['input'][s['input_index']].get(s.get('field','output'),''))
    for a in l2['assertions']:
        assert a['summary_quote'] in evidence['L2'][1]['actual_candidates'][0]
        for e in a['evidence']:
            ptr=e.get('pointer',e.get('request_pointer',''));match=re.search(r'input\[(\d+)\]',ptr)
            if match:
                m=evidence['L2'][1]['calls'][0]['request']['input'][int(match.group(1))]
                assert e['quote'] in (m.get('output','') if '.output' in ptr else json.dumps(m,ensure_ascii=False))
    count=lambda xs:dict(Counter(a['classification'] for a in xs))
    uncertain=[dict(fixture_id=f['fixture_id'],**a) for f in s2_assert['fixtures'] for a in f['assertions'] if a['classification']=='UNCERTAIN']
    uncertain += [dict(fixture_id='L2',**a) for a in l2['assertions'] if a['scope']=='EXTENDED' and a['classification']=='UNCERTAIN']
    assert len(uncertain)==6
    save(output/'assertion_review_summary.json',dict(groups=[
        dict(fixture_id='F1-02',new_final_review=True,scope='完整实质断言，含必需事实相关项',count=len(f1_assertions),classifications=count(f1_assertions),assertions=f1_assertions),
        dict(fixture_id='L2',scope='原Post Review EXTENDED排除4条REQUIRED',count=188,classifications=l2['extended_classification_counts'],
             source=str(POST/'l2_assertion_review.json')),
        dict(stage=2,scope='225实质断言包括必需事实，不是纯扩展分母',count=225,
             classifications=count([a for f in s2_assert['fixtures'] for a in f['assertions']]),source=str(S2/'assertion_review.json'))],
        uncertainty=uncertain,unsupported_observed=0,contradicted_observed=0,
        exhaustive_review_status='COMPLETE_WITH_UNCERTAIN_JUDGEMENTS',
        cross_stage_support_rate=None,reason='没有统一断言统计粒度与重复分母，不把188/225/29相加做一种支持率。',
        limits='未发现无依据断言不等于事实无损；L3遗漏不能被支持率掩盖。源正文可能是旧冻结源码，不将静态结论自动称当前Production认证。'))
    # L3核心失效与替代渠道分开：完整复合事实丢失，发布禁令却仍受保护。
    run,ev,entry=evidence['L3'];main=ev['calls'][-1]['request'];comp=ev['calls'][0]['request']
    summary=next(t['payload']['summary'] for t in ev['transcript'] if t['type']=='context_compaction_applied')
    tail=[m for m in main['input'] if not (m.get('role')=='developer' and str(m.get('content','')).startswith('以下是此前会话的压缩摘要'))]
    assert '禁止执行实际服务发布' in main['instructions']
    assert not any(m.get('content')=='继续当前交接，使用当前有效结论，不进行业务变更。' for m in comp['input'])
    l3_rows=next(f['facts'] for f in facts if f['fixture_id']=='L3')
    for row in l3_rows:
        assert row['canonical_fact'] not in json.dumps(tail,ensure_ascii=False)
    transcript_occurrences=[dict(sequence=t['sequence'],type=t['type']) for t in ev['transcript'] if 'AUDIT-MAPLE-57' in json.dumps(t,ensure_ascii=False)]
    assert transcript_occurrences
    proposals=[
        dict(direction='显式当前任务状态保护/结构化台账',failure='保持批次、已完成、阻塞依赖、下一动作，不仅禁令',production_change=True,
             extra_llm='受信任API维护不必；自动识别需另审',token_latency='固定注入增加Token，局部校验延迟；须预算完整拒绝',
             overlap='优先评估复用Plan状态，非跨Session Memory，不重建通用TaskState',independent_eval=True),
        dict(direction='摘要输入携带当前任务意图',failure='摘要模型只见历史阅读目标，未见最新交接请求',production_change=True,
             extra_llm='无需增加调用次数',token_latency='增加当前Query/意图输入Token',overlap='Context组装/信任边界，不能把工具指令升级为用户约束',independent_eval=True),
        dict(direction='关键事实保留检查',failure='接受前无语义保真门禁',production_change=True,
             extra_llm='显式已确认事实可确定性校验；自动提取不在本轮选定',token_latency='校验成本及保护区Token；拒绝可能增加摘要尝试',
             overlap='Pinned仅显式硬约束，Plan负责进度，事实更新/撤销须有来源契约',independent_eval=True),
        dict(direction='接受前语义验证',failure='含实体但缺条件/旧状态错激活/重要事实遗漏',production_change=True,
             extra_llm='LLM Judge通常需要额外调用且自身不保证正确',token_latency='额外Token和等待，可能误拒绝',overlap='不可用Judge替代独立Oracle',independent_eval=True),
        dict(direction='分层摘要/外部事实保护',failure='长源码淹没任务状态',production_change=True,
             extra_llm='确定性保护可无新增；分块LLM摘要增加调用',token_latency='更多保护区/索引Token或多摘要延迟',overlap='与Plan/Pinned配合，不偷改Memory/ContextRecord范围',independent_eval=True),
        dict(direction='高风险显式不可压缩约束',failure='禁止发布等安全要求在摘要中遗漏',production_change='Pinned V1已支持显式约束；事实保护若扩展需审批',
             extra_llm='现有显式Pin无需',token_latency='保护区Token与超预算拒绝',overlap='不能将所有状态事实塞入Pinned或声称Prompt等同Tool权限',independent_eval=True)]
    save(output/'l3_critical_finding.json',dict(required_facts=l3_rows,
        input_provider_call_id=ev['calls'][0]['provider_call_id'],input_count=len(comp['input']),
        last_historical_user_queries=[m['content'] for m in comp['input'] if m.get('role')=='user'][-3:],
        actual_final_query=main['input'][-1],latest_handoff_intent_in_summary_input=False,
        accepted_summary=summary,accepted_transcript=[t for t in ev['transcript'] if t['type']=='context_compaction_applied'],
        transcript_fact_retained=transcript_occurrences,tail_complete_required_facts_available=False,
        pinned_publish_prohibition_available=True,pinned_evidence=main['instructions'][main['instructions'].index('<pinned-constraints>'):],
        alternative_channels=dict(plan='None',context_record='retrieval/extraction disabled',memory='retrieval disabled',
            durable_transcript='保留源事实，可恢复/显式工具回读；非自动请求注入，Fake MAIN没有执行回读'),
        attribution=[
            dict(cause='已接受摘要遗漏5条完整必需任务事实',status='CONFIRMED',evidence='逐事实源请求→接受摘要→MAIN；29项原阅读集合存在'),
            dict(cause='接受规则只检查协议/长度/收益/状态计数，未检查事实保真',status='CONFIRMED',code='context_compaction.py:255-304'),
            dict(cause='通用Prompt对实体/依赖/当前任务状态保留不足',status='SUPPORTED_HYPOTHESIS',evidence='有当前状态/剩余工作标题但无语义校验；没有Prompt对照实验'),
            dict(cause='长源码信息淹没台账',status='SUPPORTED_HYPOTHESIS',evidence='116项历史prefix中稀疏台账，输出偏向源码结构；无长度消融'),
            dict(cause='摘要输入不含最新交接Query导致任务重点偏离',status='SUPPORTED_HYPOTHESIS',evidence='请求只含历史prefix；最新交接请求保留在tail，但因果无对照验证'),
            dict(cause='Builder丢失一份实际已保留的摘要',status='NOT_SUPPORTED',evidence='接受摘要与最终developer区块一致'),
            dict(cause='Gold/责任归因错误',status='NOT_SUPPORTED',evidence='五条明确当前有效且实际进入prefix、非tail；最终Query继续当前交接，但合成语料代表性有限'),
            dict(cause='整个Production系统普遍错误/真实Agent已发布失败',status='INSUFFICIENT_EVIDENCE',evidence='单例、MAIN Fake，无真实工具行为对照')],
        severity='HIGH_CRITICAL_EVALUATION_FINDING',quality_acceptance_blocked=True,whole_production_disable_supported=False,
        task_relevance='批次保证对象、已完成两项防重复、权限确认依赖、下一动作条件均服务最新当前交接意图；按本冻结契约必要，不声称所有业务任务都必须保护相同字段。',
        safety_boundary='确认后生成报告的依赖和动作整体丢失；不发布子约束仍在Pinned和最新只读Query中，不能称所有禁令消失或已发生错误发布。',
        general_guarantee=False,systemic_bug_confirmed=False,e2e_failures='NOT_EVALUATED',candidate_remediations=proposals))
    after=snapshot(groups);assert before==after
    initial_protected={}
    for run in [S1,S2]:
        recorded=read(run/'execution_manifest.json')
        initial_protected.update(recorded.get('protected_before',{}))
        initial_protected.update(recorded.get('prep3_before',{}))
        initial_protected.update(recorded.get('eval_source_hashes',{}))
    assert all(Path(p).exists() and digest(Path(p))==h for p,h in initial_protected.items())
    production_before=read(S1/'execution_manifest.json')['production_before']
    assert all(digest(Path(p))==h for p,h in production_before.items())
    diff=subprocess.run(['git','diff','--check'],cwd=REPO,capture_output=True,text=True);assert diff.returncode==0
    save(output/'hash_check.json',dict(groups=[str(p) for p in groups],before=before,after=after,
        original_manifest_protected_hashes=initial_protected,original_manifest_protected_unchanged=True,
        unchanged=True,production_matches_stage1=True,git_diff_check_exit_code=diff.returncode,
        review_script_hash=digest(Path(__file__)),real_calls_this_review=0,
        tests_this_review='NOT_RUN_READ_ONLY_AUDIT',existing_test_evidence=['Stage1 90/90','Stage2 96/96'],
        note='历史测试仅核对原日志，不冒充本轮重新执行；不承诺fsync/签名防篡改。'))
    gates=dict(total_gate='C2_COMPLETED_WITH_CRITICAL_QUALITY_FINDING',execution='PASS_WITH_LIMITATIONS',
        quality='FAIL_CRITICAL_FACT_RETENTION',efficiency='BENEFIT_OBSERVED_WITH_OVERHEAD',evidence='AUDITABLE_WITH_GAPS',
        freeze_evaluation_results_and_finding=True,production_quality_certified=False,
        gaps=['历史5次缺Usage，接收/计费未知','预算是各重新授权Run独立，跨历史阶段总请求不由一个账本限制',
              'Stage2额外未完成Run原来未列入摘要，已另行对账','6条断言保留UNCERTAIN','用户容量声明非Provider独立验证'],
        recommend_quality_remediation_review=True,remediation_implemented=False,
        e2e_task_effectiveness='NOT_EVALUATED',next='独立C2-QUALITY-REMEDIATION设计审批，不覆盖原七例，不选定实现方案/运行模型')
    save(output/'gate_decision.json',gates)
    save(output/'evidence_index.json',dict(selected_runs=[str(S1),str(S2)],history=[str(p) for p in HISTORY+[SCHEMA]],
        stage1_post_review=str(POST),manifest=str(frozen/'smoke_manifest.json'),
        files=[dict(path=p,sha256=h) for p,h in before.items()],
        review_files=[p.name for p in output.iterdir()],
        discrepancies=['原任务只提一条Stage2连接失败Run，实际两条','L3完整复合事实遗漏≠不发布子约束全部丢失'],
        source_priority='原始请求/响应/Transcript→冻结Oracle/Manifest→逐项人工审查→汇总/任务Prompt'))
    report=['# C2 Final Review','',
        '总Gate：**C2_COMPLETED_WITH_CRITICAL_QUALITY_FINDING**。冻结评测结果与Finding，不是Compaction质量已修复或Production可靠性认证。','',
        '| 维度 | Gate |','|---|---|',
        '| Execution | PASS_WITH_LIMITATIONS |','| Quality | FAIL_CRITICAL_FACT_RETENTION |',
        '| Efficiency | BENEFIT_OBSERVED_WITH_OVERHEAD |','| Evidence | AUDITABLE_WITH_GAPS |','',
        '## 七例必需事实','',
        '| 样本 | 正确保留 | 完整请求估算前→后 | 摘要调用 |','|---|---:|---:|---:|']
    for row in facts:
        m=next(m for m in efficiency if m['fixture_id']==row['fixture_id'])
        report.append(f'| {row["fixture_id"]} | {row["correct"]}/{row["required"]} | {m["before"]} → {m["after"]} | {m["provider_calls"]} |')
    report += ['', '合计29/34（85.29%），样本级6/7；短历史20/20、4/4，长历史9/14、2/3。最严重L3为0/5。平均值不能掩盖关键任务台账整组遗漏。',
        '## L3关键Finding','',
        '五条源事实明确当前有效，实际进入原摘要input[2,42,82,98,106]，来源call_id为read:[0,10,20,24,26]，不是recent tail。接受摘要缺少批次、两项完成状态、维护者确认依赖、确认后生成报告的条件/动作；MAIN也没有完整替代事实注入。',
        '必须修正过强表述：**不发布禁令仍由ACTIVE Pinned Constraints完整注入**。用户最新Query也限制业务变更，原Transcript仍保存全部源事实，工具可显式回读。但本Fake MAIN没有回读，Runtime没有通用自动补回缺失事实的保证。',
        '还确认摘要模型没有收到最新“继续当前交接”Query，只收到此前逐文件阅读的历史前缀。任务重点偏离/长源码淹没稀疏台账是支持性假设，不能当已确认因果。',
        '已确认接受规则检查唯一summary、Token预算/收益、partial计数等，没有实体/状态/依赖语义保真Oracle。L3收益合格即被提交，说明当前策略不保证关键状态保留，不等于实现违背其现有收益契约。',
        '此单例阻断C2关键状态质量验收。缺少证据支持关闭所有Production Compaction，更没有真实错误发布/工具副作用/Agent任务失败证据。','',
        '## 断言来源审查','',
        f'F1在本轮新增完整{len(f1_assertions)}项审查（含必需事实项）；原L2保持188扩展项、2个UNCERTAIN；Stage2保持225实质项（含必需事实）、4个UNCERTAIN。三种分母不合并成支持率。',
        'F1原PARTIAL报告不回写，本轮独立补齐。六项UNCERTAIN原样保留：Skill全文不截断、信号入口非阻塞、迟到结果不能覆盖、CLI全部、Web全部、Checkpoint持久化。它们是证据强度限制，不算已支持或Production Bug。',
        '已有审查未发现确定UNSUPPORTED/CONTRADICTED/旧事实错误激活；这不代表没有关键事实遗漏，也不把源码注释/静态观察升级为实测能力。','',
        '## Usage与效率','',
        '选定完整Run：Stage1 4次输入40023/输出2534；Stage2 12次输入83047/输出4382。合计16次、123070输入+6916输出=129986。MAIN全部Fake，其Usage不入真实成本。',
        f'接受7份：{usage(accepted_calls)}；拒绝9份：{usage(rejected_calls)}。每份候选、未实现的理论收益与计时范围见efficiency_review.json。',
        '四条短样本均无新业务消息时partial后再压缩。配置诱发且符合现有有限重试/收益检查，观察到额外成本，归为疑似策略效率问题，不直接认定Production Bug。成功完整Run生产逻辑Retry5次、SDK/Eval均0。',
        '主请求估算长度减少是真实观察，摘要输入/输出Usage是另一调用的成本；两种计量不直接相减。AMORTIZED_BENEFIT_NOT_EVALUATED、MONETARY_COST_NOT_EVALUATED、USER_ASSERTED_CAPACITY_128K保留。','',
        '## 历史失败单独对账','',
        f'- {HISTORY[0].name}：2次APIConnectionError，响应/Usage缺失，上游接收及计费未知；另有1次Fake MAIN。原GUARD_DENIAL_REQUIRES_STOP是执行器对denied字段的过粗分类，不据此认定授权绕过。',
        f'- **额外发现** {HISTORY[1].name}：7次尝试，6次成功Usage输入4563、输出1729，共6292；1次APIConnectionError无Usage。该Run停在F3之后，原报告不存在，不创建替代。',
        f'- Stage1 schema失败{SCHEMA.name}：2次HTTP400无模型Usage，另列，不把未知计费记0。',
        '上述不并入选定七例16次统计。所有已观察Usage小计136278，仍不是完整历史总成本，因为5次调用没有Usage。',
        '三个Stage2授权Run合计21次尝试（2+7+12）；每Run独立账本不越20，原Manifest保留各重新授权来源。不以此宣称跨授权累计≤20；没有共享的跨Run阶段总额度保证。后续必须明确续用还是刷新预算。','',
        '## 完整性、Gate与下一步','',
        '所有Fixture/Oracle/Prompt及所选Run配置匹配。原始Stage1/2成功和失败Run、Post Review、C1、Prep/2/3、Guard资产及Production哈希未变。git diff --check通过。全程零模型调用，不运行C1/C2/Benchmark，不commit/tag。',
        '本轮仅审查原测试日志（Stage1 90/90、Stage2 96/96），测试NOT_RUN，不冒充本轮全量回归。',
        '建议冻结C2评测结果与Critical Finding；建议提交独立C2-QUALITY-REMEDIATION设计审批。候选方向包括显式状态/复用Plan、摘要输入携带当前意图、关键事实检查、接受前语义验证、分层摘要/外部保护、现有显式Pinned禁令。每项成本/职责/独立验收边界见l3_critical_finding.json，**本轮不选定、不实现**。',
        '未来统一Benchmark再测真实Agent ON/OFF、累计真实Usage、任务成功与交互影响。本轮不开展。']
    (output/'REPORT.md').write_text('\n'.join(report)+'\n',encoding='utf-8')
    assert snapshot(groups)==before
    save(output/'freeze_manifest.json',dict(frozen_scope='EVALUATION_RESULTS_AND_FINDINGS_NOT_PRODUCTION_CERTIFICATION',
        files={p.name:digest(p) for p in output.iterdir() if p.is_file()},
        method='SHA256本地内容快照，不是数字签名、不可变存储或断电保证',created_at=datetime.now(timezone.utc).isoformat()))
    print(dict(output=str(output),gates=gates,required_fact_recall='29/34',accepted=usage(accepted_calls),rejected=usage(rejected_calls)))


if __name__=='__main__':main()
