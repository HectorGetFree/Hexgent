"""Phase5B 真实模型 paired benchmark；只在 Eval 层观测，不修改 Runtime。"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from statistics import mean, median
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any, Callable, Iterable

from hexgent.agent_engine import AgentEngine
from hexgent.client import get_client
from hexgent.config import HexgentConfig
from hexgent.context import ContextLimits
from hexgent.memory import ContextRecordInput, ContextRetrievalLimits, MemoryRetrievalLimits, MemoryStore
from hexgent.memory.context_record_extraction import ContextExtractionLimits
from hexgent.utils.token_estimator import estimate_request_tokens, estimate_value_tokens


EVIDENCE_TYPE = "REAL_MODEL_BEHAVIORAL_EVIDENCE"


@dataclass
class ProviderCall:
    """一次实际发往 Provider 的请求及 completed 事件中的 Usage。"""

    estimated_input_tokens: int
    instructions: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    completed: bool = False
    usage_available: bool = False
    usage_keys: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _ObservedResponses:
    def __init__(self, delegate: Any, calls: list[ProviderCall]) -> None:
        self._delegate = delegate
        self._calls = calls

    def create(self, **request: Any) -> Any:
        call = ProviderCall(
            estimated_input_tokens=estimate_request_tokens(
                str(request.get("instructions", "")),
                list(request.get("input", [])),
                list(request.get("tools", [])),
            ),
            instructions=str(request.get("instructions", "")),
        )
        self._calls.append(call)
        stream = self._delegate.create(**request)

        def observed() -> Iterable[Any]:
            for event in stream:
                if getattr(event, "type", None) == "response.completed":
                    usage = getattr(getattr(event, "response", None), "usage", None)
                    if hasattr(usage, "model_dump"):
                        usage = usage.model_dump()
                    call.input_tokens = _usage_value(usage, "input_tokens", "prompt_tokens")
                    call.output_tokens = _usage_value(usage, "output_tokens", "completion_tokens")
                    call.completed = True
                    call.usage_available = usage is not None
                    call.usage_keys = tuple(sorted(usage)) if isinstance(usage, dict) else ()
                yield event

        return observed()


class ObservedProductionClient:
    """代理 Production OpenAI client，既不改变请求也不消费/修改流事件。"""

    def __init__(self, delegate: Any) -> None:
        self.calls: list[ProviderCall] = []
        self.responses = _ObservedResponses(delegate.responses, self.calls)


def _usage_value(usage: Any, *names: str) -> int | None:
    if usage is None:
        return None
    for name in names:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if isinstance(value, int):
            return value
    return None


def _config(*, context: ContextLimits | None = None, context_records: bool = False,
            memory: bool = False) -> HexgentConfig:
    """关闭自动提取，避免把额外机制混入三组 paired 对照。"""

    return HexgentConfig(
        context=context or ContextLimits(),
        context_extraction=ContextExtractionLimits(enabled=False),
        context_retrieval=ContextRetrievalLimits(enabled=context_records),
        memory_retrieval=MemoryRetrievalLimits(enabled=memory),
    )


@dataclass(frozen=True)
class IndividualRun:
    scenario: str
    variant: str
    index: int
    success: bool
    terminal_status: str
    final_output: str
    provider_input_tokens: int | None
    provider_output_tokens: int | None
    estimated_input_tokens: int
    model_rounds: int
    tool_calls: int
    read_file_calls: int
    unique_files_read: int
    repeated_exploration: int
    compaction_count: int
    injected_tokens: int
    latency_ms: float
    guardrails: dict[str, bool]
    provider_usage_complete: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VariantAggregate:
    scenario: str
    variant: str
    runs: tuple[IndividualRun, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "variant": self.variant,
            "runs": len(self.runs),
            "success": {
                "count": sum(item.success for item in self.runs),
                "rate": sum(item.success for item in self.runs) / len(self.runs),
            },
            "provider_input_tokens": _metric(self.runs, "provider_input_tokens"),
            "provider_output_tokens": _metric(self.runs, "provider_output_tokens"),
            "total_provider_tokens": _total_tokens(self.runs),
            "estimated_input_tokens": _metric(self.runs, "estimated_input_tokens"),
            "model_rounds": _metric(self.runs, "model_rounds"),
            "tool_calls": _metric(self.runs, "tool_calls"),
            "read_file_calls": _metric(self.runs, "read_file_calls"),
            "unique_files_read": _metric(self.runs, "unique_files_read"),
            "repeated_exploration": _metric(self.runs, "repeated_exploration"),
            "compaction_count": _metric(self.runs, "compaction_count"),
            "injected_tokens": _metric(self.runs, "injected_tokens"),
            "latency_ms": _metric(self.runs, "latency_ms", p95=True),
            "provider_usage_complete_runs": sum(item.provider_usage_complete for item in self.runs),
            "guardrails_all_passed": all(all(item.guardrails.values()) for item in self.runs),
            "individual_runs": [item.to_dict() for item in self.runs],
        }


def _metric(runs: tuple[IndividualRun, ...], name: str, *, p95: bool = False) -> dict[str, float | None]:
    values = [getattr(item, name) for item in runs]
    numeric = [float(value) for value in values if value is not None]
    if not numeric:
        return {"mean": None, "median": None, **({"p95_small_sample_diagnostic": None} if p95 else {})}
    result: dict[str, float | None] = {"mean": round(mean(numeric), 2), "median": round(median(numeric), 2)}
    if p95:
        ordered = sorted(numeric)
        result["p95_small_sample_diagnostic"] = round(ordered[max(0, (len(ordered) * 95 + 99) // 100 - 1)], 2)
    return result


def _total_tokens(runs: tuple[IndividualRun, ...]) -> dict[str, float | None]:
    values = [
        item.provider_input_tokens + item.provider_output_tokens
        for item in runs
        if item.provider_input_tokens is not None and item.provider_output_tokens is not None
    ]
    if not values:
        return {"mean": None, "median": None}
    return {"mean": round(mean(values), 2), "median": round(median(values), 2)}


def _delta(baseline: dict[str, Any], enabled: dict[str, Any]) -> dict[str, Any]:
    fields = ("provider_input_tokens", "provider_output_tokens", "total_provider_tokens", "model_rounds", "tool_calls", "read_file_calls", "repeated_exploration", "latency_ms")
    result: dict[str, Any] = {}
    for field in fields:
        before = baseline[field]["mean"]
        after = enabled[field]["mean"]
        result[field] = {
            "mean_delta": None if before is None or after is None else round(after - before, 2),
            "mean_change_ratio": None if not before or after is None else round((after - before) / before, 4),
        }
    return result


def _long_context(root: Path, *, compression: bool) -> tuple[AgentEngine, str]:
    (root / "task.txt").write_text("任务文件确认值：ALPHA。", encoding="utf-8")
    limits = ContextLimits(
        context_window_tokens=10_000 if compression else 1_000_000,
        reserved_output_tokens=100 if compression else 1,
        safety_margin_tokens=100 if compression else 1,
        proactive_threshold_tokens=4_000 if compression else 900_000,
        recent_context_tokens=500 if compression else 24_000,
        reactive_recent_context_tokens=200 if compression else 8_000,
        min_compaction_savings_tokens=1 if compression else 2_000,
        min_compaction_savings_ratio=0.01 if compression else 0.1,
    )
    engine = AgentEngine.create(_config(context=limits), root)
    fact = engine.context_manager.append_user_query("关键事实：最终确认码是 ALPHA，后续任务必须保留。")
    engine.session.record_user_query(fact)
    history = {"role": "assistant", "content": "中间历史说明。" * 300}
    engine.context_manager.append_messages([history], completes_round=True)
    engine.session.record_context_messages([history], completes_round=True, source="benchmark_seed")
    query = "必须先使用 read_file 读取 task.txt；然后只输出 ANSWER=ALPHA。不要调用其他工具。"
    return engine, query


def _context_record_task(root: Path, *, enabled: bool) -> tuple[AgentEngine, str, str]:
    (root / "workspace-facts.md").write_text("发布服务端口是 8080。", encoding="utf-8")
    engine = AgentEngine.create(_config(context_records=enabled), root)
    seed = engine.context_manager.append_user_query("Stage A 已完成工作区探索。")
    engine.session.record_user_query(seed)
    engine.session.upsert_context_records([ContextRecordInput("发布服务端口是 8080。", "fact", (1,))])
    return engine, "确认发布服务端口。若上下文没有这个事实，使用 read_file 查看 workspace-facts.md；最终只输出 PORT=8080。", "PORT=8080"


def _memory_task(root: Path, *, enabled: bool) -> tuple[AgentEngine, str, str]:
    (root / "workspace-memory.md").write_text("蓝绿发布失败时，回滚到上一稳定版本。", encoding="utf-8")
    MemoryStore(root).upsert("蓝绿发布失败时，回滚到上一稳定版本。", "decision", keywords=("蓝绿发布", "回滚"), source="explicit")
    engine = AgentEngine.create(_config(memory=enabled), root)
    return engine, "回答蓝绿发布失败后的回滚策略。若上下文没有这个事实，使用 read_file 查看 workspace-memory.md；最终只输出 ROLLBACK=上一稳定版本。", "ROLLBACK=上一稳定版本"


TaskBuilder = Callable[[Path], tuple[AgentEngine, str, str]]


def _execute(scenario: str, variant: str, index: int, builder: TaskBuilder, *, injected_tag: str | None = None) -> IndividualRun:
    with TemporaryDirectory(prefix="hexgent-phase5b-") as directory:
        root = Path(directory)
        engine, query, expected = builder(root)
        client = ObservedProductionClient(get_client())
        started = perf_counter()
        events = list(engine.run(query, client=client, user_confirmation=lambda *_: "allow"))
        latency_ms = (perf_counter() - started) * 1_000
        transcript = engine.session.store.read_transcript() if engine.session.store else []
        terminal = events[-1].result
        tool_rows = [row["payload"] for row in transcript if row["type"] == "tool_execution_finished"]
        files = [str(row.get("data", {}).get("path")) for row in tool_rows if row.get("tool_name") == "read_file" and isinstance(row.get("data"), dict)]
        output = terminal.output_text or ""
        injected = _injected_tokens(client.calls, injected_tag)
        success = terminal.status == "completed" and expected in output
        provider_complete = bool(client.calls) and all(call.completed and call.input_tokens is not None and call.output_tokens is not None for call in client.calls)
        return IndividualRun(
            scenario=scenario, variant=variant, index=index, success=success,
            terminal_status=terminal.status, final_output=output,
            provider_input_tokens=(sum(call.input_tokens or 0 for call in client.calls) if provider_complete else None),
            provider_output_tokens=(sum(call.output_tokens or 0 for call in client.calls) if provider_complete else None),
            estimated_input_tokens=sum(call.estimated_input_tokens for call in client.calls),
            model_rounds=sum(row["type"] == "model_response_completed" for row in transcript),
            tool_calls=len(tool_rows), read_file_calls=len(files), unique_files_read=len(set(files)),
            repeated_exploration=max(0, len(files) - len(set(files))),
            compaction_count=sum(row["type"] == "context_compaction_applied" for row in transcript),
            injected_tokens=injected, latency_ms=round(latency_ms, 2),
            guardrails={
                "terminal_completed": terminal.status == "completed",
                "deterministic_expected_output": expected in output,
                "no_denied_or_failed_tool": all(row.get("status") == "success" for row in tool_rows),
            },
            provider_usage_complete=provider_complete,
        )


def _injected_tokens(calls: list[ProviderCall], tag: str | None) -> int:
    """只计算实际请求 instructions 中被机制注入的 XML 区块。"""

    if not tag or not calls:
        return 0
    start, end = f"<{tag}>", f"</{tag}>"
    instructions = calls[0].instructions
    left = instructions.find(start)
    right = instructions.find(end, left)
    if left < 0 or right < 0:
        return 0
    return estimate_value_tokens(instructions[left : right + len(end)])


def _runs(scenario: str, variant: str, count: int, builder: TaskBuilder, *, injected_tag: str | None = None) -> VariantAggregate:
    return VariantAggregate(scenario, variant, tuple(
        _execute(scenario, variant, index + 1, builder, injected_tag=injected_tag)
        for index in range(count)
    ))


def run_phase5b(*, runs: int = 5) -> dict[str, Any]:
    """执行三组真实模型 paired 对照，并保留每一次原始观测。"""

    if runs < 5:
        raise ValueError("Phase5B 每个 variant 至少需要 5 次运行")
    scenarios: list[tuple[str, VariantAggregate, VariantAggregate, str]] = []
    scenarios.append((
        "T1_long_context_compression",
        _runs("T1_long_context_compression", "compression_avoided", runs,
              lambda root: (*_long_context(root, compression=False), "ANSWER=ALPHA")),
        _runs("T1_long_context_compression", "compression_enabled", runs,
              lambda root: (*_long_context(root, compression=True), "ANSWER=ALPHA")),
        "Compression summary call is included in provider token and latency totals.",
    ))
    scenarios.append((
        "T2_context_record_behavior",
        _runs("T2_context_record_behavior", "retrieval_off", runs,
              lambda root: _context_record_task(root, enabled=False)),
        _runs("T2_context_record_behavior", "retrieval_on", runs,
              lambda root: _context_record_task(root, enabled=True), injected_tag="relevant-session-context"),
        "Both variants start from the same durable Stage A record fixture; auto extraction is excluded.",
    ))
    scenarios.append((
        "T3_workspace_memory_behavior",
        _runs("T3_workspace_memory_behavior", "retrieval_off", runs,
              lambda root: _memory_task(root, enabled=False)),
        _runs("T3_workspace_memory_behavior", "retrieval_on", runs,
              lambda root: _memory_task(root, enabled=True), injected_tag="relevant-memory"),
        "Both variants use a new Session over the same preseeded workspace Memory fixture; auto extraction is excluded.",
    ))
    rendered = []
    for name, baseline, enabled, note in scenarios:
        base = baseline.to_dict()
        on = enabled.to_dict()
        rendered.append({"scenario": name, "baseline": base, "enabled": on, "paired_delta": _delta(base, on), "note": note})
    return {
        "title": "Phase5B Real-model Behavioral Benchmark",
        "evidence_type": EVIDENCE_TYPE,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "runs_per_variant": runs,
        "small_sample_note": "P95 is a small-sample diagnostic only and is not a strong statistical conclusion.",
        "scenarios": rendered,
    }


def write_report(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "phase5b-real-model-report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def default_output_dir() -> Path:
    value = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("eval") / "phase5" / "runs" / f"real-model-{value}"
