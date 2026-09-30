"""工具权限规则、Session Mode 和可审计评估管线。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .tool_context import ToolExecutionContext

if TYPE_CHECKING:
    from .tool_registry import RegisteredTool, ToolPermissionCapability


PermissionBehavior = Literal["allow", "ask", "deny"]
PermissionMode = Literal[
    "default",
    "accept_edits",
    "plan",
    "dont_ask",
    "auto_approve",
]
UserConfirmationResult = Literal[
    "allow",  # 用户明确允许本次工具调用。
    "deny",  # 用户明确拒绝本次工具调用。
    "unavailable",  # 当前交互入口无法向用户发起确认。
]
PermissionDecisionSource = Literal[
    "deny_rule",  # 命中不可由 Mode 或普通批准绕过的拒绝规则。
    "ask_rule",  # 命中必须保留人工确认边界的询问规则。
    "permission_mode",  # 当前 Session Mode 已经直接作出决定。
    "allow_rule",  # 命中明确预批准的具体操作规则。
    "default_fallback",  # 所有策略均未决，回退为请求用户批准。
    "dont_ask_fallback",  # 无交互模式下，未预批准操作直接拒绝。
    "auto_approve_mode",  # 本地调试模式自动批准非硬拒绝操作。
]


@dataclass(frozen=True)
class PermissionConfig:
    """Hexgent 新建 Session 时使用的权限默认配置。"""

    # 新 Session 默认采用的权限模式。
    default_mode: PermissionMode = "default"


@dataclass(frozen=True)
class PermissionContext:
    """一次工具权限评估使用的不可变 Session 模式快照。"""

    # 当前 Session 在本次工具准备阶段使用的权限模式。
    mode: PermissionMode = "default"


PermissionCheck = Callable[[dict[str, Any], ToolExecutionContext], bool]
PermissionMessage = Callable[[dict[str, Any], ToolExecutionContext], str]


@dataclass(frozen=True)
class PermissionRule:
    """一条具有稳定身份、行为和匹配条件的权限规则。"""

    # Transcript、Report 和测试用来稳定识别规则的名称。
    rule_id: str

    # 当前规则命中后产生的 allow、ask 或 deny 行为。
    behavior: PermissionBehavior

    # 当前规则适用的注册工具名称。
    tools: tuple[str, ...]

    # 使用规范化参数和工具工作区判断是否命中。
    check: PermissionCheck

    # 命中后向用户和审计记录说明原因。
    message: PermissionMessage


@dataclass(frozen=True)
class PermissionEvaluation:
    """权限规则和 Session Mode 共同产生的可审计决策。"""

    # 权限管线最终要求允许、询问用户或直接拒绝。
    behavior: PermissionBehavior

    # 本次评估实际使用的 Session 权限模式。
    mode: PermissionMode

    # 最终决定来自规则、Mode 还是未决回退。
    source: PermissionDecisionSource

    # 命中规则时的稳定规则标识；Mode 或回退决策时为空。
    rule_id: str | None = None

    # 提供给用户或工具结果的权限说明。
    message: str | None = None

    @property
    def reason(self) -> str:
        """兼容旧调用方使用的简短决策原因。"""
        return self.rule_id or self.source


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
    target = _resolve_path(str(args.get("path", "")), context.workdir.resolve())
    return f"正在访问工作区之外的文件：{target}；工作区：{context.workdir.resolve()}"


def _glob_escapes_workspace(
    args: dict[str, Any],
    _context: ToolExecutionContext,
) -> bool:
    """识别绝对 Glob 模式和父目录遍历。"""
    pattern = args.get("pattern")
    if not isinstance(pattern, str):
        return False
    pattern_path = Path(pattern).expanduser()
    return pattern_path.is_absolute() or ".." in pattern_path.parts


def _contains_command_keyword(
    args: dict[str, Any],
    keywords: tuple[str, ...],
) -> bool:
    """检查 Bash 命令参数是否包含任一关键词。"""
    command = args.get("command")
    return isinstance(command, str) and any(
        keyword in command for keyword in keywords
    )


DENY_RULES: tuple[PermissionRule, ...] = (
    PermissionRule(
        "critical_bash_command",
        "deny",
        ("bash",),
        lambda args, _context: _contains_command_keyword(
            args,
            ("rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"),
        ),
        lambda _args, _context: "命令命中不可绕过的系统安全拒绝规则",
    ),
)
ASK_RULES: tuple[PermissionRule, ...] = (
    PermissionRule(
        "outside_workspace_access",
        "ask",
        ("read_file", "write_file", "edit_file"),
        _is_outside_workspace,
        _outside_workspace_message,
    ),
    PermissionRule(
        "outside_workspace_glob",
        "ask",
        ("glob",),
        _glob_escapes_workspace,
        lambda _args, _context: "Glob 模式可能访问工作区之外的路径",
    ),
    PermissionRule(
        "destructive_bash_command",
        "ask",
        ("bash",),
        lambda args, _context: _contains_command_keyword(
            args,
            ("rm ", "> /etc/", "chmod 777"),
        ),
        lambda _args, _context: "命令可能执行破坏性操作",
    ),
)
ALLOW_RULES: tuple[PermissionRule, ...] = (
    PermissionRule(
        "workspace_read",
        "allow",
        ("read_file",),
        lambda args, context: not _is_outside_workspace(args, context),
        lambda _args, _context: "允许读取工作区内文件",
    ),
    PermissionRule(
        "workspace_glob",
        "allow",
        ("glob",),
        lambda args, context: not _glob_escapes_workspace(args, context),
        lambda _args, _context: "允许在工作区内查找文件",
    ),
    PermissionRule(
        "plan_state_management",
        "allow",
        ("create_plan", "update_plan"),
        lambda _args, _context: True,
        lambda _args, _context: "允许维护当前 Session 的 Plan 状态",
    ),
    PermissionRule(
        "memory_state_management",
        "allow",
        ("remember",),
        lambda _args, _context: True,
        lambda _args, _context: "允许保存用户明确要求的长期记忆",
    ),
    PermissionRule(
        "skill_state_management",
        "allow",
        ("activate_skill",),
        lambda _args, _context: True,
        lambda _args, _context: "允许激活项目级 Skill 工作流",
    ),
)


def _match_rule(
    rules: tuple[PermissionRule, ...],
    tool_name: str,
    args: dict[str, Any],
    context: ToolExecutionContext,
) -> PermissionRule | None:
    """按照声明顺序返回第一条命中的规则。"""
    for rule in rules:
        if tool_name in rule.tools and rule.check(args, context):
            return rule
    return None


def _from_rule(
    rule: PermissionRule,
    mode: PermissionMode,
    args: dict[str, Any],
    context: ToolExecutionContext,
) -> PermissionEvaluation:
    """把规则命中转换为包含来源和消息的评估结果。"""
    source_by_behavior: dict[PermissionBehavior, PermissionDecisionSource] = {
        "allow": "allow_rule",
        "ask": "ask_rule",
        "deny": "deny_rule",
    }
    return PermissionEvaluation(
        rule.behavior,
        mode,
        source_by_behavior[rule.behavior],
        rule.rule_id,
        rule.message(args, context),
    )


def _evaluate_mode(
    mode: PermissionMode,
    capability: ToolPermissionCapability,
) -> PermissionEvaluation | None:
    """让当前 Session Mode 处理其明确负责的工具能力。"""
    if mode == "accept_edits" and capability == "workspace_write":
        return PermissionEvaluation(
            "allow",
            mode,
            "permission_mode",
            message="当前模式自动批准工作区内文件编辑",
        )
    if mode == "plan" and capability in {"workspace_write", "shell"}:
        return PermissionEvaluation(
            "deny",
            mode,
            "permission_mode",
            message="Plan 模式不允许文件写入或 Shell 执行",
        )
    return None


def evaluate_permission(
    tool_name: str,
    tool: RegisteredTool,
    args: dict[str, Any],
    context: ToolExecutionContext,
    permission_context: PermissionContext | None = None,
) -> PermissionEvaluation:
    """按照 deny、ask、Mode、allow 和未决回退顺序评估权限。"""
    mode = (permission_context or PermissionContext()).mode

    # 1. 硬拒绝规则不可由 Mode、allow 规则或普通审批绕过。
    deny_rule = _match_rule(DENY_RULES, tool_name, args, context)
    if deny_rule is not None:
        return _from_rule(deny_rule, mode, args, context)

    # 2. 显式 ask 规则强制保留人工审批边界。
    ask_rule = _match_rule(ASK_RULES, tool_name, args, context)
    if ask_rule is not None:
        if mode == "auto_approve":
            return PermissionEvaluation(
                "allow",
                mode,
                "auto_approve_mode",
                ask_rule.rule_id,
                "本地调试模式自动批准原本需要确认的操作",
            )
        if mode == "dont_ask":
            return PermissionEvaluation(
                "deny",
                mode,
                "dont_ask_fallback",
                ask_rule.rule_id,
                "当前模式不请求批准，命中询问规则的操作已拒绝",
            )
        return _from_rule(ask_rule, mode, args, context)

    # 3. Mode 只处理自己明确负责的能力，其他调用继续进入 allow 规则。
    mode_evaluation = _evaluate_mode(mode, tool.permission_capability)
    if mode_evaluation is not None:
        return mode_evaluation

    # 4. 显式 allow 规则预批准稳定、可解释的具体操作。
    allow_rule = _match_rule(ALLOW_RULES, tool_name, args, context)
    if allow_rule is not None:
        return _from_rule(allow_rule, mode, args, context)

    # 5. 本地调试模式只跳过人工确认；硬拒绝已在前面执行，业务授权仍由 WS 校验。
    if mode == "auto_approve":
        return PermissionEvaluation(
            "allow",
            mode,
            "auto_approve_mode",
            message="本地调试模式自动批准未预批准的工具调用",
        )

    # 6. 未预批准操作在 dont_ask 下拒绝，其余模式回退为请求用户批准。
    if mode == "dont_ask":
        return PermissionEvaluation(
            "deny",
            mode,
            "dont_ask_fallback",
            message="当前模式不请求批准，未预批准的工具调用已拒绝",
        )
    return PermissionEvaluation(
        "ask",
        mode,
        "default_fallback",
        message="本次工具调用尚未被权限规则预批准",
    )
