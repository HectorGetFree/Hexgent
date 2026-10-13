"""生成并冻结样例和独立预期答案；本步骤不运行检查器。"""
from copy import deepcopy
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT))
from eval.context_remediation_v1.offline import save,fingerprints
from eval.context_remediation_v1.factorial_pilot import file_hashes


def requirement(subjects,attribute,value=True,**extra):
    return dict(subjects=subjects,attribute=attribute,value=value,**extra)


def unit(identity,seq,subjects,attr,value=True,**extra):
    result=dict(id=identity,sequence=seq,semantic_qualification="CONFIRMED",
        raw_text=f"独立夹具证据：{subjects} 的 {attr}={value}",
        claims=[dict(subject=s,attribute=attr,value=value) for s in subjects])
    result.update(extra)
    return result


def main():
    out=Path(__file__).resolve().parent
    if (out/"manifest.json").exists():
        raise RuntimeError("禁止覆盖预注册样例")
    audit=ROOT/"eval/context_remediation_v1/diagnostics/cross_run_reference_audit"
    baseline=json.loads((audit/"fact_level_audit.json").read_text())
    assert baseline["total_facts"]==52 and baseline["counts"]["reference_gap"]==6
    assert len({f["run_id"] for f in baseline["facts"]})==6
    for name in ["reference_sufficiency_rules.md","range_claim_audit.json","cross_run_comparison.json","historical_discrepancies.md","final_review.md"]:
        assert (audit/name).is_file()
        (audit/name).read_text()
    roots=[audit,ROOT/"eval/context_remediation_v1/runs/extractor_v2_stability",
        ROOT/"eval/context_remediation_v1/runs/r_sparse_channel_corrected_v1",
        ROOT/"eval/context_remediation_v1/runs/extractor-quality-pilot-v1-20261012"]
    preserved={str(p):file_hashes(p) for p in roots}
    prod={str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/"hexgent").rglob("*.py"))}
    cases=[]
    expected={}
    def add(identity,category,polarity,text,refs,sources,claims,answer,rationale,**kwargs):
        cases.append(dict(case_id=identity,category=category,polarity=polarity,fact_id=identity,
            content=text,status="active",source_refs=refs,sources=deepcopy(sources),claim_model=claims,
            cutoff=kwargs.pop("cutoff",100),fact_types=kwargs.pop("fact_types",["UNIVERSAL_OR_RANGE_CLAIM"]),
            semantic_annotation_contract="人工核对原文、指代、authority、有效时间后规范化；检查器不从原文自动生成claim_model",**kwargs))
        expected[identity]=dict(classification=answer,rationale=rationale)
    subjects=list(range(1,31))
    all_sources=[unit(f"s{i}",i,[i],"depends","upstream") for i in subjects]
    full_refs=[s["id"] for s in all_sources]
    all_claim=[requirement(subjects,"depends","upstream")]
    summary=unit("ledger",31,subjects,"depends","upstream",kind="AUTHORITATIVE_SUMMARY",raw_title="总览",authority_qualification="CONFIRMED_BY_CONTENT_AND_PROVENANCE")
    add("A1-full","A1","positive","工作项1—30均标注依赖。",full_refs,all_sources,all_claim,"PASS","逐项完整覆盖范围及属性。")
    add("A1-endpoints","A1","negative","工作项1—30均标注依赖。",["s1","s30"],all_sources,all_claim,"REFERENCE_GAP","完整前缀有证据，端点不是权威汇总。")
    add("A1-summary","A1","boundary","工作项1—30均标注依赖。",["ledger"],[summary],all_claim,"PASS","单条权威汇总完整覆盖，不能要求30个引用。")
    add("A1-union-trap","A1","negative","工作项1—30均标注依赖。",["s1","s30"],all_sources,all_claim,"REFERENCE_GAP","其他Facts refs不能自动补足本Fact。",other_fact_refs=full_refs)
    add("A1-wording","A1","negative","整个批次的三十项都依赖上游。",["s1","s30"],all_sources,all_claim,"REFERENCE_GAP","同义措辞经人工确认指同一范围，不由正则措辞改变判定。",natural_language_manual_review=True)
    bundled=unit("bundle",31,subjects,"depends","upstream",kind="COMPLETE_TOOL_CALL_AND_RESULT_BUNDLE")
    add("A1-bundle","A1","boundary","工作项1—30均标注依赖。",["bundle"],[bundled],all_claim,"PASS","一条可核验完整工件覆盖全范围，不以refs数量判FAIL。")
    add("A2-full","A2","positive","全部30项依赖已记录。",["ledger"],[summary],all_claim,"PASS","内容、身份、范围均资格确认。")
    partial=unit("partial",31,list(range(1,16)),"depends","upstream",raw_title="完整总览全部项目")
    add("A2-partial","A2","negative","全部30项依赖已记录。",["partial"],all_sources+[partial],all_claim,"REFERENCE_GAP","标题不替代内容，只有15项。")
    ambiguous=unit("ambiguous",31,subjects,"depends","upstream",semantic_qualification="UNKNOWN",raw_text="部分相关工作项状态已汇总，范围不清。")
    add("A2-ambiguous","A2","boundary","全部30项依赖已记录。",["ambiguous"],all_sources+[ambiguous],all_claim,"UNCERTAIN","不可把模糊汇总的模型覆盖元数据当真实权威覆盖，交人工。",natural_language_manual_review=True)
    old=unit("old",1,["x"],"completed",True)
    changed=unit("changed",2,["x"],"completed",False,explicit_update=True)
    add("A2-stale","A2","negative","x当前完成。",["old"],[old,changed],[requirement(["x"],"completed")],"STATE_ERROR","明确后续更新否定旧状态，来源合法但旧状态不生效。")
    a=unit("a",1,["x"],"completed",True,effective_at=1)
    b=unit("b",2,["x"],"completed",False,effective_at=1)
    add("A2-conflict","A2","boundary","x当前完成。",["a"],[a,b],[requirement(["x"],"completed")],"UNCERTAIN","相同有效时间且没有显式解决规则的权威冲突，不能任意挑一个。",natural_language_manual_review=True)
    add("A3-above","A3","negative","上述工作项均标注依赖。",["s1","s30"],all_sources,all_claim,"REFERENCE_GAP","前文已列举1—30，指代范围确定。",antecedent="工作项1—30",natural_language_manual_review=True)
    add("A3-these","A3","positive","这些接口均已核验依赖。",full_refs,all_sources,all_claim,"PASS","有明确对象集合且引用覆盖。",antecedent="30个接口对应工作项1—30",natural_language_manual_review=True)
    add("A3-batch","A3","negative","整批工具结果均标注依赖。",["s1","s30"],all_sources,all_claim,"REFERENCE_GAP","隐式全称不比显式1—30更宽松。",natural_language_manual_review=True)
    add("A3-related","A3","boundary","所有相关模块都通过。",["s1"],all_sources,[requirement(subjects,"depends","upstream",scope_unknown=True)],"UNCERTAIN","相关模块集合无法确定，不默认PASS或FAIL。",natural_language_manual_review=True)
    add("A3-main","A3","positive","本阶段主要工作依赖上游。",full_refs[:16],all_sources,[requirement(subjects,"depends","upstream",min_support=16)],"PASS","已人工确认主要=固定30项中超过一半，16个支持足够，不机械要求全30。",antecedent="阶段对象集合1—30，主要按过半解释",natural_language_manual_review=True)
    add("A3-all-deps","A3","boundary","当前全部依赖为证书审批。",["only"],[unit("only",1,["scope"],"sole_pending","cert")],[requirement(["scope"],"sole_pending","cert")],"PASS","权威源明确排他性集合，不是因没看到其他依赖而猜唯一。")
    add("A4-unique","A4","positive","唯一未决依赖为证书审批。",["only"],[unit("only",1,["scope"],"sole_pending","cert")],[requirement(["scope"],"sole_pending","cert")],"PASS","来源原文确认唯一。")
    add("A4-existence-only","A4","negative","唯一未决依赖为证书审批。",["exists"],[unit("exists",1,["scope"],"pending_exists","cert")],[requirement(["scope"],"sole_pending","cert")],"UNSUPPORTED_FACT","存在不蕴含唯一；信息缺失不证明排他。")
    add("A4-no-blockers","A4","positive","没有其他阻塞。",["checked"],[unit("checked",1,["scope"],"other_blockers",False,kind="COMPLETE_SCOPE_CHECK")],[requirement(["scope"],"other_blockers",False)],"PASS","已确认完整范围检查及明确否定证据。")
    add("A4-absence-only","A4","negative","不存在失败记录。",["empty"],[dict(id="empty",sequence=1,semantic_qualification="CONFIRMED",raw_text="没有提供运行记录。",claims=[])],[requirement(["scope"],"failure_record",False)],"UNSUPPORTED_FACT","没提供不等于不存在；FAIL是未被支持，不宣称命题事实上为假。")
    add("A4-open-scope","A4","boundary","未发现测试证据。",["s1"],all_sources,[requirement(subjects,"test_evidence",False,scope_unknown=True)],"UNCERTAIN","检查范围未说明；不能把未见局部信息当全历史不存在。",natural_language_manual_review=True)
    ev=[unit(f"e{i}",i,[i],"test_evidence",False) for i in subjects]
    add("A4-complete-negative","A4","positive","完整前缀未发现测试证据。",[s["id"] for s in ev],ev,[requirement(subjects,"test_evidence",False)],"PASS","范围完整且逐记录证据资格明确，形成穷尽的否定性证据链。")
    add("A4-local-negative","A4","negative","完整前缀未发现测试证据。",["e1","e30"],ev,[requirement(subjects,"test_evidence",False)],"REFERENCE_GAP","两个局部免责声明不能证明全前缀缺失。")
    add("A4-all-checks","A4","negative","全部检查均通过。",["old"],[old,changed],[requirement(["x"],"completed")],"STATE_ERROR","明确失败更新使当前全部通过不成立。")
    multi=[unit("id",1,["task"],"identity","MAP"),unit("done",2,["task"],"milestone","done"),
           unit("wait",3,["task"],"sole_pending","approval"),unit("next",4,["task"],"next_condition","after_approval")]
    reqs=[requirement(["task"],"identity","MAP"),requirement(["task"],"milestone","done"),
          requirement(["task"],"sole_pending","approval"),requirement(["task"],"next_condition","after_approval")]
    add("A5-complete","A5","positive","MAP里程碑完成，唯一依赖审批，审批后继续。",[s["id"] for s in multi],multi,reqs,"PASS","各独立子命题均有来源。")
    add("A5-partial","A5","negative","MAP里程碑完成，唯一依赖审批，审批后继续。",["id","done"],multi,reqs,"REFERENCE_GAP","主体与完成充分，依赖/条件缺引用；不能把整句判无依据。")
    add("A5-one-unknown","A5","boundary","MAP里程碑完成，相关范围已验收。",["id","done"],multi,reqs[:2]+[requirement(["task"],"unknown_scope",True,scope_unknown=True)],"UNCERTAIN","一个必要子命题未知，不能因前两项充分就整条PASS。",natural_language_manual_review=True)
    add("A5-unsupported-part","A5","negative","MAP完成并已发布。",["id","done"],multi,reqs[:2]+[requirement(["task"],"published",True)],"UNSUPPORTED_FACT","部分事实有据，不掩盖新增发布状态无证据。")
    add("A5-invalid","A5","boundary","MAP身份确认。",["missing"],multi,reqs[:1],"SOURCE_INVALID","不存在的source ID明确失败，不将有据前缀当refs合法。")
    add("A5-future","A5","negative","审批后继续。",["future"],multi+[unit("future",101,["task"],"next_condition","after_approval")],[reqs[3]],"SOURCE_INVALID","未来引用不可用，即使合法前缀也有同义事实。",cutoff=100)
    complete=[unit(f"c{i}",i,[i],"completed",True) for i in subjects]
    add("A6-broad","A6","negative","工作项1—30全部完成。",["c1","c30"],complete,[requirement(subjects,"completed")],"REFERENCE_GAP","范围超过自身refs。")
    add("A6-narrow","A6","positive","工作项1和30已完成。",["c1","c30"],complete,[requirement([1,30],"completed")],"PASS","合理收窄可PASS，但Oracle若要求全部30则保留失败。",oracle_requirements=[requirement(subjects,"completed")])
    add("A6-drop-dependency","A6","boundary","MAP身份确认。",["id"],multi,reqs[:1],"PASS","引用充分，但丢失Oracle必需依赖；引用与Recall分别评分。",oracle_requirements=[reqs[0],reqs[2]])
    add("A6-wrong-downgrade","A6","negative","工作项1尚未完成。",["c1"],complete,[requirement([1],"completed",False)],"STATE_ERROR","不能为保守收窄把已完成降级为未完成。")
    # 历史样例复制原始文本/refs/切点，并人工规范化各源的局部证据，不更改原Artifact。
    historical=[]
    controls=[]
    chosen_controls={ ("Pilot10","f3"),("Pilot11","f3"),("R-SPARSE-S1","f1"),("R-SPARSE-S1","f2"),
        ("R-SPARSE-S1","f3"),("Stability-L3-R1","f3"),("Stability-L3-R2","f4"),("Stability-SP-R1","f3") }
    for entry in baseline["facts"]:
        is_gap=entry["per_fact_reference_sufficiency"]=="REFERENCE_GAP"
        if not is_gap and (entry["run_id"],entry["fact_id"]) not in chosen_controls:
            continue
        payload=json.loads((audit/entry["full_prefix_evidence_file"]).read_text())
        typed=[]
        work_ids=[]
        for index,s in enumerate(payload["history_prefix"]):
            m=s["message"]
            claims=[dict(subject=s["message_id"],attribute="has_test_execution_evidence",value=False)]
            import re
            match=re.search(r"工作项 (\d+)：完成正文阅读；依赖上一批接口梳理。",m.get("output",""))
            if match:
                number=int(match[1]); work_ids.append(number)
                claims += [dict(subject=number,attribute="depends",value="upstream"),dict(subject=number,attribute="read_kind",value=True),dict(subject=number,attribute="completed",value=True)]
            body=m.get("output","")
            if "审查范围仅限 ORBIT-93" in body:
                claims.append(dict(subject="scope",attribute="identity_scope",value="ORBIT-93"))
            if "唯一未决依赖是证书更换审批" in body:
                claims.append(dict(subject="scope",attribute="sole_pending",value="cert"))
            if m.get("type")=="function_call":
                path=json.loads(m["arguments"]).get("path")
                claims.append(dict(subject=m["call_id"],attribute="path",value=path))
            if m.get("type")=="function_call_output":
                claims.append(dict(subject=m["call_id"],attribute="returned",value=True))
            typed.append(dict(id=s["message_id"],sequence=int(s["message_id"][4:]),claims=claims,semantic_qualification="CONFIRMED",
                original_message=m,source_file=str(audit/entry["full_prefix_evidence_file"]),annotation_basis="人工核对完整冻结前缀：仅用户阅读请求/read_file调用/冻结源码资料或背景/阅读ack，无实际测试执行输出；每source只证明自身，不把整段复制为局部汇总"))
        req=[]
        if is_gap:
            if entry["fact_id"]=="f6":
                req=[requirement(list(range(1,31)),"depends","upstream")]
            elif entry["run_id"]=="Pilot11":
                req=[requirement(list(range(1,31)),"read_kind",True,min_support=16)]
            elif entry["run_id"]=="Stability-L3-R1":
                req=[requirement([s["message_id"] for s in payload["history_prefix"]],"has_test_execution_evidence",False)]
            else:
                req=[requirement(list(range(1,31)),"read_kind")]
                if entry["run_id"]=="Pilot10":
                    req.append(requirement([s["message_id"] for s in payload["history_prefix"]],"has_test_execution_evidence",False))
        else:
            if (entry["run_id"],entry["fact_id"]) in {("Pilot10","f3"),("Stability-L3-R2","f4"),("Pilot11","f3")}:
                needed=list(range(1,11)) if entry["run_id"]=="Pilot10" else list(range(11,21)) if entry["run_id"]=="Stability-L3-R2" else list(range(1,31))
                req=[requirement(needed,"completed")]
            elif entry["run_id"]=="Stability-L3-R1":
                req=[requirement([30],"completed")]
            elif "仅限" in entry["content"]:
                req=[requirement(["scope"],"identity_scope","ORBIT-93")]
            elif "唯一" in entry["content"]:
                req=[requirement(["scope"],"sole_pending","cert")]
            else:
                required_calls=[s["message"]["call_id"] for s in payload["history_prefix"] if s["message"].get("type")=="function_call"]
                if entry["run_id"]=="R-SPARSE-S1":
                    required_calls=required_calls[:1]
                req=[requirement([cid],"path",f"source-{i+1}.txt") for i,cid in enumerate(required_calls)]+[requirement(required_calls,"returned")]
        identity="H-"+entry["run_id"]+"-"+entry["fact_id"]
        case=dict(case_id=identity,category="HISTORICAL_REGRESSION" if is_gap else "HISTORICAL_POSITIVE_CONTROL",polarity="negative" if is_gap else "positive",
            fact_id=entry["fact_id"],content=entry["content"],status=entry["status"],source_refs=entry["source_refs"],sources=typed,
            claim_model=req,cutoff=int(payload["as_of_message_id"][4:]),fact_types=entry["fact_types"],natural_language_manual_review=True,
            historical_run=entry["run_id"],response_id=entry["response_id"],original_audit_entry=entry,
            original_audit_expected=entry["per_fact_reference_sufficiency"],semantic_annotation_contract="保留原文/原refs/原切点；额外模型仅离线人工证据资格化，不是重新解释历史Gate")
        expected[identity]=dict(classification="REFERENCE_GAP" if is_gap else "PASS",rationale=entry["rationale"])
        (historical if is_gap else controls).append(case)
    assert len(historical)==6 and len(controls)==8
    save(out/"adversarial_cases.json",cases)
    save(out/"historical_regression_cases.json",historical)
    save(out/"positive_controls.json",controls)
    save(out/"expected_results.json",expected)
    (out/"rules_v1.md").write_text("""# Reference Sufficiency Scoring Contract v1\n\nA来源合法、B完整前缀支持、C自身refs充分分别判断；不合并其他Fact refs。复合子命题逐一判断，必要一项不足则整条不PASS。\n全称/范围/多数/否定/排他需范围、属性、时间均有证据；权威汇总或一条完整工件可覆盖多个对象，不能按refs个数机械拒绝。\n不自动信任汇总标题，汇总内容/范围/可信性由人工资格确认；模糊指代/范围、同一有效时间冲突交UNCERTAIN，不默认PASS或FAIL。\n声明不存在必须有负向证据或完整范围检查，信息未提供不是不存在；FAIL表示不能支持，不表示证明事实为假。\n明确后续状态更新使旧状态不再有效；无法可靠解释更新时交人工，而不是泛化latest-wins。确定性模型的effective_at/显式更新先由人工确认。\n支持若干子命题不能掩盖其他子命题缺证据。合理收窄可以引用充分，但仍单独检查Oracle身份/里程碑/关键依赖是否保留。\nOVERGENERALIZATION可重叠REFERENCE_GAP，按唯一case/fact计问题，不重复累计；多个其他标签也保留。UNCERTAIN单独统计，不视作已确定错误。\nchecker只对已经人工规范化的原子命题/对象集合/来源有效性做确定性集合覆盖，不读取预期结果，不解析自然语言或证明人工注释正确。\n合格需两层：来源/语义人工资格确认＋模型覆盖检查；禁止把结构测试全绿宣传为自然语言泛化正确率。\n测试答案expected_results独立保存，manifest先冻结；不能运行后改答案。\n""",encoding="utf-8")
    manifest=dict(experiment="REFERENCE_RULES_ADVERSARIAL_V1",created_at=datetime.now(timezone.utc).isoformat(),
        synthetic_case_count=len(cases),historical_regressions=6,positive_controls=8,expected_baseline_facts=52,baseline_reference_gaps=6,
        baseline_before=preserved,production_hashes=prod,frozen_assets=fingerprints(),
        frozen_files={str(p.relative_to(out)):sha256(p.read_bytes()).hexdigest() for p in out.rglob("*") if p.is_file()},
        checker_scope="人工语义模型上的确定性检查；不解析NL",network_calls=0,model_calls=0,
        categories=sorted({c["category"] for c in cases}),test_order=[c["case_id"] for c in cases+historical+controls])
    save(out/"manifest.json",manifest)
    print(json.dumps(dict(output=str(out),prepared=True,synthetic=len(cases),historical=6,controls=8,total=len(cases)+14),ensure_ascii=False))


if __name__=="__main__":
    main()
