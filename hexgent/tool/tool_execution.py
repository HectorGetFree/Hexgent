"""单个及批量工具调用的完整执行管线。"""

from __future__ import annotations

from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
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
from hexgent.session import (
    PersistedToolResult,
    SessionStore,
    build_persisted_output_message,
)
from hexgent.utils.token_estimator import estimate_value_tokens
from .tool_context import ToolCallCacheEntry, ToolExecutionContext
from .tool_registry import RegisteredTool, get_tool

if TYPE_CHECKING:
    from hexgent.events import AgentEvent


ToolExecutionStatus = Literal[
    "success",  # 工具调用已经成功执行并生成结果。
    "invalid",  # 工具不存在，或调用参数未通过解析与合法性校验。
    "denied",  # 权限管线或用户确认拒绝执行工具。
    "error",  # 工具执行期间发生异常或超时。
    "deduplicated",  # 相同工具及参数已成功执行，本次复用此前结果引用。
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

    # 去重命中时指向首次真实执行工具的调用标识。
    source_call_id: str | None = None

    # 精确去重使用的规范化工具调用指纹。
    fingerprint: str | None = None


@dataclass(frozen=True)
class ToolBatchResult:
    """完整工具批次产生的配对结果和预算计数。"""

    # 按调用顺序生成的 function_call_output 列表。
    outputs: list[dict[str, str]]

    # 本批次计入工具预算的真实执行和去重调用数量。
    budgeted_call_count: int


@dataclass(frozen=True)
class PreparedToolCall:
    """已经完成参数与权限检查、可以进入执行阶段的工具调用。"""

    # 当前调用对应的注册工具。
    tool: RegisteredTool

    # 已解析且通过 Schema 与工具专属校验的参数。
    arguments: dict[str, Any]

    # 精确去重工具使用的规范化调用指纹。
    fingerprint: str | None


@dataclass(frozen=True)
class ToolCallGroup:
    """保持模型原始顺序的一组连续工具调用。"""

    # 当前分组是否允许使用线程池并行执行。
    concurrency_safe: bool

    # 当前分组包含的连续原始工具调用。
    calls: list[Any]


@dataclass
class ScheduledToolCall:
    """一个已经完成主线程调度、等待执行或提交结果的调用。"""

    # 模型返回的原始工具调用对象。
    tool_call: Any

    # 提供给 UI 和 Transcript 的安全调用摘要。
    description: str

    # 参数权限检查成功后等待执行的调用；无需执行时为空。
    prepared: PreparedToolCall | None = None

    # 参数拒绝、去重或真实执行后得到的结果。
    result: ToolExecutionResult | None = None

    # 因工具次数或运行时间预算不足而跳过的原因。
    skipped_message: str | None = None

    # 当前并行组内相同精确调用首次使用的调用标识。
    pending_duplicate_source_call_id: str | None = None


TOOL_SCHEMAS = {tool["name"]: tool["parameters"] for tool in TOOLS}
MAX_DUPLICATE_TOOL_CALLS = 3
MAX_CONCURRENT_TOOL_CALLS = 4


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


def _allows_null(property_schema: dict[str, Any]) -> bool:
    """判断字段是否使用可空类型表达模型侧的可选参数。"""
    expected_type = property_schema.get("type")
    return expected_type == "null" or (
        isinstance(expected_type, list) and "null" in expected_type
    )


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

    properties = schema.get("properties", {})
    required = schema.get("required", [])
    missing = [
        name
        for name in required
        if name not in arguments and not _allows_null(properties.get(name, {}))
    ]
    if missing:
        return _invalid(f"缺少必填参数：{', '.join(missing)}")

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


def _build_tool_call_fingerprint(
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """根据工具名和规范化 JSON 参数构造稳定调用指纹。"""
    canonical_arguments = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload = f"{tool_name}:{canonical_arguments}".encode("utf-8")
    return sha256(payload).hexdigest()


def _deduplicated_result(
    fingerprint: str,
    cached: ToolCallCacheEntry,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    """为精确重复调用生成轻量结果，并限制同一指纹的重复次数。"""
    with context.cache_lock:
        duplicate_count = (
            context.tool_call_duplicate_counts.get(fingerprint, 0) + 1
        )
        context.tool_call_duplicate_counts[fingerprint] = duplicate_count
    if duplicate_count >= MAX_DUPLICATE_TOOL_CALLS:
        output = (
            "错误：本次 Agent Run 中相同工具和参数的重复调用已达到上限，"
            "请使用此前结果或调整执行方案"
        )
        return ToolExecutionResult(
            "deduplicated",
            output,
            True,
            cached.source_call_id,
            fingerprint,
        )
    output = (
        "<duplicate-tool-call\n"
        f'source_call_id="{cached.source_call_id}">\n'
        "相同工具和参数已经成功执行，请使用此前工具结果。\n"
        "</duplicate-tool-call>"
    )
    return ToolExecutionResult(
        "deduplicated",
        output,
        False,
        cached.source_call_id,
        fingerprint,
    )


def _partition_tool_calls(tool_calls: list[Any]) -> list[ToolCallGroup]:
    """按原始顺序合并相邻安全调用，并让每个不安全调用独占分组。"""
    groups: list[ToolCallGroup] = []
    for tool_call in tool_calls:
        tool = get_tool(tool_call.name)
        concurrency_safe = tool is not None and tool.concurrency_safe
        if concurrency_safe and groups and groups[-1].concurrency_safe:
            groups[-1].calls.append(tool_call)
            continue
        groups.append(ToolCallGroup(concurrency_safe, [tool_call]))
    return groups


def _prepare_tool_call(
    tool_name: str,
    raw_arguments: str,
    *,
    context: ToolExecutionContext,
    user_confirmation: UserConfirmation,
    call_id: str | None,
) -> PreparedToolCall | ToolExecutionResult:
    """在主线程完成工具查找、参数、权限和去重检查。"""
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

    # 4. 权限判断和用户确认始终在调度主线程串行完成。
    permission_result = _resolve_permission(
        tool_name,
        check_permission(tool_name, parsed, context),
        user_confirmation,
    )
    if permission_result is not None:
        return permission_result

    # 5. 精确去重检查此前已经完成并进入缓存的调用。
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
    # 1. 真正执行工具，并将正常输出或异常转换为统一结果。
    try:
        output = prepared.tool.execute(prepared.arguments, context)
    except Exception as error:
        return ToolExecutionResult("error", f"错误：{error}", True)

    # 2. 副作用工具使精确缓存失效；安全工具提交自身成功结果。
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
        )
    )
    if isinstance(prepared, ToolExecutionResult):
        return prepared

    # 2. 权限允许且未命中缓存后，通知调用方工具即将开始执行。
    if execution_notifier is not None:
        execution_notifier(tool_name)

    # 3. 执行已准备调用并提交工具级缓存副作用。
    return _execute_prepared_tool_call(
        prepared,
        context=context,
        call_id=call_id,
    )


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
        context.clear_tool_caches()
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


def _execute_scheduled_group(
    scheduled_calls: list[ScheduledToolCall],
    *,
    concurrency_safe: bool,
    context: ToolExecutionContext,
    user_confirmation: UserConfirmation,
) -> None:
    """执行一个连续分组，并把结果写回原顺序的调度条目。"""
    executable = [
        item
        for item in scheduled_calls
        if item.prepared is not None
        and item.pending_duplicate_source_call_id is None
    ]
    if not executable:
        return

    def execute(item: ScheduledToolCall) -> ToolExecutionResult:
        """复用单工具公开管线的执行入口，跳过已经完成的准备阶段。"""
        return execute_tool_call(
            item.tool_call.name,
            item.tool_call.arguments,
            context=context,
            user_confirmation=user_confirmation,
            call_id=item.tool_call.call_id,
            prepared_call=item.prepared,
        )

    # 1. 至少两个安全调用才创建有界线程池，单调用避免额外调度开销。
    if concurrency_safe and len(executable) > 1:
        worker_count = min(MAX_CONCURRENT_TOOL_CALLS, len(executable))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="hexgent-tool",
        ) as executor:
            futures = [executor.submit(execute, item) for item in executable]
            for item, future in zip(executable, futures):
                item.result = future.result()
        return

    # 2. 不安全工具和单个安全工具继续在调度线程内串行执行。
    for item in executable:
        item.result = execute(item)


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

    # 1. 初始化批次结果容器，并按相邻安全性划分执行分组。
    outputs: list[dict[str, str]] = []
    inline_results: list[tuple[int, str, str, str, str]] = []
    persisted_results: dict[int, PersistedToolResult] = {}
    budgeted_call_count = 0
    limit_message: str | None = None
    groups = _partition_tool_calls(tool_calls)

    for group in groups:
        # 2. 主线程依次完成事件、参数、权限、去重和预算准备。
        scheduled_calls: list[ScheduledToolCall] = []
        pending_fingerprints: dict[str, str] = {}
        reserved_budget_count = 0
        for tool_call in group.calls:
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

            if limit_message is None:
                if (
                    remaining_tool_calls is not None
                    and budgeted_call_count + reserved_budget_count
                    >= remaining_tool_calls
                ):
                    limit_message = "本轮工具执行次数已达到上限"
                elif deadline is not None and monotonic() >= deadline:
                    limit_message = "本轮执行时间已达到上限"

            scheduled = ScheduledToolCall(tool_call, description)
            if limit_message is not None:
                scheduled.skipped_message = limit_message
            else:
                prepared = _prepare_tool_call(
                    tool_call.name,
                    tool_call.arguments,
                    context=context,
                    user_confirmation=user_confirmation,
                    call_id=tool_call.call_id,
                )
                if isinstance(prepared, ToolExecutionResult):
                    scheduled.result = prepared
                    if prepared.status == "deduplicated":
                        reserved_budget_count += 1
                else:
                    if (
                        prepared.fingerprint is not None
                        and prepared.fingerprint in pending_fingerprints
                    ):
                        scheduled.prepared = prepared
                        scheduled.pending_duplicate_source_call_id = (
                            pending_fingerprints[prepared.fingerprint]
                        )
                    else:
                        scheduled.prepared = prepared
                        if prepared.fingerprint is not None:
                            pending_fingerprints[prepared.fingerprint] = (
                                tool_call.call_id
                            )
                    reserved_budget_count += 1
            scheduled_calls.append(scheduled)

        # 3. 只有当前连续安全组的已准备调用进入有界线程池。
        _execute_scheduled_group(
            scheduled_calls,
            concurrency_safe=group.concurrency_safe,
            context=context,
            user_confirmation=user_confirmation,
        )

        # 4. 首次调用完成后再生成组内重复结果，保持重复计数语义稳定。
        for scheduled in scheduled_calls:
            source_call_id = scheduled.pending_duplicate_source_call_id
            if source_call_id is None:
                continue
            assert scheduled.prepared is not None
            source = next(
                item
                for item in scheduled_calls
                if item.tool_call.call_id == source_call_id
            )
            if source.result is not None and source.result.status == "success":
                assert scheduled.prepared.fingerprint is not None
                scheduled.result = _deduplicated_result(
                    scheduled.prepared.fingerprint,
                    ToolCallCacheEntry(source_call_id, source.result.output),
                    context,
                )
            else:
                scheduled.result = execute_tool_call(
                    scheduled.tool_call.name,
                    scheduled.tool_call.arguments,
                    context=context,
                    user_confirmation=user_confirmation,
                    call_id=scheduled.tool_call.call_id,
                    prepared_call=scheduled.prepared,
                )

        # 5. 回到主线程按模型原始顺序持久化、发事件并构造配对结果。
        for scheduled in scheduled_calls:
            tool_call = scheduled.tool_call
            if scheduled.skipped_message is not None:
                outputs.append(
                    _budget_tool_result(
                        tool_call.call_id,
                        scheduled.skipped_message,
                    )
                )
                yield ToolFinishedEvent(
                    tool_call.name,
                    tool_call.call_id,
                    "skipped",
                )
                if session_store is not None:
                    session_store.append_transcript(
                        "tool_execution_finished",
                        {
                            "call_id": tool_call.call_id,
                            "tool_name": tool_call.name,
                            "status": "skipped",
                            "output": scheduled.skipped_message,
                        },
                    )
                continue

            result = scheduled.result
            if result is None:
                raise RuntimeError("工具调度完成后缺少执行结果")
            if result.status in {"success", "error", "deduplicated"}:
                budgeted_call_count += 1

            persisted: PersistedToolResult | None = None
            model_output = result.output
            if session_store is not None:
                try:
                    persisted = session_store.persist_tool_result(
                        tool_call.call_id,
                        tool_call.name,
                        result.output,
                        description=scheduled.description,
                    )
                    if persisted is not None:
                        context.clear_tool_caches()
                        model_output = build_persisted_output_message(persisted)
                except OSError as error:
                    context.clear_tool_caches()
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
                        scheduled.description,
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
                        "source_call_id": result.source_call_id,
                        "fingerprint": result.fingerprint,
                    },
                )

            # 6. Plan 工具串行执行后投影已经生效的最新状态。
            if tool_call.name == "create_plan" and result.status == "success":
                plan = context.plan_store.get_plan()
                if plan is not None:
                    if session_store is not None:
                        session_store.append_transcript("plan_created", asdict(plan))
                    yield PlanCreatedEvent(plan)
            elif tool_call.name == "update_plan" and result.status == "success":
                plan = context.plan_store.get_plan()
                if plan is not None:
                    if session_store is not None:
                        session_store.append_transcript("plan_updated", asdict(plan))
                    yield PlanUpdatedEvent(plan)

    # 7. 工具全部完成后执行批次总预算，保证大批量小结果不会撑爆上下文。
    try:
        _apply_batch_result_budget(
            outputs,
            inline_results,
            persisted_results,
            session_store,
            context,
        )
    except OSError as error:
        context.clear_tool_caches()
        for output in outputs:
            output["output"] = f"错误：工具结果批次持久化失败：{error}"

    return ToolBatchResult(outputs, budgeted_call_count)
