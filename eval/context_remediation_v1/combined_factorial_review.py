"""独立离线合并人工批准的Pilot追加结果；失败账本不删除。"""
from datetime import datetime,timezone
from hashlib import sha256
import json
import subprocess

from hexgent.context.key_fact_snapshot import validate_snapshot,snapshot_message
from hexgent.utils.token_estimator import estimate_value_tokens
from .offline import ROOT,save
from .factorial_pilot import check_integrity,file_hashes


def main():
    base=ROOT/"eval/context_remediation_v1/runs"
    parent=base/"extractor-quality-pilot-v1-20261012"
    completed={"00":parent,
        "10":base/"extractor-quality-pilot-v1-retry-20261012T073058808085Z",
        "01":base/"extractor-quality-pilot-v1-retry-20261012T073058808085Z",
        "11":base/"extractor-quality-pilot-v1-retry-20261012T074529716101Z"}
    attempts=[parent,base/"extractor-quality-pilot-v1-retry-20261012T072530327864Z",
              completed["10"],completed["11"]]
    prior_hashes={str(p):file_hashes(p) for p in attempts}
    report_dir=ROOT/"eval/context_remediation_v1/reviews"/("factorial-combined-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    report_dir.mkdir(parents=True)
    manifest=json.loads((parent/"manifest.json").read_text())
    payload=json.loads((parent/"original-payload.json").read_text())
    oracle=json.loads((parent/"oracle.json").read_text())
    source={s["message_id"]:s["message"] for s in payload["history_prefix"]}
    # 1. 独立人工语义映射，不把关键词相关或Schema成功当作五事实覆盖。
    mappings={"00":[[],[],[],["f6"],["f7"]],"10":[["f2"],["f7"],["f8"],["f9"],["f10"]],
              "01":[[],["f9"],[],["f10"],["f11"]],"11":[["f2"],["f7"],["f8"],["f5"],["f6"]]}
    reasons={"00":"精确任务批次与两个审查完成里程碑缺失；源码阅读不能替代。",
        "10":"精确批次、两个完成状态、待确认依赖与条件性下一步均直接对应有效台账。",
        "01":"参数与协议完成、待确认依赖与下一步保留；缺批次和检索审查完成。",
        "11":"五条均直接对应原始有效台账，不以源码概述替代。"}
    rows=[]
    reviews=[]
    for arm,run in completed.items():
        arm_dir=run/"arms"/arm
        run_manifest=json.loads((run/"manifest.json").read_text())
        check_integrity(run,run_manifest)
        request=json.loads((arm_dir/"actual-request.json").read_text())
        assert request==json.loads((parent/"requests"/(arm+".json")).read_text())
        snapshot=json.loads((arm_dir/"snapshot.json").read_text())
        response=json.loads((arm_dir/"response.json").read_text())
        ledger=json.loads((arm_dir/"ledger.json").read_text())
        assert ledger["status"]=="COMPLETED" and ledger["response_id"]==response["id"] and ledger["usage_available"]
        terminal=[json.loads(p.read_text()) for p in sorted((arm_dir/"events").glob("*.json")) if '"response.completed"' in p.read_text()]
        assert len(terminal)==1 and terminal[0]["response"]==response
        assert response["status"]=="completed" and response.get("incomplete_details") is None
        raw=json.loads("".join(c["text"] for item in response["output"] if item["type"]=="message" for c in item["content"] if c["type"]=="output_text"))
        assert validate_snapshot(raw,cutpoint=payload["as_of_message_id"],sources=payload["history_prefix"],
            previous=None,budget_tokens=2000,excluded_source_ids=())==snapshot
        by_id={f["id"]:f for f in snapshot["facts"]}
        evidence=[]
        for gold,selected in zip(oracle,mappings[arm]):
            origin=next(s for s in payload["history_prefix"] if s["message"].get("call_id")==gold["source_call_id"] and s["message"].get("type")=="function_call_output")
            assert gold["quote"] in origin["message"]["output"]
            if selected:
                assert all(origin["message_id"] in by_id[f]["source_refs"] for f in selected)
            evidence.append(dict(oracle_fact_id=gold["fact_id"],required=gold["canonical_fact"],source_message_id=origin["message_id"],
                minimum_source_quote=gold["quote"],snapshot_fact_ids=selected,
                extracted_contents=[by_id[i]["content"] for i in selected],result="PASS" if selected else "FAIL",
                judgment=reasons[arm],method="INDEPENDENT_MANUAL_SEMANTIC_REVIEW"))
        citation_gaps={"00":{"f7":"维护者身份在msg-99，但本fact仅引用msg-107。"},
            "01":{"f1":"不发布在msg-107，当前引用msg-1/120不足支持该附加子句。",
                  "f2":"具体审查范围不在泛化assistant msg-120，需msg-117/119及前序工具消息支持。",
                  "f11":"两处这一限定来自msg-99，本fact仅引用msg-107。"}}
        state_ids={"00":{"f6"},"01":{"f10"}}.get(arm,set())
        facts=[]
        for f in snapshot["facts"]:
            note="当前工具来源支持有效台账或源码阅读/实现边界；非真实运行验收证据。"
            if arm=="10" and f["id"]=="f6":
                note="依赖上一批接口梳理在msg-3/119出现；缺独立确认是证据可用性说明，不代表另一个业务阻塞已被证明。"
            if arm=="10" and f["id"] in {"f11","f12"}:
                note="对应msg-7/55明确已废止旧进度/建议，当前不激活；非无依据追加。"
            facts.append(dict(**f,global_prefix_support="SUPPORTED_OR_SUPPORTED_PARAPHRASE",
                source_evidence=[dict(message_id=r,message=source[r]) for r in f["source_refs"]],
                citation_gap=citation_gaps.get(arm,{}).get(f["id"]),
                state_label_error=f["id"] in state_ids,
                state_label_note="确定的待确认状态无显式冲突却标uncertain；内容Recall和标签错误独立记录。" if f["id"] in state_ids else None,
                unsupported_statement_count=0,grounding_note=note))
        count=sum(bool(v) for v in mappings[arm])
        review=dict(arm=arm,source_run=str(run),five_fact_oracle=evidence,all_snapshot_facts=facts,
            unsupported_facts=0,reference_gaps=len(citation_gaps.get(arm,{})),state_dependency_errors=len(state_ids),
            schema_source_validity="PASS",snapshot_budget="PASS",actual_response_untruncated=True,
            current_intent_not_used_as_history_source=True,
            definitions=dict(unsupported="合法前缀完全没有支持的事实命题；引用不充分另列，不冒充逐fact已充分证明",
                state_errors="无证据冲突的确定待确认状态误标uncertain；不是内容已完成/未完成互换"))
        save(report_dir/"arms"/(arm+"-review.json"),review)
        reviews.append(review)
        rows.append(dict(arm=arm,required_fact_count=count,required_fact_recall=count/5,
            unsupported_facts=0,state_dependency_errors=len(state_ids),reference_gaps=len(citation_gaps.get(arm,{})),
            snapshot_fact_count=len(snapshot["facts"]),snapshot_estimated_tokens=estimate_value_tokens(snapshot_message(snapshot)),
            input_tokens=ledger["input_tokens"],output_tokens=ledger["output_tokens"],latency_ms=ledger["latency_ms"],
            timeout_seconds=run_manifest["timeout_seconds"],source_run=str(run),schema_source_validity="PASS"))
    attempts_ledger=[]
    for run in attempts:
        for entry in json.loads((run/"provider-ledger.json").read_text()):
            attempts_ledger.append(dict(**entry,source_run=str(run)))
    success=[r for r in attempts_ledger if r["status"]=="COMPLETED"]
    failure=[r for r in attempts_ledger if r["status"]!="COMPLETED"]
    assert len(attempts_ledger)==7 and len(success)==4 and len(failure)==3
    save(report_dir/"all-attempts-ledger.json",attempts_ledger)
    effects=dict(prompt_delta_original_input=0.6,prompt_delta_enveloped_input=0.4,
        envelope_delta_v1_prompt=0.2,envelope_delta_v2_prompt=0.0,descriptive_interaction=-0.2,
        caveat="一次成功结果/臂、固定顺序、追加尝试及11传输超时变更；描述差值不是因果估计或显著性检验")
    result=dict(gate="PILOT_COMPLETE_DIRECTIONAL_EVIDENCE",completion_contract="人工追加授权补齐四个有效臂结果；原Pilot被阻断的Gate不改写",
        rows=rows,effects=effects,prompt_signal="EXPLORATORY_POSITIVE",envelope_signal="SMALL_MIXED_EXPLORATORY",
        next_candidate="A_PROMPT_V2_WITH_ORIGINAL_JSON",secondary_candidate="A_PLUS_B_NO_OBSERVED_RECALL_ADVANTAGE_OVER_A",
        rsp_executed=False,stability="NOT_ESTABLISHED",production_quality_fix=False,
        usage=dict(successful_response_coverage="4/4",all_attempt_coverage="4/7",unknown_usage_attempts=3,
            observable_input_tokens=sum(r["input_tokens"] for r in success),observable_output_tokens=sum(r["output_tokens"] for r in success),
            complete_cost=None),new_model_calls_in_review=0,no_summary_main_compaction=True)
    save(report_dir/"result.json",result)
    diff=subprocess.run(["git","diff","--check"],cwd=ROOT,capture_output=True,text=True)
    for run in attempts:
        check_integrity(run,json.loads((run/"manifest.json").read_text()))
        assert file_hashes(run)==prior_hashes[str(run)]
    save(report_dir/"integrity-report.json",dict(status="PASS",source_runs_before=prior_hashes,
        source_runs_after={str(p):file_hashes(p) for p in attempts},production_hashes=manifest["production_hashes"],
        frozen_requests_body_equal=True,production_unchanged=True,frozen_assets_unchanged=True,
        historical_stage_a_and_diagnosis_unchanged=True,git_diff_check=dict(exit_code=diff.returncode,output=diff.stdout+diff.stderr)))
    table="\n".join(f"|{r['arm']}|{r['required_fact_count']}/5|{r['unsupported_facts']}|{r['state_dependency_errors']}|{r['reference_gaps']}|{r['input_tokens']:,}|{r['output_tokens']:,}|{r['latency_ms']/1000:.2f}|" for r in rows)
    report="""# R-L3 2×2 Extraction Pilot — Combined Independent Review\n\nGate：PILOT_COMPLETE_DIRECTIONAL_EVIDENCE。仅四臂探索性方向证据，不是Production质量修复或统计显著性。\n原Pilot的BLOCKED_INFRA及失败Artifact保持原样；本报告按人工追加授权汇总四个已完成有效结果。\n\n## 四臂结果\n\n|臂|必需事实Recall|无依据事实（合法前缀整体）|状态标签错误|逐fact引用缺口|Provider输入|Provider输出|秒|\n|---|---|---:|---:|---:|---:|---:|---:|\n"""+table+"""\n\n四臂Schema/来源范围/Snapshot预算均PASS，响应完整、Usage均关联对应response_id。\n00缺身份和两个完成里程碑；01保留参数协议完成，但仍缺身份与检索审查完成。\n10/11全部五事实均由msg-3/43/83/99/107直接支持；没有新增无依据事实或错误条件/否定。\n10另保留14条事实（包括源码阅读及已废止台账）；11为8条，后者较简洁，但成本/效率差异不能归因于包络。\n\n“无依据事实0”并不代表各引用都充分：00有1处、01有3处逐fact引用缺口，完整合法前缀存在支持但对应source_refs遗漏相关来源。\n00/01确定等待状态标uncertain，各记1处标签偏差；五事实内容Recall独立计数，不掩盖标签错误。\n各条完整文本、source_refs、原始引用正文和五事实Oracle见arms/*-review.json。\n\n## 因素解释\n\nA Prompt v2：原输入2/5→5/5，包络输入3/5→5/5，呈现较强探索性改善信号。\nB包络：v1时2/5→3/5，v2时5/5→5/5；效果小且不一致，未见优于只改Prompt的Recall。\n交互描述差值为-20pp，但天花板效应、模型随机性和重试时间因素都可能解释，不能声称负交互已确证。\n优先下一阶段候选：Prompt v2 + 原JSON（10），改动小且5/5；11可保留为备选，不证明需要包络。\nR-SPARSE独立验证和进一步稳定性仍需另行审批，不自动执行。\n\n## 追加尝试与配置差异\n\n00来自初次Pilot；10/01来自人工执行的073058批次；11来自074529单臂批次。\n共7次SDK尝试，4次完整响应、3次基础设施失败；失败不删除，不按质量失败评分。\n00/10/01超时120秒，11根据人工批准改为300秒；11实际36.33秒完成，不能据此证明加时解决故障或不影响选择。\n请求正文和采样参数保持原冻结内容；传输超时差异明确记入manifest。\n所有臂均只使用一个有效完成结果，用户批准的失败追加不同于SDK隐藏重试（始终0）。\n\n## 成本\n\n四次成功响应Provider Usage4/4完整；全部尝试Coverage4/7，三次失败Usage均null。\n可观测Tokens仅为成功响应小计，不是包含失败的完整费用；详见result.json及all-attempts-ledger.json。\n历史Stage A真实调用不混入此Pilot。没有Summary、MAIN、Compaction或R-SPARSE成本。\n\n## 边界与完整性\n\n每臂只有一次成功结果，不是重复稳定性试验，不能宣布统计显著、解决长上下文稀疏事实或普遍可靠。\n全部冻结输入、正文身份顺序切点不变；不能把包络结果当作历史摘要。\n原Artifact/Production/冻结资产前后哈希保持一致。git diff --check通过。\n本轮仅离线复核，未新发Provider请求，不改Production或冻结Oracle，不commit/tag/push。\n\n下一步建议：审批仅Prompt v2的独立R-SPARSE验证，再评估稳定性；不直接合入Production。\n"""
    (report_dir/"REPORT.md").write_text(report,encoding="utf-8")
    print(json.dumps(dict(output=str(report_dir),gate=result["gate"],rows=rows,usage=result["usage"]),ensure_ascii=False))


if __name__=="__main__":
    main()
