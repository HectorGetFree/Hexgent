"""模型流式调用的协议处理。"""

from collections.abc import Generator
from typing import Any

from hexgent.events import AgentEvent, TextDeltaEvent


class ModelCallError(RuntimeError):
    """模型调用没有产生可安全处理的完整响应。"""


def call_model(
    client: Any,
    **request: Any,
) -> Generator[AgentEvent, None, Any]:
    """调用模型、发送文本增量，并返回协议完整的响应。"""
    # 1. 发起流式请求，只把文本增量立即交给界面层。
    stream = client.responses.create(**request, stream=True)
    completed_response = None
    try:
        for event in stream:
            if event.type == "response.output_text.delta":
                yield TextDeltaEvent(event.delta)
            elif event.type == "response.completed":
                completed_response = event.response
            elif event.type == "response.failed":
                response = getattr(event, "response", None)
                error = getattr(event, "error", None) or getattr(response, "error", None)
                message = getattr(error, "message", None) or error or "未知错误"
                raise ModelCallError(f"模型流式响应失败：{message}")
            elif event.type == "response.incomplete":
                response = getattr(event, "response", None)
                details = getattr(response, "incomplete_details", None)
                reason = getattr(details, "reason", None)
                suffix = f"：{reason}" if reason else ""
                raise ModelCallError(
                    f"模型流式响应未完整结束{suffix}，本轮无法安全继续"
                )
            elif event.type == "error":
                message = getattr(event, "message", None) or "未知错误"
                raise ModelCallError(f"模型流式响应错误：{message}")
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()

    # 2. 只有 completed 事件中的完整 Response 可以进入后续工具循环。
    if completed_response is None:
        raise ModelCallError("模型流式响应结束，但没有收到 response.completed 事件")
    return completed_response
