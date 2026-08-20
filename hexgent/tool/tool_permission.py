"""工具执行前的权限检查。"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict


WORKDIR = Path.cwd().resolve()

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
    check: Callable[[dict[str, Any]], bool]
    message: str


@dataclass(frozen=True)
class PermissionDecision:
    """权限管线产生的结构化决策。"""

    behavior: Literal["allow", "ask", "deny"]
    message: str | None = None
    reason: str | None = None


def _resolve_path(path: str) -> Path:
    """将工具参数中的路径解析为绝对路径。"""
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = WORKDIR / target
    return target.resolve()


def _is_outside_workspace(args: dict[str, Any]) -> bool:
    """检查文件路径是否位于工作区之外。"""
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return False
    return not _resolve_path(path).is_relative_to(WORKDIR)


def _contains_command_keyword(args: dict[str, Any], keywords: list[str]) -> bool:
    """检查命令参数是否包含任一关键词。"""
    command = args.get("command")
    if not isinstance(command, str):
        return False
    return any(keyword in command for keyword in keywords)


PERMISSION_RULES: list[PermissionRule] = [
    {
        "tools": ["read_file", "write_file", "edit_file"],
        "check": _is_outside_workspace,
        "message": "正在访问工作区之外的文件",
    },
    {
        "tools": ["bash"],
        "check": lambda args: _contains_command_keyword(
            args,
            ["rm ", "> /etc/", "chmod 777"],
        ),
        "message": "命令可能执行破坏性操作",
    },
]


def check_rules(tool_name: str, args: dict[str, Any]) -> str | None:
    """返回首条命中的权限规则说明；未命中时返回空值。"""
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None


def ask_user(tool_name: str, message: str) -> bool:
    """等待用户确认是否允许本次工具调用。"""
    try:
        answer = input(
            f"\033[35m权限确认：{message}（工具：{tool_name}），是否允许？[y/N] \033[0m"
        )
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in {"y", "yes", "是", "允许"}


def check_permission(tool_name: str, args: dict[str, Any]) -> PermissionDecision:
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

    message = check_rules(tool_name, args)
    if message is not None:
        return PermissionDecision(
            behavior="ask",
            message=message,
            reason="permission_rule",
        )

    return PermissionDecision(behavior="allow")
