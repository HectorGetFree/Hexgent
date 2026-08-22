"""工具执行前的权限检查。"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from .tool_context import ToolExecutionContext

DENY_LIST = [
    "rm -rf /",
    "sudo",
    "shutdown",
    "reboot",
    "mkfs",
    "dd if=",
    "> /dev/sda",
]


class PermissionRule(TypedDict):
    """需要用户确认的权限规则。"""

    tools: list[str]
    check: Callable[[dict[str, Any], ToolExecutionContext], bool]
    message: Callable[[dict[str, Any], ToolExecutionContext], str]


@dataclass(frozen=True)
class PermissionDecision:
    """权限管线产生的结构化决策。"""

    # 权限管线最终要求允许、询问用户或直接拒绝。
    behavior: Literal["allow", "ask", "deny"]

    # 提供给用户或工具结果的权限说明。
    message: str | None = None

    # 供程序内部识别和审计的决策原因。
    reason: str | None = None


def _resolve_path(path: str, workdir: Path) -> Path:
    """将工具参数中的路径解析为绝对路径。"""
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = workdir / target
    return target.resolve()


def _is_outside_workspace(
    args: dict[str, Any],
    context: ToolExecutionContext,
) -> bool:
    """检查文件路径是否位于工作区之外。"""
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return False
    workdir = context.workdir.resolve()
    return not _resolve_path(path, workdir).is_relative_to(workdir)


def _outside_workspace_message(
    args: dict[str, Any],
    context: ToolExecutionContext,
) -> str:
    """展示工作区边界以及工具准备访问的绝对路径。"""
    path = args.get("path", "")
    target = _resolve_path(path, context.workdir.resolve())
    return f"正在访问工作区之外的文件：{target}；工作区：{context.workdir.resolve()}"


def _contains_command_keyword(
    args: dict[str, Any],
    _context: ToolExecutionContext,
    keywords: list[str],
) -> bool:
    """检查命令参数是否包含任一关键词。"""
    command = args.get("command")
    if not isinstance(command, str):
        return False
    return any(keyword in command for keyword in keywords)


PERMISSION_RULES: list[PermissionRule] = [
    {
        "tools": ["read_file", "write_file", "edit_file"],
        "check": _is_outside_workspace,
        "message": _outside_workspace_message,
    },
    {
        "tools": ["bash"],
        "check": lambda args, context: _contains_command_keyword(
            args,
            context,
            ["rm ", "> /etc/", "chmod 777"],
        ),
        "message": lambda _args, _context: "命令可能执行破坏性操作",
    },
]


def check_rules(
    tool_name: str,
    args: dict[str, Any],
    context: ToolExecutionContext,
) -> str | None:
    """返回首条命中的权限规则说明；未命中时返回空值。"""
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args, context):
            return rule["message"](args, context)
    return None


UserConfirmationResult = Literal["allow", "deny", "unavailable"]


def check_permission(
    tool_name: str,
    args: dict[str, Any],
    context: ToolExecutionContext,
) -> PermissionDecision:
    """根据工具及参数计算权限决策，不执行用户交互。"""
    if tool_name == "bash":
        command = args.get("command")
        if not isinstance(command, str):
            command = ""
        denied_keyword = next(
            (keyword for keyword in DENY_LIST if keyword in command),
            None,
        )
        if denied_keyword is not None:
            return PermissionDecision(
                behavior="deny",
                message=f"错误：命令命中拒绝列表“{denied_keyword}”，已拒绝执行",
                reason="deny_list",
            )

    message = check_rules(tool_name, args, context)
    if message is not None:
        return PermissionDecision(
            behavior="ask",
            message=message,
            reason="permission_rule",
        )

    return PermissionDecision(behavior="allow")
