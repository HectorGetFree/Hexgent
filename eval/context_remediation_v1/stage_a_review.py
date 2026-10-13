"""仅离线复核已完成 R-L3；保留原始结果，绝不再次调用 Provider。"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import subprocess
from pathlib import Path

from hexgent.context.key_fact_snapshot import validate_snapshot
from .offline import ROOT, save, fingerprints


def main():
    run = ROOT/"eval/context_remediation_v1/runs/stage-a-real-20261012-rl3"
    output = run/("review-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir()
    original_hashes = {str(p.relative_to(run)):sha256(p.read_bytes()).hexdigest()
        for p in run.rglob("*") if p.is_file() and output not in p.parents}
    manifest = json.loads((run/"frozen-manifest.json").read_text())
    request = json.loads((run/"extractor-request.json").read_text())
    payload = json.loads(request["input"][0]["content"])
    snapshot = json.loads((run/"snapshot.json").read_text())
    config = json.loads((run/"config.json").read_text())
    oracle = json.loads((run/"oracle.json").read_text())
    ledger = json.loads((run/"provider-ledger.json").read_text())
    sources = payload["history_prefix"]
    source_map = {s["message_id"]:s["message"] for s in sources}
    # 1. 重新验证真实输出的结构、来源身份、切点与预算；不据此推断语义保留。
    raw = {k:snapshot[k] for k in ("as_of_message_id", "facts", "dispositions")}
    revalidated = validate_snapshot(raw, cutpoint=payload["as_of_message_id"], sources=sources,
        previous=None, budget_tokens=config["key_fact_snapshot_tokens"], excluded_source_ids=())
    assert revalidated == snapshot
    assert request == json.loads((run/"candidate-requests.json").read_text())["extraction"]
    assert len(ledger)==1 and ledger[0]["status"]=="COMPLETED" and ledger[0]["retry_count"]==0
    assert ledger[0]["usage_available"]
    required_reviews = [
        dict(fact_id=oracle[0]["fact_id"], pass_=False, snapshot_fact_ids=[], source_id="msg-3",
             reason="Snapshot 没有 AUDIT-MAPLE-57；f1 的泛化源码审查任务不是指定交接批次。"),
        dict(fact_id=oracle[1]["fact_id"], pass_=False, snapshot_fact_ids=[], source_id="msg-43",
             reason="f2 只记载正文阅读完成；f3 的参数/协议实现描述不等于参数与协议审查已完成。"),
        dict(fact_id=oracle[2]["fact_id"], pass_=False, snapshot_fact_ids=[], source_id="msg-83",
             reason="Snapshot 未记载失败恢复后的检索审查已完成；f6 的运行验证警告不能替代该台账状态。"),
        dict(fact_id=oracle[3]["fact_id"], pass_=True, snapshot_fact_ids=["f7"], source_id="msg-99",
             reason="保留两处权限契约尚待维护者确认的依赖；status=uncertain 需单独保留，不宣称完成或已确认。"),
        dict(fact_id=oracle[4]["fact_id"], pass_=True, snapshot_fact_ids=["f8"], source_id="msg-107",
             reason="保留维护者确认后生成交接报告的前置条件及不执行发布。"),
    ]
    for review, gold in zip(required_reviews, oracle):
        source = source_map[review["source_id"]]
        assert source.get("call_id")==gold["source_call_id"]
        assert gold["quote"] in source["output"]
        review.update(expected=gold["canonical_fact"], exact_source_quote=gold["quote"],
                      expected_channel=gold["expected_channel"], source_in_prefix=True,
                      result="PASS" if review.pop("pass_") else "FAIL")
    # 2. 模型生成的额外事实分别复核，来源合法与来源语义支持分开记录。
    support = [
        ("f1", "SUPPORTED_GENERALIZATION", "msg-1 支持只读审查目标；不包含关键任务实体。"),
        ("f2", "SUPPORTED_GENERALIZATION", "msg-3 至 msg-119 的30条工具输出均记录对应工作项正文阅读；不得把阅读等同审查验收。"),
        ("f3", "SUPPORTED_STATIC_SOURCE_SUMMARY", "引用的 CLI/Web/Integration 冻结源码支持接口和控制流程描述；非执行成功证据。"),
        ("f4", "SUPPORTED_STATIC_SOURCE_SUMMARY", "msg-67/83 中 RunManager、RunRequest、单Run锁和共享状态实现支持该描述；非本轮运行验证。"),
        ("f5", "SUPPORTED_STATIC_SOURCE_SUMMARY", "msg-91/103/107/111 支持配置、注入预算、同文Memory去重、轮次压缩和Checkpoint恢复实现描述。"),
        ("f6", "SUPPORTED_EXACT_SCOPE", "引用工具输出明确限定冻结源码阅读，不构成实际测试通过证据。"),
        ("f7", "PARTIALLY_SUPPORTED", "msg-99 台账支持等待维护者确认；不能将额外的发布/结论建议升级为该单一来源已经证明的状态。"),
        ("f8", "SUPPORTED_PARAPHRASE", "msg-107 台账支持确认权限契约后生成交接报告、不执行发布。"),
    ]
    reviews=[]
    for identity, status, reason in support:
        fact=next(f for f in snapshot["facts"] if f["id"]==identity)
        reviews.append(dict(**fact, support_classification=status, rationale=reason,
            refs_legal=True, sources=[dict(message_id=ref, message=source_map[ref]) for ref in fact["source_refs"]]))
    save(output/"required-fact-review.json", required_reviews)
    save(output/"snapshot-support-review.json", reviews)
    current=fingerprints()
    production_ok=all(sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in manifest["production_hashes"].items())
    frozen_ok=current==json.loads((run/"frozen-before.json").read_text())
    original_ok=all(sha256((run/p).read_bytes()).hexdigest()==h for p,h in original_hashes.items())
    diff=subprocess.run(["git","diff","--check"],cwd=ROOT,capture_output=True,text=True)
    result=dict(gate="STAGE_A_FAIL_QUALITY", required_fact_recall=dict(numerator=2,denominator=5,value=0.4),
        reason="三条必需事实未进入 Snapshot；根据预注册停止条件不执行 Summary 或提交。",
        real_auxiliary_calls=1,extractor_calls=1,summary_calls=0,real_main_calls=0,new_paid_calls_in_review=0,
        capacity="USER_ASSERTED_CAPACITY_128K_UNVERIFIED", sdk_hidden_retries=0,
        provider_usage=dict(input_tokens=ledger[0]["input_tokens"],output_tokens=ledger[0]["output_tokens"],
                            total_tokens=ledger[0]["input_tokens"]+ledger[0]["output_tokens"],completeness="COMPLETE_1_OF_1"),
        extractor_latency_ms=ledger[0]["latency_ms"], semantic_review="INDEPENDENT_MANUAL_REVIEW_NO_MODEL_JUDGE",
        snapshot_structure_source_budget="PASS", frozen_request_equality="PASS",
        summary="NOT_RUN_STOPPED_BY_QUALITY_GATE", commit="NOT_RUN",fake_main="NOT_RUN",
        checkpoint_restore_continue="NOT_RUN", production_unchanged=production_ok,frozen_unchanged=frozen_ok,
        original_artifacts_unchanged=original_ok,git_diff_check="PASS" if diff.returncode==0 else "FAIL",
        original_run=str(run),next="等待人工审核；不自动修复或重跑。")
    if not all((production_ok,frozen_ok,original_ok,diff.returncode==0)):
        result["gate"]="STAGE_A_BLOCKED_SAFETY_OR_AUDIT"
    save(output/"result.json",result)
    save(output/"original-artifact-hashes.json",original_hashes)
    save(output/"production-hashes.json",manifest["production_hashes"])
    save(output/"oracle-matrix.json",[
        dict(oracle="EXTRACTION_REQUIRED_FACTS",status="FAIL",evidence="required-fact-review.json"),
        dict(oracle="SOURCE_ID_CUTPOINT_SNAPSHOT_BUDGET",status="PASS",evidence="重新执行 Production validate_snapshot"),
        dict(oracle="EXTRACTION_SEMANTIC_SUPPORT",status="PARTIAL",evidence="snapshot-support-review.json"),
        dict(oracle="PROVIDER_USAGE_ASSOCIATION",status="PASS",evidence="../provider-ledger.json"),
        *[dict(oracle=name,status="NOT_RUN",reason="Extraction 质量失败前置停止")
          for name in ("SUMMARY","SNAPSHOT_DELIVERY_TO_FAKE_MAIN","ATOMIC_COMMIT","CHECKPOINT_RESTORE_CONTINUE")]])
    report="""# R-L3 Stage A 独立质量复核\n\nGate：STAGE_A_FAIL_QUALITY。\n\n## 执行与停止\n\n沿用上次已完成的唯一真实 Extractor 请求，不重发、不追加任何真实请求。SDK 隐藏重试为0。\nProduction 结构、来源身份、切点和 Snapshot 预算检查通过，但语义五事实仅2/5保留。\n三条缺失事实都确实存在于被冻结的 Extraction 前缀，不是仅存在 Recent Tail 的事实。\n按停止合同，Summary、Compaction提交、Fake MAIN、Checkpoint及正式恢复均未执行，不能计为PASS。\n\n## 五事实\n\n|事实|结果|原因|\n|---|---|---|\n|AUDIT-MAPLE-57|FAIL|泛化任务描述遗漏交接批次|\n|参数与协议审查已完成|FAIL|源码阅读/实现描述不等于该完成状态|\n|失败恢复后的检索审查已完成|FAIL|未提取该完成事实|\n|维护者确认两处权限契约的待确认依赖|PASS|f7保留，但uncertain标签及额外建议单独披露|\n|确认后生成交接报告、不执行发布|PASS|f8保留前置条件与下一步|\n\n## 成本与风险\n\nProvider input=47,963，output=1,385，总计49,348 Tokens；Usage 1/1完整。Extractor耗时51,594.91ms。\n本地输入估算54,345与Provider Usage不同，不混用；本轮最大估算预算161,067未耗尽。\nSummary未执行，没有Summary实测成本。128K仍为USER_ASSERTED_CAPACITY_128K_UNVERIFIED，单次成功不证明128K容量。\n\n## 能力边界\n\nSnapshot含8条模型事实，另有部分概括性源码内容；有合法source_refs并不代表必需事实被保留。\nf7单一来源支持等待确认，但额外规范性建议不作为已经证明的事实状态。\n本次不能证明五事实保护、提交后交付或正式恢复有效，也不是端到端Agent行为测试。\n不据单样本判断模型普遍能力；不修改Prompt/Oracle/Fixture来追求PASS。\n\n## 完整性\n\nProduction与冻结资产哈希不变。原始result.json及所有原始Artifact保留，复核为独立新增目录。\ngit diff --check通过。本轮没有修改Production、测试或冻结Artifact，没有commit/tag/push。\n"""
    (output/"REPORT.md").write_text(report,encoding="utf-8")
    print(json.dumps(dict(output=str(output),**result),ensure_ascii=False))


if __name__=="__main__":
    main()
