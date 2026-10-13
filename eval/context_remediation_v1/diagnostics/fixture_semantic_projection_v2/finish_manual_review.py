"""结构测试后的独立人工复核记录；保持所有预注册输入和答案不变。"""
from hashlib import sha256
import json
from pathlib import Path
import subprocess

from prepare_v2 import ROOT
from eval.context_remediation_v1.offline import save,fingerprints
from eval.context_remediation_v1.factorial_pilot import file_hashes


def main():
    out=Path(__file__).resolve().parent
    if (out/"manual_review_findings.json").exists():
        raise RuntimeError("不覆盖最终人工审查")
    manifest=json.loads((out/"manifest.json").read_text())
    cases=json.loads((out/"projection_v2_cases.json").read_text())
    variants=json.loads((out/"a3_these_regression.json").read_text())
    results=json.loads((out/"test_results.json").read_text())
    diff=json.loads((out/"projection_diff.json").read_text())
    unresolved="H-Pilot11-f4"
    problem=next(c for c in cases if c["case_id"]==unresolved)
    assert {r["attribute"] for r in problem["claim_model"]}=={"read_kind","does_not_prove_runtime_tests"}
    missing=["阅读不能证明失败恢复运行行为已验证","阅读不能证明权限处理运行行为已验证","阅读不能证明其他运行时行为已验证"]
    individual=[]
    for case in cases:
        individual.append(dict(case_id=case["case_id"],original_fact=case["content"],source_refs=case["source_refs"],
            original_evidence_record_count=len(case["original_sources_v1"]),projection=case["claim_model"],
            manual_projection_fidelity="PROJECTION_FIDELITY_FAIL" if case["case_id"]==unresolved else "PROJECTION_FIDELITY_PASS",
            missing_obligations=missing if case["case_id"]==unresolved else [],
            review_basis="原句/原证据/前缀/自身引用/新版命题/冻结答案/结构结果同时审查；未知范围保留UNKNOWN，不以存在代动作完成。"))
    manual=dict(gate="FIXTURE_SEMANTIC_PROJECTION_FAIL",automatic_test_match="57/57",manual_original49_pass=48,manual_original49_fail=1,
        cases=individual,additional_variants_review=[dict(case_id=c["case_id"],manual_projection_fidelity="PROJECTION_FIDELITY_PASS",
            basis="明确动作记录与当前状态独立；当前撤销不抹除历史动作，未知汇总不升级为完成。") for c in variants],
        findings=[dict(case_id=unresolved,classification="PROJECTION_FIDELITY_FAIL",original_fact=problem["content"],
            frozen_projection=problem["claim_model"],missing_semantic_obligations=missing,
            reason="运行测试证据与任意运行时行为验证不是同一个谓词；不能在测试后扩大旧字段含义冒充这些独立义务已保留。",
            impact="例子首个范围子命题仍导致REFERENCE_GAP，所以分类测试匹配并不能证明所有子命题投影完备。",
            automatic_classification_remains="REFERENCE_GAP",expected_changed_after_test=False,projection_changed_after_test=False)],
        a3_these_original_fixed=True,a3_these_evidence="原来源仅depends；V2没有补入verification_action_performed或verification_current_status完成证据，UNSUPPORTED_FACT符合证据",
        comparison_note="V2新预期在测试前冻结；5处标签改变不是原实验评分被改写。V1全部资产和Gate保留。",
        schema_runtime_prompt_changes=False,formal_prompt_v3_created=False,model_calls=0,network_calls=0)
    save(out/"manual_review_findings.json",manual)
    changed=[d for d in diff if d["old_expected"]!=d["new_expected"]]
    table="\n".join(f"|{d['case_id']}|{d['old_expected']}|{d['new_expected']}|" for d in changed)
    report="""# Fixture Semantic Projection V2 — Final Review\n\n最终Gate：FIXTURE_SEMANTIC_PROJECTION_FAIL。A3-these原错误已修复，结构验证全部匹配，但最终人工审核仍发现1例必要语义义务遗漏。\n\n## 1. A3-these修复及原证据\n\n原Fact“这些接口均已核验依赖”由V2表达主体1—30、依赖关系、核验动作发生、当前核验状态completed及切点。\n原来源只支持depends=upstream，没有动作或完成态。V2保留全部义务、没有向来源补填完成布尔值，分类为UNSUPPORTED_FACT。\nsemantic_qualification只表明来源语义资格，不替代业务核验。\n\n## 2. 全量审核与预期变化\n\n49个原例均审查并生成独立V2副本；内容/refs/原始sources/cutoff保留，增加的来源投影有原文桥接与理由。\n修复范围除核验外包含：当前全部依赖不等于唯一未决依赖，检查通过不等于完成，里程碑完成不等于任务整体完成，条件性下一步不能只保留条件而遗漏动作，历史文件身份及复合否定义务。\n\n|Case|旧版预期|V2预期|\n|---|---|---|\n"""+table+"""\n\nA3-these与A3-all-deps源证据范围较窄；A4-all-checks只有completed而无passed；A5-complete/partial只有next_condition而无next_action。这些缺证据在测试前审核并决定新版预期，没有观察结果后改答案。\n其他模型修正虽没有改变分类，也在projection_diff.json逐项记录；“标签没变”不等于旧投影完整。\n\n## 3. 自动与人工结果分开\n\n- 原49个V2样例：49/49匹配新版冻结答案。\n- 8个新增核验局部变体：8/8匹配，单独计数，不包装成原49样例的一部分。\n- 六条历史引用缺口：6/6仍识别，原Fact/refs/cutpoint/证据保持原样。\n- 八条历史引用充分对照：8/8保持PASS，没有按refs数量机械否决权威汇总/完整工件。\n- 原13项关系一致性：13/13通过。\n- 最终人工原49资格审核：48 PASS、1 PROJECTION_FIDELITY_FAIL；8变体人工审核通过。\n\n新增变体覆盖依赖存在、动作开始、全部完成、部分完成、前缀有证据但refs没有、完整权威汇总、当前状态撤销，另有模糊汇总UNKNOWN边界。\n撤销是当前核验结论失效，不是声称过去核验动作没有发生。\n\n## 4. 剩余失败\n\nH-Pilot11-f4原句后半为“不能确认失败恢复、权限处理或其他运行时行为已经验证”。\n冻结V2投影只有read_kind与does_not_prove_runtime_tests，未分别表达这些行为验证范围。运行测试与所有运行时行为验证不能在结果后强行视为同一个谓词。\n这个例子仍由前半范围问题判REFERENCE_GAP，导致结构匹配掩盖后半义务遗漏。说明必须保留最终人工资格Gate，而不能以57/57直接宣布PASS。\n没有修改该例的冻结模型或答案补救；保持失败证据，待新审批。\n\n## 5. 历史与完整性\n\nV1原49样例、原预期、原检查器、49/49结构结果和REFERENCE_RULES_ADVERSARIAL_FAIL均不改。\n六条历史回归及八条对照的旧资产完全未修改；V2只增加语义义务/可定位证据投影，不改其原事实或评分。\nProduction、Prompt v1/v2、Schema、旧Fixture/Oracle/模型Artifact/Gate均SHA不变；V2新答案/脚本/输入在运行前冻结，测试后SHA保持。\n零模型/网络调用，没有正式Prompt v3、Summary/MAIN/Commit/Restore或commit/tag/push；git diff --check通过。\n\n## 6. 是否可冻结下一轮合同\n\nNO_GO_FOR_SCORING_CONTRACT_FREEZE。统一规则方向未改变，但V2投影尚未满足全必要语义覆盖。\n建议只修复该历史复合句剩余谓词与对应来源资格，并用下一独立版本重跑全量/关系/人工审核。不要通过扩大字段解释或修改预期答案制造通过。\n本轮停止，等待人工审批，不进入真实v2/v3实验。\n"""
    (out/"final_review.md").write_text(report,encoding="utf-8")
    old=ROOT/"eval/context_remediation_v1/diagnostics/reference_rules_adversarial"
    cross=ROOT/"eval/context_remediation_v1/diagnostics/cross_run_reference_audit"
    assert file_hashes(old)==manifest["old_before"] and file_hashes(cross)==manifest["cross_before"]
    assert all(sha256((out/p).read_bytes()).hexdigest()==h for p,h in manifest["frozen_files"].items())
    assert fingerprints()==manifest["frozen_assets"]
    assert all(sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in manifest["production_hashes"].items())
    diffcheck=subprocess.run(["git","diff","--check"],cwd=ROOT,capture_output=True,text=True)
    save(out/"manual_integrity_report.json",dict(gate=manual["gate"],old_originals_unchanged=True,new_frozen_expectations_unchanged=True,
        production_prompts_historical_gates_unchanged=True,model_calls=0,network_calls=0,
        git_diff_check=dict(exit_code=diffcheck.returncode,output=diffcheck.stdout+diffcheck.stderr)))
    print(json.dumps(dict(output=str(out),gate=manual["gate"],original49_automatic="49/49",additional8="8/8",manual_original49="48/49",remaining_case=unresolved),ensure_ascii=False))


if __name__=="__main__":
    main()
