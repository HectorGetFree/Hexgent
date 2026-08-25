"""会话 Transcript 与大工具结果的本地持久化。"""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo


SESSION_QUERY_SLUG_MAX_LENGTH = 18
SESSION_QUERY_HASH_LENGTH = 6
TOOL_RESULT_SLUG_MAX_LENGTH = 40
TOOL_RESULT_HASH_LENGTH = 12
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

    # 由完整内容 SHA-256 生成的稳定工件标识。
    artifact_id: str

    # 标识本次调用是否复用了当前 Session 已有的物理工件。
    reused: bool = False


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

        # 保存每次 Agent Run 结构化报告及索引的目录。
        self.reports_dir = self.session_dir / "reports"

        # 当前会话采用的工具结果内联和预览限制。
        self.tool_result_limits = tool_result_limits or ToolResultLimits()

        # Transcript 中下一条记录使用的单调递增序号。
        self._next_sequence = 1

        # 标记是否已经写入会话开始事件。
        self._session_started = False

        # 首轮 Query 到达前暂存的运行时事件。
        self._pending_records: list[tuple[str, Any]] = []

        # Session 生命周期内按完整内容 Hash 复用的工具结果工件。
        self._artifact_index: dict[str, PersistedToolResult] = {}

    def _build_session_dir(self, session_id: str) -> Path:
        """根据稳定会话标识构造持久化目录。"""
        return self.workdir / ".hexgent" / "sessions" / session_id

    def _initialize_from_first_query(self, query: str) -> None:
        """使用时间和首轮 Query 摘要初始化可读的会话目录。"""
        if self.session_id is not None:
            return
        timestamp = _beijing_now().strftime("%Y%m%d-%H%M%S")
        query_hash = sha256(query.encode("utf-8")).hexdigest()[:SESSION_QUERY_HASH_LENGTH]
        candidate = f"{timestamp}_{_build_query_slug(query)}_{query_hash}"
        session_dir = self._build_session_dir(candidate)
        if session_dir.exists():
            candidate = f"{candidate}_{uuid4().hex[:8]}"
        self.session_id = candidate
        self.session_dir = self._build_session_dir(candidate)
        self.transcript_path = self.session_dir / "transcript.jsonl"
        self.tool_results_dir = self.session_dir / "tool-results"
        self.reports_dir = self.session_dir / "reports"

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

    @property
    def next_sequence(self) -> int:
        """返回下一条 Transcript 记录将使用的序号。"""
        return self._next_sequence

    def next_run_id(self) -> str:
        """根据现有 Report 文件分配 Session 内下一个运行编号。"""
        existing_numbers = []
        if self.reports_dir.is_dir():
            for path in self.reports_dir.glob("run-*.json"):
                match = re.fullmatch(r"run-(\d+)\.json", path.name)
                if match:
                    existing_numbers.append(int(match.group(1)))
        next_number = max(existing_numbers, default=0) + 1
        return f"run-{next_number:03d}"

    def read_transcript(self, start_sequence: int = 1) -> list[dict[str, Any]]:
        """读取指定序号起的 Transcript 记录，供 Session 构造派生视图。"""
        if not self.transcript_path.is_file():
            return []
        records = []
        for line in self.transcript_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("sequence", 0) >= start_sequence:
                records.append(record)
        return records

    def persist_run_report(self, report: dict[str, Any]) -> Path:
        """原子保存单次 Run Report，并更新当前 Session 的报告索引。"""
        # 1. 每个已经结束的 Run 独占一个不可变 JSON 文件。
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        run_id = report["run_id"]
        report_path = self.reports_dir / f"{run_id}.json"
        _write_json_atomically(report_path, report)

        # 2. 索引只保留导航字段，完整详情继续由独立 Run Report 承载。
        index_path = self.reports_dir / "index.json"
        if index_path.is_file():
            index = json.loads(index_path.read_text(encoding="utf-8"))
        else:
            index = {
                "schema_version": 1,
                "session_id": self.session_id,
                "runs": [],
            }
        index["runs"] = [
            item for item in index["runs"] if item.get("run_id") != run_id
        ]
        index["runs"].append(
            {
                "run_id": run_id,
                "status": report["status"],
                "user_query": report["user_query"],
                "started_at": report["started_at"],
                "finished_at": report["finished_at"],
                "relative_path": str(report_path.relative_to(self.session_dir)),
            }
        )
        index["run_count"] = len(index["runs"])
        index["latest_run_id"] = run_id
        index["latest_status"] = report["status"]
        index["updated_at"] = report["finished_at"]
        _write_json_atomically(index_path, index)
        return report_path

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

        # 1. 工件使用完整内容 Hash 标识，Context 缓存清空不影响 Session 复用。
        artifact_id = sha256(output.encode("utf-8")).hexdigest()
        existing = self._find_persisted_tool_result(artifact_id, output)
        reused = existing is not None
        if existing is None:
            self.tool_results_dir.mkdir(parents=True, exist_ok=True)
            filename = _build_tool_result_filename(
                tool_name,
                description,
                artifact_id,
            )
            result_path = self.tool_results_dir / filename
            result_path.write_text(output, encoding="utf-8")
            relative_path = str(result_path.relative_to(self.workdir))
        else:
            relative_path = existing.relative_path

        # 2. 每次调用都生成独立返回值和 Transcript 映射，但只保存一份正文。
        persisted = PersistedToolResult(
            relative_path=relative_path,
            original_characters=len(output),
            preview=output[: limits.preview_characters],
            artifact_id=artifact_id,
            reused=reused,
        )
        self._artifact_index[artifact_id] = persisted
        self.append_transcript(
            "tool_result_persisted",
            {
                "call_id": call_id,
                "tool_name": tool_name,
                "artifact_id": artifact_id,
                "relative_path": relative_path,
                "original_characters": len(output),
                "reused": reused,
            },
        )
        return persisted

    def _find_persisted_tool_result(
        self,
        artifact_id: str,
        output: str,
    ) -> PersistedToolResult | None:
        """从内存索引或 Session 目录查找内容完全一致的已有工件。"""
        cached = self._artifact_index.get(artifact_id)
        if cached is not None and (self.workdir / cached.relative_path).is_file():
            return cached

        if not self.tool_results_dir.is_dir():
            return None
        pattern = f"*__{artifact_id[:TOOL_RESULT_HASH_LENGTH]}.txt"
        for candidate in self.tool_results_dir.glob(pattern):
            try:
                existing_output = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if existing_output != output:
                continue
            return PersistedToolResult(
                relative_path=str(candidate.relative_to(self.workdir)),
                original_characters=len(output),
                preview=output[: self.tool_result_limits.preview_characters],
                artifact_id=artifact_id,
                reused=True,
            )
        return None


def _build_query_slug(query: str) -> str:
    """把首轮 Query 转换为长度受限且适合目录名的摘要。"""
    normalized = re.sub(r"\s+", " ", query).strip()
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", normalized)
    safe = safe.strip(" ._")[:SESSION_QUERY_SLUG_MAX_LENGTH].strip(" ._")
    return safe or "空白请求"


def _build_tool_result_filename(
    tool_name: str,
    description: str | None,
    artifact_id: str,
) -> str:
    """使用短语义名称和内容 Hash 构造稳定的工件文件名。"""
    # 1. 文件工具只保留目标 basename，避免绝对路径和 Session 名递归膨胀。
    readable_argument = _extract_readable_argument(description)
    readable_source = "_".join(
        part for part in (tool_name, readable_argument) if part
    )
    readable_source = readable_source or "tool_result"
    readable_source = readable_source.encode("utf-8", errors="replace").decode("utf-8")
    normalized = re.sub(r"\s+", "_", readable_source).strip()
    readable_slug = re.sub(r"[^\w.-]", "_", normalized, flags=re.UNICODE)
    readable_slug = re.sub(r"_+", "_", readable_slug)
    readable_slug = readable_slug.strip(" ._")[:TOOL_RESULT_SLUG_MAX_LENGTH]
    readable_slug = readable_slug.rstrip(" ._") or "tool_result"

    # 2. 内容 Hash 负责物理工件唯一性，call_id 只保留在 Transcript 映射中。
    return f"{readable_slug}__{artifact_id[:TOOL_RESULT_HASH_LENGTH]}.txt"


def _extract_readable_argument(description: str | None) -> str:
    """从工具调用说明中提取适合文件名的短参数。"""
    if not description:
        return ""
    match = re.match(r"^[^(]+\((.*)\)$", description.strip())
    value = match.group(1) if match else description
    value = value.split(",", 1)[0].strip()
    if "/" in value or "\\" in value:
        return Path(value).name
    return value


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


def _write_json_atomically(path: Path, value: Any) -> None:
    """先写临时文件再替换目标，避免留下半份 JSON 工件。"""
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, default=_json_default)
            + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


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
