"""Phase5A 不修改 Runtime 的 paired deterministic benchmark 场景。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Callable

from eval.support.agent_contracts import FakeToolCall, text_response
from hexgent.agent_engine import AgentEngine
from hexgent.config import HexgentConfig, RunOptions
from hexgent.context import ContextLimits
from hexgent.context.context_compaction import COMPACTION_INSTRUCTIONS
from hexgent.memory import (
    ContextRecordInput,
    ContextRetrievalLimits,
    MemoryRetrievalLimits,
    MemoryStore,
)
from hexgent.memory.context_record_extraction import ContextExtractionLimits
from hexgent.session import Session
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_call
from hexgent.plan import InMemoryPlanStore

from .observation import VariantObservation, observe_requests, tagged_context_block
from .report import PairedScenarioResult


class BenchmarkResponses:
    """记录正式 Loop 请求，并为摘要与任务响应提供确定性输出。"""

    def __init__(self, task_responses: list[Any], *, summary: str = "关键事实=Alpha。") -> None:
        self.responses = self
        self._task_responses = list(task_responses)
        self._summary = summary
        self.calls: list[dict[str, Any]] = []
        self.kinds: list[str] = []

    def create(self, **request: Any):
        # 1. Snapshot 提取是独立辅助调用，不消费原 MAIN/SUMMARY 响应队列。
        from tests.support.fact_extraction_fixture import extraction_event
        extraction = extraction_event(self, request)
        if extraction is not None:
            return extraction
        self.calls.append(request)
        if str(request.get("instructions", "")).startswith(COMPACTION_INSTRUCTIONS):
            self.kinds.append("compaction")
            response = SimpleNamespace(
                output_text=f"<summary>{self._summary}</summary>",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )
        else:
            self.kinds.append("main")
            if not self._task_responses:
                raise AssertionError("FakeModel 缺少主任务响应")
            response = self._task_responses.pop(0)
        events = [
            SimpleNamespace(type="response.output_text.delta", delta=delta)
            for delta in getattr(response, "text_deltas", [])
        ]
        events.append(SimpleNamespace(type="response.completed", response=response))
        return iter(events)


def _base_config(
    *,
    context: ContextLimits | None = None,
    context_retrieval: ContextRetrievalLimits | None = None,
    memory_retrieval: MemoryRetrievalLimits | None = None,
) -> HexgentConfig:
    """关闭自动提取，保证 Benchmark 只改变被测机制。"""

    return HexgentConfig(
        context=context or ContextLimits(),
        context_extraction=ContextExtractionLimits(enabled=False),
        context_retrieval=context_retrieval or ContextRetrievalLimits(enabled=False),
        memory_retrieval=memory_retrieval or MemoryRetrievalLimits(enabled=False),
    )


def _run_engine(
    engine: AgentEngine,
    query: str,
    client: BenchmarkResponses,
    *,
    options: RunOptions | None = None,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """通过正式 AgentEngine 执行固定 Query，并保留 durable Transcript。"""

    events = list(engine.run(query, client=client, options=options))
    assert engine.session.store is not None
    return events, engine.session.store.read_transcript()


def _seed_long_history(root: Path, config: HexgentConfig) -> AgentEngine:
    """构造与 Variant 无关的 durable 长历史与早期关键事实。"""

    engine = AgentEngine.create(config, root)
    first = engine.context_manager.append_user_query("关键事实=Alpha，后续总结必须保留该事实。")
    engine.session.record_user_query(first)
    history = {"role": "assistant", "content": "历史解释内容。" * 300}
    engine.context_manager.append_messages([history], completes_round=True)
    engine.session.record_context_messages([history], completes_round=True, source="model_response")
    return engine


def _run_compression_variant(*, compact: bool) -> VariantObservation:
    """在相同历史和工具轨迹下比较是否允许正式 compaction。"""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "task.txt").write_text("工具读取内容", encoding="utf-8")
        context = (
            ContextLimits(
                # 完整请求包含现有工具 Schema 和真实 MAIN 输出预留；不能沿用旧 10K 假窗口。
                context_window_tokens=32_000,
                reserved_output_tokens=8_000,
                safety_margin_tokens=100,
                # 较低主动阈值用于触发机制；物理窗口仍完整容纳工具定义和输出预留。
                proactive_threshold_tokens=4_000,
                recent_context_tokens=500,
                reactive_recent_context_tokens=200,
                min_compaction_savings_tokens=1,
                min_compaction_savings_ratio=0.01,
            )
            if compact else ContextLimits(
                context_window_tokens=32_000,
                reserved_output_tokens=8_000,
                safety_margin_tokens=100,
                proactive_threshold_tokens=31_000,
                recent_context_tokens=500,
                reactive_recent_context_tokens=200,
                min_compaction_savings_tokens=1,
                min_compaction_savings_ratio=0.01,
            )
        )
        config = _base_config(context=context)
        engine = _seed_long_history(root, config)
        client = BenchmarkResponses([
            text_response(output=[FakeToolCall(
                "function_call", "read-1", "read_file", '{"path":"task.txt"}',
            )]),
            text_response("关键事实=Alpha；任务完成。"),
        ])
        events, transcript = _run_engine(
            engine, "读取 task.txt 后结合关键事实完成总结", client,
        )
        main_requests = [
            request for request, kind in zip(client.calls, client.kinds) if kind == "main"
        ]
        second_main = main_requests[-1]
        compactions = [
            record["payload"] for record in transcript
            if record["type"] == "context_compaction_applied"
        ]
        finished = events[-1].result
        observation = observe_requests(
            "C1_context_compression",
            "compression_on" if compact else "compression_avoided",
            client.calls,
            success=finished.status == "completed",
            tool_calls=sum(
                record["type"] == "tool_execution_finished" for record in transcript
            ),
            compaction_count=len(compactions),
            mechanism_specific_metrics={
                "main_model_requests": len(main_requests),
                "compaction_model_requests": client.kinds.count("compaction"),
                "before_after_tokens": [
                    {"before_tokens": item["before_tokens"], "after_tokens": item["after_tokens"]}
                    for item in compactions
                ],
                "reduction_ratio": (
                    (compactions[0]["before_tokens"] - compactions[0]["after_tokens"])
                    / compactions[0]["before_tokens"]
                    if compactions and compactions[0]["before_tokens"] else 0.0
                ),
            },
            correctness_guardrails={
                "terminal_completed": finished.status == "completed",
                "same_final_output": finished.output_text == "关键事实=Alpha；任务完成。",
                "early_fact_available_later": "关键事实=Alpha" in str(second_main["input"]),
                "same_tool_trace": sum(
                    record["type"] == "tool_execution_finished" for record in transcript
                ) == 1,
            },
        )
        return observation


def context_compression() -> PairedScenarioResult:
    """C1：压缩与避免触发压缩的 paired 机制级对照。"""

    enabled = _run_compression_variant(compact=True)
    baseline = _run_compression_variant(compact=False)
    delta = _paired_delta(baseline, enabled)
    delta["reduction_ratio"] = enabled.mechanism_specific_metrics["reduction_ratio"]
    return PairedScenarioResult(
        "C1_context_compression", baseline, enabled, delta,
        "包含摘要调用在内的正式估算输入成本；不使用 FakeModel usage 作为 token 成本。",
    )


def _read_trace(*, clear_before_read: bool) -> VariantObservation:
    """执行完全一致的三次 read_file trace，仅通过 fixture 清 cache 构造 micro baseline。"""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        content = "重复读取正文。" * 200
        (root / "notes.txt").write_text(content, encoding="utf-8")
        context = ToolExecutionContext(InMemoryPlanStore(), root)
        outputs: list[str] = []
        for index in range(3):
            if clear_before_read:
                context.clear_read_file_cache()
            result = execute_tool_call(
                "read_file", '{"path":"notes.txt"}', context=context,
                user_confirmation=lambda *_: "allow", call_id=f"read-{index}",
            )
            assert result.status == "success"
            outputs.append(result.output)

        # Guardrail 不属于 paired trace：写入后同一缓存必须不能复用旧正文。
        write = execute_tool_call(
            "write_file", '{"path":"notes.txt","content":"变更后的正文"}',
            context=context, user_confirmation=lambda *_: "allow", call_id="write-1",
        )
        after_write = execute_tool_call(
            "read_file", '{"path":"notes.txt"}', context=context,
            user_confirmation=lambda *_: "allow", call_id="read-after-write",
        )
        full_outputs = [item for item in outputs if "<file-unchanged" not in item]
        unchanged_count = sum("<file-unchanged" in item for item in outputs)
        full_chars = sum(len(item) for item in full_outputs)
        return VariantObservation(
            scenario="C2_read_file_content_reuse",
            variant="cache_off_fixture" if clear_before_read else "cache_on",
            success=all(item == content for item in full_outputs),
            model_requests=0,
            estimated_input_tokens_total=0,
            estimated_input_tokens_peak=0,
            per_round_estimated_input_tokens=(),
            tool_calls=3,
            compaction_count=0,
            context_injected_chars=full_chars,
            context_injected_estimated_tokens=sum(
                _estimate_text(item) for item in full_outputs
            ),
            mechanism_specific_metrics={
                "full_content_response_count": len(full_outputs),
                "file_unchanged_count": unchanged_count,
                "full_content_injected_chars": full_chars,
                "physical_file_io_saved": None,
                "tool_dispatch_saved": 0,
                "off_variant_scope": "eval_fixture_only" if clear_before_read else None,
            },
            correctness_guardrails={
                "same_three_tool_calls": len(outputs) == 3,
                "unchanged_content_is_not_reinjected": (
                    unchanged_count == (0 if clear_before_read else 2)
                ),
                "write_succeeded": write.status == "success",
                "write_invalidates_by_content_hash": (
                    after_write.status == "success"
                    and after_write.output == "变更后的正文"
                ),
            },
        )


def _estimate_text(text: str) -> int:
    """避免将固定输出误写入 FakeModel usage，仍使用 Production estimator。"""

    from hexgent.utils.token_estimator import estimate_value_tokens
    return estimate_value_tokens(text)


def read_file_content_reuse() -> PairedScenarioResult:
    """C2：只量化正文注入复用，不宣称 I/O 或 dispatch 节省。"""

    enabled = _read_trace(clear_before_read=False)
    baseline = _read_trace(clear_before_read=True)
    delta = _paired_delta(baseline, enabled)
    delta["full_content_injected_chars_delta"] = (
        enabled.context_injected_chars - baseline.context_injected_chars
    )
    delta["full_content_injected_chars_reduction_ratio"] = _reduction_ratio(
        baseline.context_injected_chars, enabled.context_injected_chars,
    )
    return PairedScenarioResult(
        "C2_read_file_content_reuse", baseline, enabled, delta,
        "OFF 仅在 Eval fixture 每次读取前清 cache；两侧 tool dispatch 均为三次，物理 I/O 不在节省范围内。",
    )


def _run_context_record_variant(*, enabled: bool) -> VariantObservation:
    """C3：相同 durable Record 下仅开关 retrieval。"""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        config = _base_config(context_retrieval=ContextRetrievalLimits(enabled=enabled))
        engine = AgentEngine.create(config, root)
        seed = engine.context_manager.append_user_query("记录当前服务端口")
        engine.session.record_user_query(seed)
        old = engine.session.upsert_context_records([
            ContextRecordInput("服务端口为 8080。", "fact", (1,)),
        ])[0].record
        engine.session.upsert_context_records([
            ContextRecordInput("服务端口为 8098。", "fact", (1,), (old.record_id,)),
        ])
        client = BenchmarkResponses([text_response("端口结论已给出。")])
        events, transcript = _run_engine(engine, "当前服务端口是什么", client)
        instructions = str(client.calls[0]["instructions"])
        injected = tagged_context_block(instructions, "relevant-session-context")
        retrieved = next(
            record["payload"] for record in transcript
            if record["type"] == "context_records_retrieved" and record["payload"]["context_query_sequences"] == [2]
        )
        return observe_requests(
            "C3_context_record_retrieval",
            "retrieval_on" if enabled else "retrieval_off",
            client.calls,
            success=events[-1].result.status == "completed",
            tool_calls=0,
            compaction_count=0,
            context_injected_text=injected,
            mechanism_specific_metrics={
                "recalled_record_count": len(retrieved["records"]),
                "recalled_record_ids": [item["record_id"] for item in retrieved["records"]],
                "automatic_behavioral_savings_claimed": False,
            },
            correctness_guardrails={
                "terminal_completed": events[-1].result.status == "completed",
                "active_record_is_current": "服务端口为 8098。" in instructions if enabled else "服务端口为 8098。" not in instructions,
                "superseded_record_not_injected": "服务端口为 8080。" not in instructions,
            },
        )


def context_record_retrieval() -> PairedScenarioResult:
    """C3：展示确定性注入开销，不将召回包装为行为效率收益。"""

    enabled = _run_context_record_variant(enabled=True)
    baseline = _run_context_record_variant(enabled=False)
    return PairedScenarioResult(
        "C3_context_record_retrieval", baseline, enabled,
        _paired_delta(baseline, enabled),
        "该实验只量化 retrieval 注入开销与 active/superseded 正确性；FakeModel 不用于推断轮次或工具节省。",
    )


def _run_memory_variant(*, enabled: bool) -> VariantObservation:
    """C4：同一工作区预置 Memory，跨新 Session 开关 recall。"""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        store = MemoryStore(root)
        store.upsert(
            "部署使用蓝绿发布，回滚目标是上一稳定版本。",
            "decision", keywords=("蓝绿发布", "回滚"), source="explicit",
        )
        config = _base_config(memory_retrieval=MemoryRetrievalLimits(enabled=enabled))
        # 新 Engine 即新 Session；MemoryStore 仍由同一 workspace 文件恢复。
        engine = AgentEngine.create(config, root)
        client = BenchmarkResponses([text_response("发布策略结论已给出。")])
        events, _ = _run_engine(engine, "蓝绿发布失败后如何回滚", client)
        instructions = str(client.calls[0]["instructions"])
        injected = tagged_context_block(instructions, "relevant-memory")
        return observe_requests(
            "C4_workspace_memory_retrieval",
            "retrieval_on" if enabled else "retrieval_off",
            client.calls,
            success=events[-1].result.status == "completed",
            tool_calls=0,
            compaction_count=0,
            context_injected_text=injected,
            mechanism_specific_metrics={
                "recalled_memory_count": 1 if injected else 0,
                "workspace_scoped_cross_session": True,
                "auto_extraction_evaluated": False,
                "automatic_behavioral_savings_claimed": False,
            },
            correctness_guardrails={
                "terminal_completed": events[-1].result.status == "completed",
                "new_session_reads_workspace_memory": (
                    "部署使用蓝绿发布" in instructions if enabled
                    else "部署使用蓝绿发布" not in instructions
                ),
            },
        )


def workspace_memory_retrieval() -> PairedScenarioResult:
    """C4：展示 workspace-scoped Memory 的确定性 injection overhead。"""

    enabled = _run_memory_variant(enabled=True)
    baseline = _run_memory_variant(enabled=False)
    return PairedScenarioResult(
        "C4_workspace_memory_retrieval", baseline, enabled,
        _paired_delta(baseline, enabled),
        "该实验验证跨 Session 工作区 Memory 的召回与注入成本；不评测尚未接入 Agent Loop 的自动提取。",
    )


def _paired_delta(
    baseline: VariantObservation,
    enabled: VariantObservation,
) -> dict[str, Any]:
    """统一输出 enabled 相对 baseline 的绝对差和可解释比例。"""

    return {
        "estimated_input_tokens_delta": (
            enabled.estimated_input_tokens_total - baseline.estimated_input_tokens_total
        ),
        "estimated_input_tokens_reduction_ratio": _reduction_ratio(
            baseline.estimated_input_tokens_total,
            enabled.estimated_input_tokens_total,
        ),
        "estimated_input_tokens_peak_delta": (
            enabled.estimated_input_tokens_peak - baseline.estimated_input_tokens_peak
        ),
        "model_requests_delta": enabled.model_requests - baseline.model_requests,
        "tool_calls_delta": enabled.tool_calls - baseline.tool_calls,
        "context_injected_chars_delta": (
            enabled.context_injected_chars - baseline.context_injected_chars
        ),
        "context_injected_estimated_tokens_delta": (
            enabled.context_injected_estimated_tokens
            - baseline.context_injected_estimated_tokens
        ),
    }


def _reduction_ratio(baseline: int, enabled: int) -> float | None:
    """仅在基线存在正成本时返回相对减少比例。"""

    return (baseline - enabled) / baseline if baseline else None


def v1_scenarios() -> tuple[Callable[[], PairedScenarioResult], ...]:
    """Phase5A 的四个机制级实验，不包含真实模型行为推断。"""

    return (
        context_compression,
        read_file_content_reuse,
        context_record_retrieval,
        workspace_memory_retrieval,
    )
