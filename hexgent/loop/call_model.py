"""模型流式调用的协议处理。"""

from collections.abc import Callable, Generator
from typing import Any, Literal
from urllib.parse import urlsplit
from hashlib import sha256
from contextlib import nullcontext

from hexgent.events import AgentEvent, TextDeltaEvent
from hexgent.run_control import InterruptController
from hexgent.observation import begin_span, end_span


ModelCallErrorReason = Literal[
    "prompt_too_long",  # 模型拒绝接收超过上下文窗口的输入。
    "response_failed",  # 流式响应以失败事件结束。
    "response_incomplete",  # 流式响应未完整结束，不能使用残缺结果。
    "stream_error",  # 创建或消费流时发生其他异常。
    "missing_completed",  # 流结束时没有收到完整响应事件。
    "PINNED_CONSTRAINTS_LIMIT_EXCEEDED",
    "CONTEXT_BUDGET_EXCEEDED",
]


class ModelCallError(RuntimeError):
    """模型调用没有产生可安全处理的完整响应。"""

    def __init__(self, message: str, reason: ModelCallErrorReason) -> None:
        super().__init__(message)

        # 供 Agent Loop 判断错误能否通过上下文压缩恢复。
        self.reason = reason


class ModelCallInterrupted(RuntimeError):
    """模型流在产生完整 Response 之前收到当前 Run 中断请求。"""

    def __init__(self) -> None:
        super().__init__("模型流式响应已在业务控制请求下中断")


def _error_details(error: Any) -> str:
    """提取 SDK 异常或响应错误中的稳定诊断信息。"""
    values = [
        getattr(error, "code", None),
        getattr(error, "type", None),
        getattr(error, "message", None),
        str(error) if error is not None else None,
    ]
    return " ".join(str(value) for value in values if value)


def _is_prompt_too_long(error: Any) -> bool:
    """识别不同兼容服务返回的上下文过长错误。"""
    details = _error_details(error).lower()
    markers = (
        "prompt_too_long",
        "context_length_exceeded",
        "maximum context length",
        "context window",
        "input is too long",
        "request too large",
    )
    return any(marker in details for marker in markers)


def _model_call_error(message: str, error: Any) -> ModelCallError:
    """把底层异常转换为带可恢复原因的统一模型错误。"""
    reason: ModelCallErrorReason = (
        "prompt_too_long" if _is_prompt_too_long(error) else "stream_error"
    )
    return ModelCallError(message, reason)


def _is_retryable_empty_stream_error(error: Any) -> bool:
    """仅识别尚未产生输出时可安全重试的传输层断流。"""
    details = _error_details(error).lower()
    return any(marker in details for marker in (
        "incomplete chunked read",
        "peer closed connection",
        "server disconnected",
        "connection reset",
        "connection aborted",
    ))


def call_model(
    client: Any,
    interrupt_controller: InterruptController | None = None,
    observation_request_type: str = "MAIN_AGENT",
    observation_memory_receipt: tuple[dict[str, str], ...] | None = None,
    memory_store: Any = None,
    prepare_request: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    **request: Any,
) -> Generator[AgentEvent, None, Any]:
    """调用模型、发送文本增量，并返回协议完整的响应。"""
    # 1. 只有未输出任何文本且未收到 completed 的空流，才可安全重试一次。
    #    此时没有完整 Response、更没有工具调用，重复请求不会造成外部副作用或界面文字重复。
    for attempt in range(2):
        stream = None
        completed_response = None
        remove_interrupt_callback = None
        emitted_text = False
        observed_status = "INCOMPLETE"
        observed_error = None
        observed = begin_span("provider", request_type=observation_request_type,
                              model=request.get("model"), attempt=attempt + 1,
                              provider_host=urlsplit(str(getattr(client, "base_url", ""))).hostname)
        try:
            if interrupt_controller is not None and interrupt_controller.interrupted:
                raise ModelCallInterrupted()
            # 2. 每次实际请求（包括重试）复核权威状态，提交撤销与启动请求互斥。
            # 2. 空流重试也必须从当前 Session 状态重新组装 Record 区块。
            prepared_request = prepare_request(request) if prepare_request is not None else request
            guard = memory_store.request_guard(prepared_request) if memory_store is not None else nullcontext((prepared_request, observation_memory_receipt))
            with guard as (current_request, current_receipt):
                if observed[0] is not None and current_receipt is not None:
                    observed[0].emit(
                        "memory_injected", span_id=observed[2],
                        request_type=observation_request_type,
                        memories=list(current_receipt),
                        instructions_sha256=sha256(str(current_request.get("instructions", "")).encode()).hexdigest(),
                    )
                stream = client.responses.create(**current_request, stream=True)
            close = getattr(stream, "close", None)
            if interrupt_controller is not None and callable(close):
                remove_interrupt_callback = interrupt_controller.add_callback(close)
            for event in stream:
                if interrupt_controller is not None and interrupt_controller.interrupted:
                    raise ModelCallInterrupted()
                if event.type == "response.output_text.delta":
                    emitted_text = True
                    yield TextDeltaEvent(event.delta)
                elif event.type == "response.completed":
                    completed_response = event.response
                    observed_status = "SUCCEEDED"
                elif event.type == "response.failed":
                    response = getattr(event, "response", None)
                    error = getattr(event, "error", None) or getattr(response, "error", None)
                    message = getattr(error, "message", None) or error or "未知错误"
                    reason: ModelCallErrorReason = (
                        "prompt_too_long"
                        if _is_prompt_too_long(error)
                        else "response_failed"
                    )
                    raise ModelCallError(f"模型流式响应失败：{message}", reason)
                elif event.type == "response.incomplete":
                    response = getattr(event, "response", None)
                    details = getattr(response, "incomplete_details", None)
                    reason = getattr(details, "reason", None)
                    suffix = f"：{reason}" if reason else ""
                    raise ModelCallError(
                        f"模型流式响应未完整结束{suffix}，本轮无法安全继续",
                        "response_incomplete",
                    )
                elif event.type == "error":
                    message = getattr(event, "message", None) or "未知错误"
                    raise _model_call_error(f"模型流式响应错误：{message}", event)
        except (ModelCallError, ModelCallInterrupted) as error:
            observed_status = "INTERRUPTED" if isinstance(error, ModelCallInterrupted) else "FAILED"
            observed_error = getattr(error, "reason", type(error).__name__)
            raise
        except Exception as error:
            observed_status = "FAILED"
            observed_error = type(error).__name__
            if interrupt_controller is not None and interrupt_controller.interrupted:
                observed_status = "INTERRUPTED"
                raise ModelCallInterrupted() from error
            # 尚未产生文本或工具调用时，连接中断不会造成重复副作用；允许一次完整重试。
            if attempt == 0 and not emitted_text and _is_retryable_empty_stream_error(error):
                continue
            raise _model_call_error(f"模型流式响应错误：{error}", error) from error
        finally:
            end_span(observed, status=observed_status, response=completed_response,
                     failure_reason=observed_error)
            if remove_interrupt_callback is not None:
                remove_interrupt_callback()
            close = getattr(stream, "close", None)
            if callable(close):
                close()

        # 2. 只有 completed 事件中的完整 Response 可以进入后续工具循环。
        if interrupt_controller is not None and interrupt_controller.interrupted:
            raise ModelCallInterrupted()
        if completed_response is not None:
            return completed_response
        if attempt == 0 and not emitted_text:
            continue
        raise ModelCallError(
            "模型流式响应结束，但没有收到 response.completed 事件",
            "missing_completed",
        )

    raise AssertionError("模型流重试循环未按预期结束")
