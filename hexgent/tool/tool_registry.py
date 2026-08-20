"""工具注册与分发。"""

from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class RegisteredTool:
    """注册表中的工具函数及其上下文需求。"""

    function: ToolFunction
    requires_context: bool = False
    validator: ToolValidator | None = None

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
    "bash": RegisteredTool(bash),
    "read_file": RegisteredTool(read_file),
    "write_file": RegisteredTool(write_file),
    "edit_file": RegisteredTool(edit_file),
    "glob": RegisteredTool(glob),
    "create_task": RegisteredTool(
        create_task,
        requires_context=True,
        validator=validate_create_task,
    ),
    "update_task_plan": RegisteredTool(
        update_task_plan,
        requires_context=True,
        validator=validate_update_task_plan,
    ),
}


def get_tool(name: str) -> RegisteredTool | None:
    """根据名称查找已注册工具。"""
    return TOOL_REGISTRY.get(name)
