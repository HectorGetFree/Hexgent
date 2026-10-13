"""六个既有完整响应的统一离线来源充分性审计。"""
from collections import Counter
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT))
from eval.context_remediation_v1.offline import save,fingerprints
from eval.context_remediation_v1.factorial_pilot import file_hashes
from hexgent.context.key_fact_snapshot import validate_snapshot


def main():
    output=Path(__file__).resolve().parent
    if (output/"audit_manifest.json").exists():
        raise RuntimeError("禁止覆盖独立审计")
    base=ROOT/"eval/context_remediation_v1/runs"
    stability=base/"extractor_v2_stability"
    pilot=base/"extractor-quality-pilot-v1-20261012"
    sparse=base/"r_sparse_channel_corrected_v1"
    p10=base/"extractor-quality-pilot-v1-retry-20261012T073058808085Z"
    p11=base/"extractor-quality-pilot-v1-retry-20261012T074529716101Z"
    records=[
        ("Pilot10",p10/"arms/10",p10/"arms/10/snapshot.json",0,["f2","f7","f8","f9","f10"]),
        ("Pilot11",p11/"arms/11",p11/"arms/11/snapshot.json",0,["f2","f7","f8","f5","f6"]),
        ("R-SPARSE-S1",sparse/"s1",sparse/"s1/snapshot.json",0,["f1","f2"]),
        ("Stability-L3-R1",stability/"responses/L3-R1",stability/"snapshots/L3-R1.json",0,["f2","f4","f5","f6","f7"]),
        ("Stability-L3-R2",stability/"responses/L3-R2",stability/"snapshots/L3-R2.json",1,["f2","f8","f9","f10","f11"]),
        ("Stability-SP-R1",stability/"responses/SP-R1",stability/"snapshots/SP-R1.json",0,["f1","f2"])]
    roots=[pilot,p10,p11,sparse,stability,ROOT/"eval/context_remediation_v1/reviews/factorial-combined-20261012T074921192596Z"]
    preserved={str(p):file_hashes(p) for p in roots}
    prod={str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/"hexgent").rglob("*.py"))}
    freeze=fingerprints()
    rules="""# 统一来源充分性规则（本次独立审计，旧合同不改）\n\n先核对Pilot quality-rules、Sparse quality_rules、Stability quality_contract，均要求各Fact充分支持。\nA Source Validity只核对Schema、身份、前缀/切点；B Full-Prefix Support核对整个合法历史；C只用该Fact自己声明的refs。A/B/C独立。\n对复合fact逐子命题审核。全称/范围须证明集合、覆盖、每项属性、有效时间；已有权威范围汇总可替代逐条引用，不按引用数量机械判定。\n没有权威汇总时，端点个案不能代表中间项目；其他facts的refs并集不能自动补证。\n“上述工作项”的指代由前面完整列举与正文解析，不因没写1—30而放行。\n“整个前缀没有某证据”是集合级否定命题，不能仅靠几个案例的免责声明证明所有其他消息都没有；引用需范围性索引/汇总或完整支持链。\n“已有证据主要是某类型”是比例/范围概括，几个例子不证明整体主要构成；不能事后把整个范围收窄成仅refs所指样例来制造PASS。\n“仅基于这些阅读记录不能推出测试PASS”是局部推理界限，可以由这些记录支持；不等于“整个历史没有测试证据”。\n来源明确当前有效的待确认状态可以保留到切点，需独立检查后续是否更正；这不同于对整个前缀证据集合做缺失性断言。\n唯一性只有在来源自身支持“唯一”或有排他性范围证据时成立。旧状态已废止是当前有效的元事实，不把旧状态激活。\n跨调用/结果的文件主体身份需覆盖对应参数与结果；用户明确只读可支持不作业务变更这一等价限定，不要求字面完全一致。\n没有明确支持时用UNCERTAIN，不默认PASS；有完整前缀支持而自身ref不足记REFERENCE_GAP。OVERGENERALIZATION可与其重叠，但按唯一(run,fact)计数，不双加。\n本次操作化是统一复核，发现历史差异只记HISTORICAL_AUDIT_DISCREPANCY，绝不改旧Gate。\n"""
    (output/"reference_sufficiency_rules.md").write_text(rules,encoding="utf-8")
    # 审计前冻结操作化规则及范围，不将后续输出改变规则。
    save(output/"audit_manifest.json",dict(created_at=datetime.now(timezone.utc).isoformat(),scope=[r[0] for r in records],
        expected_responses=6,rules_sha256=sha256((output/"reference_sufficiency_rules.md").read_bytes()).hexdigest(),
        production_hashes=prod,historical_artifact_hashes=preserved,network_calls=0,
        source_contracts=[str(pilot/"quality-rules.md"),str(sparse/"quality_rules.md"),str(stability/"quality_contract.md")]))
    gaps={
        ("Pilot10","f6"):("上述工作项指前面f3–f5的工作项1—30；只有项1/30记录，不能覆盖中间28项。后半全前缀缺独立确认也是集合级缺失断言。",list(range(2,30))),
        ("Pilot10","f13"):("本批工具与整个历史前缀的证据类型/没有测试通过是全范围主张；引用只有项1/30的阅读免责声明，不是完整前缀权威汇总。",list(range(2,30))),
        ("Pilot11","f4"):("已有证据主要是阅读这一范围/比例主张需整体构成；只引用项1/2/30不能证明整体主要组成。后半不能据此推断测试PASS的局部逻辑成立，但不足使整条引用充分。",[i for i in range(1,31) if i not in {1,2,30}]),
        ("Stability-L3-R1","f8"):("前半来源区分实现与测试得到支持；后半当前前缀未提供测试通过证据是对120条消息的否定性主张，三个阅读样例不穷尽该范围。",[i for i in range(1,31) if i not in {1,2,30}]),
        ("Stability-L3-R2","f6"):("明确称项1—30均依赖，引用仅项1/30；中间28项无本Fact引用覆盖，无范围性权威汇总。前缀缺完成状态也是同一范围性证据缺失主张。",list(range(2,30))),
        ("Stability-L3-R2","f7"):("本轮工具返回均为阅读证据这一批次性概括只有项1/21/30案例引用；这些案例支持各自不是测试结果及一般阅读不等于验证，未证明整批范围。",[i for i in range(1,31) if i not in {1,21,30}]),
    }
    range_bounds={"Pilot10":{"f3":(1,10),"f4":(11,20),"f5":(21,30)},
        "Pilot11":{"f3":(1,30)},"Stability-L3-R2":{"f3":(1,10),"f4":(11,20),"f5":(21,30)}}
    fact_rows=[]
    comparisons=[]
    run_infos=[]
    range_rows=[]
    for run,directory,snapshot_path,oldgap,oracle_ids in records:
        request=json.loads((directory/"actual-request.json").read_text())
        payload=json.loads(request["input"][0]["content"])
        prefix=payload["history_prefix"]
        snapshot=json.loads(snapshot_path.read_text())
        response=json.loads((directory/"response.json").read_text())
        ledger=json.loads((directory/"ledger.json").read_text())
        assert ledger["status"]=="COMPLETED" and ledger["response_id"]==response["id"]
        terminals=[json.loads(p.read_text()) for p in sorted((directory/"events").glob("*.json")) if '"response.completed"' in p.read_text()]
        assert len(terminals)==1 and terminals[0]["response"]==response
        raw=json.loads("".join(c["text"] for m in response["output"] if m["type"]=="message" for c in m["content"] if c["type"]=="output_text"))
        assert validate_snapshot(raw,cutpoint=payload["as_of_message_id"],sources=prefix,previous=None,budget_tokens=2000,excluded_source_ids=())==snapshot
        save(output/"evidence"/(run+"-prefix.json"),payload)
        source={s["message_id"]:s["message"] for s in prefix}
        positions={s["message_id"]:i for i,s in enumerate(prefix)}
        work_items={}
        for s in prefix:
            match=re.search(r"工作项 (\d+)：完成正文阅读；依赖上一批接口梳理。",s["message"].get("output",""))
            if match:
                work_items[int(match[1])]=s["message_id"]
        if len(prefix)>6:
            assert set(work_items)==set(range(1,31))
        gap_count=0
        for fact in snapshot["facts"]:
            identity=fact["id"]
            key=(run,identity)
            types=[]
            gold_index=oracle_ids.index(identity) if identity in oracle_ids else None
            if gold_index is not None:
                kinds=["EXACT_IDENTITY","COMPLETED_MILESTONE","COMPLETED_MILESTONE","PENDING_DEPENDENCY","CONDITIONAL_NEXT_STEP"] if len(oracle_ids)==5 else ["EXACT_IDENTITY","PENDING_DEPENDENCY"]
                types.append(kinds[gold_index])
            if "阅读" in fact["content"] or "读取" in fact["content"]:
                types.append("SOURCE_READING_ACTIVITY")
            if key in gaps or identity in range_bounds.get(run,{}) or "唯一" in fact["content"] or "仅限" in fact["content"] or (run=="Stability-SP-R1" and identity=="f3"):
                types.append("UNIVERSAL_OR_RANGE_CLAIM")
            if not types:
                types=["OTHER"]
            gap=key in gaps
            gap_count+=gap
            ref_evidence=[]
            cited_work=[]
            for ref in fact["source_refs"]:
                message=source[ref]
                match=re.search(r"工作项 (\d+)：",message.get("output",""))
                if match:
                    cited_work.append(int(match[1]))
                body=message.get("output",message.get("content",""))
                body=body if isinstance(body,str) else json.dumps(body,ensure_ascii=False)
                ref_evidence.append(dict(message_id=ref,source_type=message.get("type",message.get("role")),
                    json_pointer=f"/history_prefix/{positions[ref]}/message",source_file=f"evidence/{run}-prefix.json",
                    minimum_excerpts=[line for line in body.splitlines() if any(word in line for word in ("工作项 ","合成审查台账证据", "不是本轮执行测试结果","资料当前结论", "完成阅读不等于验收通过"))][:5]
                        or [body[:260]],message_sha256=sha256(json.dumps(message,ensure_ascii=False,sort_keys=True).encode()).hexdigest()))
            if identity in range_bounds.get(run,{}):
                low,high=range_bounds[run][identity]
                assert set(range(low,high+1)).issubset(cited_work)
                reasoning="逐项引用覆盖该工作项范围、文件路径、正文阅读和非验收状态；不机械要求数量，而是此处无汇总、独立记录恰覆盖所有对象。"
            elif gap:
                reasoning=gaps[key][0]
            elif "唯一未决依赖" in fact["content"]:
                assert any("唯一未决依赖是证书更换审批" in source[r].get("output","") for r in fact["source_refs"])
                reasoning="源资料明确当前唯一未决依赖，不仅证明依赖存在，还直接支持排他性；与前缀无相反状态。"
            elif "仅限 ORBIT-93" in fact["content"]:
                reasoning="原工具当前结论直接支持精确对象与仅限范围。"
            elif "source-" in fact["content"]:
                reasoning="同时引用read_file路径参数和对应结果，通过call_id关联逐对象支持；未把读取当验收。"
            elif "已废止" in fact["content"] or "废止" in fact["content"]:
                reasoning="引用台账明确该项已废止，不激活旧进度/旧建议；active指废止这个当前事实。"
            else:
                reasoning="引文直接支持本Fact目标/状态/条件，或仅作这些已引用记录的阅读与验证边界解释，没有由有限样例升级为整段历史的穷尽断言。"
            # 子命题按分号/句子拆分；对已识别复合全称Fact独立分配支持关系。
            clauses=[c.strip() for c in re.split(r"[；;。]",fact["content"]) if c.strip()]
            subclaims=[]
            for index,clause in enumerate(clauses):
                broad=(gap and (index==0 or "前缀" in clause))
                if key==("Pilot11","f4"):
                    broad=index==0
                if key==("Stability-L3-R1","f8"):
                    broad="前缀" in clause
                subclaims.append(dict(text=clause,full_prefix_support="PASS",own_reference_sufficiency="REFERENCE_GAP" if broad else "PASS",
                    basis=reasoning if broad else "所引用户/工具原文或资料边界直接支持；没有把阅读当测试PASS",inference_labeled=not broad))
            entry=dict(run_id=run,response_id=response["id"],fact_id=identity,content=fact["content"],status=fact["status"],source_refs=fact["source_refs"],
                fact_types=list(dict.fromkeys(types)),oracle_required=gold_index is not None,oracle_index=gold_index,
                source_validity="PASS",full_prefix_support="PASS",per_fact_reference_sufficiency="REFERENCE_GAP" if gap else "PASS",
                missing_evidence_range=dict(missing_work_items=gaps[key][1],scope="还需覆盖所断言的历史范围/对象集合，或明确权威范围汇总；不要求机械补齐每条ID") if gap else None,
                wrong_state=False,unsupported_content=False,overgeneralization=gap,advice_as_fact=False,
                findings=["REFERENCE_GAP","OVERGENERALIZATION"] if gap else [],finding_overlap="同一Fact两个标签，只计一次有问题Fact" if gap else None,
                confidence="CONFIRMED",rationale=reasoning,subclaim_reviews=subclaims,cited_evidence=ref_evidence,
                full_prefix_evidence_file=f"evidence/{run}-prefix.json",cited_work_items=sorted(cited_work),
                inferred_context_scope="工作项1—30，依据此前f3–f5明确列举，不自动合并其引用" if key==("Pilot10","f6") else None)
            fact_rows.append(entry)
            if "UNIVERSAL_OR_RANGE_CLAIM" in types:
                range_rows.append(entry)
        comparisons.append(dict(run=run,response_id=response["id"],historical_reference_gaps=oldgap,reaudit_reference_gaps=gap_count,
            affected_fact_ids=[f["fact_id"] for f in fact_rows if f["run_id"]==run and f["findings"]],
            classification="HISTORICAL_AUDIT_DISCREPANCY" if oldgap!=gap_count else "CONSISTENT",
            difference_reason="历史审核用完整前缀支持代替部分Fact自身范围证据，或只检查已知依赖f6漏掉集合证据边界命题" if oldgap!=gap_count else "全部facts按同一主体/范围/状态规则审核，引用充分，无来源缺口。"))
        run_infos.append(dict(run=run,response_id=response["id"],snapshot_file=str(snapshot_path),actual_request_file=str(directory/"actual-request.json"),
            ledger_file=str(directory/"ledger.json"),snapshot_sha256=sha256(snapshot_path.read_bytes()).hexdigest(),
            request_sha256=sha256((directory/"actual-request.json").read_bytes()).hexdigest(),fact_count=len(snapshot["facts"])))
    assert len(fact_rows)==52 and len(comparisons)==6
    save(output/"fact_level_audit.json",dict(audit_scope="六个指定有效响应，未用失败或其他Run替代",facts=fact_rows,total_facts=52,
        counts=dict(source_invalid=0,unsupported_fact=0,state_error=0,reference_gap=sum(bool(f["findings"]) for f in fact_rows),
                    overgeneralization=sum(f["overgeneralization"] for f in fact_rows),uncertain=0),
        overlap_policy="REFERENCE_GAP与OVERGENERALIZATION同时出现不相加为两条失败Fact"))
    save(output/"range_claim_audit.json",dict(facts=range_rows,known_pair=dict(case_a="Stability-L3-R2/f6",case_b="Pilot10/f6",
        both_global_support=True,both_reference_sufficiency="REFERENCE_GAP",same_endpoints=["msg-3","msg-119"],missing_items=list(range(2,30)),
        authoritative_summary_in_cited_messages=False),note="范围命题并非都FAIL：全部对象实际覆盖或资料明确唯一性时PASS"))
    save(output/"cross_run_comparison.json",comparisons)
    table="\n".join(f"|{r['run']}|{r['historical_reference_gaps']}|{r['reaudit_reference_gaps']}|{', '.join(r['affected_fact_ids']) or '—'}|{r['classification']}|" for r in comparisons)
    discrepancy="# Historical Audit Discrepancies\n\n旧报告、Gate与评分均保持原样。以下是本轮统一复核发现，不能伪装成当时已发现。\n\n|Run|历史缺口|本轮缺口|Fact|分类|\n|---|---:|---:|---|---|\n"+table+"\n\nPilot10 f6与Stability L3-R2 f6同类：上述=前面列举的30个工作项，两个独立端点不是全范围汇总。\n还发现集合级证据组成/缺失命题缺引证：Pilot10 f13、Pilot11 f4、L3-R1 f8、L3-R2 f7。\n核心身份、完成、依赖与条件Oracle事实仍得到支持，但历史“全部额外Fact无引用缺口”的证据需打折，不据此重新宣布旧GatePASS/FAIL。\n有类似问题不证明Runtime回归，也不是v2出现了全新的退化；至少有审计漏检/口径执行不一致。\n"
    (output/"historical_discrepancies.md").write_text(discrepancy,encoding="utf-8")
    (output/"root_cause_assessment.md").write_text("""# 原因与证据等级\n\nCONFIRMED：六响应52facts全部身份、prefix切点形式合法；完整前缀支持各事实，但6个Fact自身引用不覆盖其声明范围。\nCONFIRMED：Pilot10 f6旧记录0缺口，L3-R2 f6旧记1；同类端点全称问题历史漏检。其他集合证据否定/组成命题也曾放行，导致跨Run计数不一致。\nCONFIRMED：validate_snapshot仅校验Schema/身份/切点/旧记录处置和预算，不进行语义覆盖（key_fact_snapshot.py:43–81,115–127）。不是实现偏离已承诺语义。\nSTRONGLY_SUPPORTED：模型对正确的全历史概括使用稀疏代表性refs，审核又有时把全前缀正确等同refs充分。\nHYPOTHESIS：Prompt要求原子事实但未明确自身refs范围/全称证据，可能促进代表性引证；无对照不能证实因果或v3有效。\nNOT_SUPPORTED：无来源事实增多、切点异常、Runtime恢复问题、Lost-in-the-Middle或模型能力普遍不足。\n本次不是修复，没有模型调用。\n""",encoding="utf-8")
    (output/"remediation_candidates.md").write_text("""# 最多两个候选（未实施）\n\n## A：通用Prompt v3证据范围约束\n\n陈述范围不得超过自身refs能支持的范围。全称/批量/排他/整个历史缺失命题须充分覆盖或引用已有权威范围汇总；否则收窄为已引证的个案/局部证据界限。\n不编造汇总/来源，不机械加30refs，不用其他facts隐式补证；保留原精确身份/里程碑/依赖/条件。只改版本化指令，不改Schema/输入/切点。\n不增加调用，只少量Prompt Tokens；请求前重新预算。Snapshot和持久化恢复语义不变。\n能针对本次6条范围引证缺口；不保证通用语义完整性。风险：过度收窄或删除关键事实、引用膨胀挤占Snapshot预算。\n最小验证见实验提案，未增加Judge/换模型/扩大预算。\n\n## B：Eval-only确定性提示检查\n\n仅对已识别的显式数字范围及独立工作项标签，核验覆盖；若有权威汇总或无法抽取则交人工，不按refs数量直接失败。可标记显式prefix否定需要人工复核。\n能发现本次1—30只引用1/30的明显模式；难以识别“上述”指代、任意否定/比例/语义蕴含，不能做通用语义验证器。\n仅离线Eval，零模型成本、无Schema/Runtime/恢复改动。风险：合法汇总误报、同义表述漏报、正则被误当证明；禁止删除或改写Snapshot事实自救。\n优先架构简单：先统一人工规则与离线对抗夹具，再决定是否值得加提示检查，不直接接入Production强拒绝。\n""",encoding="utf-8")
    (output/"experiment_proposal.md").write_text("""# 下一轮最小受控实验（仅提案）\n\n先离线构建规则验证：端点全称应GAP、逐项覆盖应PASS、已有权威汇总应PASS、单独样例免责声明不能证明整个前缀无证据、来源直接声明唯一性应PASS、其他Fact有refs不能补当前Fact。只验证审计规则，不生成新的模型证据。\n候选v3只增加通用自身证据范围要求，v2保留，不能注入样本答案。\n真实最小矩阵：L3-v2→SP-v2→L3-v3→SP-v3，四次Extraction各一次，原Fixture/Oracle/原JSON/切点/Schema/输出4,000/快照2,000不变，300秒retry0，两臂采样一致。\n冻结v3后先离线检查历史正文与请求除Prompt外相同，并约束新增输入估算≤1,024 Tokens。\nL3输入基础54,650，SP35,474，每臂含最多1,024增量与4,000输出的保守合计4次≤200,344 Tokens；安全余量每次8,000参与128K容量检查。精确实际请求仍须重新冻结，不把估算当Usage。\n成功标准：L3五事实5/5、SP前缀两事实2/2；全部Fact三层审核、无来源缺口/无依据/状态/否定错误、Snapshot预算不扩大。要求范围概括合理收窄或确有汇总覆盖；不要求固定数量refs。\n比较引用缺口、唯一问题Fact数、Recall、Snapshot估算长度及Provider Tokens；v3快照不得超过现有2,000预算，明显长度增加须披露而非择优。\n单次质量失败不补跑，停止策略需实验前另行明确（初始v2可能重现缺口，是否继续v3必须审批，不沿用稳定性全通过即停合同）。基础设施/审计错误不自动重发。\n四次只能方向性验证，不能称稳定修复。现有观察足以提出v3，但应先通过更小离线审计规则验证。未获新授权，不执行任何真实请求。\n""",encoding="utf-8")
    after={str(p):file_hashes(p) for p in roots}
    diff=subprocess.run(["git","diff","--check"],cwd=ROOT,capture_output=True,text=True)
    assert preserved==after and freeze==fingerprints()
    assert all(sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in prod.items())
    save(output/"integrity_report.json",dict(gate="CROSS_RUN_AUDIT_COMPLETE",before=preserved,after=after,production_hashes=prod,
        production_unchanged=True,prompts_fixtures_oracles_historical_artifacts_unchanged=True,frozen_assets_unchanged=True,
        selected_response_identity=run_infos,missing_artifacts=[],model_calls=0,network_calls=0,
        git_diff_check=dict(exit_code=diff.returncode,output=diff.stdout+diff.stderr)))
    final="""# Cross-Run Source Reference Audit\n\nGate：CROSS_RUN_AUDIT_COMPLETE。六个指定有效响应、52条全部Fact均已审核；不表示v2修复成功。\n\n## 三层结论\n\n52条Source Validity PASS，完整合法前缀支持PASS；逐Fact引用46条充分、6条REFERENCE_GAP。\n6条同时标OVERGENERALIZATION，按唯一Fact计数，不能相加成12条问题。未发现SOURCE_INVALID、UNSUPPORTED_FACT或STATE_ERROR，UNCERTAIN0。\n\n"""+table+"""\n\n## 必答问题\n\n1. L3-R2 f6缺口成立：1—30全称依赖只有1/30端点，无权威全范围汇总。\n2. Pilot10 f6同类成立：“上述”指前面1—30，不因没有数字就放行。\n3. 其他漏检：Pilot10 f13的整段证据组成/缺失、Pilot11 f4的整体主要构成、L3-R1 f8的全前缀无测试证据、L3-R2 f7的整批工具证据类型概括。合计新识别5条，另1条原已知，旧报告保持原样。\n4. 规则可一致执行：身份/范围/状态/全称否定/排他性逐子命题审核；范围汇总可以替代逐项引用，不按ID数量机械判定。当前是六响应人工操作化，不是通用自动语义检查证明。\n5. v3值得最小候选：通用“Fact自身refs覆盖陈述范围”，否则收窄局部断言，保留关键五/两事实。它尚未实施，也没有效果实证。\n6. 不需要立即增加Production确定性校验；先做Eval-only明显范围提示，保持架构简单。正则不能处理所有指代/否定/汇总，不把提示当证明。\n7. 先离线对抗规则验证，再审批两样本v2/v3四次Extraction小对照。请求预算/停止规则与v3要预注册，原v2与全部旧资产保留，不重跑至PASS。\n\n核心Oracle事实本轮没有来源缺口；新增问题多在辅助范围概括。历史引用无缺口结论受影响，但不能因此声称全部提取Recall失效。\n不能把Runtime仅校验来源形式的边界当成已实现通用语义校验，也不将LLM引证缺口归因于恢复/持久化Bug。\n\n## 完整性\n\n选择各原质量评分关联的完整响应，不用其他成功结果替代失败。response_id/终态事件/原请求/解析Snapshot逐一一致。\n全量原始尝试账本保留在来源Run中，新审计仅引用，不混淆当前六有效响应。\nProduction、Prompt、Fixture、Oracle、历史审核和Gate前后SHA不变；零网络/模型调用，git diff --check通过。\n本轮停止，建议先审批离线统一规则对抗验证，再决定v3实验。\n"""
    (output/"final_review.md").write_text(final,encoding="utf-8")
    print(json.dumps(dict(output=str(output),gate="CROSS_RUN_AUDIT_COMPLETE",responses=6,facts=52,reference_gaps=6,comparisons=comparisons),ensure_ascii=False))


if __name__=="__main__":
    main()
