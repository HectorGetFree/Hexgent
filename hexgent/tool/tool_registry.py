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
from .builtin_tools import bash, edit_file, glob, read_file, write_file


ToolFunction = Callable[..., str]
ToolValidator = Callable[[dict[str, Any], ToolExecutionContext], str | None]
ToolDescriber = Callable[[dict[str, Any]], str]
ToolDeduplicationPolicy = Literal[
    "none",  # 每次调用都必须真实执行，不允许复用结果。
    "exact",  # 同一 Agent Run 内相同规范化参数可以复用此前成功结果。
    "content_aware",  # 由工具根据外部内容状态自行判断是否需要返回完整结果。
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
    ) -> str:
        """根据工具声明注入上下文并执行函数。"""
        if self.requires_context:
            return self.function(**arguments, context=context)
        return self.function(**arguments)


TOOL_REGISTRY: dict[str, RegisteredTool] = {
    "bash": RegisteredTool(
        bash,
        requires_context=True,
        display_argument="command",
        invalidates_tool_call_cache=True,
    ),
    "read_file": RegisteredTool(
        read_file,
        requires_context=True,
        display_argument="path",
        deduplication_policy="content_aware",
    ),
    "write_file": RegisteredTool(
        write_file,
        requires_context=True,
        display_argument="path",
        invalidates_tool_call_cache=True,
    ),
    "edit_file": RegisteredTool(
        edit_file,
        requires_context=True,
        display_argument="path",
        invalidates_tool_call_cache=True,
    ),
    "glob": RegisteredTool(
        glob,
        requires_context=True,
        display_argument="pattern",
        deduplication_policy="exact",
    ),
    "create_plan": RegisteredTool(
        create_plan,
        requires_context=True,
        validator=validate_create_plan,
        display_argument="objective",
    ),
    "update_plan": RegisteredTool(
        update_plan,
        requires_context=True,
        validator=validate_update_plan,
        display_argument="status",
    ),
}


def get_tool(name: str) -> RegisteredTool | None:
    """根据名称查找已注册工具。"""
    return TOOL_REGISTRY.get(name)
