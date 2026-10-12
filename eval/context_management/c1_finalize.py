"""保留原始 C1 Run，独立记录回归及验收观测失效的更正结论。"""
import argparse
import json
import re
from pathlib import Path
import subprocess
import sys
from .c1_independent import ROOT, hashes, save


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--existing-logs", action="store_true")
    args = parser.parse_args()
    out = args.run
    original = json.loads((out / "result.json").read_text())
    modules = ["eval.phase3.test_recovery_contract", "eval.recovery_hardening.test_hardening",
        "eval.phase4.test_context_memory_contract", "eval.test_memory_hardening", "eval.test_memory_m1",
        "eval.test_memory_m2_real", "eval.memory_m3.test_real_smoke", "eval.pinned_constraints.test_gate",
        "eval.pinned_constraints.test_real_smoke", "eval.pinned_constraints.test_production_smoke_audit",
        "eval.modules_v1.test_benchmark"]
    commands = {
        "runtime": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        "related_eval": [sys.executable, "-m", "unittest", *modules, "-v"],
        "diff_check": ["git", "diff", "--check"],
        "new_runner_diff_check": ["git", "diff", "--no-index", "--check", "/dev/null",
                                  "eval/context_management/c1_independent.py"],
    }
    results = {}
    for name, command in commands.items():
        if args.existing_logs:
            text = (out / (name + ".txt")).read_text()
            previous = json.loads((out / "result.corrected.json").read_text())
            code = previous["regressions"][name]["exit_code"]
        else:
            process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            text = process.stdout + process.stderr
            code = process.returncode
            (out / (name + ".txt")).write_text(text)
        # unittest 输出会和工具诊断同行；以权威汇总统计，不用行首筛选漏计。
        match = re.search(r"Ran (\d+) tests? in", text)
        total = int(match[1]) if match else 0
        counts = {"ok": 0, "FAIL": 0, "ERROR": 0, "SKIP": 0}
        for field, target in (("failures", "FAIL"), ("errors", "ERROR"), ("skipped", "SKIP")):
            matches = re.findall(r"\b" + field + r"=(\d+)", text)
            counts[target] = int(matches[-1]) if matches else 0
        counts["ok"] = total - counts["FAIL"] - counts["ERROR"] - counts["SKIP"]
        results[name] = dict(command=command, exit_code=code, observed_test_counts=counts,
                             total=sum(counts.values()), log=str(out / (name + ".txt")))
        print(name, code, counts, flush=True)
    before = json.loads((out / "manifest.json").read_text())["production_hashes"]
    unchanged = before == hashes()
    corrected = dict(original)
    corrected.update(gate="C1_BLOCKED", correction_revision=2 if args.existing_logs else 1, original_result=str(out / "result.json"),
        reason="EVAL_ORACLE_ERROR：C1-06 连续失败熔断测试将内部两次 attempt 与两轮失败混淆，且第二轮默认传入有效摘要。",
        confirmed_new_production_defects=[], regressions=results, production_unchanged=unchanged)
    corrected["scenarios"] = [dict(row, status="BLOCKED", raw_status=row["status"],
        classification="EVAL_ORACLE_ERROR") if row["scenario"] == "C1-06" else row for row in original["scenarios"]]
    suffix = ".final" if args.existing_logs else ".corrected"
    save(out / ("result" + suffix + ".json"), corrected)
    report = """# Context Management C1 独立验收：最终审计

结论：**C1_BLOCKED**。本轮不进入 C2/C3，不调用付费模型，未修改 Production。

## 独立矩阵

| 场景 | 结果 | MAIN / COMPACTION 实际 create |
|---|---|---|
"""
    for row in corrected["scenarios"]:
        count = row.get("provider_call_count", {})
        report += f"| {row['scenario']} | {row['status']} | {count.get('main', 'NOT_TESTED')} / {count.get('compaction', 'NOT_TESTED')} |\n"
    report += """
01–05 的已执行 Oracle 通过，但不等于 10/10 通过；06 未完成全部故障子场景；07–10 为 NOT_TESTED。
每个已执行场景的完整请求与 Oracle 在对应目录的 oracle.json；新 Session 场景保存 state-N.json 和原始 Session 目录。
02/04 重跑原测试，不复制历史结果。原测试临时目录的持久化证据覆盖有限，不声称全部复用测试工件已归档。

## C1-06 阻断原因：Eval 逻辑失误，不是已确认 Production 缺陷

冻结矩阵要求按 Production 当前连续失败规则检查熔断。源码 context_compaction.py:183–200 在 consecutive_compaction_failures >= max_compaction_attempts 时熔断；一次压缩中的两个摘要 attempt 全失败后，context_compaction.py:322 调用 record_compaction_failure() 一次；context_manager.py:273–275 加一。因此一次操作的两次 attempt 失败只算一轮失败，并未达到默认两轮阈值。

新 Runner c06 错误地在一轮失败后检查下一轮没有摘要调用；同时第二次 run() 未传 summary 故障参数，默认提供合法 SUMMARY。实际第一次两次 TimeoutError 后，state-1.json 显示 compaction_count=0、consecutive_compaction_failures=1；第二次接收合法摘要，state-2.json 显示 compaction_count=1、consecutive_compaction_failures=0。共两次主请求、三次摘要请求与源码一致。

原始 oracle.json 的 FAIL、result.json 的 C1_FAIL 和 REPORT.md 全部保留。此更正独立存放，不将错误 Oracle 失败解释为 Production 失败，也不把06改为PASS。未临时放宽断言、重新执行剩余矩阵或修改冻结条件。

## 最小后续建议（本轮未实施）

仅修正 Eval 故障序列：连续两轮均返回同一预注册摘要故障，分别核对失败计数；第三轮检查熔断、零额外摘要调用及 soft/hard 后续主请求行为。保持冻结 PASS 条件、Production 配置及所有状态/预算/请求 Oracle。审批后新建 Run 重新完整执行，保留此失败 Run。

## 本轮回归

"""
    for name, record in results.items():
        report += f"- {name}: exit={record['exit_code']}, counts={record['observed_test_counts']}，日志 {name}.txt。\n"
    report += f"\nProduction 的 {len(before)} 个 Python 文件 SHA256 前后一致：{unchanged}。完整哈希见 manifest.json/result.corrected.json。\n"
    report += "\n旧 Phase5A 两个 FIXTURE_OR_API_DRIFT ERROR 单独隔离，未运行/修复，不计为本轮 PASS。相关 Eval 为明确列出的11个模块，不声称 Eval 全量通过。FakeModel Usage 不作为 Provider 实测 Token。无 commit/tag。\n"
    (out / ("REPORT" + suffix + ".md")).write_text(report)
    print(json.dumps(dict(gate=corrected["gate"], report=str(out / ("REPORT" + suffix + ".md"))), ensure_ascii=False))


if __name__ == "__main__":
    main()
