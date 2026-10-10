"""Eval 请求级观测；不改变 SDK 参数、异常或底层流关闭路径。"""

from datetime import datetime, timezone
import json
import threading
from time import perf_counter

from eval.phase5c.provider_preflight import redact_diagnostic


def snapshot(value):
    """复制请求事实，避免后续 Context 修改历史观测。"""
    return json.loads(json.dumps(value, default=lambda v: v.model_dump(mode="json") if hasattr(v, "model_dump") else str(v)))


def now():
    return datetime.now(timezone.utc).isoformat()


class ObservedStream:
    """保留 SDK 流的同步关闭入口，包括其他线程发出的中断。"""

    def __init__(self, raw, observer, row, started):
        self.raw, self.observer, self.row, self.started = raw, observer, row, started
        self.iterator = iter(raw)
        self.closed = False

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            event = next(self.iterator)
        except StopIteration:
            if self.row["status"] == "RUNNING":
                self.observer.finish(self.row, self.started, "STREAM_INTERRUPTED", "STREAM_ENDED_WITHOUT_TERMINAL_EVENT")
            raise
        except BaseException as error:
            if self.row["status"] == "RUNNING":
                self.observer.fail(self.row, self.started, error)
            raise
        self.row["events"].append(event.type)
        if event.type in {"response.completed", "response.failed", "response.incomplete"}:
            self.observer.response(self.row, event.response)
            status = "SUCCEEDED" if event.type == "response.completed" else "PROVIDER_FAILED"
            reason = None if status == "SUCCEEDED" else snapshot(getattr(event.response, "error", None) or getattr(event.response, "incomplete_details", None) or event.type)
            self.observer.finish(self.row, self.started, status, reason)
        elif event.type == "error":
            self.observer.finish(self.row, self.started, "PROVIDER_FAILED", getattr(event, "message", "PROVIDER_ERROR_EVENT"))
        return event

    def close(self):
        """直接关闭底层流，而非关闭正在迭代的 Python generator。"""
        if self.closed:
            return
        self.closed = True
        try:
            return self.raw.close()
        finally:
            if self.row["status"] == "RUNNING":
                self.observer.finish(self.row, self.started, "CANCELLED", "CONSUMER_CLOSED_BEFORE_TERMINAL_EVENT")

    def __enter__(self):
        self.raw.__enter__()
        return self

    def __exit__(self, *args):
        try:
            return self.raw.__exit__(*args)
        finally:
            self.closed = True
            if self.row["status"] == "RUNNING":
                self.observer.finish(self.row, self.started, "CANCELLED", "CONTEXT_EXIT_BEFORE_TERMINAL_EVENT")


class Observer:
    """作用域客户端共享同一观测账本，未知属性透明交由 SDK。"""

    def __init__(self, delegate, destination, state=None):
        self.delegate, self.destination = delegate, destination
        self.state = state if state is not None else {"turns": [], "current": None, "observation_errors": [], "lock": threading.RLock()}
        self.responses = Responses(self)

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    @property
    def turns(self):
        return self.state["turns"]

    @property
    def current(self):
        return self.state["current"]

    def with_options(self, *args, **kwargs):
        return Observer(self.delegate.with_options(*args, **kwargs), self.destination, self.state)

    def copy(self, *args, **kwargs):
        return Observer(self.delegate.copy(*args, **kwargs), self.destination, self.state)

    def persist(self):
        try:
            with self.state["lock"]:
                self.destination.write_text(json.dumps(redact_diagnostic(self.turns), ensure_ascii=False, indent=2))
        except Exception as error:
            self.state["observation_errors"].append(type(error).__name__)

    def response(self, row, response):
        value = snapshot(response)
        row.update(response=value, response_id=value.get("id"), usage=value.get("usage"))
        row["tool_calls"] = [c for c in (value.get("output") or []) if c.get("type") == "function_call"]

    def finish(self, row, started, status, reason=None):
        row.update(status=status, end_timestamp=now(), provider_latency_ms=(perf_counter() - started) * 1000,
                   failure_reason=redact_diagnostic(reason))
        usage = row.get("usage") or {}
        row.update(input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"))
        row["usage_available"] = row["input_tokens"] is not None and row["output_tokens"] is not None
        self.persist()

    def fail(self, row, started, error):
        name = type(error).__name__
        status = "TIMEOUT" if "timeout" in name.lower() else "CANCELLED" if "cancel" in name.lower() else "PROVIDER_FAILED"
        self.finish(row, started, status, {"type": name, "message": str(error)})


class Responses:
    """保留 create 的即时执行语义，兼容流式和非流式调用。"""

    def __init__(self, observer):
        self.observer = observer

    def __getattr__(self, name):
        return getattr(self.observer.delegate.responses, name)

    def create(self, *args, **request):
        observer = self.observer
        instructions = str(request.get("instructions", ""))
        kind = "CONTEXT_RECORD_EXTRACTION" if "提取可复用记录" in instructions else "MAIN_AGENT"
        # 使用正式 prompt 常量做身份识别，不通过 tools 是否为空猜测辅助调用。
        from hexgent.memory.memory_prompts import CONTEXT_RECORD_EXTRACTION_INSTRUCTIONS
        from hexgent.context.context_compaction import COMPACTION_INSTRUCTIONS
        if instructions == CONTEXT_RECORD_EXTRACTION_INSTRUCTIONS:
            kind = "CONTEXT_RECORD_EXTRACTION"
        elif instructions.startswith(COMPACTION_INSTRUCTIONS):
            kind = "COMPACTION"
        row = dict(turn_id=len(observer.turns) + 1, response_id=None, request=snapshot(request),
                   request_type=kind, provider=str(getattr(observer.delegate, "base_url", "UNKNOWN")), model=request.get("model"),
                   start_timestamp=now(), end_timestamp=None, status="RUNNING", failure_reason=None,
                   input_tokens=None, output_tokens=None, usage_available=False, usage=None, events=[], tool_calls=[],
                   provider_latency_ms=None, peak_concurrency=0, handler_entries=0, tool_batch_latency_ms=0)
        retries = getattr(observer.delegate, "max_retries", None)
        row["client_options"] = {"timeout": str(getattr(observer.delegate, "timeout", None)), "max_retries": retries if isinstance(retries, int) else None}
        observer.turns.append(row)
        observer.state["current"] = row
        started = perf_counter()
        try:
            raw = observer.delegate.responses.create(*args, **request)
        except BaseException as error:
            observer.fail(row, started, error)
            raise
        if request.get("stream"):
            return ObservedStream(raw, observer, row, started)
        observer.response(row, raw)
        observer.finish(row, started, "SUCCEEDED" if getattr(raw, "status", "completed") == "completed" else "PROVIDER_FAILED")
        return raw


def usage_report(turns):
    """可观测小计不是完整成本；缺失 Usage 永不填零。"""
    def group(rows):
        known = [r for r in rows if r["usage_available"]]
        return {"requests": len(rows), "usage_available_requests": len(known), "unobservable_requests": len(rows) - len(known),
                "completeness": "COMPLETE" if rows and len(known) == len(rows) else "INCOMPLETE" if rows else "NOT_APPLICABLE",
                "observable_input_tokens": sum(r["input_tokens"] for r in known) if known else None,
                "observable_output_tokens": sum(r["output_tokens"] for r in known) if known else None,
                "status_counts": {s: sum(r["status"] == s for r in rows) for s in sorted({r["status"] for r in rows})}}
    return {"main": group([r for r in turns if r["request_type"] == "MAIN_AGENT"]),
            "auxiliary": group([r for r in turns if r["request_type"] != "MAIN_AGENT"]), "all": group(turns)}
