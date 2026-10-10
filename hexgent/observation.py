"""非权威运行观测；写入失败不影响业务执行，不参与恢复。"""

from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from threading import RLock, get_ident
from time import monotonic_ns
from uuid import uuid4
from hashlib import sha256
from pathlib import Path
from collections import Counter


current_observation = ContextVar("runtime_observation", default=None)


class RunObservation:
    """每次执行独立关联，线程安全追加诊断事件，不记录请求正文和密钥。"""

    def __init__(self, store):
        self.store = store
        self.execution_id = uuid4().hex
        self.lock = RLock()
        self.sequence = 0
        self.available = True

    def emit(self, event, **fields):
        """观测失败显式告警并停止写入，不把诊断异常传播到 Agent。"""
        if not self.available or self.store is None:
            return
        try:
            with self.lock:
                self.sequence += 1
                row = dict(schema_version=1, execution_id=self.execution_id,
                           sequence=self.sequence, event=event,
                           timestamp=datetime.now(timezone.utc).isoformat(),
                           monotonic_ns=monotonic_ns(), thread_id=get_ident(), **fields)
                path = self.store.session_dir / "observation.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            self.available = False
            logging.getLogger(__name__).exception("运行观测写入失败；业务执行继续，指标不完整")


def begin_span(kind, *, observer=None, **fields):
    """保存单调时钟起点；返回显式句柄，支持线程及迟到完成。"""
    observer = observer or current_observation.get()
    handle = (observer, kind, uuid4().hex, monotonic_ns())
    if observer:
        observer.emit(kind + "_started", span_id=handle[2], **fields)
    return handle


def end_span(handle, *, status, response=None, **fields):
    """Provider 缺失用量保持 null；耗时是客户端调用边界而非服务端推理时间。"""
    observer, kind, span_id, started = handle
    if not observer:
        return
    if kind == "provider":
        usage = getattr(response, "usage", None)
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        usage = usage if isinstance(usage, dict) else {}
        fields.update(input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                      usage_available=all(usage.get(k) is not None for k in ("input_tokens", "output_tokens")),
                      response_id=getattr(response, "id", None))
    observer.emit(kind + "_finished", span_id=span_id, status=status,
                  latency_ms=(monotonic_ns() - started) / 1_000_000, **fields)


def source_fingerprint():
    """记录实际 Runtime 源码树摘要，包含未提交改动，不调用外部命令。"""
    root = Path(__file__).parent
    value = sha256()
    for path in sorted(root.rglob("*.py")):
        value.update(str(path.relative_to(root)).encode())
        value.update(path.read_bytes())
    return value.hexdigest()


def observed_handler(function, arguments, context, call_id, attempt, tool_name):
    """紧贴真实 Handler 边界；迟到退出仅追加诊断，不重新提交结果。"""
    handle = begin_span("handler", observer=context.observation, call_id=call_id,
                        attempt=attempt, tool_name=tool_name)
    status = "RAISED"
    try:
        result = function(arguments, context)
        status = "RETURNED"
        return result
    finally:
        end_span(handle, status=status, call_id=call_id, attempt=attempt)


def summarize(rows):
    """离线聚合；不将调用时长总和声称为互斥阶段占比。"""
    result = {}
    for execution in sorted({row["execution_id"] for row in rows}):
        events = [row for row in rows if row["execution_id"] == execution]
        providers = [row for row in events if row["event"] == "provider_finished"]
        usage = {}
        starts = {row["span_id"]: row for row in events if row["event"] == "provider_started"}
        for category in sorted({row.get("request_type", "UNKNOWN") for row in starts.values()}):
            calls = [row for row in providers if starts.get(row["span_id"], {}).get("request_type") == category]
            observed = [row for row in calls if row["usage_available"]]
            usage[category] = dict(requests=len(calls), usage_available_requests=len(observed),
                status_counts=dict(Counter(row["status"] for row in calls)),
                missing_usage_requests=len(calls)-len(observed),
                input_tokens_observed_subtotal=sum(row["input_tokens"] for row in observed),
                output_tokens_observed_subtotal=sum(row["output_tokens"] for row in observed),
                input_tokens_total=sum(row["input_tokens"] for row in observed) if len(observed)==len(calls) else None,
                output_tokens_total=sum(row["output_tokens"] for row in observed) if len(observed)==len(calls) else None,
                client_latency_ms_sum=sum(row["latency_ms"] for row in calls))
        active = peak = 0
        for row in sorted(events, key=lambda row: row["monotonic_ns"]):
            if row["event"] == "handler_started":
                active += 1
                peak = max(peak, active)
            elif row["event"] == "handler_finished":
                active -= 1
        spans_started = {row["span_id"] for row in events if row["event"].endswith("_started") and "span_id" in row}
        spans_finished = {row["span_id"] for row in events if row["event"].endswith("_finished") and "span_id" in row}
        result[execution] = dict(provider=usage, handler_peak_concurrency=peak,
            run_binding=next((row for row in events if row["event"] == "run_binding"), None),
            run_execution_latency_ms=next(((row["monotonic_ns"]-events[0]["monotonic_ns"])/1_000_000
                for row in events if row["event"] == "run_execution_finished"), None),
            open_span_count=len(spans_started-spans_finished),
            tool_batches=[row for row in events if row["event"] == "tool_batch_finished"],
            limitations=["客户端 Provider 耗时包含消费流与调用方处理，不等于服务端推理耗时",
                         "观测非权威且非 fsync；进程崩溃可能缺失尾部，不参与恢复",
                         "Handler/批次/Provider 耗时可能重叠，不可直接求阶段占比"])
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="离线读取生产运行观测，不执行模型")
    parser.add_argument("path", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(summarize([json.loads(line) for line in arguments.path.read_text().splitlines() if line.strip()]), ensure_ascii=False, indent=2))
