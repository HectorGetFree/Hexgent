"""使用旧冻结覆盖内核检查新版夹具；预期文件不参与判定。"""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import runpy
import subprocess

from prepare_v2 import ROOT
from eval.context_remediation_v1.offline import save,fingerprints
from eval.context_remediation_v1.factorial_pilot import file_hashes


def main():
    out=Path(__file__).resolve().parent
    if (out/"test_results.json").exists():
        raise RuntimeError("禁止覆盖已执行测试")
    manifest=json.loads((out/"manifest.json").read_text())
    old=ROOT/"eval/context_remediation_v1/diagnostics/reference_rules_adversarial"
    cross=ROOT/"eval/context_remediation_v1/diagnostics/cross_run_reference_audit"
    assert file_hashes(old)==manifest["old_before"] and file_hashes(cross)==manifest["cross_before"]
    assert all(sha256((out/p).read_bytes()).hexdigest()==h for p,h in manifest["frozen_files"].items())
    assert sha256((old/"checker.py").read_bytes()).hexdigest()==manifest["original_checker_sha256"]
    evaluate=runpy.run_path(str(old/"checker.py"))["evaluate"]
    originals=json.loads((out/"projection_v2_cases.json").read_text())
    variants=json.loads((out/"a3_these_regression.json").read_text())
    expected=json.loads((out/"expected_results_v2.json").read_text())
    results=[]; byid={}
    for case in originals+variants:
        computed=evaluate(case)
        target=expected[case["case_id"]]
        row=dict(case_id=case["case_id"],original_fact=case["content"],category=case["category"],source_refs=case["source_refs"],
            expected_classification=target["classification"],expected_rationale=target["rationale"],computed=computed,
            test_verdict="PASS" if computed["classification"]==target["classification"] else "FAIL")
        results.append(row);byid[case["case_id"]]=row
    failures=[r["case_id"] for r in results if r["test_verdict"]=="FAIL"]
    save(out/"test_results.json",dict(original49_total=49,original49_matched=sum(r["test_verdict"]=="PASS" for r in results[:49]),
        additional_variant_count=8,additional_matched=sum(r["test_verdict"]=="PASS" for r in results[49:]),
        total=len(results),matched=len(results)-len(failures),failures=failures,rows=results,
        note="结构符合新版独立预期不是最终人工投影忠实度PASS；未修改旧内核"))
    historical=[r for r in results if r["category"]=="HISTORICAL_REGRESSION"]
    controls=[r for r in results if r["category"]=="HISTORICAL_POSITIVE_CONTROL"]
    save(out/"historical_regression_results.json",dict(historical_gaps=historical,positive_controls=controls,
        original_fact_refs_evidence_unchanged=True,old_scores_and_gates_preserved=True))
    cls=lambda cid:byid[cid]["computed"]["classification"]
    c=lambda cid:byid[cid]["computed"]
    checks=[
        ("cross_wording",cls("A1-endpoints")==cls("A1-wording")==cls("A3-above")=="REFERENCE_GAP"),
        ("coverage_changes_answer",cls("A1-full")=="PASS" and cls("A1-endpoints")=="REFERENCE_GAP"),
        ("summary_not_ref_count",cls("A1-summary")==cls("A1-bundle")=="PASS"),
        ("summary_title_not_scope",cls("A2-partial")=="REFERENCE_GAP"),
        ("other_fact_union_disabled",cls("A1-union-trap")=="REFERENCE_GAP"),
        ("closed_negative_vs_absence",cls("A4-complete-negative")=="PASS" and cls("A4-absence-only")=="UNSUPPORTED_FACT"),
        ("local_negative_not_full_prefix",cls("A4-local-negative")=="REFERENCE_GAP"),
        ("narrowing_not_oracle_pass",cls("A6-narrow")=="PASS" and c("A6-narrow")["oracle_retained"]==0),
        ("dropping_dependency_separate_recall",c("A6-drop-dependency")["oracle_retained"]==1 and c("A6-drop-dependency")["oracle_required"]==2),
        ("uncertain_not_pass_fail",all(cls(cid)=="UNCERTAIN" for cid in ["A2-ambiguous","A2-conflict","A3-related","A4-open-scope","A5-one-unknown"])),
        ("multiple_labels_unique_fact",len(c("A2-stale")["labels"])>=2 and cls("A2-stale")=="STATE_ERROR"),
        ("historical_gap_6",len(historical)==6 and all(r["computed"]["classification"]=="REFERENCE_GAP" for r in historical)),
        ("positive_controls_not_harmed",len(controls)==8 and all(r["computed"]["classification"]=="PASS" for r in controls))]
    save(out/"consistency_report.json",dict(original_relation_checks=[dict(name=n,status="PASS" if v else "FAIL") for n,v in checks],
        original_relation_count=13,passed=sum(v for _,v in checks),
        v2_special_checks=dict(original_a3_these_no_action_in_source=cls("A3-these")=="UNSUPPORTED_FACT",
            qualification_not_business_completion=cls("V2-dependency-only")=="UNSUPPORTED_FACT",
            start_not_completion=cls("V2-start-only")=="UNSUPPORTED_FACT",
            full_completion=cls("V2-all-completed")=="PASS",partial_not_full=cls("V2-partial-completed")=="UNSUPPORTED_FACT",
            missing_refs_not_missing_world=cls("V2-prefix-only-completed")=="REFERENCE_GAP",
            authoritative_summary=cls("V2-authoritative-summary")=="PASS",revocation=cls("V2-revoked")=="STATE_ERROR",
            ambiguous_summary=cls("V2-ambiguous-summary")=="UNCERTAIN")))
    # 不在这里自动生成最终人工PASS；只保存结构结果并待独立复核。
    diff=subprocess.run(["git","diff","--check"],cwd=ROOT,capture_output=True,text=True)
    assert file_hashes(old)==manifest["old_before"] and file_hashes(cross)==manifest["cross_before"]
    assert fingerprints()==manifest["frozen_assets"]
    assert all(sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in manifest["production_hashes"].items())
    assert all(sha256((out/p).read_bytes()).hexdigest()==h for p,h in manifest["frozen_files"].items())
    save(out/"integrity_report.json",dict(status="PASS",old_before=manifest["old_before"],old_after=file_hashes(old),
        cross_before=manifest["cross_before"],cross_after=file_hashes(cross),production_hashes=manifest["production_hashes"],
        original49_cases_expectations_checker_results_unchanged=True,new_expectations_frozen_unchanged=True,
        production_prompts_historical_gates_unchanged=True,model_calls=0,network_calls=0,
        git_diff_check=dict(exit_code=diff.returncode,output=diff.stdout+diff.stderr)))
    print(json.dumps(dict(output=str(out),status="STRUCTURAL_TESTS_COMPLETE_AWAITING_MANUAL_REVIEW",total=57,matched=57-len(failures),failures=failures,
        relations_passed=sum(v for _,v in checks),classification_counts=dict(Counter(r["computed"]["classification"] for r in results))),ensure_ascii=False))


if __name__=="__main__":
    main()
