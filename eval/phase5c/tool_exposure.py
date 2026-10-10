"""Phase 5C Eval-only per-task tool exposure。"""

from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import replace

from hexgent.loop import agent_turn
from hexgent.prompt import TOOLS
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_registry import TOOL_REGISTRY
from hexgent.tool.tool_result import ToolHandlerResult

from .specifications import TaskSpecification


def visible_tool_names(spec: TaskSpecification) -> tuple[str, ...]:
    """返回冻结 TaskSpecification 声明的模型可见工具集合。"""

    available = {definition["name"] for definition in TOOLS}
    return tuple(name for name in spec.required_tools if name in available)


@contextmanager
def task_tool_exposure(spec: TaskSpecification) -> Iterator[tuple[str, ...]]:
    """仅在 Eval run 内收紧 schema 与 dispatch，并始终恢复全局引用。

    schema 隐藏只能约束正常模型调用；若模型或测试伪造未暴露工具的 function
    call，仍可能绕过这一层而命中全局 registry。因此 Eval 期间同时把未声明
    工具替换为稳定的 policy denial。Production registry 在退出时原样恢复。
    """

    original = agent_turn.TOOLS
    declared = set(spec.required_tools)
    definitions_by_name = {definition["name"]: definition for definition in original}
    filtered = [definitions_by_name[name] for name in visible_tool_names(spec)]
    replaced_tools = {
        name: registered
        for name, registered in TOOL_REGISTRY.items()
        if name not in declared
    }

    def deny_unexposed_tool(*_: object, context: ToolExecutionContext, **__: object) -> ToolHandlerResult:
        return ToolHandlerResult.failed(
            "eval_tool_policy_denied",
            "该工具不属于当前评测任务的可见工具集合",
            phase="permission",
            side_effect_state="not_started",
            execution_status="denied",
        )

    agent_turn.TOOLS = filtered
    for name, registered in replaced_tools.items():
        TOOL_REGISTRY[name] = replace(
            registered,
            function=deny_unexposed_tool,
            requires_context=True,
            validator=None,
        )
    try:
        yield tuple(definition["name"] for definition in filtered)
    finally:
        agent_turn.TOOLS = original
        TOOL_REGISTRY.update(replaced_tools)
