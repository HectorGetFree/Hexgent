"""会话 Transcript 与大工具结果的本地持久化。"""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from threading import RLock
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo


SESSION_QUERY_HASH_LENGTH = 8
TOOL_RESULT_SLUG_MAX_LENGTH = 40
TOOL_RESULT_HASH_LENGTH = 12
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
TRANSCRIPT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class ToolResultLimits:
    """控制工具结果内联和落盘预览的大小。"""

    # 工具结果超过该字符数后不再完整放入模型上下文。
    max_inline_characters: int = 12_000

    # 大结果落盘后继续向模型展示的开头预览字符数。
    preview_characters: int = 500

    # 同一工具批次允许完整内联到模型上下文的结果 Token 总量。
    max_batch_inline_tokens: int = 3_000

    # 同一工具批次所有外置结果允许保留的预览 Token 总量。
    max_batch_preview_tokens: int = 1_000


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

        # 首轮 Query 的展示名称，与机器使用的 Session ID 分离。
        self.display_name: str | None = None

        # 当前会话 Transcript 和工具结果所在目录。
        self.session_dir = self._build_session_dir(session_id or "待初始化会话")

        # 追加写入完整会话事实的 JSONL 文件。
        self.transcript_path = self.session_dir / "transcript.jsonl"

        # 保存超出内联预算的完整工具结果的目录。
        self.tool_results_dir = self.session_dir / "tool-results"

        # 保存每次 Agent Run 结构化报告及索引的目录。
        self.reports_dir = self.session_dir / "reports"

        # 保存当前有效/失效 ContextRecord 的可再生 JSON 投影。
        self.context_records_path = self.session_dir / "context-records.json"

        # 当前会话采用的工具结果内联和预览限制。
        self.tool_result_limits = tool_result_limits or ToolResultLimits()

        # Transcript 中下一条记录使用的单调递增序号。
        self._next_sequence = 1

        # Runtime 控制线程和 Agent 工具线程可能并发追加事实；序号分配与写入必须原子。
        self._transcript_lock = RLock()

        # 标记是否已经写入会话开始事件。
        self._session_started = False

        # 首轮 Query 到达前暂存的运行时事件。
        self._pending_records: list[tuple[str, Any]] = []

        # Session 生命周期内按完整内容 Hash 复用的工具结果工件。
        self._artifact_index: dict[str, PersistedToolResult] = {}

    @classmethod
    def open_existing(
        cls,
        workdir: Path,
        session_id: str,
        tool_result_limits: ToolResultLimits | None = None,
    ) -> "SessionStore":
        """打开一个已经存在且采用当前恢复协议的 Session。"""
        # 1. 先按稳定 Session ID 定位 Transcript，不允许静默创建空会话。
        store = cls(workdir, session_id, tool_result_limits)
        if not store.transcript_path.is_file():
            raise FileNotFoundError(f"Session Transcript 不存在：{session_id}")

        # 2. 完整校验记录序号和协议版本，再恢复追加写入位置。
        records = store.read_transcript()
        if not records or records[0]["type"] != "session_started":
            raise ValueError("Session Transcript 缺少 session_started 记录")
        recorded_session_id = records[0]["payload"].get("session_id")
        if recorded_session_id != session_id:
            raise ValueError(
                f"Session 标识不一致：目录为 {session_id}，记录为 {recorded_session_id}"
            )
        first_query = records[0]["payload"].get("first_query")
        if isinstance(first_query, str):
            store.display_name = _build_display_name(first_query)
        store._next_sequence = records[-1]["sequence"] + 1
        store._session_started = True
        return store

    @classmethod
    def list_restorable_session_ids(
        cls,
        workdir: Path,
        *,
        limit: int = 10,
    ) -> list[str]:
        """列出最近创建且采用当前 Transcript 协议的 Session。"""
        # 1. Session 目录不存在时返回空列表，终端入口无需区分首次运行。
        sessions_dir = workdir.resolve() / ".hexgent" / "sessions"
        if not sessions_dir.is_dir():
            return []

        # 2. 只读取首条记录筛选协议兼容会话，完整合法性留给恢复阶段校验。
        candidates: list[tuple[float, str]] = []
        for session_dir in sessions_dir.iterdir():
            transcript_path = session_dir / "transcript.jsonl"
            if not session_dir.is_dir() or not transcript_path.is_file():
                continue
            try:
                with transcript_path.open("r", encoding="utf-8") as transcript:
                    first_line = transcript.readline()
                record = json.loads(first_line)
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            payload = record.get("payload")
            if (
                record.get("schema_version") != TRANSCRIPT_SCHEMA_VERSION
                or record.get("type") != "session_started"
                or not isinstance(payload, dict)
                or payload.get("session_id") != session_dir.name
            ):
                continue
            candidates.append((transcript_path.stat().st_mtime, session_dir.name))

        # 3. 最近写入的会话优先展示，并在适配层控制最大展示数量。
        candidates.sort(reverse=True)
        return [session_id for _, session_id in candidates[:limit]]

    @classmethod
    def list_restorable_session_details(
        cls,
        workdir: Path,
        *,
        limit: int = 10,
    ) -> list[tuple[str, str]]:
        """列出恢复所需的机器 ID 和人类可读名称。"""
        # 1. ID 列表已经过滤协议和目录合法性，展示名称从首条事实读取。
        details: list[tuple[str, str]] = []
        for session_id in cls.list_restorable_session_ids(workdir, limit=limit):
            transcript_path = (
                workdir.resolve() / ".hexgent" / "sessions" / session_id / "transcript.jsonl"
            )
            try:
                with transcript_path.open("r", encoding="utf-8") as transcript:
                    record = json.loads(transcript.readline())
                payload = record.get("payload", {})
                display_name = payload.get("display_name") or payload.get("first_query")
            except (OSError, UnicodeError, json.JSONDecodeError):
                display_name = None
            details.append(
                (
                    session_id,
                    display_name if isinstance(display_name, str) else session_id,
                )
            )
        return details

    def _build_session_dir(self, session_id: str) -> Path:
        """根据稳定会话标识构造持久化目录。"""
        return self.workdir / ".hexgent" / "sessions" / session_id

    def _initialize_from_first_query(self, query: str) -> None:
        """使用时间和内容 Hash 初始化纯 ASCII 的会话目录。"""
        if self.session_id is not None:
            return
        timestamp = _beijing_now().strftime("%Y%m%d-%H%M%S")
        query_hash = sha256(query.encode("utf-8")).hexdigest()[:SESSION_QUERY_HASH_LENGTH]
        candidate = f"{timestamp}-{query_hash}"
        session_dir = self._build_session_dir(candidate)
        if session_dir.exists():
            candidate = f"{candidate}-{uuid4().hex[:8]}"
        self.session_id = candidate
        self.display_name = _build_display_name(query)
        self.session_dir = self._build_session_dir(candidate)
        self.transcript_path = self.session_dir / "transcript.jsonl"
        self.tool_results_dir = self.session_dir / "tool-results"
        self.reports_dir = self.session_dir / "reports"
        self.context_records_path = self.session_dir / "context-records.json"

    def start_session(self, first_query: str) -> None:
        """确保会话已由首轮 Query 命名，并记录会话开始事实。"""
        if self._session_started:
            return
        self._initialize_from_first_query(first_query)
        self._session_started = True
        self.append_transcript(
            "session_started",
            {
                "session_id": self.session_id,
                "display_name": self.display_name,
                "first_query": first_query,
            },
        )
        pending_records = self._pending_records
        self._pending_records = []
        for record_type, payload in pending_records:
            self.append_transcript(record_type, payload)

    def start_named_session(self, session_id: str, display_name: str) -> None:
        """在首个用户 Query 前创建具有外部稳定 ID 的持久化 Session。"""
        if self._session_started:
            return
        valid_characters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        if not session_id or any(
            character not in valid_characters for character in session_id
        ):
            raise ValueError("Session ID 只能包含字母、数字、下划线和连字符")
        self.session_id = session_id
        self.display_name = display_name
        self.session_dir = self._build_session_dir(session_id)
        self.transcript_path = self.session_dir / "transcript.jsonl"
        self.tool_results_dir = self.session_dir / "tool-results"
        self.reports_dir = self.session_dir / "reports"
        self.context_records_path = self.session_dir / "context-records.json"
        self._session_started = True
        self.append_transcript(
            "session_started",
            {
                "session_id": self.session_id,
                "display_name": self.display_name,
                "first_query": None,
            },
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
        with self._transcript_lock:
            if self.session_id is None:
                self._pending_records.append((record_type, payload))
                return
            self._ensure_session_dir()
            record = {
                "schema_version": TRANSCRIPT_SCHEMA_VERSION,
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
        with self._transcript_lock:
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
        """严格读取并校验当前协议的 Transcript 记录。"""
        if not self.transcript_path.is_file():
            return []
        records: list[dict[str, Any]] = []
        expected_sequence = 1
        for line_number, line in enumerate(
            self.transcript_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Transcript 第 {line_number} 行不是有效 JSON：{error.msg}"
                ) from error
            if record.get("schema_version") != TRANSCRIPT_SCHEMA_VERSION:
                raise ValueError(
                    f"Transcript 第 {line_number} 行协议版本无效，"
                    f"当前仅支持 {TRANSCRIPT_SCHEMA_VERSION}"
                )
            if record.get("sequence") != expected_sequence:
                raise ValueError(
                    f"Transcript 序号不连续：期望 {expected_sequence}，"
                    f"收到 {record.get('sequence')}"
                )
            if not isinstance(record.get("type"), str) or not isinstance(
                record.get("payload"), dict
            ):
                raise ValueError(f"Transcript 第 {line_number} 行结构无效")
            expected_sequence += 1
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

    def persist_context_records(self, projection: dict[str, Any]) -> Path | None:
        """原子保存 Session ContextRecord 投影，恢复仍以 Transcript 为准。"""
        if self.session_id is None:
            return None
        self._ensure_session_dir()
        _write_json_atomically(self.context_records_path, projection)
        return self.context_records_path

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


def _build_display_name(query: str) -> str:
    """生成用于终端和历史列表展示的首轮 Query 摘要。"""
    normalized = re.sub(r"\s+", " ", query).strip()
    return normalized[:40] or "空白请求"


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
