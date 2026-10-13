"""只读预注册输入/答案，运行离线覆盖模型检查与一致性验证。"""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import subprocess

from checker import evaluate
from prepare import ROOT
from eval.context_remediation_v1.offline import save,fingerprints
from eval.context_remediation_v1.factorial_pilot import file_hashes


def main():
    out=Path(__file__).resolve().parent
    if (out/"test_results.json").exists():
        raise RuntimeError("禁止覆盖已运行测试")
    manifest=json.loads((out/"manifest.json").read_text())
    for name,h in manifest["frozen_files"].items():
        assert sha256((out/name).read_bytes()).hexdigest()==h
    assert all(file_hashes(Path(p))==h for p,h in manifest["baseline_before"].items())
    assert all(sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in manifest["production_hashes"].items())
    expected=json.loads((out/"expected_results.json").read_text())
    cases=sum([json.loads((out/name).read_text()) for name in ["adversarial_cases.json","historical_regression_cases.json","positive_controls.json"]],[])
    assert [c["case_id"] for c in cases]==manifest["test_order"]
    rows=[]
    byid={}
    for case in cases:
        result=evaluate(case)
        answer=expected[case["case_id"]]
        passed=result["classification"]==answer["classification"]
        row=dict(case_id=case["case_id"],category=case["category"],polarity=case["polarity"],fact_types=case["fact_types"],
            original_fact=case["content"],refs=case["source_refs"],result=result,expected=answer,
            test_verdict="PASS" if passed else "FAIL",evidence_location="原输入对应Case.sources和历史original_audit_entry；规范化字段人工来源qualification合同")
        rows.append(row); byid[case["case_id"]]=row
    classes=Counter(r["result"]["classification"] for r in rows)
    errors=[r["case_id"] for r in rows if r["test_verdict"]=="FAIL"]
    save(out/"test_results.json",dict(total=len(rows),passed=len(rows)-len(errors),failed=len(errors),errors=errors,classification_counts=dict(classes),cases=rows,
        scope="预注册语义模型检查器结果，不是自动NL语义准确率；人工历史证据资格检查另列"))
    checks=[
        ("cross_wording",byid["A1-endpoints"]["result"]["classification"]==byid["A1-wording"]["result"]["classification"]==byid["A3-above"]["result"]["classification"]=="REFERENCE_GAP"),
        ("coverage_changes_answer",byid["A1-full"]["result"]["classification"]=="PASS" and byid["A1-endpoints"]["result"]["classification"]=="REFERENCE_GAP"),
        ("summary_not_ref_count",byid["A1-summary"]["result"]["classification"]==byid["A1-bundle"]["result"]["classification"]=="PASS"),
        ("summary_title_not_scope",byid["A2-partial"]["result"]["classification"]=="REFERENCE_GAP"),
        ("other_fact_union_disabled",byid["A1-union-trap"]["result"]["classification"]=="REFERENCE_GAP"),
        ("closed_negative_vs_absence",byid["A4-complete-negative"]["result"]["classification"]=="PASS" and byid["A4-absence-only"]["result"]["classification"]=="UNSUPPORTED_FACT"),
        ("local_negative_not_full_prefix",byid["A4-local-negative"]["result"]["classification"]=="REFERENCE_GAP"),
        ("narrowing_not_oracle_pass",byid["A6-narrow"]["result"]["classification"]=="PASS" and byid["A6-narrow"]["result"]["oracle_retained"]==0),
        ("dropping_dependency_separate_recall",byid["A6-drop-dependency"]["result"]["oracle_retained"]==1 and byid["A6-drop-dependency"]["result"]["oracle_required"]==2),
        ("uncertain_not_pass_fail",all(byid[c]["result"]["classification"]=="UNCERTAIN" for c in ["A2-ambiguous","A2-conflict","A3-related","A4-open-scope","A5-one-unknown"])),
        ("multiple_labels_unique_fact",len(byid["A2-stale"]["result"]["labels"])>=2 and byid["A2-stale"]["result"]["classification"]=="STATE_ERROR")]
    synthetic=[c for c in cases if c["category"].startswith("A")]
    coverage={kind:sorted({c["polarity"] for c in synthetic if c["category"]==kind}) for kind in ["A1","A2","A3","A4","A5","A6"]}
    assert all(v==["boundary","negative","positive"] for v in coverage.values())
    history=[r for r in rows if r["category"]=="HISTORICAL_REGRESSION"]
    controls=[r for r in rows if r["category"]=="HISTORICAL_POSITIVE_CONTROL"]
    assert len(history)==6 and len(controls)==8
    checks.append(("historical_gap_6",all(r["result"]["classification"]=="REFERENCE_GAP" for r in history)))
    checks.append(("positive_controls_not_harmed",all(r["result"]["classification"]=="PASS" for r in controls)))
    save(out/"consistency_report.json",dict(checks=[dict(name=n,status="PASS" if ok else "FAIL") for n,ok in checks],category_coverage=coverage,
        issue_count=sum(r["result"]["classification"] not in {"PASS","UNCERTAIN"} for r in rows),
        uncertain_count=classes["UNCERTAIN"],overgeneralization_count=sum(r["result"]["overgeneralization"] for r in rows),
        overlap_policy="issue_count按唯一CaseID计算，多标签不累加；UNCERTAIN不计确定失败；testPASS可对应正确的UNCERTAIN",
        interpretation="跨措辞先由人工确认同义与指代，再检查相同命题结果，不声称自动识别自然语言同义"))
    (out/"manual_review_boundaries.md").write_text("""# 人工边界与资格检查\n\n检查器不解析自然语言。每个case的claim_model、source claims、对象集合、可信范围、effective_at由人工核对并预注册；自动测试只验证其后的集合蕴含和三维分类。\n历史六条原文/refs/切点/response_id完整保留，来源正文见original_message和原audit_entry。本次没有据运行结果改历史判定。\n必须人工确认：上述/这些/相关/主要的指代和范围，来源是否权威及汇总是否完整，是否显式状态更新，否定的搜索范围是否穷尽，复合句分解和新语义。\n多数例只在人工确定主要=固定集合过半后使用16/30阈值；不能把这个解释推广到任意中文“主要”。\n时间更新例只有明示更正才按有效时间顺序，冲突未解决用UNCERTAIN；不能泛化为后收到文本就一定覆盖真实事实。\n“没有测试执行证据”历史规范化是原prefix全部消息的人工资格确认，不是正则根据缺少关键词判不存在。\n自动可靠范围：已规范化的明确身份、截止位置、显式范围与原子属性覆盖、明确资料排他性、必要子命题组合。\n历史正对照包含10/30项全覆盖、单项阅读、完整调用+结果以及原文唯一性；未机械按refs数量或命中全称词判失败。\n未识别的NL不应默认PASS，也不能强行FAIL；应输出人工复核状态。该机制不得接入Production拒绝逻辑或宣传通用语义验证器。\n""",encoding="utf-8")
    (out/"v3_experiment_proposal.md").write_text("""# 下一轮候选与受控实验草案（未创建Prompt v3）\n\n离线规则覆盖已具备，可以请求批准仅通用Evidence Scope约束的v3候选：陈述范围不超过自身refs支持范围；使用已有可核验汇总或收窄非关键断言，不编造来源，不牺牲身份/里程碑/关键依赖。此处为设计要求，不是正式Prompt文件，不应用于Production。\n风险：过度删除精确任务信息、把正确完成状态降级、增加大量refs使快照膨胀。OracleRecall与引用质量必须分别评分。\n不增加Schema/输出4,000/快照2,000预算、Runtime能力或调用策略；没有证据需要扩大预算/增加Judge。\n最小下一轮四次Extraction：L3-v2→SP-v2→L3-v3→SP-v3各一次，同原JSON/Fixture/Oracle/切点，SP分母2且tail仅核验，SDK300秒retry0。\nv2保留，v3另行审批版本化，再生成精确请求冻结；Prompt增量≤1,024估算Tokens。四次保守输入+最大输出合计≤200,344；每请求安全余量8,000另参与容量核查。未经重新预算不得执行。\n成功要求保持L3五事实5/5与SP两事实2/2，所有输出fact来源充分、无新无依据/状态/条件错误，快照不超2,000。记录与v2的快照长度及refs数量，不用更长引用数假装质量更好。\n基线v2可能失败，因此预先审批是否继续v3用于比较；不能事后改停止规则。失败不补发、不重跑至PASS，Provider/审计不可继续则停止。\n各一次仅方向性试验，不能宣称稳定修复；若需要重复次数必须另行申请预算。不自动执行本提案。\n""",encoding="utf-8")
    current={p:file_hashes(Path(p)) for p in manifest["baseline_before"]}
    diff=subprocess.run(["git","diff","--check"],cwd=ROOT,capture_output=True,text=True)
    unchanged=current==manifest["baseline_before"] and fingerprints()==manifest["frozen_assets"] and all(sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in manifest["production_hashes"].items())
    frozen_cases_ok=all(sha256((out/name).read_bytes()).hexdigest()==h for name,h in manifest["frozen_files"].items())
    gate="REFERENCE_RULES_ADVERSARIAL_PASS" if not errors and all(ok for _,ok in checks) and unchanged and frozen_cases_ok and diff.returncode==0 else "REFERENCE_RULES_ADVERSARIAL_FAIL"
    save(out/"integrity_report.json",dict(gate=gate,baseline_before=manifest["baseline_before"],baseline_after=current,
        production_hashes=manifest["production_hashes"],production_unchanged=unchanged,frozen_tests_expectations_unchanged=frozen_cases_ok,
        historical_gates_unchanged=True,prompts_unchanged=True,network_calls=0,model_calls=0,
        git_diff_check=dict(exit_code=diff.returncode,output=diff.stdout+diff.stderr)))
    final=f"""# Reference Sufficiency Rules Adversarial Validation\n\nGate：{gate}。这是人工资格确认的证据模型上的离线规则验证，不是自动NL语义准确率或Prompt修复证据。\n\n## 样本与测试\n\n六类A1–A6均含正/反/边界例，共{len(synthetic)}合成对抗样例；六条真实历史缺口回归、八条历史引用充分负对照，共{len(rows)}个独立预注册样例。\n检查结果{len(rows)-len(errors)}/{len(rows)}符合预期；{classes['UNCERTAIN']}个样例预期UNCERTAIN，正确转人工，不算语义PASS/FAIL。\n六条原缺口一致识别，八个历史正对照未误伤。全部预期答案与检查器在运行前分文件冻结，没有运行后修改答案。\n\n## 七项答复\n\n1. 六类完整覆盖，分别有范围端点/权威汇总/指代/排他否定/复合/收窄，状态更新与相同有效时间冲突单列。\n2. 六条历史缺口在保留原文/refs/cutpoint/response身份下按同一范围规则再现；没有修改历史Gate。\n3. 八个历史PASS负对照包含完整10/30项引用、单项事实、工具调用+返回、直接唯一性，全部保持PASS；不能据此推广为不存在其他误报。\n4. 自然语言指代、权威汇总资格、有效时间与更新语义、否定范围、复合分解必须人工；checker不读取预期答案，也不解析原文。\n5. 已预注册同义措辞、引用覆盖变化、权威单条汇总、局部与全局否定、他Fact并集、收窄与Recall独立等{len(checks)}项关系检查均通过；没有发现这些模型内的已知系统性矛盾。自然语言泛化仍未验证。\n6. 可形成rules_v1评分合同，冻结三维判定与全称覆盖要求，不以结构/sourceID合法代替充分引用。\n7. 有理由提交v3最小实验审批；本轮不创建正式v3 Prompt。Schema/预算/Runtime先保持不变，优先架构简单，确定性规则仅Eval辅助。\n\n## 解释限制\n\nREFERENCES充分不等于Oracle保留：合理收窄A6-narrow得到PASS但不能保留全部范围Oracle；A6-drop-dependency只保留身份、丢关键依赖，单独Recall1/2。\n信息没有提供导致UNSUPPORTED表示缺乏蕴含，不证明事实为假。UNCERTAIN不默认PASS，多个错误标签按唯一Case去重。\n本轮预设语义模型由人工提供，不把测试全绿包装成通用自然语言验证成功。\n\n## 完整性与停止\n\nProduction、Prompt v1/v2、所有旧Fixture/Oracle/审核/Gate前后SHA一致；新测试和预期答案哈希不变。零网络/模型调用，git diff --check通过，无commit/tag/push。\n下一步：人工审核rules_v1与v3实验草案，另批版本化候选和四次受控Extraction。完成后停止。\n"""
    (out/"final_review.md").write_text(final,encoding="utf-8")
    print(json.dumps(dict(output=str(out),gate=gate,total=len(rows),passed=len(rows)-len(errors),failed=errors,classification_counts=dict(classes),relation_checks=len(checks)),ensure_ascii=False))


if __name__=="__main__":
    main()
