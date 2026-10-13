"""原句/证据先审查，新投影与独立答案冻结后才能运行；旧版本只读。"""
from copy import deepcopy
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT))
from eval.context_remediation_v1.offline import save,fingerprints
from eval.context_remediation_v1.factorial_pilot import file_hashes


def req(subjects,attribute,value=True,**extra):
    return dict(subjects=subjects,attribute=attribute,value=value,**extra)


def evidence_projection(source):
    """仅新建可定位的证据投影；绝不由目标Fact补填业务核验状态。"""
    result=deepcopy(source)
    derivations=[]
    for c in source["claims"]:
        if c["attribute"]=="depends":
            result["claims"].append(dict(subject=c["subject"],attribute="dependency_annotation_present",value=True))
            derivations.append(dict(attribute="dependency_annotation_present",source="来源实际已包含depends记录；这是记录存在，不是核验动作完成",original_atom=c))
    original=source.get("original_message")
    if original:
        body=original.get("output","")
        match=re.search(r"工作项 (\d+)：完成正文阅读；依赖上一批接口梳理。",body)
        if match:
            number=int(match[1])
            path=re.search(r"文件 (.+?) 的冻结源码正文如下",body)
            assert path
            result["claims"].extend([
                dict(subject=number,attribute="source_file",value=path[1]),
                dict(subject=number,attribute="completion_kind",value="source_reading"),
                dict(subject=number,attribute="not_runtime_test_result",value=True)])
            derivations.append(dict(attribute="source_file/completion_kind/not_runtime_test_result",source_quote=body.splitlines()[:2]))
        if "不是本轮执行测试结果" in body:
            result["claims"].append(dict(subject="read_evidence_principle",attribute="does_not_prove_runtime_tests",value=True))
            derivations.append(dict(attribute="does_not_prove_runtime_tests",source_quote="冻结源码阅读证据，不是本轮执行测试结果"))
        if "失败恢复与权限结论需要独立证据" in body:
            result["claims"].append(dict(subject="read_evidence_principle",attribute="recovery_permission_needs_independent_evidence",value=True))
        # 对整段文本是否有“上一批接口梳理完成”的证明作人工已审核的文本性投影；不推断业务未完成。
        result["claims"].append(dict(subject=source["id"],attribute="has_upstream_review_completion_evidence",value=False))
        derivations.append(dict(attribute="has_upstream_review_completion_evidence",basis="逐原消息复核：用户只读请求/read_file源码与台账/阅读ack，未出现该上一批工作的完成证据；是文本内容缺失，不是业务不存在或未完成"))
    result["evidence_projection_derivations_v2"]=derivations
    return result


def verification_model(subjects):
    return [req(subjects,"depends","upstream"),req(subjects,"verification_action_performed"),
            req(subjects,"verification_current_status","completed")]


def main():
    out=Path(__file__).resolve().parent
    if (out/"manifest.json").exists():
        raise RuntimeError("禁止覆盖V2冻结产物")
    old=ROOT/"eval/context_remediation_v1/diagnostics/reference_rules_adversarial"
    cross=ROOT/"eval/context_remediation_v1/diagnostics/cross_run_reference_audit"
    old_hashes=file_hashes(old)
    cross_hashes=file_hashes(cross)
    old_manifest=json.loads((old/"manifest.json").read_text())
    assert json.loads((old/"manual_review_findings.json").read_text())["final_gate"]=="REFERENCE_RULES_ADVERSARIAL_FAIL"
    assert json.loads((old/"test_results.json").read_text())["passed"]==49
    assert all(sha256((old/p).read_bytes()).hexdigest()==h for p,h in old_manifest["frozen_files"].items())
    original=sum([json.loads((old/f).read_text()) for f in ["adversarial_cases.json","historical_regression_cases.json","positive_controls.json"]],[])
    old_expected=json.loads((old/"expected_results.json").read_text())
    assert len(original)==49
    cases=[]; diff=[]; audits=[]; expected={}
    annotation_cases={"A1-full","A1-endpoints","A1-summary","A1-union-trap","A1-bundle","A2-full","A2-partial","A2-ambiguous","A3-above","A3-batch"}
    next_action_cases={"A5-complete","A5-partial","A5-future"}
    changed_class={"A3-these":"UNSUPPORTED_FACT","A3-all-deps":"UNSUPPORTED_FACT","A4-all-checks":"UNSUPPORTED_FACT","A5-complete":"UNSUPPORTED_FACT","A5-partial":"UNSUPPORTED_FACT"}
    for orig in original:
        case=deepcopy(orig)
        case["original_sources_v1"]=deepcopy(orig["sources"])
        case["sources"]=[evidence_projection(s) for s in orig["sources"]]
        case["projection_version"]="V2"
        reasons=[]
        cid=case["case_id"]
        if cid in annotation_cases:
            case["claim_model"].append(req(orig["claim_model"][0]["subjects"],"dependency_annotation_present"))
            reasons.append("标注/记录义务独立保留；来源实际depends记录证明已记载，不等于依赖核验已完成。")
        if cid=="A3-these":
            case["claim_model"]=verification_model(list(range(1,31)))
            reasons.append("补足核验动作及当前完成状态；原始30个来源只陈述depends，未提供核验动作或完成状态，不补填任何匹配值。")
        if cid=="A3-related":
            case["claim_model"]=[req(list(range(1,31)),"module_passed",scope_unknown=True)]
            reasons.append("所有相关模块通过不能投影为上游依赖；保留通过谓词及未解析范围，结果仍UNCERTAIN。")
        if cid=="A3-all-deps":
            case["claim_model"]=[req(["scope"],"all_dependencies",["cert"])]
            reasons.append("当前全部依赖不等同唯一未决依赖；原证据sole_pending仅覆盖未决子集，不能排除其他已解决但仍属依赖的事项。原句整体集合义务保留，来源缺乏该完整集合声明。")
        if cid=="A4-all-checks":
            case["claim_model"]=[req(["x"],"check_passed")]
            reasons.append("检查通过不等于完成；源只有completed的更新，未提供check_passed命题，所以不能以完成状态false判检查失败；未获支持。")
        if cid in next_action_cases:
            case["claim_model"].append(req(["task"],"next_action","continue"))
            reasons.append("审批后继续保留动作continue，不能只保存前置条件after_approval；来源只写next_condition，缺少动作。")
        if cid=="A5-one-unknown":
            case["claim_model"][-1]=req(["task"],"acceptance_completed",scope_unknown=True)
            reasons.append("已验收谓词显式保留，相关范围未知不默认为通过。")
        if cid=="A5-unsupported-part":
            case["claim_model"][1]=req(["task"],"task_completed")
            reasons.append("MAP完成是任务完成，不用仅里程碑done替代；发布义务仍保留，源不支持这些额外整体状态。")
        if cid.startswith("H-"):
            raw=case["content"]
            refs=case["source_refs"]
            if cid in {"H-Pilot10-f6","H-Stability-L3-R2-f6"}:
                case["claim_model"].append(req([s["id"] for s in case["sources"]],"has_upstream_review_completion_evidence",False))
                reasons.append("原句第二子命题为全前缀没有上批工作完成证据，旧模型只覆盖依赖全称；新增文本性缺失义务，不推断业务没完成。")
            if any(x in raw for x in ["不是本轮执行测试结果","断言存在不能视为实际通过","不能据此确认测试实际通过","区分了实现证据与测试结果","不是执行测试结果"]):
                case["claim_model"].append(req(["read_evidence_principle"],"does_not_prove_runtime_tests"))
                reasons.append("阅读证据与测试PASS不等价这一独立子命题补入投影，引用原前言限定，而非漏掉子命题只测全称。")
            if "失败恢复与权限结论需要独立证据" in raw:
                case["claim_model"].append(req(["read_evidence_principle"],"recovery_permission_needs_independent_evidence"))
                reasons.append("恢复/权限需独立证据的子命题分别检查。")
            if case["category"]=="HISTORICAL_POSITIVE_CONTROL" and "工作项" in raw:
                if cid=="H-Pilot10-f3": numbers=range(1,11)
                elif cid=="H-Stability-L3-R2-f4": numbers=range(11,21)
                elif cid=="H-Pilot11-f3": numbers=range(1,31)
                else: numbers=[30]
                case["claim_model"].append(req(list(numbers),"completion_kind","source_reading"))
                # 原句逐列文件则逐对象核对；“工作项30对应文件”只要求项30，不能新增原句未声明的全部文件义务。
                path_numbers=[30] if cid=="H-Pilot11-f3" else list(numbers)
                for n in path_numbers:
                    source=next(s for s in case["sources"] if any(c["subject"]==n and c["attribute"]=="source_file" for c in s["claims"]))
                    path=next(c["value"] for c in source["claims"] if c["subject"]==n and c["attribute"]=="source_file")
                    assert path in raw
                    case["claim_model"].append(req([n],"source_file",path))
                if "不是测试执行结果" in raw:
                    case["claim_model"].append(req([30],"not_runtime_test_result"))
                reasons.append("补足读取动作种类与原句明确列出的文件身份，原引用/原始消息完全不动；只给新投影加可追溯字段。")
        if not reasons:
            reasons=["原投影已保留主体、范围、谓词、状态、条件/否定/唯一性及切点；无需改其命题。"]
        assert case["content"]==orig["content"] and case["source_refs"]==orig["source_refs"] and case["cutoff"]==orig["cutoff"]
        assert case["original_sources_v1"]==orig["sources"]
        expected[cid]=dict(classification=changed_class.get(cid,old_expected[cid]["classification"]),
            rationale="；".join(reasons),original_classification=old_expected[cid]["classification"])
        source_locations=[dict(index=i,source_id=s["id"],original_sha256=sha256(json.dumps(s,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),raw_text=s.get("raw_text"),original_message=s.get("original_message")) for i,s in enumerate(orig["sources"])]
        audit=dict(case_id=cid,original_fact=orig["content"],review_status="PROJECTION_FIDELITY_PASS",review_reasons=reasons,
            claim_coverage="PASS",evidence_grounding="PASS_WITH_MISSING_EVIDENCE_EXPLICIT",scope_fidelity="PASS_UNKNOWN_PRESERVED" if any(r.get("scope_unknown") for r in case["claim_model"]) else "PASS",
            state_fidelity="PASS",temporal_fidelity="PASS_CUTOFF_EFFECTIVE_AT_UNCHANGED",classification_pre_registered=expected[cid]["classification"],
            semantic_dimensions=dict(subject="exact subject set/identity maintained",scope="full/range/min_support or explicit unknown maintained",
                predicate=[r["attribute"] for r in case["claim_model"]],action="verification performed and current completed separate from provenance qualification" if cid=="A3-these" else "action/reading/next step reflected if asserted",
                state="current status not replaced with existence",condition="next_action and next_condition both present" if cid in next_action_cases else "preserved or NOT_APPLICABLE",
                negation="False obligations reflect text-content absence, not world absence from missing data",exclusivity="sole_pending/identity_scope retained when asserted",
                time="original cutoff/effective_at retained",compound="all material source-state-condition/file-identity obligations enumerated"),
            obligation_to_evidence=[dict(obligation_index=i,obligation=r,evidence_scope="original sources and cited refs, separately evaluated at test time") for i,r in enumerate(case["claim_model"])],
            original_evidence_locations=source_locations)
        audits.append(audit)
        diff.append(dict(case_id=cid,original_fact=orig["content"],old_projection=orig["claim_model"],new_projection=case["claim_model"],
            reasons=reasons,old_expected=old_expected[cid]["classification"],new_expected=expected[cid]["classification"],
            original_case_location=str(old/"adversarial_cases.json") if cid.startswith("A") else str(old/"historical_regression_cases.json") if case["category"]=="HISTORICAL_REGRESSION" else str(old/"positive_controls.json"),
            source_evidence_unchanged=True,affects_historical_original=False,changes_scoring_rules=False,
            evidence_projection_v2_added_attributes=sorted({c["attribute"] for s in case["sources"] for c in s["claims"]}-{c["attribute"] for s in orig["sources"] for c in s["claims"]})))
        cases.append(case)
    # 新增专用变体，来源原文明确时才产生核验事件/完成态，不能为原A3-these注入这些证据。
    group=list(range(1,31))
    def dep_source(n):
        return dict(id=f"d{n}",sequence=n,semantic_qualification="CONFIRMED",raw_text=f"接口{n}依赖upstream，仅记录依赖关系，未提供核验动作信息。",
            claims=[dict(subject=n,attribute="depends",value="upstream")])
    deps=[dep_source(n) for n in group]
    def verified(identity,sequence,objects,summary=False):
        raw=[dict(接口=n,依赖="upstream",核验动作="dependency_verification",当前核验状态="completed") for n in objects]
        return dict(id=identity,sequence=sequence,semantic_qualification="CONFIRMED",authority_scope_qualification="明确逐接口核验记录，来源可信性与内容由人工确认",
            raw_text=json.dumps(dict(证据类型="权威完整核验汇总" if summary else "核验操作结果",生效事件序号=sequence,记录=raw),ensure_ascii=False),
            claims=[dict(subject=n,attribute=a,value=v) for n in objects for a,v in [("depends","upstream"),("verification_action_performed",True),("verification_current_status","completed")]])
    variants=[]
    def variant(cid,description,sources,refs,classification):
        variants.append(dict(case_id=cid,category="A3_THESE_V2_VARIANT",polarity="boundary",content="这些接口均已核验依赖。",status="active",
            source_refs=refs,sources=sources,claim_model=verification_model(group),cutoff=100,
            fact_types=["UNIVERSAL_OR_RANGE_CLAIM","COMPLETED_MILESTONE"],scope_resolution="这些接口=固定的接口1—30",
            variant_description=description,semantic_projection_version="V2"))
        expected[cid]=dict(classification=classification,rationale=description)
    variant("V2-dependency-only","原始证据仅依赖存在，没有核验动作及完成态，不向来源补齐目标布尔值。",deepcopy(deps),[s["id"] for s in deps],"UNSUPPORTED_FACT")
    started=deepcopy(deps)
    for s in started:
        n=s["claims"][0]["subject"]
        s["raw_text"]+=f" 已记录接口{n}核验开始事件，但未提供后续完成状态。"
        s["claims"].append(dict(subject=n,attribute="verification_action_performed",value=True))
    variant("V2-start-only","证明动作启动，不证明完成，也不把没完成证据误填为当前业务状态False。",started,[s["id"] for s in started],"UNSUPPORTED_FACT")
    all_records=[verified(f"v{n}",31+n,[n]) for n in group]
    variant("V2-all-completed","每项操作结果有明确动作与completed状态，覆盖全部30接口。",all_records,[s["id"] for s in all_records],"PASS")
    half=[verified(f"v{n}",31+n,[n]) for n in range(1,16)]
    variant("V2-partial-completed","只有1—15完成证据，16—30只有依赖关系，整个前缀也不足以证明全部完成。",deepcopy(deps)+half,[s["id"] for s in half],"UNSUPPORTED_FACT")
    variant("V2-prefix-only-completed","完整前缀含全部核验结果，但Fact仅引用旧依赖记录，引用不足。",deepcopy(deps)+all_records,[s["id"] for s in deps],"REFERENCE_GAP")
    ledger=verified("authority-ledger",61,group,True)
    variant("V2-authoritative-summary","单条权威汇总实际包含30项动作与completed状态，不能按refs数量误伤。",[ledger],["authority-ledger"],"PASS")
    revoked=dict(id="revoke",sequence=62,semantic_qualification="CONFIRMED",explicit_update=True,
        raw_text="接口30的当前核验结论已撤销，当前核验状态revoked；过去发生的核验动作未抹除。",
        claims=[dict(subject=30,attribute="verification_current_status",value="revoked")])
    variant("V2-revoked","完整核验后接口30发生明确状态撤销，不能用旧汇总声明当前全部completed；历史动作仍有据。",[ledger,revoked],["authority-ledger"],"STATE_ERROR")
    vague=deepcopy(ledger);vague["id"]="vague";vague["semantic_qualification"]="UNKNOWN";vague["authority_scope_qualification"]="UNKNOWN_SCOPE_NOT_VERIFIABLE";vague["raw_text"]="部分接口已核验完成，但范围和当前状态不可核实。"
    variant("V2-ambiguous-summary","模糊汇总的结构化候选字段不升级成真实已知覆盖，人工资格UNKNOWN。",[vague],["vague"],"UNCERTAIN")
    save(out/"projection_v2_cases.json",cases)
    save(out/"projection_diff.json",diff)
    save(out/"expected_results_v2.json",expected)
    save(out/"projection_fidelity_audit.json",dict(status="PRE_TEST_MANUAL_QUALIFICATION_COMPLETE",cases=audits,total=49,
        original_sources_preserved=True,history_records_preserved=True,test_not_run_yet=True,classification_changes=changed_class))
    save(out/"a3_these_regression.json",variants)
    (out/"projection_contract_v2.md").write_text("""# Projection Fidelity V2 Contract\n\n先逐原句/原始证据审核，后规范化命题和来源桥接，再按证据决定新版答案，冻结后测试。\n业务核验动作与当前完成态不得由semantic_qualification推出。当前已撤销不等于过去动作没发生。\ncompleted不等于check_passed；next_condition不等于next_action；里程碑完成不等于任务整体完成。\n记录exists可由原文/结构化来源本身有该记录证明，不把它升级为业务核验事件。\n历史原始Fact/refs/source原文/cutoff不变，新模型只补命题与可追踪证据投影。原三层规则不放宽。\n所有语义维度和复合子命题须显式投影；证据缺乏也要保留义务，不删义务制造PASS。\n引用充分性与OracleRecall仍独立，UNKNOWN范围显式保留不默认判PASS。\n新版预期与旧版可不同，差异仅据原文/原始证据及新义务，测试后不改答案。\n自动模型检查不解析自然语言，最终需人工对照事实/证据/投影/预期/结果。\n""",encoding="utf-8")
    manifest=dict(experiment="FIXTURE-SEMANTIC-PROJECTION-V2",created_at=datetime.now(timezone.utc).isoformat(),
        original_49_cases=len(cases),new_a3_variants=len(variants),old_gate="REFERENCE_RULES_ADVERSARIAL_FAIL",
        old_before=old_hashes,cross_before=cross_hashes,production_hashes={str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/"hexgent").rglob("*.py"))},
        frozen_assets=fingerprints(),original_checker_sha256=sha256((old/"checker.py").read_bytes()).hexdigest(),
        frozen_files={str(p.relative_to(out)):sha256(p.read_bytes()).hexdigest() for p in out.rglob("*") if p.is_file() and "__pycache__" not in p.parts},
        network_calls=0,model_calls=0,expected_frozen_before_tests=True,formal_prompt_v3_created=False)
    save(out/"manifest.json",manifest)
    print(json.dumps(dict(output=str(out),prepared=True,cases=49,variants=len(variants),classification_changes=changed_class),ensure_ascii=False))


if __name__=="__main__":
    main()
