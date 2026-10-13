"""独立语义复核：明确保留结构匹配但语义失败，不修改冻结预期。"""
from prepare_v3 import OUT, BASE, ROOT, read, save, hashes
import hashlib
import subprocess

def main():
    cases=read(OUT/'projection_v3_cases.json')
    tests=read(OUT/'test_results.json')
    byid={r['case_id']:r for r in tests['rows']}
    reviews=[]
    for c in cases:
        # 来源映射保留原始自然语言、全文记录和局部投影；不把原49之外的问题算作原49失败。
        refs=set(c['source_refs'])
        evidence=[dict(id=s['id'],sequence=s['sequence'],original_evidence=s.get('original_message',s.get('raw_text')),
            projected_atoms=s.get('claims',[]),in_own_refs=s['id'] in refs) for s in c['sources'] if s['sequence']<=c['cutoff']]
        group=c['case_id'].split('-')[0]
        basis={
          'A1':'全称、多数、权威汇总与端点引用分开；不得用其他Fact来源并集补足自身引用。',
          'A2':'记录存在与当前完成分开；新状态优先，相同时点冲突保留未知，模糊汇总不扩张范围。',
          'A3':'指代范围、动作发生与完成状态分别保留；全部依赖不同于唯一未决依赖。',
          'A4':'否定限定证据范围；无记录不推出未发生；完成不推出检查通过。',
          'A5':'身份、里程碑、任务完成、条件和后续动作分别保留；来源缺失不补造动作。',
          'A6':'缩窄命题引用充分不等于Oracle事实保留；错误降级与缺引用分开。',
          'H':'历史冻结正文为阅读证据，不是执行结果；按真实文件身份、阅读范围、依赖、切点及否定范围核验。'
        }[group]
        reviews.append(dict(case_id=c['case_id'],original_fact=c['content'],projection=c['claim_model'],source_refs=c['source_refs'],cutoff=c['cutoff'],
            evidence=evidence,expected=byid[c['case_id']]['expected'],actual=byid[c['case_id']]['computed'],manual_projection_fidelity='PROJECTION_FIDELITY_PASS',review_reason=basis,
            target_note='三个验证对象均为据现有阅读证据不能确认，不是确认未发生；原引用只覆盖3/30阅读项。' if c['case_id']=='H-Pilot11-f4' else None))
    finding=dict(case_id='V3-explicit-not-verified',manual_verdict='FAIL',kind='EXPECTED_CLASSIFICATION_SEMANTIC_ERROR',
        evidence='来源明确三类尚未验证；待判原句为“现有证据不能确认三类运行时行为已验证”。',
        computed='STATE_ERROR',frozen_expected='STATE_ERROR',reason='明确未验证和不能确认的认识状态不能无损等同，但前者支持后者这个较弱自然语言命题。标签不等并不等于命题矛盾；专项给精确标签匹配赋予STATE_ERROR，使语义判断错误。',
        affected_scope='新增局部专项；原H-Pilot11-f4实际证据的投影完整性不受此问题影响。',expected_changed=False)
    gate='FIXTURE_SEMANTIC_PROJECTION_V3_FAIL'
    save(OUT/'manual_review_findings.json',dict(gate=gate,manual_original49_pass=49,manual_original49_fail=0,cases=reviews,
        v2_variant_manual_pass=8,new_variant_manual_pass=9,new_variant_manual_fail=1,findings=[finding],
        remaining_projection_omissions=False,historical_regression_harmed=False,new_rule_contradiction=True,
        source_qualification_boundary='静态代码与审查完成台账均不能证明执行验证；不把其他运行时行为解释为某一单独测试。',
        scoring_contract_ready=False,model_calls=0,network_calls=0))
    report='''# Fixture Semantic Projection V3 Final Review

最终 Gate：FIXTURE_SEMANTIC_PROJECTION_V3_FAIL。

## 修复与原证据

仅H-Pilot11-f4副本增加failure_recovery、permission_handling、other_runtime_behaviors三个独立认识状态命题，其余48个完整样例与V2逐对象相等。
保留NOT_CONFIRMED_VERIFIED、CONFIRMED_NOT_VERIFIED、VERIFIED、UNKNOWN四种状态；没有把未看到证据投影成测试未发生。
原msg-3/msg-7/msg-119明确“阅读证据，不是本轮执行测试结果”“失败恢复与权限结论需要独立证据”；30条工具输出均有这些限定。完整前缀只有阅读活动与审查台账，未提供这些运行验证结果。审查完成不是执行验证完成。
原文到投影及投影到原始证据的桥接见h_pilot11_f4_projection_diff.json；所有原始消息、refs、cutoff和响应身份不改。
完整前缀支持主要阅读构成及三项证据限定；自身3个引用不足以覆盖30项中的多数，因此H-Pilot11-f4仍是REFERENCE_GAP。后半三个独立命题均在自身引用中有支持，不由前半GAP替代审核。

## 结构与人工结果

- 原49：49/49结构匹配；逐Fact语义资格复核49 PASS。
- V2专项：8/8匹配，人工8 PASS。
- 历史缺口6/6保持REFERENCE_GAP；正对照8/8保持PASS。
- 原13关系检查13/13通过。
- 新增局部10例：10/10匹配冻结答案；最终人工9 PASS、1 FAIL。

以上数字分别统计，不把结构匹配当语义PASS。原49的来源、投影、预期、实际结果与逐项复核保存于manual_review_findings.json。

## 新发现的边界

V3-explicit-not-verified来源明确“尚未验证”，待判原句只说“不能确认已验证”。固定内核因标签不等判STATE_ERROR，冻结预期也这样设置。
精确状态保持区分是必要的，但较强否定支持较弱非确认命题；不得把认识状态不相同误写成原自然语言命题矛盾。这是局部专项预期与评分语义错误，并非修复成功。
专项说明曾限定“精确状态匹配”，但这不足以满足基于原自然语言Fact的评分合同。最终人工审核推翻该专项语义资格，不改冻结输入、预期和自动结果。

## 完整性与执行

V1/V2原Gate仍FAIL，历史结果保持；冻结预期先于测试，之后未改。Production、Prompt、Schema、Fixture、Oracle及既有模型资产哈希一致。零Provider/网络调用；不执行Summary、MAIN、提交、恢复、集成Benchmark或Git提交。
准备脚本一次因旧报告字段名差异中断；评分脚本一次因exec局部作用域中断。均是离线Runner问题，修复后续跑；已有内容相等才允许继续，不覆盖结果，不改答案。脚本未作为原49结构证据。

## 下一阶段建议

NO_GO_FOR_SCORING_CONTRACT_FREEZE。停止扩大结构化语义模型，不自动创建V4。
建议将稳定的来源合法性、范围覆盖、切点等继续自动检查；对认识状态、否定蕴含、复合命题等采用轻量逐Fact人工证据裁定，并保留明确审查记录。当前内核不是自然语言逻辑系统；为少量Fixture不断增加状态蕴含规则，维护成本和错误机会可能超过收益。
这不否定H-Pilot11-f4局部义务已补全，但本轮完整Gate必须FAIL。等待人工决定是否采用人工裁定合同，不执行Prompt对照。
'''
    p=OUT/'final_review.md'
    if p.exists():raise RuntimeError('禁止覆盖最终报告')
    p.write_text(report)
    m=read(OUT/'manifest.json'); integrity=read(OUT/'integrity_report.json')
    assert hashes(BASE)==m['v2_hashes']
    assert hashes(ROOT/'hexgent')==m['production_hashes']
    assert all(hashlib.sha256((OUT/p).read_bytes()).hexdigest()==h for p,h in m['frozen_files'].items())
    diff=subprocess.run(['git','diff','--check'],cwd=ROOT,capture_output=True,text=True)
    assert diff.returncode==0
    print(gate)

if __name__=='__main__':main()
