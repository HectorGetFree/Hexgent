"""会话 Transcript 与大工具结果的本地持久化。"""

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo


SESSION_QUERY_SLUG_MAX_LENGTH = 48
TOOL_RESULT_SLUG_MAX_LENGTH = 80
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class ToolResultLimits:
    """控制工具结果内联和落盘预览的大小。"""

    # 工具结果超过该字符数后不再完整放入模型上下文。
    max_inline_characters: int = 50_000

    # 大结果落盘后继续向模型展示的开头预览字符数。
    preview_characters: int = 500

    # 同一工具批次允许完整内联到模型上下文的结果 Token 总量。
    max_batch_inline_tokens: int = 8_000

    # 同一工具批次所有外置结果允许保留的预览 Token 总量。
    max_batch_preview_tokens: int = 4_000


@dataclass(frozen=True)
class PersistedToolResult:
    """一个已经落盘并可由模型重新读取的工具结果。"""

    # 工具结果文件相对于工作区的路径。
    relative_path: str

    # 原始完整工具结果的字符数量。
    original_characters: int

    # 继续保留在模型上下文中的内容预览。
    preview: str


class SessionStore:
    """在工作区内部维护一个可恢复的 Agent 会话目录。"""

    def __init__(
        self,
        workdir: Path,
        session_id: str | None = None,
        tool_result_limits: ToolResultLimits | None = None,
    ) -> None:
        # 当前会话所属的工作区根目录。
        self.workdir = workdir.resolve()

        # 调用方显式指定的会话标识；未指定时由首轮 Query 延迟生成。
        self.session_id = session_id

        # 当前会话 Transcript 和工具结果所在目录。
        self.session_dir = self._build_session_dir(session_id or "待初始化会话")

        # 追加写入完整会话事实的 JSONL 文件。
        self.transcript_path = self.session_dir / "transcript.jsonl"

        # 保存超出内联预算的完整工具结果的目录。
        self.tool_results_dir = self.session_dir / "tool-results"

        # 当前会话采用的工具结果内联和预览限制。
        self.tool_result_limits = tool_result_limits or ToolResultLimits()

        # Transcript 中下一条记录使用的单调递增序号。
        self._next_sequence = 1

        # 标记是否已经写入会话开始事件。
        self._session_started = False

        # 首轮 Query 到达前暂存的运行时事件。
        self._pending_records: list[tuple[str, Any]] = []

    def _build_session_dir(self, session_id: str) -> Path:
        """根据稳定会话标识构造持久化目录。"""
        return self.workdir / ".hexgent" / "sessions" / session_id

    def _initialize_from_first_query(self, query: str) -> None:
        """使用时间和首轮 Query 摘要初始化可读的会话目录。"""
        if self.session_id is not None:
            return
        timestamp = _beijing_now().strftime("%Y%m%d-%H%M%S")
        candidate = f"{timestamp}_{_build_query_slug(query)}"
        session_dir = self._build_session_dir(candidate)
        if session_dir.exists():
            candidate = f"{candidate}_{uuid4().hex[:8]}"
        self.session_id = candidate
        self.session_dir = self._build_session_dir(candidate)
        self.transcript_path = self.session_dir / "transcript.jsonl"
        self.tool_results_dir = self.session_dir / "tool-results"

    def start_session(self, first_query: str) -> None:
        """确保会话已由首轮 Query 命名，并记录会话开始事实。"""
        if self._session_started:
            return
        self._initialize_from_first_query(first_query)
        self._session_started = True
        self.append_transcript(
            "session_started",
            {"session_id": self.session_id, "first_query": first_query},
        )
        pending_records = self._pending_records
        self._pending_records = []
        for record_type, payload in pending_records:
            self.append_transcript(record_type, payload)

    def _ensure_session_dir(self) -> None:
        """按需创建当前会话目录。"""
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def append_transcript(self, record_type: str, payload: Any) -> None:
        """把一条不可变会话事实追加写入 JSONL Transcript。"""
        if self.session_id is None:
            self._pending_records.append((record_type, payload))
            return
        self._ensure_session_dir()
        record = {
            "sequence": self._next_sequence,
            "type": record_type,
            "created_at": _beijing_now().isoformat(),
            "payload": payload,
        }
        with self.transcript_path.open("a", encoding="utf-8") as transcript:
            transcript.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    default=_json_default,
                    separators=(",", ":"),
                )
            )
            transcript.write("\n")
        self._next_sequence += 1

    def persist_tool_result(
        self,
        call_id: str,
        tool_name: str,
        output: str,
        *,
        force: bool = False,
        description: str | None = None,
    ) -> PersistedToolResult | None:
        """在结果超过内联预算时保存完整内容并返回模型可见引用。"""
        limits = self.tool_result_limits
        if not force and len(output) <= limits.max_inline_characters:
            return None

        self.tool_results_dir.mkdir(parents=True, exist_ok=True)
        filename = _build_tool_result_filename(call_id, tool_name, description)
        result_path = self.tool_results_dir / filename
        result_path.write_text(output, encoding="utf-8")
        relative_path = str(result_path.relative_to(self.workdir))
        persisted = PersistedToolResult(
            relative_path=relative_path,
            original_characters=len(output),
            preview=output[: limits.preview_characters],
        )
        self.append_transcript(
            "tool_result_persisted",
            {
                "call_id": call_id,
                "tool_name": tool_name,
                "relative_path": relative_path,
                "original_characters": len(output),
            },
        )
        return persisted


def _build_query_slug(query: str) -> str:
    """把首轮 Query 转换为长度受限且适合目录名的摘要。"""
    normalized = re.sub(r"\s+", " ", query).strip()
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", normalized)
    safe = safe.strip(" ._")[:SESSION_QUERY_SLUG_MAX_LENGTH].strip(" ._")
    return safe or "空白请求"


def _build_tool_result_filename(
    call_id: str,
    tool_name: str,
    description: str | None,
) -> str:
    """构造包含工具调用语义和完整 call_id 的安全结果文件名。"""
    # 1. 优先使用工具自身生成的调用说明，缺失时退回工具名称。
    readable_source = description or tool_name or "tool_result"
    readable_source = readable_source.encode("utf-8", errors="replace").decode("utf-8")
    normalized = re.sub(r"\s+", "_", readable_source).strip()
    readable_slug = re.sub(r"[^\w.-]", "_", normalized, flags=re.UNICODE)
    readable_slug = re.sub(r"_+", "_", readable_slug)
    readable_slug = readable_slug.strip(" ._")[:TOOL_RESULT_SLUG_MAX_LENGTH]
    readable_slug = readable_slug.rstrip(" ._") or "tool_result"

    # 2. 完整保留经过过滤的 call_id，继续承担唯一定位和结果配对职责。
    safe_call_id = re.sub(r"[^a-zA-Z0-9._-]", "_", call_id).strip(" ._")
    safe_call_id = safe_call_id or uuid4().hex
    return f"{readable_slug}__{safe_call_id}.txt"


def _beijing_now() -> datetime:
    """返回带有明确东八区偏移的北京时间。"""
    return datetime.now(BEIJING_TIMEZONE)


def _json_default(value: Any) -> Any:
    """将 SDK 对象转换为能够写入 JSONL 的基础数据。"""
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    if hasattr(value, "__dict__"):
        return vars(value)
    return str(value)


def build_persisted_output_message(result: PersistedToolResult) -> str:
    """构造包含恢复路径和内容预览的模型可见工具结果。"""
    return (
        "<persisted-output\n"
        f'path="{result.relative_path}"\n'
        f'original_characters="{result.original_characters}">\n'
        f"{result.preview}\n"
        "</persisted-output>\n"
        "完整输出已保存到上述文件，需要更多内容时请使用 read_file 的 "
        "start_line 和 line_count 分段读取。"
    )
