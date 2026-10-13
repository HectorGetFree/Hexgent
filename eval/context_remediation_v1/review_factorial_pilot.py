"""保存已停止Pilot的独立复核；不请求模型，不覆盖原始尝试。"""
import json
from pathlib import Path
import subprocess

from hexgent.utils.token_estimator import estimate_value_tokens
from hexgent.context.key_fact_snapshot import snapshot_message
from .offline import ROOT,save
from .factorial_pilot import check_integrity,file_hashes


def main():
    output=ROOT/"eval/context_remediation_v1/runs/extractor-quality-pilot-v1-20261012"
    manifest=json.loads((output/"manifest.json").read_text())
    check_integrity(output,manifest)
    execution=json.loads((output/"execution-result.json").read_text())
    assert execution["status"]=="PILOT_BLOCKED_INFRA" and execution["real_calls"]==2
    ledger=json.loads((output/"provider-ledger.json").read_text())
    payload=json.loads((output/"original-payload.json").read_text())
    oracle=json.loads((output/"oracle.json").read_text())
    snapshot=json.loads((output/"arms/00/snapshot.json").read_text())
    source={s["message_id"]:s["message"] for s in payload["history_prefix"]}
    judgments=[
        dict(oracle_fact_id=oracle[0]["fact_id"],result="FAIL",snapshot_ids=[],reason="f1描述审查范围，未包含当前交接批次AUDIT-MAPLE-57。",source_id="msg-3"),
        dict(oracle_fact_id=oracle[1]["fact_id"],result="FAIL",snapshot_ids=[],reason="f3正文阅读不能替代参数与协议审查已完成。",source_id="msg-43"),
        dict(oracle_fact_id=oracle[2]["fact_id"],result="FAIL",snapshot_ids=[],reason="f4正文阅读及f5测试免责声明不能替代失败恢复后的检索审查完成。",source_id="msg-83"),
        dict(oracle_fact_id=oracle[3]["fact_id"],result="PASS",snapshot_ids=["f6"],reason="等待维护者确认两处契约的内容保留；uncertain标签另记语义状态错误，不隐藏。",source_id="msg-99"),
        dict(oracle_fact_id=oracle[4]["fact_id"],result="PASS",snapshot_ids=["f7"],reason="保留确认后生成报告且不发布；维护者主体由另一来源支持，逐条引证不足另记。",source_id="msg-107"),
    ]
    for gold,j in zip(oracle,judgments):
        assert gold["quote"] in source[j["source_id"]]["output"]
        j.update(canonical_fact=gold["canonical_fact"],quote=gold["quote"],source_message=source[j["source_id"]])
    save(output/"arms/00/five-fact-oracle-review.json",judgments)
    notes={
        "f1":("SUPPORTED_PARAPHRASE","msg-1及117支持只读审查与当前源码范围；遗漏批次。"),
        "f2":("SUPPORTED_SOURCE_READING","引用工具输出有对应冻结文件和阅读说明，不代表实际测试或审查验收。"),
        "f3":("SUPPORTED_SOURCE_READING","引用文件、阅读结果支持该概述，不包含指定里程碑。"),
        "f4":("SUPPORTED_SOURCE_READING","引用文件、阅读结果支持该概述，不等于检索审查完成。"),
        "f5":("SUPPORTED_SCOPE_CAVEAT","冻结源码阅读不是本轮执行测试，资料的反复免责声明支持该边界。"),
        "f6":("CONTENT_SUPPORTED_STATUS_ERROR","等待确认确有证据且未发现冲突/未知取值；来源明确当前有效，但被标为uncertain。"),
        "f7":("GLOBALLY_SUPPORTED_WITH_REFERENCE_GAP","msg-107支持确认后报告与不发布；维护者主体在msg-99而未纳入本fact引用。只读范围支持不进行业务变更的释义，不计为新无依据建议。"),
    }
    facts=[]
    for f in snapshot["facts"]:
        kind,reason=notes[f["id"]]
        facts.append(dict(**f,classification=kind,rationale=reason,
            cited_sources=[dict(message_id=r,message=source[r]) for r in f["source_refs"]],
            unsupported_claim_count=0,state_label_error_count=1 if f["id"]=="f6" else 0,
            reference_gap_count=1 if f["id"]=="f7" else 0,
            supplemental_support=[dict(message_id="msg-99",message=source["msg-99"])] if f["id"]=="f7" else []))
    save(output/"arms/00/semantic-review.json",dict(method="INDEPENDENT_MANUAL_EVIDENCE_REVIEW_NO_MODEL_JUDGE",
        facts=facts,unsupported_facts=0,state_dependency_errors=1,reference_gaps=1,
        definitions=dict(unsupported="整个合法前缀均不能支持的事实命题；逐fact引用缺口单列，不能混作完全有据",
            state_dependency_errors="包括无冲突的确定待确认状态被误标uncertain，不等同已完成/未完成互换",
            recall="内容覆盖且来源支持；Recall与status准确性独立统计")))
    rows=[]
    for arm in manifest["order"]:
        record=next((r for r in ledger if r["arm"]==arm),None)
        rows.append(dict(arm=arm,status="COMPLETED_QUALITY_FAIL" if arm=="00" else "PROVIDER_CONNECTION_FAILURE" if arm=="10" else "NOT_RUN",
            required_fact_recall=0.4 if arm=="00" else None,required_fact_count=2 if arm=="00" else None,
            unsupported_facts=0 if arm=="00" else None,state_dependency_errors=1 if arm=="00" else None,
            reference_gaps=1 if arm=="00" else None,schema_source_validity="PASS" if arm=="00" else "NOT_EVALUABLE" if arm=="10" else "NOT_RUN",
            input_tokens=record["input_tokens"] if record else None,output_tokens=record["output_tokens"] if record else None,
            latency_ms=record["latency_ms"] if record else None,usage_available=record["usage_available"] if record else None))
    result=dict(gate="PILOT_BLOCKED_INFRA",experiment=manifest["experiment"],rows=rows,
        sdk_invocation_attempts=2,completed_provider_responses=1,failed_invocation_attempts=1,hidden_retries=0,
        usage_coverage=dict(complete=1,attempted=2,unknown_cost_attempts=1),
        observable_token_subtotal=dict(input_tokens=47963,output_tokens=1215,total_tokens=49178),
        complete_pilot_token_cost=None,unknown_cost_reason="10无终态Usage；不知道Provider是否收到/计费，不能填零或按输出估算补齐",
        prompt_v2_signal="NOT_EVALUABLE",envelope_v2_signal="NOT_EVALUABLE",interaction="NOT_EVALUABLE",
        rsp_candidate="NONE_SELECTED_INCOMPLETE_FACTORIAL",stability="NOT_EVALUABLE_REQUIRES_NEW_APPROVAL",
        stopped_reason="10 SDK连接异常，无HTTP响应和Usage；无法完成预注册可对账矩阵，保守停止，不据此证明Provider系统性宕机",
        no_summary_main_compaction=True,no_rsp=True,stage_a_failure_preserved=True,
        next="人工决定是否批准独立新Pilot；不能自动重发10或执行01/11，不选获胜方案")
    save(output/"pilot-review.json",result)
    diff=subprocess.run(["git","diff","--check"],cwd=ROOT,capture_output=True,text=True)
    check_integrity(output,manifest)
    save(output/"integrity-report.json",dict(status="PASS" if diff.returncode==0 else "FAIL",
        production_hashes=manifest["production_hashes"],production_unchanged=True,frozen_assets_unchanged=True,
        prior_stage_a_unchanged=True,diagnosis_unchanged=True,exact_requests_unchanged=True,
        git_diff_check=dict(exit_code=diff.returncode,output=diff.stdout+diff.stderr),
        raw_evidence_hashes_file="raw-evidence-hashes.json"))
    report="""# EXTRACTOR-QUALITY-PILOT-V1 — Partial Review\n\nGate：PILOT_BLOCKED_INFRA。四臂尚未完成，不得宣布因素改善或无改善。\n\n## 执行前Gate\n\nProduction与冻结资产一致，v2不含Oracle实体/答案。\n包络仅统一增加k来源类型及envelope_v2边界元数据，不变更任何历史正文/ID/顺序/切点/只读意图，120消息可逆恢复。\n输入估算00=54,345、10=54,650、01=54,826、11=55,131；增量0/305/481/786均不超1,024。\n输出每次4,000，四臂冻结总估算上界234,952，不超过授权237,476。安全余量每请求8,000仍参与检查。\nSampling temperature/top_p/seed均未设置，使用一致Provider默认；不声称固定随机状态。\n\n## 原始结果\n\n|指标|00|10|01|11|\n|---|---:|---|---|---|\n|执行|完成|APIConnectionError|NOT_RUN|NOT_RUN|\n|Required Fact Recall|2/5|NOT_EVALUABLE|NOT_RUN|NOT_RUN|\n|Unsupported Facts（合法前缀整体支持）|0|NOT_EVALUABLE|NOT_RUN|NOT_RUN|\n|逐fact引用支持缺口|1|NOT_EVALUABLE|NOT_RUN|NOT_RUN|\n|State/Dependency Errors|1（uncertain标签偏差）|NOT_EVALUABLE|NOT_RUN|NOT_RUN|\n|Schema / Source / Snapshot预算|PASS|NOT_EVALUABLE|NOT_RUN|NOT_RUN|\n|Provider Input Tokens|47,963|null|null|null|\n|Provider Output Tokens|1,215|null|null|null|\n|Latency ms|50,662.23|96,327.49|null|null|\n\n00再次遗漏精确交接批次和两个审查完成里程碑；不是源码阅读/架构功能能替代的完成状态。\n00的f6保留等待确认内容，但把确定等待状态标为uncertain；Recall内容与状态标签独立报告。\nf7的维护者主体由msg-99支持，引用仅msg-107，不能把合法引用直接视为充分逐条来源支持；该缺口单列，不包装成完全无误。\n\n## Provider故障与停止\n\n00完整response_id关联Usage；10在SDK调用约96.33秒后抛APIConnectionError，message='Connection error.'，没有HTTP status/request_id/response终态。\n没有充分证据确认是timeout、容量、认证、policy、过载或Provider系统性故障，不能猜测。\n因该臂Usage/成本及完整响应不可得，停止01/11；不是因质量失败而停止。不重试、不更换路由或参数。\n2次SDK请求尝试、1次完整响应；第二次是否被Provider收到/计费UNKNOWN。\n可观测Token小计49,178（47,963+1,215）只覆盖00，不是完整Pilot成本；10 Usage保持null。\n\n## 因素解释与下一步\n\nPrompt v2改善信号：无法评价。输入包络改善信号：无法评价。交互：无法评价。\n没有候选可按四臂结果选入R-SPARSE；不能因原00重复2/5就说v2没有改善。\n四臂各一次原本也仅是探索性证据，不支持稳定性或显著性。现在连四臂方向性对比也未完成。\n若用户希望继续，需要批准新独立Run或明确剩余臂的异常恢复合同；本轮不自动重发10、不补执行01/11。\n\n## 完整性\n\nProduction、历史Stage A、诊断结果及冻结资产哈希未变化。git diff --check通过。\n所有四臂冻结请求、Prompt Diff、输入差异、隔离证据、00响应/Snapshot/Oracle、10失败账本均保留。\n01/11只有冻结请求，没有真实响应，不构造Snapshot，不计算质量分数。\n零Summary、MAIN、完整Compaction、R-SPARSE、统一Benchmark；无Production改动或commit/tag/push。\n"""
    (output/"PILOT_REVIEW.md").write_text(report,encoding="utf-8")
    evidence_hashes=file_hashes(output/"arms")
    save(output/"raw-evidence-hashes.json",evidence_hashes)
    print(json.dumps(dict(output=str(output),gate=result["gate"],real_attempts=2,completed=1),ensure_ascii=False))


if __name__=="__main__":
    main()
