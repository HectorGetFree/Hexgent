"""工具注册与分发。"""

from collections.abc import Callable
from .tools import bash, edit_file, glob, read_file, write_file

ToolFunction = Callable[..., str]

TOOL_REGISTRY: dict[str, ToolFunction] = {
    "bash": bash,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "glob": glob,
}


def get_tool(name: str) -> ToolFunction | None:
    """根据名称查找工具函数。"""
    return TOOL_REGISTRY.get(name)
