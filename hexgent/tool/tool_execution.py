"""单个及批量工具调用的完整执行管线。"""

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass, replace
import inspect
import json
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal

from hexgent.tool.tool_permission import (
    PermissionDecision,
    UserConfirmationResult,
    check_permission,
)
from hexgent.prompt import TOOLS
from hexgent.context.session_store import (
    PersistedToolResult,
    SessionStore,
    build_persisted_output_message,
)
from hexgent.utils.token_estimator import estimate_value_tokens
from .tool_context import ToolExecutionContext
from .tool_registry import RegisteredTool, get_tool

if TYPE_CHECKING:
    from hexgent.events import AgentEvent


ToolExecutionStatus = Literal[
    "success",  # 工具调用已经成功执行并生成结果。
    "invalid",  # 工具不存在，或调用参数未通过解析与合法性校验。
    "denied",  # 权限管线或用户确认拒绝执行工具。
    "error",  # 工具执行期间发生异常或超时。
]
UserConfirmation = Callable[[str, str], UserConfirmationResult]
ToolExecutionNotifier = Callable[[str], None]


@dataclass(frozen=True)
class ToolExecutionResult:
    """一次工具调用的最终结果。"""

    # 工具调用经过完整执行管线后得到的状态。
    status: ToolExecutionStatus

    # 返回给模型的工具执行文本。
    output: str

    # 标识该结果是否应被模型视为错误结果。
    is_error: bool


@dataclass(frozen=True)
class ToolBatchResult:
    """完整工具批次产生的配对结果和实际执行数量。"""

    # 按调用顺序生成的 function_call_output 列表。
    outputs: list[dict[str, str]]

    # 本批次实际计入工具预算的调用数量。
    executed_count: int


TOOL_SCHEMAS = {tool["name"]: tool["parameters"] for tool in TOOLS}


def unavailable_user_confirmation(
    _tool_name: str,
    _message: str,
) -> UserConfirmationResult:
    """在调用方没有提供界面确认能力时安全地停止执行。"""
    return "unavailable"


def _invalid(message: str) -> ToolExecutionResult:
    """创建参数无效结果。"""
    return ToolExecutionResult("invalid", f"错误：{message}", True)


def _parse_arguments(raw_arguments: str) -> ToolExecutionResult | dict[str, Any]:
    """解析模型返回的 JSON 工具参数。"""
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as error:
        return _invalid(f"工具参数不是有效的 JSON：{error}")
    except TypeError as error:
        return _invalid(f"工具参数无法解析：{error}")

    if not isinstance(arguments, dict):
        return _invalid("工具参数必须是 JSON 对象")
    return arguments


def _matches_json_type(value: Any, expected_type: str | list[str]) -> bool:
    """判断 Python 值是否符合基础 JSON Schema 类型。"""
    if isinstance(expected_type, list):
        return any(_matches_json_type(value, item) for item in expected_type)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)

    type_mapping: dict[str, type | tuple[type, ...]] = {
        "string": str,
        "boolean": bool,
        "object": dict,
        "array": list,
        "null": type(None),
    }
    expected_python_type = type_mapping.get(expected_type)
    return expected_python_type is None or isinstance(value, expected_python_type)


def _validate_arguments(
    tool_name: str,
    tool: RegisteredTool,
    arguments: dict[str, Any],
    context: ToolExecutionContext,
) -> ToolExecutionResult | None:
    """按照工具 Schema 和函数签名校验参数。"""
    schema = TOOL_SCHEMAS.get(tool_name)
    if schema is None:
        return _invalid(f"工具“{tool_name}”缺少参数 Schema")

    required = schema.get("required", [])
    missing = [name for name in required if name not in arguments]
    if missing:
        return _invalid(f"缺少必填参数：{', '.join(missing)}")

    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        extra = [name for name in arguments if name not in properties]
        if extra:
            return _invalid(f"包含未声明的参数：{', '.join(extra)}")

    for name, value in arguments.items():
        expected_type = properties.get(name, {}).get("type")
        if expected_type and not _matches_json_type(value, expected_type):
            return _invalid(f"参数“{name}”必须是 {expected_type} 类型")

    try:
        bind_arguments = dict(arguments)
        if tool.requires_context:
            bind_arguments["context"] = context
        inspect.signature(tool.function).bind(**bind_arguments)
    except TypeError as error:
        return _invalid(f"参数与工具函数签名不匹配：{error}")

    tool_validation_error = tool.validate(arguments, context)
    if tool_validation_error is not None:
        return _invalid(tool_validation_error)
    return None


def _resolve_permission(
    tool_name: str,
    decision: PermissionDecision,
    user_confirmation: UserConfirmation,
) -> ToolExecutionResult | None:
    """处理 deny 和 ask，将允许决策继续传向执行阶段。"""
    if decision.behavior == "deny":
        return ToolExecutionResult(
            "denied",
            decision.message or "错误：工具调用已被拒绝",
            True,
        )

    if decision.behavior == "ask":
        message = decision.message or "本次工具调用需要用户确认"
        confirmation = user_confirmation(tool_name, message)
        if confirmation == "deny":
            return ToolExecutionResult(
                "denied",
                f"错误：用户拒绝本次操作：{message}",
                True,
            )
        if confirmation == "unavailable":
            return ToolExecutionResult(
                "error",
                "错误：当前输入环境无法读取权限确认，本次工具调用未执行",
                True,
            )
    return None


def execute_tool_call(
    tool_name: str,
    raw_arguments: str,
    *,  # 表示后面的参数只能用参数名传递
    context: ToolExecutionContext,
    user_confirmation: UserConfirmation = unavailable_user_confirmation,
    execution_notifier: ToolExecutionNotifier | None = None,
) -> ToolExecutionResult:
    """完成工具查找、参数校验、权限处理和真正执行。"""
    # 1. 校验工具是否存在，并取得对应的工具函数。
    tool = get_tool(tool_name)
    if tool is None:
        return ToolExecutionResult(
            "invalid",
            f"错误：未知工具“{tool_name}”",
            True,
        )

    # 2. 将模型生成的原始参数解析为 JSON 对象。
    parsed = _parse_arguments(raw_arguments)
    if isinstance(parsed, ToolExecutionResult):
        return parsed

    # 3. 按照工具 Schema 和函数签名校验参数合法性。
    validation_error = _validate_arguments(tool_name, tool, parsed, context)
    if validation_error is not None:
        return validation_error

    # 4. 计算权限决策，并处理直接拒绝或等待用户确认的情况。
    permission_result = _resolve_permission(
        tool_name,
        check_permission(tool_name, parsed, context),
        user_confirmation,
    )
    if permission_result is not None:
        return permission_result

    # 5. 权限允许后，通知调用方工具即将开始执行。
    if execution_notifier is not None:
        execution_notifier(tool_name)

    # 6. 真正执行工具，并将正常输出或异常转换为统一结果。
    try:
        output = tool.execute(parsed, context)
    except Exception as error:
        return ToolExecutionResult("error", f"错误：{error}", True)
    return ToolExecutionResult("success", output, False)


def _budget_tool_result(
    call_id: str,
    message: str,
) -> dict[str, str]:
    """为因预算不足而未执行的工具调用创建配对结果。"""
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": f"错误：{message}，本次工具调用未执行",
    }


def _truncate_to_token_budget(text: str, token_budget: int) -> str:
    """在不超过估算 Token 预算的前提下保留文本开头。"""
    if token_budget <= 0:
        return ""
    if estimate_value_tokens(text) <= token_budget:
        return text
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_value_tokens(text[:middle]) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def _apply_batch_result_budget(
    outputs: list[dict[str, str]],
    inline_results: list[tuple[int, str, str, str, str]],
    persisted_results: dict[int, PersistedToolResult],
    session_store: SessionStore | None,
    context: ToolExecutionContext,
) -> None:
    """按批次总预算从最大结果开始外置，并限制所有预览的总量。"""
    if session_store is None:
        return
    limits = session_store.tool_result_limits

    # 1. 超过批次内联预算时，持续选择当前最大的完整结果进行外置。
    remaining = list(inline_results)
    total_inline_tokens = sum(
        estimate_value_tokens(output)
        for _, _, _, output, _ in remaining
    )
    while remaining and total_inline_tokens > limits.max_batch_inline_tokens:
        largest = max(remaining, key=lambda item: estimate_value_tokens(item[3]))
        remaining.remove(largest)
        output_index, call_id, tool_name, raw_output, description = largest
        persisted = session_store.persist_tool_result(
            call_id,
            tool_name,
            raw_output,
            force=True,
            description=description,
        )
        if persisted is None:
            raise RuntimeError("工具结果批次预算要求外置，但未生成持久化结果")
        persisted_results[output_index] = persisted
        context.clear_read_file_cache()
        total_inline_tokens -= estimate_value_tokens(raw_output)

    # 2. 在所有外置结果之间平均分配总预览预算，并更新模型可见结果。
    if not persisted_results:
        return
    preview_budget = max(
        1,
        limits.max_batch_preview_tokens // len(persisted_results),
    )
    for output_index, persisted in persisted_results.items():
        preview = _truncate_to_token_budget(persisted.preview, preview_budget)
        outputs[output_index]["output"] = build_persisted_output_message(
            replace(persisted, preview=preview)
        )


def execute_tool_batch(
    tool_calls: list[Any],
    *,
    context: ToolExecutionContext,
    user_confirmation: UserConfirmation,
    remaining_tool_calls: int | None,
    deadline: float | None,
    session_store: SessionStore | None = None,
) -> Generator[AgentEvent, None, ToolBatchResult]:
    """执行完整工具批次，并保证每个调用都有配对结果。"""
    from hexgent.events import (
        PlanCreatedEvent,
        PlanUpdatedEvent,
        ToolFinishedEvent,
        ToolStartedEvent,
    )

    # 1. 按响应顺序处理工具，持续累计本批次实际执行数量。
    outputs: list[dict[str, str]] = []
    inline_results: list[tuple[int, str, str, str, str]] = []
    persisted_results: dict[int, PersistedToolResult] = {}
    executed_count = 0
    limit_message: str | None = None

    for tool_call in tool_calls:
        persisted: PersistedToolResult | None = None
        registered_tool = get_tool(tool_call.name)
        description = (
            registered_tool.describe(tool_call.arguments)
            if registered_tool is not None
            else f"{tool_call.name}()"
        )
        yield ToolStartedEvent(tool_call.name, tool_call.call_id, description)
        if session_store is not None:
            session_store.append_transcript(
                "tool_execution_started",
                {
                    "call_id": tool_call.call_id,
                    "tool_name": tool_call.name,
                    "arguments": tool_call.arguments,
                    "description": description,
                },
            )

        # 2. 每个工具执行前检查批次额度，耗尽后仍为剩余调用配对结果。
        if limit_message is None:
            if (
                remaining_tool_calls is not None
                and executed_count >= remaining_tool_calls
            ):
                limit_message = "本轮工具执行次数已达到上限"
            elif deadline is not None and monotonic() >= deadline:
                limit_message = "本轮执行时间已达到上限"

        if limit_message is not None:
            outputs.append(_budget_tool_result(tool_call.call_id, limit_message))
            yield ToolFinishedEvent(tool_call.name, tool_call.call_id, "skipped")
            if session_store is not None:
                session_store.append_transcript(
                    "tool_execution_finished",
                    {
                        "call_id": tool_call.call_id,
                        "tool_name": tool_call.name,
                        "status": "skipped",
                        "output": limit_message,
                    },
                )
            continue

        # 3. 调用单工具执行管线并累计实际进入执行阶段的调用数量。
        result = execute_tool_call(
            tool_call.name,
            tool_call.arguments,
            context=context,
            user_confirmation=user_confirmation,
        )
        if result.status in {"success", "error"}:
            executed_count += 1
        model_output = result.output
        if session_store is not None:
            try:
                persisted = session_store.persist_tool_result(
                    tool_call.call_id,
                    tool_call.name,
                    result.output,
                    description=description,
                )
                if persisted is not None:
                    context.clear_read_file_cache()
                    model_output = build_persisted_output_message(persisted)
            except OSError as error:
                context.clear_read_file_cache()
                result = ToolExecutionResult(
                    "error",
                    f"错误：工具已经执行，但完整结果持久化失败：{error}",
                    True,
                )
                model_output = result.output
        outputs.append(
            {
                "type": "function_call_output",
                "call_id": tool_call.call_id,
                "output": model_output,
            }
        )
        output_index = len(outputs) - 1
        if persisted is None:
            inline_results.append(
                (
                    output_index,
                    tool_call.call_id,
                    tool_call.name,
                    result.output,
                    description,
                )
            )
        else:
            persisted_results[output_index] = persisted
        yield ToolFinishedEvent(tool_call.name, tool_call.call_id, result.status)
        if session_store is not None:
            session_store.append_transcript(
                "tool_execution_finished",
                {
                    "call_id": tool_call.call_id,
                    "tool_name": tool_call.name,
                    "status": result.status,
                    "output": result.output if persisted is None else None,
                    "persisted_output": (
                        persisted.relative_path if persisted is not None else None
                    ),
                },
            )

        # 4. Plan 工具成功后把存储中已经生效的状态投影给界面层。
        if tool_call.name == "create_plan" and result.status == "success":
            plan = context.plan_store.get_plan()
            if plan is not None:
                yield PlanCreatedEvent(plan)
        elif tool_call.name == "update_plan" and result.status == "success":
            plan = context.plan_store.get_plan()
            if plan is not None:
                yield PlanUpdatedEvent(plan)

    # 5. 工具全部完成后执行批次总预算，保证大批量小结果不会撑爆上下文。
    try:
        _apply_batch_result_budget(
            outputs,
            inline_results,
            persisted_results,
            session_store,
            context,
        )
    except OSError as error:
        context.clear_read_file_cache()
        for output in outputs:
            output["output"] = f"错误：工具结果批次持久化失败：{error}"

    return ToolBatchResult(outputs, executed_count)
