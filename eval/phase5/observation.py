"""从 Eval 内实际模型请求与 Transcript 提取 Phase5A 指标。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from hexgent.utils.token_estimator import estimate_request_tokens, estimate_value_tokens


EvidenceType = Literal[
    "DETERMINISTIC_MECHANISM_EVIDENCE",
    "REAL_MODEL_BEHAVIORAL_EVIDENCE",
]


@dataclass(frozen=True)
class VariantObservation:
    """统一的机制级 Variant 观测；Token 均来自当前 Production estimator。"""

    scenario: str
    variant: str
    success: bool
    model_requests: int
    estimated_input_tokens_total: int
    estimated_input_tokens_peak: int
    per_round_estimated_input_tokens: tuple[int, ...]
    tool_calls: int
    compaction_count: int
    context_injected_chars: int
    context_injected_estimated_tokens: int
    mechanism_specific_metrics: dict[str, Any]
    correctness_guardrails: dict[str, bool]
    evidence_type: EvidenceType = "DETERMINISTIC_MECHANISM_EVIDENCE"

    def to_dict(self) -> dict[str, Any]:
        """返回可 JSON 序列化的稳定报告结构。"""

        return asdict(self)


def observe_requests(
    scenario: str,
    variant: str,
    requests: list[dict[str, Any]],
    *,
    success: bool,
    tool_calls: int,
    compaction_count: int,
    context_injected_text: str = "",
    mechanism_specific_metrics: dict[str, Any] | None = None,
    correctness_guardrails: dict[str, bool] | None = None,
) -> VariantObservation:
    """从客户端真实接收的请求按正式 estimator 聚合输入成本。"""

    per_round = tuple(
        estimate_request_tokens(
            str(request.get("instructions", "")),
            list(request.get("input", [])),
            list(request.get("tools", [])),
        )
        for request in requests
    )
    return VariantObservation(
        scenario=scenario,
        variant=variant,
        success=success,
        model_requests=len(requests),
        estimated_input_tokens_total=sum(per_round),
        estimated_input_tokens_peak=max(per_round, default=0),
        per_round_estimated_input_tokens=per_round,
        tool_calls=tool_calls,
        compaction_count=compaction_count,
        context_injected_chars=len(context_injected_text),
        context_injected_estimated_tokens=(
            estimate_value_tokens(context_injected_text)
            if context_injected_text else 0
        ),
        mechanism_specific_metrics=mechanism_specific_metrics or {},
        correctness_guardrails=correctness_guardrails or {},
    )


def tagged_context_block(instructions: str, tag: str) -> str:
    """提取 ContextBuilder 注入的完整 XML 风格区块，缺失时返回空文本。"""

    start = f"<{tag}>"
    end = f"</{tag}>"
    first = instructions.find(start)
    if first < 0:
        return ""
    last = instructions.find(end, first)
    if last < 0:
        return ""
    return instructions[first : last + len(end)]
