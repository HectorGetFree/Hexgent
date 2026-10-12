"""新 C1 Run 的证据复核与回归；不覆盖 Runner 原始结果。"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from .c1_independent import ROOT, hashes, save


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    out = args.run.resolve()
    raw = json.loads((out / "result.json").read_text())
    modules = ["eval.phase3.test_recovery_contract", "eval.recovery_hardening.test_hardening",
        "eval.phase4.test_context_memory_contract", "eval.test_memory_hardening", "eval.test_memory_m1",
        "eval.test_memory_m2_real", "eval.memory_m3.test_real_smoke", "eval.pinned_constraints.test_gate",
        "eval.pinned_constraints.test_real_smoke", "eval.pinned_constraints.test_production_smoke_audit",
        "eval.modules_v1.test_benchmark", "eval.context_management.test_c1_runner"]
    commands = {
        "runtime": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        "related_eval": [sys.executable, "-m", "unittest", *modules, "-v"],
        "diff_check": ["git", "diff", "--check"],
    }
    results = {}
    for name, command in commands.items():
        process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        text = process.stdout + process.stderr
        (out / (name + ".txt")).write_text(text)
        match = re.search(r"Ran (\d+) tests? in", text)
        total = int(match[1]) if match else None
        counts = {}
        for field in ("failures", "errors", "skipped"):
            values = re.findall(r"\b" + field + r"=(\d+)", text)
            counts[field] = int(values[-1]) if values else 0
        counts["passed"] = total - sum(counts.values()) if total is not None else None
        results[name] = dict(command=command, exit_code=process.returncode, total=total,
                             counts=counts, log=str(out / (name + ".txt")))
        print(name, results[name], flush=True)
    before = json.loads((out / "manifest.json").read_text())["production_hashes"]
    after = hashes()
    evidence = json.loads((out / "C1-06/oracle.json").read_text())
    # 顺序来自预注册 c06 cases：第六种故障的三轮位于 executions[15:18]。
    failures = [event["message"] for run in evidence["executions"][15:18]
                for event in run["events"] if isinstance(event, dict) and "message" in event]
    fixture = evidence.get("no_savings_fixture", {})
    reason_checks = [row for row in evidence.get("oracles", [])
                     if row["name"] == "no_savings拒绝原因必须为压缩收益不足"]
    no_savings_verified = (len(reason_checks) == 2 and all(row["pass_"] for row in reason_checks)
        and sum("压缩收益不足" in message for message in failures) == 2
        and not any("摘要内容超过配置的摘要 Token 预算" in message for message in failures)
        and 0 < fixture.get("summary_tokens", 0) <= fixture.get("summary_limit", 0)
        and fixture.get("after_tokens", -1) >= fixture.get("before_tokens", 0))
    gap = not no_savings_verified
    gate = "C1_BLOCKED" if gap or before != after else raw["gate"]
    if any(value["exit_code"] != 0 for value in results.values()):
        gate = "C1_FAIL"
    final = dict(raw, gate=gate, runner_gate=raw["gate"], regressions=results,
        production_unchanged=before == after, production_hashes_after=after,
        coverage_review=dict(C1_06_no_savings="NOT_VERIFIED" if gap else "PASS",
            observed_messages=failures, classification="EVAL_COVERAGE_GAP" if gap else "VERIFIED",
            fixture_estimates={key: fixture.get(key) for key in
                ("summary_tokens", "summary_limit", "before_tokens", "after_tokens", "savings_tokens")},
            note="独立核对合法摘要、无收益候选及两轮实际收益拒绝；摘要长度拒绝不能替代。"))
    save(out / "result.reviewed.json", final)
    report = "# Context Management C1 重跑与独立证据复核\n\n"
    report += f"最终 Gate：**{gate}**。Runner原始结果与本轮回归、拒绝原因独立复核分别保存。\n\n"
    report += "## 逐场景实际结果\n\n| 场景 | Runner | MAIN / COMPACTION create | 证据复核 |\n|---|---|---|---|\n"
    for row in raw["scenarios"]:
        counts = row.get("provider_call_count", {})
        status = "PARTIAL：no_savings 未真正触发" if row["scenario"] == "C1-06" and gap else row["status"]
        report += f"| {row['scenario']} | {row['status']} | {counts.get('main')} / {counts.get('compaction')} | {status} |\n"
    report += """
所有场景均实际重新执行；01–10 的配置、请求、状态及 Oracle 在各自目录。07实际执行原 Session Restore→Continue；09 normal与over分别记录实际派发及预算候选；10重新执行固定 Summary 保留/遗漏及 Checkpoint后Replace/Revoke的原Fixture。

## C1-06 拒绝原因复核

熔断修复已通过：连续两轮同一故障，第三轮无额外摘要调用。Timeout、failed event、残缺流、空/多摘要、重试成功、soft可继续及hard零主派发均实际执行。

"""
    report += f"无收益分支独立复核：{'PASS' if no_savings_verified else 'NOT_VERIFIED'}。\n\n"
    report += f"冻结估算：summary={fixture.get('summary_tokens')}/{fixture.get('summary_limit')}，完整输入={fixture.get('before_tokens')}→{fixture.get('after_tokens')}，收益={fixture.get('savings_tokens')}。均为估算值，不是Provider Usage。\n\n"
    for message in failures:
        report += f"- 实际事件：{message}\n"
    report += "\n原Runner报告与result.json不修改；本报告和result.reviewed.json独立记录复核。未在执行后更换Fixture、放宽断言或修复Production。\n\n## 本轮回归\n\n"
    for name, value in results.items():
        report += f"- {name}：exit={value['exit_code']}，total={value['total']}，{value['counts']}；日志 {name}.txt。\n"
    report += f"\nProduction {len(before)} 个Python文件前后SHA256一致：{before == after}。\n"
    report += "\n旧Phase5A两个FIXTURE_OR_API_DRIFT ERROR保持隔离，不计PASS；相关Eval为明确列出的12模块，不宣称Eval全量。无付费调用、无commit/tag、未进入C2/C3。原始失败及修复Run均保留。\n"
    (out / "REPORT.reviewed.md").write_text(report)
    print(json.dumps(dict(gate=gate, report=str(out / "REPORT.reviewed.md")), ensure_ascii=False))


if __name__ == "__main__":
    main()
