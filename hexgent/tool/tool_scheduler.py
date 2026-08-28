"""工具批次的分组、并行调度、顺序提交与结果持久化。"""

from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError, as_completed
from dataclasses import asdict, dataclass, replace
from time import monotonic
from typing import TYPE_CHECKING, Any

from hexgent.session import (
    PersistedToolResult,
    SessionStore,
    build_persisted_output_message,
)
from hexgent.utils.token_estimator import estimate_value_tokens

from .tool_context import ToolCallCacheEntry, ToolExecutionContext
from .tool_execution import (
    PreparedToolCall,
    ToolExecutionResult,
    UserConfirmation,
    _deduplicated_result,
    _prepare_tool_call,
)
from .tool_registry import get_tool
from .tool_result import ToolFailure
from .tool_retry import ToolRetryLimits
from .tool_permission_pipeline import PermissionContext

if TYPE_CHECKING:
    from hexgent.events import AgentEvent


@dataclass(frozen=True)
class ToolParallelLimits:
    """并行工具批次使用的运行配置。"""

    # 是否允许声明为并行安全的相邻工具并发执行。
    enabled: bool = True

    # 单个并行批次最多同时运行的工具数量。
    max_workers: int = 4

    # 单个并行批次从提交开始允许等待的最长秒数。
    batch_timeout_seconds: float = 60.0


@dataclass(frozen=True)
class ToolBatchResult:
    """完整工具批次产生的配对结果和预算计数。"""

    # 按模型调用顺序生成的 function_call_output 列表。
    outputs: list[dict[str, str]]

    # 本批次计入工具预算的真实执行和去重调用数量。
    budgeted_call_count: int


@dataclass(frozen=True)
class ToolCallGroup:
    """保持模型原始顺序的一组连续工具调用。"""

    # 当前分组是否允许使用线程池并行执行。
    concurrency_safe: bool

    # 当前分组包含的连续原始工具调用。
    calls: list[Any]


@dataclass
class ScheduledToolCall:
    """已经完成主线程调度、等待执行或提交结果的调用。"""

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


def partition_tool_calls(tool_calls: list[Any]) -> list[ToolCallGroup]:
    """合并相邻安全调用，并让每个不安全调用形成顺序屏障。"""
    groups: list[ToolCallGroup] = []
    for tool_call in tool_calls:
        tool = get_tool(tool_call.name)
        concurrency_safe = tool is not None and tool.concurrency_safe
        if concurrency_safe and groups and groups[-1].concurrency_safe:
            groups[-1].calls.append(tool_call)
            continue
        groups.append(ToolCallGroup(concurrency_safe, [tool_call]))
    return groups


def _budget_tool_result(call_id: str, message: str) -> dict[str, str]:
    """为因预算不足而未执行的工具调用创建配对结果。"""
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": f"错误：{message}，本次工具调用未执行",
    }


def _truncate_to_token_budget(text: str, token_budget: int) -> str:
    """在估算 Token 预算内保留文本开头。"""
    if token_budget <= 0:
        return ""
    if estimate_value_tokens(text) <= token_budget:
        return text
    low, high = 0, len(text)
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
    """从最大结果开始外置，并限制全部外置预览的总量。"""
    if session_store is None:
        return
    limits = session_store.tool_result_limits

    # 1. 超过批次内联预算时持续外置当前最大的完整结果。
    remaining = list(inline_results)
    total_inline_tokens = sum(
        estimate_value_tokens(output) for _, _, _, output, _ in remaining
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

    # 2. 在外置结果之间平均分配预览预算，并更新模型可见结果。
    if not persisted_results:
        return
    preview_budget = max(
        1, limits.max_batch_preview_tokens // len(persisted_results)
    )
    for output_index, persisted in persisted_results.items():
        preview = _truncate_to_token_budget(persisted.preview, preview_budget)
        outputs[output_index]["output"] = build_persisted_output_message(
            replace(persisted, preview=preview)
        )


def _run_scheduled_call(
    item: ScheduledToolCall,
    context: ToolExecutionContext,
    user_confirmation: UserConfirmation,
    retry_limits: ToolRetryLimits,
    permission_context: PermissionContext | None,
) -> ToolExecutionResult:
    """执行一个已经完成主线程准备的工具调用。"""
    # 惰性取用执行函数，既避免模块循环，也保留测试替换执行入口的能力。
    from . import tool_execution

    return tool_execution.execute_tool_call(
        item.tool_call.name,
        item.tool_call.arguments,
        context=context,
        user_confirmation=user_confirmation,
        call_id=item.tool_call.call_id,
        prepared_call=item.prepared,
        retry_limits=retry_limits,
        permission_context=permission_context,
    )


def _execute_scheduled_group(
    scheduled_calls: list[ScheduledToolCall],
    *,
    concurrency_safe: bool,
    context: ToolExecutionContext,
    user_confirmation: UserConfirmation,
    limits: ToolParallelLimits,
    retry_limits: ToolRetryLimits,
    permission_context: PermissionContext | None,
) -> Generator[AgentEvent, None, tuple[bool, float]]:
    """执行连续分组，并立即发送并行工具的真实完成事件。"""
    from hexgent.events import ToolExecutionCompletedEvent

    executable = [
        item
        for item in scheduled_calls
        if item.prepared is not None
        and item.pending_duplicate_source_call_id is None
    ]
    if not executable:
        return False, 0.0

    started_at = monotonic()
    run_in_parallel = limits.enabled and concurrency_safe and len(executable) > 1
    if not run_in_parallel:
        for item in executable:
            item.result = _run_scheduled_call(
                item,
                context,
                user_confirmation,
                retry_limits,
                permission_context,
            )
        return False, monotonic() - started_at

    # 1. 将安全调用提交到有界线程池，权限确认已经在主线程完成。
    worker_count = min(limits.max_workers, len(executable))
    executor = ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="hexgent-tool",
    )
    future_to_item: dict[Future[ToolExecutionResult], ScheduledToolCall] = {
        executor.submit(
            _run_scheduled_call,
            item,
            context,
            user_confirmation,
            retry_limits,
            permission_context,
        ): item
        for item in executable
    }

    # 2. 按真实完成顺序取得 Future，立即向 UI 报告执行阶段结果。
    timed_out = False
    try:
        for future in as_completed(
            future_to_item,
            timeout=limits.batch_timeout_seconds,
        ):
            item = future_to_item[future]
            try:
                item.result = future.result()
            except Exception as error:
                item.result = ToolExecutionResult(
                    "error",
                    f"错误：并行工具执行失败：{error}",
                    True,
                    failure=ToolFailure(
                        "tool_internal_error",
                        "scheduling",
                        f"并行工具执行失败：{error}",
                    ),
                )
            yield ToolExecutionCompletedEvent(
                item.tool_call.name,
                item.tool_call.call_id,
                item.result.status,
            )
    except TimeoutError:
        timed_out = True

    # 3. 超时调用按原顺序获得配对错误；已运行线程不再阻塞等待。
    if timed_out:
        for future, item in future_to_item.items():
            if item.result is not None:
                continue
            item.result = ToolExecutionResult(
                "error",
                f"错误：并行工具批次超过 {limits.batch_timeout_seconds:g} 秒限制",
                True,
                failure=ToolFailure(
                    "tool_timeout",
                    "scheduling",
                    f"并行工具批次超过 {limits.batch_timeout_seconds:g} 秒限制",
                ),
            )
            future.cancel()
            yield ToolExecutionCompletedEvent(
                item.tool_call.name,
                item.tool_call.call_id,
                item.result.status,
            )
    executor.shutdown(wait=not timed_out, cancel_futures=timed_out)
    return True, monotonic() - started_at


def _status_counts(
    scheduled_calls: list[ScheduledToolCall],
) -> tuple[int, int, int]:
    """统计并行批次的成功、失败和未执行数量。"""
    succeeded = sum(
        item.result is not None
        and item.result.status in {"success", "deduplicated"}
        and not item.result.is_error
        for item in scheduled_calls
    )
    skipped = sum(item.skipped_message is not None for item in scheduled_calls)
    failed = len(scheduled_calls) - succeeded - skipped
    return succeeded, failed, skipped


def execute_tool_batch(
    tool_calls: list[Any],
    *,
    context: ToolExecutionContext,
    user_confirmation: UserConfirmation,
    remaining_tool_calls: int | None,
    deadline: float | None,
    session_store: SessionStore | None = None,
    parallel_limits: ToolParallelLimits | None = None,
    retry_limits: ToolRetryLimits | None = None,
    permission_context: PermissionContext | None = None,
) -> Generator[AgentEvent, None, ToolBatchResult]:
    """调度完整工具批次，并保证每个调用都有配对结果。"""
    from hexgent.events import (
        PlanCreatedEvent,
        PlanUpdatedEvent,
        ToolBatchFinishedEvent,
        ToolBatchStartedEvent,
        ToolFinishedEvent,
        ToolStartedEvent,
    )

    # 1. 初始化批次状态，并按相邻安全性划分执行分组。
    limits = parallel_limits or ToolParallelLimits()
    active_retry_limits = retry_limits or ToolRetryLimits()
    outputs: list[dict[str, str]] = []
    inline_results: list[tuple[int, str, str, str, str]] = []
    persisted_results: dict[int, PersistedToolResult] = {}
    budgeted_call_count = 0
    limit_message: str | None = None

    for batch_index, group in enumerate(partition_tool_calls(tool_calls), start=1):
        scheduled_calls: list[ScheduledToolCall] = []
        pending_fingerprints: dict[str, str] = {}
        reserved_budget_count = 0

        # 2. 主线程依次完成事件、参数、权限、去重和预算准备。
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
                    permission_context=permission_context,
                    permission_event_recorder=(
                        session_store.append_transcript
                        if session_store is not None
                        else None
                    ),
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

        # 3. 仅为至少两个可执行的安全调用发送并行批次事件。
        executable_count = sum(
            item.prepared is not None
            and item.pending_duplicate_source_call_id is None
            for item in scheduled_calls
        )
        is_parallel = limits.enabled and group.concurrency_safe and executable_count > 1
        batch_id = f"tool-batch-{batch_index}"
        if is_parallel:
            yield ToolBatchStartedEvent(
                batch_id,
                executable_count,
                min(limits.max_workers, executable_count),
            )
        _, elapsed_seconds = yield from _execute_scheduled_group(
            scheduled_calls,
            concurrency_safe=group.concurrency_safe,
            context=context,
            user_confirmation=user_confirmation,
            limits=limits,
            retry_limits=active_retry_limits,
            permission_context=permission_context,
        )

        # 4. 首次调用成功后生成组内重复结果；失败时重新真实执行。
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
                scheduled.result = _run_scheduled_call(
                    scheduled,
                    context,
                    user_confirmation,
                    active_retry_limits,
                    permission_context,
                )

        # 5. 按模型原始顺序持久化、发事件并构造配对结果。
        for scheduled in scheduled_calls:
            tool_call = scheduled.tool_call
            if scheduled.skipped_message is not None:
                outputs.append(
                    _budget_tool_result(
                        tool_call.call_id, scheduled.skipped_message
                    )
                )
                yield ToolFinishedEvent(
                    tool_call.name, tool_call.call_id, "skipped"
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
                        failure=ToolFailure(
                            "output_persistence_failed",
                            "persistence",
                            f"工具已经执行，但完整结果持久化失败：{error}",
                            side_effect_state="committed",
                        ),
                    )
                    model_output = result.output
                    scheduled.result = result

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
            yield ToolFinishedEvent(
                tool_call.name, tool_call.call_id, result.status
            )
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
                        "failure": (
                            asdict(result.failure)
                            if result.failure is not None
                            else None
                        ),
                        "attempt_count": result.attempt_count,
                        "data": result.data,
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

        # 7. 所有单工具结束事件提交后，再发送并行批次汇总事件。
        if is_parallel:
            succeeded, failed, skipped = _status_counts(scheduled_calls)
            yield ToolBatchFinishedEvent(
                batch_id,
                len(scheduled_calls),
                succeeded,
                failed,
                skipped,
                elapsed_seconds,
            )

    # 8. 批次全部完成后执行总结果预算，防止大量小结果撑爆上下文。
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
