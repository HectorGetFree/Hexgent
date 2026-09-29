"""单个工具调用的准备、权限校验和执行管线。"""

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import asdict, dataclass
import inspect
from time import sleep
from typing import TYPE_CHECKING, Any

from hexgent.tool.tool_permission_pipeline import (
    PermissionContext,
    PermissionEvaluation,
    UserConfirmationResult,
    evaluate_permission,
)
from hexgent.session import SessionStore
from .tool_context import (
    ToolCallCacheEntry,
    ToolExecutionContext,
    build_deduplicated_output,
    build_tool_call_fingerprint,
)
from .tool_registry import RegisteredTool, get_tool
from .tool_result import (
    ToolFailure,
    ToolHandlerResult,
    ToolExecutionResult,
    ToolExecutionStatus,
    build_tool_failure_result as _failure_result,
)
from .tool_retry import (
    ToolRetryLimits,
    retry_backoff_seconds,
    should_retry_tool_failure,
)
from .tool_validation import parse_and_validate_tool_arguments

if TYPE_CHECKING:
    from hexgent.events import AgentEvent
    from hexgent.run_control import InterruptController


UserConfirmation = Callable[[str, str, str | None], UserConfirmationResult]
ToolExecutionNotifier = Callable[[str], None]
PermissionEventRecorder = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class PreparedToolCall:
    """已经完成参数与权限检查、可以进入执行阶段的工具调用。"""

    # 当前调用对应的注册工具。
    tool: RegisteredTool

    # 已解析且通过 Schema 与工具专属校验的参数。
    arguments: dict[str, Any]

    # 精确去重工具使用的规范化调用指纹。
    fingerprint: str | None


def unavailable_user_confirmation(
    _tool_name: str,
    _message: str,
    _call_id: str | None = None,
) -> UserConfirmationResult:
    """在调用方没有提供界面确认能力时安全地停止执行。"""
    return "unavailable"


def _resolve_permission(
    tool_name: str,
    decision: PermissionEvaluation,
    user_confirmation: UserConfirmation,
    *,
    call_id: str | None = None,
    permission_event_recorder: PermissionEventRecorder | None = None,
) -> ToolExecutionResult | None:
    """处理 deny 和 ask，将允许决策继续传向执行阶段。"""
    # 1. 保存规则与 Mode 形成的原始评估，审批结果不能覆盖评估依据。
    if permission_event_recorder is not None:
        permission_event_recorder(
            "permission_evaluated",
            {
                "call_id": call_id,
                "tool_name": tool_name,
                **asdict(decision),
            },
        )

    if decision.behavior == "deny":
        message = decision.message or "工具调用已被拒绝"
        if permission_event_recorder is not None:
            permission_event_recorder(
                "permission_resolved",
                {
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "behavior": "deny",
                    "resolution": "policy_denied",
                },
            )
        return _failure_result(
            "denied",
            "permission_denied",
            "permission",
            message.removeprefix("错误："),
            side_effect_state="not_started",
        )

    if decision.behavior == "ask":
        message = decision.message or "本次工具调用需要用户确认"
        try:
            accepts_call_id = len(inspect.signature(user_confirmation).parameters) >= 3
        except (TypeError, ValueError):
            accepts_call_id = True
        confirmation = (
            user_confirmation(tool_name, message, call_id)
            if accepts_call_id
            else user_confirmation(tool_name, message)  # type: ignore[call-arg]
        )
        if permission_event_recorder is not None:
            resolution_by_confirmation = {
                "allow": "user_allowed",
                "deny": "user_denied",
                "unavailable": "confirmation_unavailable",
            }
            permission_event_recorder(
                "permission_resolved",
                {
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "behavior": "allow" if confirmation == "allow" else "deny",
                    "resolution": resolution_by_confirmation[confirmation],
                },
            )
        if confirmation == "deny":
            return _failure_result(
                "denied",
                "user_denied",
                "permission",
                f"用户拒绝本次操作：{message}",
                side_effect_state="not_started",
            )
        if confirmation == "unavailable":
            return _failure_result(
                "error",
                "confirmation_unavailable",
                "permission",
                "当前输入环境无法读取权限确认，本次工具调用未执行",
                side_effect_state="not_started",
            )
    elif permission_event_recorder is not None:
        permission_event_recorder(
            "permission_resolved",
            {
                "call_id": call_id,
                "tool_name": tool_name,
                "behavior": "allow",
                "resolution": "policy_allowed",
            },
        )
    return None


def _build_tool_call_fingerprint(
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """根据工具名和规范化 JSON 参数构造稳定调用指纹。"""
    return build_tool_call_fingerprint(tool_name, arguments)


def _deduplicated_result(
    fingerprint: str,
    cached: ToolCallCacheEntry,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    """为精确重复调用生成轻量结果，并限制同一指纹的重复次数。"""
    output, is_error = build_deduplicated_output(fingerprint, cached, context)
    failure = (
        ToolFailure(
            "duplicate_call_limit",
            "scheduling",
            output.removeprefix("错误："),
        )
        if is_error
        else None
    )
    return ToolExecutionResult(
        "deduplicated",
        output,
        is_error,
        cached.source_call_id,
        fingerprint,
        failure,
    )


def _prepare_tool_call(
    tool_name: str,
    raw_arguments: str,
    *,
    context: ToolExecutionContext,
    user_confirmation: UserConfirmation,
    call_id: str | None,
    permission_context: PermissionContext | None = None,
    permission_event_recorder: PermissionEventRecorder | None = None,
) -> PreparedToolCall | ToolExecutionResult:
    """在主线程完成工具查找、参数、权限和去重检查。"""
    # 1. 校验工具是否存在，并取得对应的工具函数。
    tool = get_tool(tool_name)
    if tool is None:
        return _failure_result(
            "invalid",
            "unknown_tool",
            "lookup",
            f"未知工具“{tool_name}”",
            side_effect_state="not_started",
        )

    # 2. 将模型生成的原始参数解析为 JSON 对象。
    parsed = parse_and_validate_tool_arguments(
        tool_name,
        tool,
        raw_arguments,
        context,
    )
    if isinstance(parsed, ToolExecutionResult):
        return parsed

    # 3. 权限判断和用户确认始终在调度主线程串行完成。
    permission_result = _resolve_permission(
        tool_name,
        evaluate_permission(
            tool_name,
            tool,
            parsed,
            context,
            permission_context,
        ),
        user_confirmation,
        call_id=call_id,
        permission_event_recorder=permission_event_recorder,
    )
    if permission_result is not None:
        return permission_result

    # 4. 精确去重检查此前已经完成并进入缓存的调用。
    fingerprint: str | None = None
    if tool.deduplication_policy == "exact" and call_id is not None:
        fingerprint = _build_tool_call_fingerprint(tool_name, parsed)
        with context.cache_lock:
            cached = context.tool_call_cache.get(fingerprint)
        if cached is not None:
            return _deduplicated_result(fingerprint, cached, context)

    return PreparedToolCall(tool, parsed, fingerprint)


def _execute_prepared_tool_call(
    prepared: PreparedToolCall,
    *,
    context: ToolExecutionContext,
    call_id: str | None,
) -> ToolExecutionResult:
    """只执行已经允许的工具函数，并提交工具级缓存副作用。"""
    # 1. 真正执行工具，并把基础设施异常转换为结构化失败。
    try:
        handler_result = prepared.tool.execute(prepared.arguments, context)
    except FileNotFoundError as error:
        return _failure_result(
            "error",
            "resource_not_found",
            "execution",
            f"资源不存在：{error.filename or error}",
        )
    except OSError as error:
        return _failure_result(
            "error",
            "external_dependency_failed",
            "execution",
            str(error),
            retryable=True,
        )
    except Exception as error:
        return _failure_result(
            "error",
            "tool_internal_error",
            "execution",
            str(error),
        )

    # 2. 严格区分处理器协议错误、业务失败和业务成功。
    if not isinstance(handler_result, ToolHandlerResult):
        return _failure_result(
            "error",
            "invalid_tool_output",
            "output_validation",
            "工具返回值必须是 ToolHandlerResult",
        )
    if not handler_result.ok:
        if handler_result.failure is None:
            return _failure_result(
                "error",
                "invalid_tool_output",
                "output_validation",
                "工具失败结果缺少结构化 failure",
            )
        return ToolExecutionResult(
            "error",
            handler_result.output,
            True,
            failure=handler_result.failure,
        )
    if handler_result.failure is not None:
        return _failure_result(
            "error",
            "invalid_tool_output",
            "output_validation",
            "工具成功结果不能携带 failure",
        )
    output_validation_error = prepared.tool.validate_output(handler_result)
    if output_validation_error is not None:
        return _failure_result(
            "error",
            "invalid_tool_output",
            "output_validation",
            output_validation_error,
        )
    output = handler_result.output

    # 3. 副作用工具使精确缓存失效；安全工具提交自身成功结果。
    if prepared.tool.invalidates_tool_call_cache:
        context.clear_tool_call_cache()
    if prepared.fingerprint is not None and call_id is not None:
        with context.cache_lock:
            context.tool_call_cache[prepared.fingerprint] = ToolCallCacheEntry(
                call_id,
                output,
            )
            context.tool_call_duplicate_counts[prepared.fingerprint] = 0
    return ToolExecutionResult(
        "success",
        output,
        False,
        fingerprint=prepared.fingerprint,
        data=handler_result.data,
    )


def execute_tool_call(
    tool_name: str,
    raw_arguments: str,
    *,  # 表示后面的参数只能用参数名传递
    context: ToolExecutionContext,
    user_confirmation: UserConfirmation = unavailable_user_confirmation,
    execution_notifier: ToolExecutionNotifier | None = None,
    call_id: str | None = None,
    prepared_call: PreparedToolCall | None = None,
    retry_limits: ToolRetryLimits | None = None,
    permission_context: PermissionContext | None = None,
    permission_event_recorder: PermissionEventRecorder | None = None,
) -> ToolExecutionResult:
    """完成工具查找、参数校验、权限处理和真正执行。"""
    # 1. 普通调用现场完成准备；批次调用复用主线程已经完成的准备结果。
    prepared: PreparedToolCall | ToolExecutionResult = prepared_call or (
        _prepare_tool_call(
            tool_name,
            raw_arguments,
            context=context,
            user_confirmation=user_confirmation,
            call_id=call_id,
            permission_context=permission_context,
            permission_event_recorder=permission_event_recorder,
        )
    )
    if isinstance(prepared, ToolExecutionResult):
        return prepared

    # 2. 权限允许且未命中缓存后，通知调用方工具即将开始执行。
    if execution_notifier is not None:
        execution_notifier(tool_name)

    # 3. 按稳定失败元数据、安全声明和副作用状态决定是否自动重试。
    active_retry_limits = retry_limits or ToolRetryLimits()
    attempt_count = 0
    while True:
        attempt_count += 1
        result = _execute_prepared_tool_call(
            prepared,
            context=context,
            call_id=call_id,
        )
        result = ToolExecutionResult(
            result.status,
            result.output,
            result.is_error,
            result.source_call_id,
            result.fingerprint,
            result.failure,
            attempt_count,
            result.data,
        )
        if not should_retry_tool_failure(
            result.failure,
            automatic_retry_safe=prepared.tool.automatic_retry_safe,
            attempt_count=attempt_count,
            limits=active_retry_limits,
        ):
            return result

        # 4. 有界退避发生在线程工作单元内部，不阻塞其他并行安全工具。
        backoff_seconds = retry_backoff_seconds(
            attempt_count,
            active_retry_limits,
        )
        if result.failure is not None:
            suggested_delay = result.failure.retry_after_seconds
            if suggested_delay is not None:
                backoff_seconds = max(backoff_seconds, suggested_delay)
        sleep(backoff_seconds)


def execute_tool_batch(
    tool_calls: list[Any],
    *,
    context: ToolExecutionContext,
    user_confirmation: UserConfirmation,
    remaining_tool_calls: int | None,
    deadline: float | None,
    session_store: SessionStore | None = None,
    parallel_limits: Any = None,
    retry_limits: ToolRetryLimits | None = None,
    permission_context: PermissionContext | None = None,
    interrupt_controller: InterruptController | None = None,
) -> Generator[AgentEvent, None, Any]:
    """兼容旧导入路径，并在调用时惰性委托给批次调度器。"""
    from .tool_scheduler import execute_tool_batch as schedule_tool_batch

    return (
        yield from schedule_tool_batch(
            tool_calls,
            context=context,
            user_confirmation=user_confirmation,
            remaining_tool_calls=remaining_tool_calls,
            deadline=deadline,
            session_store=session_store,
            parallel_limits=parallel_limits,
            retry_limits=retry_limits,
            permission_context=permission_context,
            interrupt_controller=interrupt_controller,
        )
    )


def _partition_tool_calls(tool_calls: list[Any]) -> list[Any]:
    """兼容旧测试入口，实际分组逻辑由调度器维护。"""
    from .tool_scheduler import partition_tool_calls

    return partition_tool_calls(tool_calls)
