"""把 Session 事实投影为 Web 刷新所需的只读视图。"""

from typing import Any


def transcript_messages(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从完整 Transcript 中提取用户与 Assistant 可见消息。"""
    messages: list[dict[str, Any]] = []
    hidden_queries: list[str] = []
    assistant_since_query = False
    for record in records:
        if record.get("type") == "web_hidden_query":
            payload = record.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("content"), str):
                hidden_queries.append(payload["content"])
            continue
        role, content = _visible_message(record)
        if role is None or not content:
            if record.get("type") == "agent_run_finished":
                payload = record.get("payload")
                output = payload.get("output_text") if isinstance(payload, dict) else None
                if isinstance(output, str) and output and not assistant_since_query:
                    messages.append(_message(record, "assistant", output))
                    assistant_since_query = True
            continue
        if role == "user" and record.get("type") == "user_query" and content in hidden_queries:
            hidden_queries.remove(content)
            continue
        if role == "user":
            assistant_since_query = False
        elif role == "assistant":
            assistant_since_query = True
        messages.append(_message(record, role, content))
    return messages


def _message(record: dict[str, Any], role: str, content: str) -> dict[str, Any]:
    """构造稳定的页面消息投影。"""
    return {
        "sequence": record["sequence"],
        "role": role,
        "content": content,
        "created_at": record["created_at"],
    }


def _visible_message(record: dict[str, Any]) -> tuple[str | None, str]:
    """判断一条 Transcript 事实是否属于用户可见对话。"""
    record_type = record.get("type")
    payload = record.get("payload")
    if record_type in {"user_query", "web_steering_submitted"}:
        if isinstance(payload, dict) and isinstance(payload.get("content"), str):
            return "user", payload["content"]
    if record_type == "message":
        return "assistant", _assistant_text(payload)
    return None, ""


def _assistant_text(payload: Any) -> str:
    """从 Responses API Assistant message 中提取文本内容。"""
    if not isinstance(payload, dict) or payload.get("role") != "assistant":
        return ""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        item.get("text", "")
        for item in content
        if isinstance(item, dict)
        and item.get("type") in {"output_text", "text"}
        and isinstance(item.get("text"), str)
    )
