"""工具注册与分发。"""

from collections.abc import Callable
from dataclasses import dataclass
import json
from typing import Any

from .tool_context import ToolExecutionContext
from .task_tools import (
    create_task,
    update_task_plan,
    validate_create_task,
    validate_update_task_plan,
)
from .builtin_tools import bash, edit_file, glob, read_file, write_file


ToolFunction = Callable[..., str]
ToolValidator = Callable[[dict[str, Any], ToolExecutionContext], str | None]
ToolDescriber = Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class RegisteredTool:
    """注册表中的工具函数及其上下文需求。"""

    function: ToolFunction
    requires_context: bool = False
    validator: ToolValidator | None = None
    display_argument: str | None = None
    describer: ToolDescriber | None = None

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
    "bash": RegisteredTool(bash, requires_context=True, display_argument="command"),
    "read_file": RegisteredTool(
        read_file,
        requires_context=True,
        display_argument="path",
    ),
    "write_file": RegisteredTool(
        write_file,
        requires_context=True,
        display_argument="path",
    ),
    "edit_file": RegisteredTool(
        edit_file,
        requires_context=True,
        display_argument="path",
    ),
    "glob": RegisteredTool(glob, requires_context=True, display_argument="pattern"),
    "create_task": RegisteredTool(
        create_task,
        requires_context=True,
        validator=validate_create_task,
        display_argument="objective",
    ),
    "update_task_plan": RegisteredTool(
        update_task_plan,
        requires_context=True,
        validator=validate_update_task_plan,
        display_argument="status",
    ),
}


def get_tool(name: str) -> RegisteredTool | None:
    """根据名称查找已注册工具。"""
    return TOOL_REGISTRY.get(name)
