"""工具注册与分发。"""

from collections.abc import Callable
from dataclasses import dataclass
import json
from typing import Any, Literal

from .tool_context import ToolExecutionContext
from .plan_tools import (
    create_plan,
    update_plan,
    validate_create_plan,
    validate_update_plan,
)
from hexgent.tool.memory_tools import (
    remember, validate_remember, validate_lifecycle,
    supersede_memory, invalidate_memory, delete_memory,
)
from hexgent.tool.skill_tools import (
    activate_skill,
    create_skill,
    validate_activate_skill,
    validate_create_skill,
)
from .builtin_tools import (
    bash,
    edit_file,
    glob,
    read_file,
    list_directory,
    validate_list_directory,
    validate_read_file,
    write_file,
)
from .workspace_capability_tool import (
    invoke_create_document,
    invoke_get_document,
    invoke_list_folder,
    invoke_search_workspace,
    invoke_workspace_capability,
    invoke_update_document,
)
from .tool_result import (
    ToolHandlerResult,
    ToolOutputValidator,
    validate_json_output,
)


ToolFunction = Callable[..., ToolHandlerResult]
ToolValidator = Callable[[dict[str, Any], ToolExecutionContext], str | None]
ToolDescriber = Callable[[dict[str, Any]], str]
ToolDeduplicationPolicy = Literal[
    "none",  # 每次调用都必须真实执行，不允许复用结果。
    "exact",  # 同一 Agent Run 内相同规范化参数可以复用此前成功结果。
    "content_aware",  # 由工具根据外部内容状态自行判断是否需要返回完整结果。
]
ToolPermissionCapability = Literal[
    "workspace_read",  # 读取工作区内的文件、目录和元数据。
    "workspace_write",  # 创建、覆盖或编辑工作区内文件。
    "shell",  # 执行可能组合本地、网络或外部副作用的 Shell 命令。
    "plan_state",  # 仅维护当前 Session 内的结构化 Plan 状态。
    "memory_state",  # 维护工作区级 Markdown Memory。
    "skill_state",  # 激活当前 Run 使用的项目级 Skill。
]
ToolExecutionDomain = Literal[
    "internal_runtime",  # 由 Hexgent Runtime 自己提供并授权的工具。
    "workspace_capability",  # 必须属于 WS AgentRun 冻结 capability scope 的工具。
]
ToolInterruptBehavior = Literal[
    "cancel",  # 工具只读且可安全请求终止，不产生需要保留的副作用。
    "block",  # 工具一旦开始必须等待明确结果，默认保留已经发生的副作用。
]


@dataclass(frozen=True)
class RegisteredTool:
    """注册表中的工具函数及其上下文需求。"""

    # 工具名称最终映射到的 Python 函数。
    function: ToolFunction

    # 标识执行函数时是否需要注入 ToolExecutionContext。
    requires_context: bool = False

    # 工具专属的额外参数校验方法。
    validator: ToolValidator | None = None

    # 生成调用摘要时优先展示的参数名称。
    display_argument: str | None = None

    # 复杂工具用于生成调用摘要的自定义方法。
    describer: ToolDescriber | None = None

    # 当前工具使用的重复调用去重策略。
    deduplication_policy: ToolDeduplicationPolicy = "none"

    # 工具成功执行后是否使其他精确调用缓存失效。
    invalidates_tool_call_cache: bool = False

    # 工具是否可以与相邻的其他安全工具并行执行。
    concurrency_safe: bool = False

    # 工具在无副作用失败后是否允许执行器使用相同参数自动重试。
    automatic_retry_safe: bool = False

    # 工具成功结果中 data 字段需要满足的 JSON Schema。
    output_schema: dict[str, Any] | None = None

    # JSON Schema 通过后执行的可选工具专属输出校验。
    output_validator: ToolOutputValidator | None = None

    # Permission Mode 用来判断工具默认行为的稳定能力分类。
    permission_capability: ToolPermissionCapability = "shell"

    # 执行归属与 Permission category 正交，决定是否受 WS capability scope 约束。
    execution_domain: ToolExecutionDomain = "internal_runtime"

    # 当前工具已经开始后收到 Pause 或 Cancel 时采用的保守处理策略。
    interrupt_behavior: ToolInterruptBehavior = "block"

    def describe(self, raw_arguments: str) -> str:
        """根据工具自身声明生成安全、简短的调用说明。"""
        # 1. 尽力解析原始参数；展示失败不能阻止后续统一参数校验。
        try:
            arguments = json.loads(raw_arguments)
        except (json.JSONDecodeError, TypeError):
            return f"{self.function.__name__}()"
        if not isinstance(arguments, dict):
            return f"{self.function.__name__}()"

        # 2. 优先使用复杂工具的自定义描述器，否则读取声明的展示参数。
        if self.describer is not None:
            try:
                description = self.describer(arguments)
            except Exception:
                description = ""
        elif self.display_argument is not None:
            value = arguments.get(self.display_argument)
            description = value if isinstance(value, str) else ""
        else:
            description = ""

        # 3. 压缩并截断展示信息，避免多行参数破坏终端布局。
        if not isinstance(description, str):
            description = ""
        compact_description = " ".join(description.split())
        if len(compact_description) > 120:
            compact_description = f"{compact_description[:117]}..."
        if not compact_description:
            return f"{self.function.__name__}()"
        return f"{self.function.__name__}({compact_description})"

    def validate(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> str | None:
        """执行可选的工具专属参数校验。"""
        if self.validator is None:
            return None
        return self.validator(arguments, context)

    def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolHandlerResult:
        """根据工具声明注入上下文并执行函数。"""
        if self.requires_context:
            return self.function(**arguments, context=context)
        return self.function(**arguments)

    def validate_output(self, result: ToolHandlerResult) -> str | None:
        """校验工具成功结果的正文、JSON 数据和专属输出约束。"""
        # 1. 迁移期间没有声明 Schema 的工具继续保持原有字符串协议。
        if not isinstance(result.output, str):
            return "工具成功结果的 output 必须是字符串"
        if self.output_schema is None:
            return None
        error = validate_json_output(result.data, self.output_schema)
        if error is not None:
            return error

        # 2. 通用 Schema 通过后再执行工具自身的语义校验。
        if self.output_validator is None:
            return None
        assert result.data is not None
        return self.output_validator(result.data)


def _describe_read_file(arguments: dict[str, Any]) -> str:
    """展示读取路径，并在分段读取时补充有效行范围。"""
    path = arguments.get("path")
    if not isinstance(path, str):
        return ""
    start_line = arguments.get("start_line")
    line_count = arguments.get("line_count")
    if start_line is None and line_count is None:
        return path
    effective_start = start_line if isinstance(start_line, int) else 1
    effective_count = line_count if isinstance(line_count, int) else 200
    return f"{path}, 第 {effective_start} 行起读取 {effective_count} 行"


TEXT_METADATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "characters": {"type": "integer"},
    },
    "required": ["path", "characters"],
    "additionalProperties": False,
}
READ_FILE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "sha256": {"type": "string"},
        "mode": {"type": "string", "enum": ["full", "partial"]},
        "unchanged": {"type": "boolean"},
        "start_line": {"type": ["integer", "null"]},
        "end_line": {"type": ["integer", "null"]},
        "total_lines": {"type": ["integer", "null"]},
    },
    "required": [
        "path",
        "sha256",
        "mode",
        "unchanged",
        "start_line",
        "end_line",
        "total_lines",
    ],
    "additionalProperties": False,
}
GLOB_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "match_count": {"type": "integer"},
        "returned_count": {"type": "integer"},
        "truncated": {"type": "boolean"},
    },
    "required": ["match_count", "returned_count", "truncated"],
    "additionalProperties": False,
}
PLAN_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["pending", "in_progress", "completed", "blocked"],
        },
        "revision": {"type": "integer"},
        "step_count": {"type": "integer"},
        "completed_count": {"type": "integer"},
    },
    "required": ["status", "revision", "step_count", "completed_count"],
    "additionalProperties": False,
}
SKILL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skill": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "instructions": {"type": "string"},
                "allowed_tools": {"type": "array", "items": {"type": "string"}},
                "relative_path": {"type": "string"},
            },
            "required": ["name", "description", "instructions", "allowed_tools", "relative_path"],
            "additionalProperties": False,
        },
    },
    "required": ["skill"],
    "additionalProperties": False,
}
CREATE_SKILL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skill": SKILL_OUTPUT_SCHEMA["properties"]["skill"],
        "created": {"type": "boolean"},
    },
    "required": ["skill", "created"],
    "additionalProperties": False,
}


TOOL_REGISTRY: dict[str, RegisteredTool] = {
    "create_document": RegisteredTool(
        invoke_create_document,
        requires_context=True,
        display_argument="title",
        permission_capability="workspace_write",
        execution_domain="workspace_capability",
    ),
    "update_document": RegisteredTool(
        invoke_update_document,
        requires_context=True,
        display_argument="documentId",
        permission_capability="workspace_write",
        execution_domain="workspace_capability",
    ),
    "list_folder": RegisteredTool(
        invoke_list_folder,
        requires_context=True,
        display_argument="folderId",
        permission_capability="workspace_read",
        execution_domain="workspace_capability",
    ),
    "get_document": RegisteredTool(
        invoke_get_document,
        requires_context=True,
        display_argument="documentId",
        permission_capability="workspace_read",
        execution_domain="workspace_capability",
        concurrency_safe=True,
        interrupt_behavior="cancel",
    ),
    "search_workspace": RegisteredTool(
        invoke_search_workspace,
        requires_context=True,
        display_argument="query",
        permission_capability="workspace_read",
        execution_domain="workspace_capability",
    ),
    "workspace_capability": RegisteredTool(
        invoke_workspace_capability,
        requires_context=True,
        display_argument="capability",
        permission_capability="workspace_read",
        execution_domain="workspace_capability",
    ),
    "bash": RegisteredTool(
        bash,
        requires_context=True,
        display_argument="command",
        invalidates_tool_call_cache=True,
        output_schema={
            "type": "object",
            "properties": {
                "exit_code": {"type": "integer"},
                "stdout_characters": {"type": "integer"},
                "stderr_characters": {"type": "integer"},
            },
            "required": ["exit_code", "stdout_characters", "stderr_characters"],
            "additionalProperties": False,
        },
        permission_capability="shell",
    ),
    "list_directory": RegisteredTool(
        list_directory,
        requires_context=True,
        validator=validate_list_directory,
        display_argument="path",
        concurrency_safe=True,
        automatic_retry_safe=True,
        permission_capability="workspace_read",
        interrupt_behavior="cancel",
    ),
    "read_file": RegisteredTool(
        read_file,
        requires_context=True,
        validator=validate_read_file,
        describer=_describe_read_file,
        deduplication_policy="content_aware",
        concurrency_safe=True,
        automatic_retry_safe=True,
        output_schema=READ_FILE_OUTPUT_SCHEMA,
        permission_capability="workspace_read",
        interrupt_behavior="cancel",
    ),
    "write_file": RegisteredTool(
        write_file,
        requires_context=True,
        display_argument="path",
        invalidates_tool_call_cache=True,
        output_schema=TEXT_METADATA_SCHEMA,
        permission_capability="workspace_write",
    ),
    "edit_file": RegisteredTool(
        edit_file,
        requires_context=True,
        display_argument="path",
        invalidates_tool_call_cache=True,
        output_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "replacements": {"type": "integer"},
            },
            "required": ["path", "replacements"],
            "additionalProperties": False,
        },
        permission_capability="workspace_write",
    ),
    "glob": RegisteredTool(
        glob,
        requires_context=True,
        display_argument="pattern",
        deduplication_policy="exact",
        concurrency_safe=True,
        automatic_retry_safe=True,
        output_schema=GLOB_OUTPUT_SCHEMA,
        permission_capability="workspace_read",
        interrupt_behavior="cancel",
    ),
    "create_plan": RegisteredTool(
        create_plan,
        requires_context=True,
        validator=validate_create_plan,
        display_argument="objective",
        output_schema=PLAN_OUTPUT_SCHEMA,
        permission_capability="plan_state",
    ),
    "update_plan": RegisteredTool(
        update_plan,
        requires_context=True,
        validator=validate_update_plan,
        display_argument="status",
        output_schema=PLAN_OUTPUT_SCHEMA,
        permission_capability="plan_state",
    ),
    "supersede_memory": RegisteredTool(
        supersede_memory, requires_context=True, validator=validate_lifecycle,
        permission_capability="memory_state", invalidates_tool_call_cache=True,
    ),
    "invalidate_memory": RegisteredTool(
        invalidate_memory, requires_context=True, validator=validate_lifecycle,
        permission_capability="memory_state", invalidates_tool_call_cache=True,
    ),
    "delete_memory": RegisteredTool(
        delete_memory, requires_context=True, validator=validate_lifecycle,
        permission_capability="memory_state", invalidates_tool_call_cache=True,
    ),
    "remember": RegisteredTool(
        remember,
        requires_context=True,
        validator=validate_remember,
        display_argument="content",
        deduplication_policy="exact",
        invalidates_tool_call_cache=True,
        output_schema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "action": {"type": "string", "enum": ["created", "updated"]},
            },
            "required": ["memory_id", "action"],
            "additionalProperties": False,
        },
        permission_capability="memory_state",
    ),
    "activate_skill": RegisteredTool(
        activate_skill,
        requires_context=True,
        validator=validate_activate_skill,
        display_argument="name",
        deduplication_policy="exact",
        output_schema=SKILL_OUTPUT_SCHEMA,
        permission_capability="skill_state",
    ),
    "create_skill": RegisteredTool(
        create_skill,
        requires_context=True,
        validator=validate_create_skill,
        display_argument="name",
        deduplication_policy="exact",
        invalidates_tool_call_cache=True,
        output_schema=CREATE_SKILL_OUTPUT_SCHEMA,
        permission_capability="workspace_write",
    ),
}


def get_tool(name: str) -> RegisteredTool | None:
    """根据名称查找已注册工具。"""
    return TOOL_REGISTRY.get(name)
