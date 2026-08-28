"""模型流式调用的协议处理。"""

from collections.abc import Generator
from typing import Any, Literal

from hexgent.events import AgentEvent, TextDeltaEvent
from .interrupt import InterruptController, InterruptReason


ModelCallErrorReason = Literal[
    "prompt_too_long",  # 模型拒绝接收超过上下文窗口的输入。
    "response_failed",  # 流式响应以失败事件结束。
    "response_incomplete",  # 流式响应未完整结束，不能使用残缺结果。
    "stream_error",  # 创建或消费流时发生其他异常。
    "missing_completed",  # 流结束时没有收到完整响应事件。
]


class ModelCallError(RuntimeError):
    """模型调用没有产生可安全处理的完整响应。"""

    def __init__(self, message: str, reason: ModelCallErrorReason) -> None:
        super().__init__(message)

        # 供 Agent Loop 判断错误能否通过上下文压缩恢复。
        self.reason = reason


class ModelCallInterrupted(RuntimeError):
    """模型流在产生完整 Response 之前收到当前 Run 中断请求。"""

    def __init__(self, reason: InterruptReason) -> None:
        super().__init__("模型流式响应已被中断")

        # 供 Agent Turn 形成明确的中断终态。
        self.reason = reason


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


def call_model(
    client: Any,
    interrupt_controller: InterruptController | None = None,
    **request: Any,
) -> Generator[AgentEvent, None, Any]:
    """调用模型、发送文本增量，并返回协议完整的响应。"""
    # 1. 发起流式请求，只把文本增量立即交给界面层。
    stream = None
    completed_response = None
    remove_interrupt_callback = None
    try:
        if interrupt_controller is not None and interrupt_controller.interrupted:
            assert interrupt_controller.reason is not None
            raise ModelCallInterrupted(interrupt_controller.reason)
        stream = client.responses.create(**request, stream=True)
        close = getattr(stream, "close", None)
        if interrupt_controller is not None and callable(close):
            remove_interrupt_callback = interrupt_controller.add_callback(close)
        for event in stream:
            if interrupt_controller is not None and interrupt_controller.interrupted:
                assert interrupt_controller.reason is not None
                raise ModelCallInterrupted(interrupt_controller.reason)
            if event.type == "response.output_text.delta":
                yield TextDeltaEvent(event.delta)
            elif event.type == "response.completed":
                completed_response = event.response
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
    except (ModelCallError, ModelCallInterrupted):
        raise
    except Exception as error:
        if interrupt_controller is not None and interrupt_controller.interrupted:
            assert interrupt_controller.reason is not None
            raise ModelCallInterrupted(interrupt_controller.reason) from error
        raise _model_call_error(f"模型流式响应错误：{error}", error) from error
    finally:
        if remove_interrupt_callback is not None:
            remove_interrupt_callback()
        close = getattr(stream, "close", None)
        if callable(close):
            close()

    # 2. 只有 completed 事件中的完整 Response 可以进入后续工具循环。
    if interrupt_controller is not None and interrupt_controller.interrupted:
        assert interrupt_controller.reason is not None
        raise ModelCallInterrupted(interrupt_controller.reason)
    if completed_response is None:
        raise ModelCallError(
            "模型流式响应结束，但没有收到 response.completed 事件",
            "missing_completed",
        )
    return completed_response
