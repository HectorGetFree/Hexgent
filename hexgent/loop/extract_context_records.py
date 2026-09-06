"""在安全 Turn 边界协调记录提取，失败不改变主任务终态。"""

from threading import Timer
from typing import Any

from ..client import get_model
from ..memory.context_record_extraction import (
    ContextExtractionLimits, EXTRACTION_INSTRUCTIONS, prepare_extraction, parse_candidates,
)
from ..run_control import InterruptController
from ..session import Session
from .call_model import call_model


def extract_context_records(
    session: Session,
    client: Any,
    limits: ContextExtractionLimits,
    controller: InterruptController,
    *,
    force: bool = False,
) -> tuple[int, int]:
    """执行至多一次增量提取，返回已知用量，不把提取文字发给界面。"""
    if session.store is None or not limits.enabled or controller.interrupted:
        return (0, 0)
    input_tokens = output_tokens = 0
    try:
        # 1. 从持久事实构建有上限的输入，未达到频率或没有完整边界时跳过。
        batch = prepare_extraction(
            session.store.read_transcript(
                session.last_extracted_transcript_sequence + 1
            ),
            session.context_records.active_records, limits, force=force,
        )
        if batch is None:
            return (0, 0)
        session.record_runtime_event("context_record_extraction_started", {
            "after_sequence": session.last_extracted_transcript_sequence,
            "through_sequence": batch.through_sequence,
            "source_sequences": [fact["sequence"] for fact in batch.facts],
        })

        # 2. 复用主 Agent 客户端和同一模型目的地，独立超时只中断提取。
        extraction_control = InterruptController()
        remove_callback = controller.add_callback(extraction_control.interrupt)
        timer = Timer(limits.timeout_seconds, extraction_control.interrupt)
        timer.daemon = True
        timer.start()
        try:
            scoped_client = client.with_options(max_retries=0, timeout=limits.timeout_seconds) if callable(
                getattr(client, "with_options", None)
            ) else client
            stream = call_model(
                scoped_client, extraction_control,
                model=get_model(), instructions=EXTRACTION_INSTRUCTIONS,
                input=[{"role": "user", "content": batch.model_input()}],
                tools=[], max_output_tokens=limits.max_output_tokens,
            )
            while True:
                try:
                    next(stream)
                except StopIteration as completed:
                    response = completed.value
                    break
        finally:
            timer.cancel()
            remove_callback()

        # 3. 用量独立记账；提取结果必须完整且来源合法，用户中断后禁止提交。
        usage = getattr(response, "usage", None)
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = usage.get("input_tokens", 0) or 0
        output_tokens = usage.get("output_tokens", 0) or 0
        session.record_runtime_event("context_record_extraction_response", {
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        })
        if controller.interrupted:
            return (input_tokens, output_tokens)
        candidates = parse_candidates(response.output_text, batch, limits)
        session.upsert_context_records(candidates, processed_sequence=batch.through_sequence)
    except Exception as error:
        session.record_runtime_event("context_record_extraction_failed", {
            "error_type": type(error).__name__,
            "message": "上下文记录提取未完成，保留原处理进度，等待后续安全边界",
        })
    return (input_tokens, output_tokens)
