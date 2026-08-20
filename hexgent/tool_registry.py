"""工具注册与分发。"""

from collections.abc import Callable
from typing import Any

from .tools import bash, edit_file, glob, read_file, write_file

ToolFunction = Callable[..., str]

TOOL_REGISTRY: dict[str, ToolFunction] = {
    "bash": bash,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "glob": glob,
}


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """执行已注册的工具，并将异常转换为工具输出。"""
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        return f"错误：未知工具“{name}”"

    try:
        return tool(**arguments)
    except (OSError, UnicodeError, TypeError, ValueError) as error:
        return f"错误：{error}"
