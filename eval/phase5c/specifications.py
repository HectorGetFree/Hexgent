"""Phase5C-1 的任务契约；不包含供 Agent 直接使用的隐藏答案。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


MechanismVariant = Literal["full_harness", "compression_pair", "context_record_pair"]


@dataclass(frozen=True)
class TaskSpecification:
    task_id: str
    domain: str
    user_prompt: str
    fixture: str
    required_tools: tuple[str, ...]
    allowed_changes: tuple[str, ...]
    forbidden_changes: tuple[str, ...]
    oracle: str
    expected_artifacts: tuple[str, ...]
    mechanism_variant: MechanismVariant
    stage_definition: str | None = None


FEASIBILITY_TASKS: tuple[TaskSpecification, ...] = (
    TaskSpecification(
        "R1", "research_information_synthesis",
        "阅读调研资料，生成到 brief.md 的带来源标识决策简报；明确指出存在冲突的时间线信息。",
        "research-synthesis", ("glob", "read_file", "write_file"),
        ("brief.md",), ("market-notes.md", "field-interviews.md", "raw-data.csv"),
        "brief.md 必须包含结论、来源归因和冲突声明。", ("brief.md",), "compression_pair",
    ),
    TaskSpecification(
        "D2", "document_structured_review",
        "依据审核规则和申请材料生成 review-result.json，保留每位申请人的可追溯决定。",
        "document-operations", ("glob", "read_file", "write_file"),
        ("review-result.json",), ("policy.json", "applications/alice.md", "applications/bob.md"),
        "JSON schema、决定和理由必须与输入规则一致。", ("review-result.json",), "full_harness",
    ),
    TaskSpecification(
        "W1", "stateful_project_workflow",
        "整理需求和风险。仅更新 project-state.json：将 phase 设为 handoff_ready、risk_status 设为 acknowledged；"
        "并创建 handoff.md，使用 ## 当前状态、## 已知风险、## 下一步 三个章节。不要修改需求、风险或模板输入文件。",
        "project-workflow", ("glob", "read_file", "write_file", "create_plan", "update_plan"),
        ("project-state.json", "handoff.md"), ("requirements.md", "risks.md", "handoff-template.md"),
        "状态迁移、风险确认和交接必要章节必须完整。", ("project-state.json", "handoff.md"), "full_harness",
    ),
    TaskSpecification(
        "C1", "structured_configuration_manipulation",
        "合并服务目录和生产策略，生成可发布的 release-config.json。",
        "document-operations", ("glob", "read_file", "write_file"),
        ("release-config.json",), ("service-catalog.json", "deploy-policy.json", "release-template.json"),
        "输出 JSON 必须符合模板并使用生产策略优先级。", ("release-config.json",), "full_harness",
    ),
    TaskSpecification(
        "T1", "technical_regression_fix",
        "修复标签规范化 regression，并运行项目测试验证。tests/ 是只读 benchmark contract：不得修改、删除、跳过或弱化任何测试；"
        "只能修改 app/parser.py 中的 production implementation。",
        "technical-mini-service", ("glob", "read_file", "edit_file", "write_file", "bash"),
        ("app/parser.py",), ("tests/test_parser.py",),
        "fixture-local unittest 必须通过，且只允许修改 parser 实现。", ("app/parser.py",), "full_harness",
    ),
    TaskSpecification(
        "L1", "long_session_continuation",
        "先分析项目发布资料；后续继续完成一项依赖此前导航结论的交接更新。",
        "project-workflow", ("glob", "read_file", "write_file", "create_plan", "update_plan"),
        ("project-state.json", "handoff.md"), ("README.md", "config/release-policy.json"),
        "Stage B 输出、文件 diff 与已资格化的 Stage A ContextRecord 必须同时满足。",
        ("project-state.json", "handoff.md", ".hexgent/sessions"), "context_record_pair",
        "Stage A 使用 Production extraction；只有 active、有 provenance、与 Stage B 相关且不含最终答案的 record 才能冻结为 Stage B baseline。",
    ),
)


def task_by_id(task_id: str) -> TaskSpecification:
    for task in FEASIBILITY_TASKS:
        if task.task_id == task_id:
            return task
    raise KeyError(f"未知 Phase5C task：{task_id}")
