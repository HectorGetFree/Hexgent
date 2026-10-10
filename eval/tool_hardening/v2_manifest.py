"""只构建源码覆盖计划；不执行工具、不计时、不运行 Benchmark。"""

import argparse
import ast
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLANS = {
    "tool_pipeline": {
        "discovery": ["hexgent/tool/*.py", "hexgent/loop/*.py", "tests/test_tool*.py", "tests/test_permission*.py"],
        "core": {
            "hexgent/tool/tool_registry.py": "工具声明、真实函数、并发/重试/去重/权限能力元数据",
            "hexgent/tool/tool_scheduler.py": "分组屏障、线程池、顺序提交、超时和输出预算",
            "hexgent/tool/tool_execution.py": "prepare/dispatch/handler、权限事实、失败转换与重试",
            "hexgent/tool/builtin_tools.py": "read_file/glob 实际 I/O、内容缓存及文件读写语义",
        },
        "dependencies": {
            "hexgent/tool/tool_context.py": "缓存、锁、指纹、工具运行上下文",
            "hexgent/tool/tool_permission_pipeline.py": "PermissionConfig、mode、规则优先级与工作区边界",
            "hexgent/tool/tool_validation.py": "参数解析、Schema 与工具专属校验",
            "hexgent/tool/tool_result.py": "Handler/Execution Result、failure、输出验证",
            "hexgent/tool/tool_retry.py": "安全重试条件与退避预算",
            "hexgent/loop/agent_turn.py": "主要调用方：Tool Batch 与下一轮 Context 提交",
            "hexgent/session/session_store.py": "调用事实持久化、大结果外置与引用",
            "hexgent/config.py": "并发、重试、Permission、结果预算加载与覆盖",
            "hexgent/run_control.py": "中断控制与调度器取消边界",
            "hexgent/events.py": "开始、执行完成、顺序提交及批次事件的数据契约",
            "hexgent/prompt.py": "正式工具参数 Schema 来源",
            "hexgent/utils/token_estimator.py": "批次结果预算依赖的估算器",
        },
        "tests": {
            "tests/test_tool_parallel_execution.py": "并发、屏障分组、完成/输出顺序、超时、取消",
            "tests/test_tool_failure_boundaries.py": "参数/输出失败、安全重试与未知副作用",
            "tests/test_tool_deduplication.py": "只读去重、写入失效与调用配对",
            "tests/test_permission_pipeline.py": "权限模式、拒绝优先级与 durable decision",
            "tests/test_tool_permission.py": "本地路径与具体工具的权限边界",
            "tests/test_session_store.py": "大结果 Artifact、并发 Transcript 与持久化引用",
        },
        "exclusions": "不追踪每个 Workspace capability/Skill/Plan 的业务 Handler；本题覆盖通用工具管线与内置本地工具，不声称覆盖全部工具业务。",
    },
    "recovery_checkpoint": {
        "discovery": ["hexgent/session/*.py", "hexgent/loop/*.py", "tests/test_session*.py", "tests/test_runtime_command*.py"],
        "core": {
            "hexgent/session/session_restore.py": "checkpoint 选择、Transcript replay、结果补回和恢复状态",
            "hexgent/session/checkpoint.py": "Checkpoint 数据结构、checksum、原子文件与安全快照",
            "hexgent/session/tool_call_recovery.py": "未闭合调用分类与未知副作用保守结果",
            "hexgent/session/session_store.py": "Transcript 序号、锁、原子写与外置工件",
            "hexgent/session/session.py": "run 生命周期、checkpoint 提交与 durable Context facts",
        },
        "dependencies": {
            "hexgent/agent_engine.py": "主要调用方：restore、continue 与新 Engine 重建",
            "hexgent/loop/loop_state.py": "RunStateSnapshot 与可恢复执行状态",
            "hexgent/loop/agent_loop.py": "Run 开始、恢复后 Loop 入口与终态",
            "hexgent/loop/agent_turn.py": "安全批次边界、Context output 与 checkpoint 的先后关系",
            "hexgent/tool/tool_scheduler.py": "started/dispatched/finished durable 顺序",
            "hexgent/context/context_manager.py": "被恢复的 query/messages/summary 状态",
            "hexgent/plan/plan_model.py": "checkpoint 中 Plan/Step 数据结构",
            "hexgent/config.py": "恢复时运行配置与覆盖契约",
            "hexgent/adapters/web/run_manager.py": "外部恢复调用方、PendingPermission 与 RunHandle 生命周期",
            "hexgent/integration/runtime_adapter.py": "durable command reconcile 的恢复入口",
            "hexgent/run_control.py": "恢复后执行中断状态的边界",
        },
        "tests": {
            "tests/test_session_recovery.py": "损坏快照 fallback、未派发/未知副作用分类、finished 补回",
            "tests/test_session_store.py": "持久化事实、序号、外置结果恢复",
            "tests/test_loop_interrupt.py": "执行中断与安全边界",
            "tests/test_runtime_command_effect_reconciliation_phase4.py": "真实命令持久化与重启 reconcile",
            "tests/test_agent_engine_stream.py": "Engine 执行及工具结果流转",
        },
        "exclusions": "不执行 crash 注入，不重新评测 Recovery 能力；不展开 HTTP 路由、CLI 展示和全部 Lifecycle 投影业务。",
    },
    "memory_context": {
        "discovery": ["hexgent/memory/*.py", "hexgent/context/*.py", "tests/test_memory*.py", "tests/test_context*.py"],
        "core": {
            "hexgent/memory/memory_store.py": "Workspace Memory 的身份、存储、显式写入和迁移",
            "hexgent/memory/memory_retrieval.py": "Workspace 召回、排序与注入预算",
            "hexgent/memory/context_records.py": "ContextRecord/Mutation、active/superseded 与 durable 来源",
            "hexgent/memory/context_retrieval.py": "Session record 检索、相关性与 fallback",
            "hexgent/context/context_manager.py": "query/messages、完整轮次与摘要边界",
            "hexgent/context/context_builder.py": "模型请求组装、记录/Memory 注入与裁剪",
        },
        "dependencies": {
            "hexgent/memory/context_record_extraction.py": "候选抽取、source quote 校验、进度与失败",
            "hexgent/memory/memory_auto_extraction.py": "自动 Memory 模块实际实现边界，不假定已接入 Loop",
            "hexgent/memory/memory_prompts.py": "Memory 提取的正式提示契约",
            "hexgent/context/context_compaction.py": "摘要请求、安全完整轮次与无效摘要处理",
            "hexgent/context/context_budget.py": "固定/检索上下文预算与裁剪优先级",
            "hexgent/tool/memory_tools.py": "remember 显式工具入口",
            "hexgent/loop/extract_context_records.py": "Production 抽取调度、提交与失败抑制",
            "hexgent/loop/agent_turn.py": "turn 内压缩、召回与抽取的主要调用方",
            "hexgent/agent_engine.py": "新 Query 的跨 Session Memory 召回与引擎入口",
            "hexgent/session/session.py": "ContextRecord 来源验证、存储及检索提交",
            "hexgent/session/session_restore.py": "record 与 Context durable replay",
            "hexgent/config.py": "检索、提取、压缩配置及 Run override",
            "hexgent/utils/retrieval_text.py": "共享分词和词项相关性依赖",
            "hexgent/utils/token_estimator.py": "请求与注入预算估算",
        },
        "tests": {
            "tests/test_memory.py": "显式写入、跨 Query 注入、去重、预算与迁移",
            "tests/test_context_records.py": "durable 来源与 record replay",
            "tests/test_context_retrieval.py": "active/相关性/数量预算与实际请求注入",
            "tests/test_context_record_extraction.py": "quote 验证、提交失败、进度与恢复",
            "tests/test_context_compaction.py": "完整轮次、关键请求部分、安全失败与摘要",
            "tests/test_session_recovery.py": "压缩/记录与恢复状态的连接",
            "tests/test_agent_loop_limits.py": "上下文容量与 Loop 限制边界",
        },
        "exclusions": "不评测真实模型理解、记忆提取质量或跨 Session 行为收益；不展开 Skill 的独立业务能力。",
    },
}


def build():
    """静态扫描当前 checkout，冻结每次读取的真实文件与参数。"""
    tasks = []
    for name, plan in PLANS.items():
        batches = []
        discovery = []
        for i, pattern in enumerate(plan["discovery"]):
            discovery.append({"call_id": f"{name}-discovery-{i}", "tool_name": "glob", "arguments": {"pattern": pattern}, "purpose": "确认相关实现与测试目录存在，不据此动态扩大计划", "expected_paths": sorted(str(p.relative_to(ROOT)) for p in ROOT.glob(pattern))})
        batches.append({"stage": "discovery", "depends_on": [], "calls": discovery})
        files = []
        for stage, previous in (("core", "discovery"), ("dependencies", "core"), ("tests", "dependencies")):
            calls = []
            for i, (relative, purpose) in enumerate(plan[stage].items()):
                path = ROOT / relative
                raw = path.read_bytes()
                tree = ast.parse(raw.decode("utf-8"))
                entry = {"path": relative, "role": stage, "purpose": purpose, "bytes": len(raw), "lines": len(raw.splitlines()), "sha256": sha256(raw).hexdigest(), "top_level_symbols": [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]}
                files.append(entry)
                calls.append({"call_id": f"{name}-{stage}-{i}", "tool_name": "read_file", "arguments": {"path": relative}, "purpose": purpose, "expected_sha256": entry["sha256"]})
            batches.append({"stage": stage, "depends_on": [previous], "calls": calls})
        unique = {f["path"] for f in files}
        assert len(unique) == len(files), "同一任务不得无意义重复读取"
        tasks.append({"task_name": name, "total_tool_calls": sum(len(b["calls"]) for b in batches), "total_batches": len(batches), "calls_per_batch": [len(b["calls"]) for b in batches], "total_files_read": len(files), "total_source_bytes": sum(f["bytes"] for f in files if f["role"] != "tests"), "total_test_bytes": sum(f["bytes"] for f in files if f["role"] == "tests"), "total_read_bytes": sum(f["bytes"] for f in files), "unique_files_covered": sorted(unique), "duplicate_reads": 0, "tool_type_distribution": {"glob": len(discovery), "read_file": len(files)}, "dependency_barriers": ["discovery → core：验证计划入口存在", "core → dependencies：实现职责确定后追踪调用方和数据契约", "dependencies → tests：按已识别边界核对测试；仅读取，不执行测试"], "in_batch_independence": "计划在准备阶段已冻结；每批只读不同文件，无写入、输出参数传递或顺序依赖；dependencies 包含逻辑互相引用文件，但读取行为独立", "source_coverage_manifest": files, "batches": batches, "excluded_scope": plan["exclusions"]})
    return {"status": "PENDING_HUMAN_REVIEW", "benchmark_executed": False, "planning_mode": "PRECOMPUTED_FROZEN_PLAN", "tasks": tasks}


def main():
    """只保存 Manifest，禁止覆盖已冻结的计划。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build()
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    for task in result["tasks"]:
        print(task["task_name"], task["total_tool_calls"], task["calls_per_batch"], task["total_files_read"], task["total_read_bytes"])


if __name__ == "__main__":
    main()
