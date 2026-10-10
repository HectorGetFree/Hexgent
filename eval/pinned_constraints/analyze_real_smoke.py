"""只读已有真实配对 Artifact，生成汇总及请求渠道 CSV，不调用模型。"""
import argparse
import csv
import json
from pathlib import Path
from statistics import mean

from eval.memory_m1 import dump


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",type=Path,required=True)
    parser.add_argument("--revision",default="",help="为修订报告增加后缀，不覆盖前一版")
    args=parser.parse_args()
    root=args.run.resolve()
    suffix="-"+args.revision if args.revision else ""
    report=json.loads((root/"report.json").read_text())
    manifest=json.loads((root/"manifest.json").read_text())
    summary=[]
    costs=[]
    requests=[]
    for case in manifest["cases"]:
        pairs=[r for r in report["pairs"] if r["case"]==case["id"]]
        valid_pairs=[r for r in pairs if len(r["sides"])==2 and all(all(s["gates"].values()) and (s["terminal"] or {}).get("status")=="completed" for s in r["sides"].values())]
        counts={name:sum(r["outcome"]==name for r in valid_pairs) for name in ("Baseline","Pinned","Tie")}
        counts["INVALID_PAIR"]=len(pairs)-len(valid_pairs)
        sides={}
        for variant in ("Baseline","Pinned"):
            runs=[r["sides"][variant] for r in pairs if variant in r["sides"]]
            sides[variant]=dict(executed=len(runs),constraint_pass=sum(r["behavior"]["constraint_behavior_pass"] for r in runs),
                task_output_pass=sum(r["behavior"]["task_output_pass"] for r in runs),
                terminal_completed=sum((r["terminal"] or {}).get("status")=="completed" for r in runs),
                usage_complete=sum(r["usage_completeness"]=="COMPLETE" for r in runs),provider_requests=sum(r["provider_requests"] for r in runs),
                audit_valid_runs=sum(all(r["gates"].values()) and (r["terminal"] or {}).get("status")=="completed" for r in runs),
                provider_input_tokens=sum(r["provider_input_tokens"] for r in runs) if runs and all(r["provider_input_tokens"] is not None for r in runs) else None,
                provider_output_tokens=sum(r["provider_output_tokens"] for r in runs) if runs and all(r["provider_output_tokens"] is not None for r in runs) else None,
                mean_agent_latency_ms=mean(r["latency_ms"] for r in runs) if runs else None)
        first_input_deltas=[]
        estimated_deltas=[]
        for pair in pairs:
            for variant,result in pair["sides"].items():
                costs.append(dict(case=case["id"],pair=pair["pair"],variant=variant,
                    constraint_pass=result["behavior"]["constraint_behavior_pass"],task_output_pass=result["behavior"]["task_output_pass"],
                    usage_completeness=result["usage_completeness"],provider_requests=result["provider_requests"],
                    input_tokens=result["provider_input_tokens"],output_tokens=result["provider_output_tokens"],latency_ms=result["latency_ms"],outcome=pair["outcome"]))
                for request in result["availability"]:
                    requests.append(dict(case=case["id"],pair=pair["pair"],variant=variant,request_index=request["request_index"],
                        effective_available=request["effective_constraint_available"],**request["channels"],
                        old_in_input=request["old_constraint_in_input"],stale_in_pinned=request["stale_constraint_in_pinned"]))
            if len(pair["sides"])==2:
                a=pair["sides"]["Baseline"]["provider_spans"][0].get("input_tokens")
                b=pair["sides"]["Pinned"]["provider_spans"][0].get("input_tokens")
                if a is not None and b is not None:
                    first_input_deltas.append(b-a)
                directory=root/case["id"]/f"pair-{pair['pair']}"/"Pinned"
                audit=json.loads((directory/"request-wrapper-audit.json").read_text())[0]
                original=audit["original"]
                from hexgent.utils.token_estimator import estimate_request_tokens
                estimated_deltas.append(audit["estimated_input_tokens"]-estimate_request_tokens(original["instructions"],original["input"],original.get("tools",[])))
        summary.append(dict(case=case["id"],planned_pairs=3,executed_pairs=len(pairs),paired_outcomes=counts,sides=sides,
            first_request_provider_input_delta_pinned_minus_baseline=first_input_deltas,
            first_request_estimated_input_delta=estimated_deltas))
    audit_complete=all(all(side["gates"].values()) for pair in report["pairs"] for side in pair["sides"].values())
    result=dict(status=report["status"],model=manifest["model"],cases=summary,completed_pairs=report["completed_pairs"],stop=report["stop"],
        executed_pairs=len(report["pairs"]),fully_completed_pairs=sum(len(r["sides"])==2 and all((s["terminal"] or {}).get("status")=="completed" for s in r["sides"].values()) for r in report["pairs"]),
        qualified_pairs=0,qualification_blocker="EVAL_WRAPPER_TRANSPARENCY_FAILURE",
        eval_transparency="FAILED: SDK history objects were converted using full model_dump before dispatch; not a protection-only transformation",
        behavioral_evidence_qualification="PRELIMINARY_OBSERVATIONS_ONLY; do not declare qualified paired benchmark",
        audit_gates_pass=audit_complete,production_unchanged=report["production_unchanged"],frozen_unchanged=report["frozen_unchanged"],
        recommendation="建议进入最小 Production 设计审批，不能以本次 Smoke 宣称通用可靠性或自动识别" if audit_complete and report["completed_pairs"]==9 else "NO_GO：补齐审计阻断原因后等待审批，不重跑替换失败样本")
    dump(root/("analysis"+suffix+".json"),result)
    for name,rows in (("paired-costs.csv",costs),("request-availability.csv",requests)):
        if rows:
            with (root/(Path(name).stem+suffix+".csv")).open("x",encoding="utf-8",newline="") as stream:
                writer=csv.DictWriter(stream,fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
    lines=["# Pinned Constraints Real-Model Paired Smoke", "", "证据：真实 Stage B + EVAL_SIMULATION；Stage A 与 Compaction 摘要固定，不评测自动摘要能力。", "",
        f"模型：{manifest['model']}；已执行配对 {len(report['pairs'])}/9，两侧均正常完成 {result['fully_completed_pairs']}/9；正式 qualified 配对 0，Eval 透明性阻断；状态 {report['status']}。", "",
        "| 场景 | Baseline 观察到遵守 | Pinned 观察到遵守 | Baseline / Pinned / Tie / Invalid |", "|---|---|---|---|"]
    for row in summary:
        sides=row["sides"]
        counts=row["paired_outcomes"]
        lines.append(f"| {row['case']} | {sides['Baseline']['constraint_pass']}/{sides['Baseline']['executed']} | {sides['Pinned']['constraint_pass']}/{sides['Pinned']['executed']} | {counts['Baseline']} / {counts['Pinned']} / {counts['Tie']} / {counts['INVALID_PAIR']} |")
    lines.extend(["", "## 成本（各场景三次总量；耗时为观测均值，不是调度或注入的因果收益）", "",
        "| 场景 / 侧 | 请求 | Provider 输入 / 输出 Tokens | Agent 平均耗时 ms |", "|---|---|---|---|"])
    for row in summary:
        for variant,side in row["sides"].items():
            lines.append(f"| {row['case']} / {variant} | {side['provider_requests']} | {side['provider_input_tokens']} / {side['provider_output_tokens']} | {side['mean_agent_latency_ms']} |")
    lines.extend(["", "## 审计与边界", "", "- 每场景付费调用前验证首请求；在已序列化参数层面，移除保护区后相等。SDK 原始对象层面的透明性没有通过，详见 STOP_FINDINGS.md。",
        "- 成功请求保存完整参数与 completed response，通过 span_id 核对 Usage。R3 Pinned 第二请求 HTTP 400，无 completed response / Usage，停止扩展，不能宣称所有请求 Usage 完整。原始 Artifact 不改写。",
        "- 仅使用注册的 read_file/glob/list_directory/write_file，Production Handler/权限管线保持原样；两侧同一确认策略，不以约束 A/B 拒绝写入。没有任意 shell 入口。",
        "- 用户规则违背按实际写入企图及文件差异判定；即便被权限挡住也计违背。业务 JSON 正确性独立于约束遵守。",
        "- 首请求之后模型自主轨迹可以分叉；完整 Token 与耗时差异不单独归因于保护区。首请求 Token 差见 analysis.json。",
        "- 请求渠道统计用预注册约束原文定位，不将关键词相关或模型回答视为约束已注入。正常历史、摘要、工具回读全部保留。",
        "- R3 旧 A 允许留在历史；只有 B 在有效保护区。约束来自预注册映射，没有自动识别能力。",
        "- seed 支持未验证，未添加未知参数；SDK 自动重试配置保存于 manifest，未重跑替换模型表现不佳的案例。",
        f"- Production 未改：{report['production_unchanged']}；冻结 Artifact 未改：{report['frozen_unchanged']}。", "", result["recommendation"]])
    with (root/("REPORT"+suffix+".md")).open("x",encoding="utf-8") as stream:
        stream.write("\n".join(lines)+"\n")
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
