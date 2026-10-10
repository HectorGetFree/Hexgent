"""Provider preflight 入口；仅显式调用时请求 Provider。"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PREFLIGHT_REQUEST: dict[str, Any] = {
    "instructions": "Return exactly PREFLIGHT_OK.",
    "input": [{"role": "user", "content": "PREFLIGHT"}],
    "tools": [],
    "stream": True,
    "max_output_tokens": 8,
}


def redact_diagnostic(value: Any) -> Any:
    """递归脱敏诊断字段，不保存 Authorization、API key 或 bearer token。"""

    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in {"authorization", "api_key", "apikey", "token"}
            else redact_diagnostic(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_diagnostic(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
        return re.sub(r"(?i)(sk|key)-[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
    return value


def classify_preflight_failure(message: str) -> str:
    """按明确 Provider 证据分类；未知错误不被吞掉。"""

    lowered = message.lower()
    if "upstream policy" in lowered or "request blocked" in lowered:
        return "PROVIDER_UPSTREAM_POLICY_BLOCK"
    if "overloaded" in lowered or "too many requests" in lowered or "503" in lowered:
        return "PROVIDER_OVERLOADED"
    if "account" in lowered and any(marker in lowered for marker in ("disabled", "no longer enabled", "not allowed for this key")):
        return "PROVIDER_ACCOUNT_DISABLED"
    if "previous response account binding" in lowered:
        return "PROVIDER_RESPONSE_BINDING_ERROR"
    if "timeout" in lowered or "timed out" in lowered:
        return "PROVIDER_TIMEOUT"
    if "peer closed" in lowered or "incomplete chunked" in lowered or "connection reset" in lowered:
        return "PROVIDER_STREAM_INTERRUPTED"
    if "upstream request failed" in lowered:
        return "PROVIDER_UPSTREAM_REQUEST_FAILED"
    return "UNKNOWN"


def _event_value(event: Any, name: str) -> Any:
    """兼容 SDK 对象与轻量测试桩，读取有限的公开诊断字段。"""

    return event.get(name) if isinstance(event, dict) else getattr(event, name, None)


def _event_diagnostic(event: Any) -> dict[str, Any]:
    """提取终止事件的有限错误证据，避免序列化完整 Provider 对象。"""

    response = _event_value(event, "response")
    error = _event_value(event, "error") or _event_value(response, "error")
    incomplete = _event_value(response, "incomplete_details")
    message = (
        _event_value(error, "message")
        or _event_value(event, "message")
        or _event_value(incomplete, "reason")
        or ""
    )
    return redact_diagnostic({
        "event_type": _event_value(event, "type"),
        "error_type": type(error).__name__ if error is not None else None,
        "message": str(message),
        "incomplete_reason": _event_value(incomplete, "reason"),
    })


def run_preflight(client: Any, *, model: str) -> dict[str, Any]:
    """显式运行一次极小 streaming preflight；不参与 Benchmark 统计。"""

    try:
        events: Iterable[Any] = client.responses.create(model=model, **PREFLIGHT_REQUEST)
        completed = False
        usage: Any = None
        event_types: list[str] = []
        terminal_diagnostic: dict[str, Any] | None = None
        for event in events:
            event_type = _event_value(event, "type")
            if isinstance(event_type, str) and len(event_types) < 20:
                event_types.append(event_type)
            if event_type == "response.completed":
                completed = True
                usage = _event_value(_event_value(event, "response"), "usage")
            elif event_type in {"response.failed", "response.incomplete", "error"}:
                terminal_diagnostic = _event_diagnostic(event)
                break
        if completed:
            return {
                "status": "PASS",
                "failure_category": None,
                "usage_available": usage is not None,
                "model_id": model,
                "event_types": event_types,
            }
        message = str((terminal_diagnostic or {}).get("message") or "")
        category = classify_preflight_failure(message)
        return {
            "status": "FAIL",
            "failure_category": category,
            "retryable": category == "PROVIDER_OVERLOADED",
            "diagnostic": terminal_diagnostic or {
                "event_types": event_types,
                "message": "stream ended without response.completed or explicit terminal failure event",
            },
            "event_types": event_types,
            "usage_available": False,
            "model_id": model,
        }
    except Exception as error:
        message = str(error)
        category = classify_preflight_failure(message)
        return {
            "status": "FAIL",
            "failure_category": category,
            "retryable": category == "PROVIDER_OVERLOADED",
            "diagnostic": redact_diagnostic({"exception_type": type(error).__name__, "message": message}),
            "model_id": model,
        }


def write_preflight_artifact(destination: Path, result: dict[str, Any]) -> Path:
    """保存独立且已脱敏的 preflight 结果，不写入正式 benchmark 统计。"""

    destination.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "kind": "PROVIDER_PREFLIGHT",
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "included_in_benchmark_statistics": False,
        "result": redact_diagnostic(result),
    }
    destination.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run an explicit Provider preflight; no Benchmark statistics are written.")
    parser.add_argument("--execute", action="store_true", help="explicitly send the tiny streaming request")
    parser.add_argument("--output", type=Path, help="optional sanitized JSON artifact destination")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"status": "NOT_RUN", "request": PREFLIGHT_REQUEST}, ensure_ascii=False))
    else:
        from hexgent.client import get_client, get_model
        result = run_preflight(get_client(), model=get_model())
        if args.output is not None:
            write_preflight_artifact(args.output, result)
        print(json.dumps(result, ensure_ascii=False))
