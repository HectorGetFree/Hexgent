"""以 Markdown 索引和独立条目文件持久化跨 Session Memory。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from contextlib import contextmanager
import fcntl
import os
import re
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable, Literal
from zoneinfo import ZoneInfo


MemoryCategory = Literal[
    "preference",  # 用户长期偏好。
    "constraint",  # 持续有效且不可忽略的边界。
    "project",  # 已验证的项目级知识。
    "decision",  # 仍然有效的设计决策。
]
MemorySource = Literal[
    "explicit",  # 用户明确要求记住。
    "automatic",  # 从稳定 ContextRecord 自动筛选。
]
MemoryStatus = Literal["ACTIVE", "SUPERSEDED", "INVALIDATED"]

_CATEGORY_TITLES: dict[MemoryCategory, str] = {
    "preference": "Preferences",
    "constraint": "Constraints",
    "project": "Project Knowledge",
    "decision": "Decisions",
}
_VALID_CATEGORIES = set(_CATEGORY_TITLES)


@dataclass(frozen=True)
class MemoryItem:
    """一条可跨 Session 召回的完整 Memory 内容。"""

    # 由类别和正文生成的稳定机器标识。
    memory_id: str
    # 面向模型和用户的完整正文；当前版本限制为单段短文本。
    content: str
    # 决定索引分组和检索规则的最小语义类别。
    category: MemoryCategory
    # 为词法召回准备的显式关键词。
    keywords: tuple[str, ...]
    # explicit 或 automatic，说明写入入口。
    source: MemorySource
    # 自动沉淀时引用的 ContextRecord 标识；显式记忆为空。
    source_record_ids: tuple[str, ...]
    # 最近一次写入或合并的北京时间。
    updated_at: str
    status: MemoryStatus = "ACTIVE"
    superseded_by: str | None = None


@dataclass(frozen=True)
class MemoryMutation:
    """一次写入独立条目文件后的精确结果。"""

    action: Literal["created", "updated"]
    item: MemoryItem


class MemoryStore:
    """维护内容权威条目、可再生索引和自动提取运行状态。"""

    def __init__(self, workdir: Path) -> None:
        self.directory = workdir.resolve() / ".hexgent" / "memory"
        # MEMORY.md 只承担人类浏览，不决定生命周期有效性。
        self.path = self.directory / "MEMORY.md"
        # 旧格式以条目为权威；迁移后 lifecycle.json 是事务权威，条目为正文投影。
        self.items_directory = self.directory / "items"
        # 自动提取游标是运行状态，不混入索引正文。
        self.state_path = self.directory / ".state.json"
        self.lifecycle_path = self.directory / "lifecycle.json"
        self._items = {}
        self._processed_record_ids = self._load_state()

    @contextmanager
    def transaction(self):
        """1. 同一目录的跨进程修改和请求启动共享锁；不锁住整个响应流。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / ".lifecycle.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                self._reload_authority()
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _reload_authority(self):
        if not self.lifecycle_path.exists():
            self._items = self._load_items()
            if not self._items and self.path.exists() and _looks_like_legacy_memory(self.path):
                self._items, self._processed_record_ids = _load_legacy_memory(self.path)
                self._write_all()
            self._revision = 0
            self._deleted = set()
            self._commit_authority()
            return
        payload = json.loads(self.lifecycle_path.read_text(encoding="utf-8"))
        if (payload.get("schema_version") != 1 or not isinstance(payload.get("revision"), int)
                or not isinstance(payload.get("items"), list) or not isinstance(payload.get("deleted"), list)):
            raise ValueError("Memory 生命周期版本无效")
        self._revision = payload["revision"]
        self._deleted = set(payload["deleted"])
        for identity in self._deleted:
            _validate_memory_id(identity)
        self._items = {}
        for raw in payload["items"]:
            raw = dict(raw)
            raw["keywords"] = tuple(raw["keywords"])
            raw["source_record_ids"] = tuple(raw["source_record_ids"])
            item = MemoryItem(**raw)
            if item.superseded_by is not None:
                _validate_memory_id(item.superseded_by)
            if (item.category not in _VALID_CATEGORIES or item.source not in {"explicit", "automatic"}
                    or item.status not in {"ACTIVE", "SUPERSEDED", "INVALIDATED"}
                    or item.memory_id != _build_memory_id(item.category, item.content)
                    or item.memory_id in self._deleted or item.memory_id in self._items):
                raise ValueError("Memory 生命周期条目无效")
            self._items[item.memory_id] = item

    def _commit_authority(self):
        self._revision += 1
        _write_text_atomically(self.lifecycle_path, json.dumps({
            "schema_version": 1, "revision": self._revision,
            "deleted": sorted(self._deleted),
            "items": [asdict(item) for item in self._items.values()],
        }, ensure_ascii=False))

    def revalidate(self, memories):
        """2. Checkpoint 正文只作候选，当前清单决定是否仍可注入。"""
        if not memories:
            return ()
        with self.transaction():
            return tuple(m for m in memories if self._is_current(m.memory_id, m.content))

    def _is_current(self, identity, content):
        item = self._items.get(identity)
        return item is not None and item.status == "ACTIVE" and item.content == content

    def is_active_content(self, content: str, category: MemoryCategory) -> bool:
        """精确工具缓存命中前仍须确认相同正文未撤销。"""
        normalized = _normalize_content(content)
        with self.transaction():
            return self._is_current(_build_memory_id(category, normalized), normalized)

    @contextmanager
    def request_guard(self, request):
        """3. 复核结构化 Memory 区块并在锁内启动 Provider 请求。"""
        from .memory_retrieval import RetrievedMemory, format_retrieved_memory
        if "<relevant-memory>" not in str(request.get("instructions", "")):
            yield request, ()
            return
        with self.transaction():
            request = dict(request)
            receipt = []
            def filter_section(match):
                block = match.group(0)
                payload = json.loads(block[block.index("[{"):block.rindex("</relevant-memory>")].strip())
                selected = []
                for raw in payload:
                    if self._is_current(raw["memory_id"], raw["content"]):
                        category = self._items[raw["memory_id"]].category
                        selected.append(RetrievedMemory(raw["memory_id"], raw["content"], category, 0, ()))
                        receipt.append({"memory_id": raw["memory_id"], "category": category,
                                        "content_sha256": sha256(raw["content"].encode()).hexdigest()})
                return format_retrieved_memory(tuple(selected))
            request["instructions"] = re.sub(r"<relevant-memory>.*?</relevant-memory>", filter_section,
                                              str(request.get("instructions", "")), flags=re.S)
            yield request, tuple(receipt)

    def supersede(self, old_id: str, content: str, category: MemoryCategory,
                  *, keywords: Iterable[str] = ()) -> MemoryMutation:
        _validate_memory_id(old_id)
        with self.transaction():
            old = self._items.get(old_id)
            if old is None:
                raise ValueError("MEMORY_NOT_FOUND")
            identity = _build_memory_id(category, _normalize_content(content))
            if old.status == "SUPERSEDED" and old.superseded_by == identity:
                target = self._items.get(identity)
                if target is None or target.status != "ACTIVE":
                    raise ValueError("MEMORY_LIFECYCLE_CONFLICT")
                return MemoryMutation("updated", target)
            if old.status != "ACTIVE" or identity == old_id:
                raise ValueError("MEMORY_LIFECYCLE_CONFLICT")
            target = self._upsert_locked(content, category, keywords=keywords, source="explicit")
            self._items[old_id] = replace(old, status="SUPERSEDED", superseded_by=identity, updated_at=_beijing_now())
            self._commit_authority()
            self._write_index()
            return target

    def invalidate(self, memory_id: str) -> MemoryItem:
        _validate_memory_id(memory_id)
        with self.transaction():
            item = self._items.get(memory_id)
            if item is None:
                raise ValueError("MEMORY_NOT_FOUND")
            if item.status == "ACTIVE":
                self._items[memory_id] = replace(item, status="INVALIDATED", updated_at=_beijing_now())
                self._commit_authority()
                self._write_index()
            return self._items[memory_id]

    def delete(self, memory_id: str) -> None:
        _validate_memory_id(memory_id)
        with self.transaction():
            if memory_id not in self._items and memory_id not in self._deleted:
                raise ValueError("MEMORY_NOT_FOUND")
            if memory_id not in self._deleted:
                self._items.pop(memory_id, None)
                self._deleted.add(memory_id)
                self._commit_authority()
            (self.items_directory / f"{memory_id}.md").unlink(missing_ok=True)
            if self.items_directory.exists():
                descriptor = os.open(self.items_directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            self._write_index()

    @property
    def items(self) -> tuple[MemoryItem, ...]:
        """刷新当前权威清单，按类别和稳定标识返回有效 Memory。"""
        if not self.lifecycle_path.exists() and not self.items_directory.exists() and not self.path.exists():
            return ()
        with self.transaction():
            return tuple(sorted((i for i in self._items.values() if i.status == "ACTIVE"), key=lambda item: (item.category, item.memory_id)))

    @property
    def all_items(self) -> tuple[MemoryItem, ...]:
        """管理入口读取包括失效条目的审计视图；不得直接作为检索候选。"""
        with self.transaction():
            return tuple(sorted(self._items.values(), key=lambda item: item.memory_id))

    def pending_context_record_ids(self, record_ids: Iterable[str]) -> tuple[str, ...]:
        return tuple(record_id for record_id in record_ids if record_id not in self._processed_record_ids)

    def upsert(
        self,
        content: str,
        category: MemoryCategory,
        *,
        keywords: Iterable[str] = (),
        source: MemorySource,
        source_record_ids: Iterable[str] = (),
    ) -> MemoryMutation:
        """在跨进程锁内提交内容与生命周期。"""
        with self.transaction():
            result = self._upsert_locked(content, category, keywords=keywords, source=source, source_record_ids=source_record_ids)
            self._commit_authority()
            self._write_index()
            return result

    def _upsert_locked(self, content, category, *, keywords=(), source, source_record_ids=()):
        normalized_content = _normalize_content(content)
        if category not in _VALID_CATEGORIES:
            raise ValueError("Memory category 无效")
        normalized_keywords = _normalize_keywords(keywords)
        normalized_sources = _normalize_record_ids(source_record_ids)
        if source == "explicit" and normalized_sources:
            raise ValueError("显式 Memory 不应携带 ContextRecord 来源")
        if source == "automatic" and not normalized_sources:
            raise ValueError("自动 Memory 必须携带 ContextRecord 来源")

        memory_id = _build_memory_id(category, normalized_content)
        existing = self._items.get(memory_id)
        if memory_id in self._deleted or (existing is not None and existing.status != "ACTIVE"):
            raise ValueError("MEMORY_REVOKED")
        if existing is not None and (existing.content != normalized_content or existing.category != category):
            raise ValueError("Memory 标识碰撞")
        now = _beijing_now()
        if existing is None:
            item = MemoryItem(memory_id, normalized_content, category, normalized_keywords, source, normalized_sources, now)
            action: Literal["created", "updated"] = "created"
        else:
            item = replace(
                existing,
                keywords=tuple(sorted(set(existing.keywords) | set(normalized_keywords))),
                source_record_ids=tuple(sorted(set(existing.source_record_ids) | set(normalized_sources))),
                updated_at=now,
            )
            action = "updated"
        self._items[item.memory_id] = item
        self._write_item(item)
        return MemoryMutation(action, item)

    def mark_context_records_processed(self, record_ids: Iterable[str]) -> None:
        normalized = _normalize_record_ids(record_ids)
        if normalized:
            self._processed_record_ids.update(normalized)
            self._write_state()

    def _load_items(self) -> dict[str, MemoryItem]:
        if not self.items_directory.is_dir():
            return {}
        items: dict[str, MemoryItem] = {}
        for path in sorted(self.items_directory.glob("mem-*.md")):
            item = _parse_item(path)
            if item.memory_id in items:
                raise ValueError(f"Memory 条目标识重复：{item.memory_id}")
            items[item.memory_id] = item
        return items

    def _load_state(self) -> set[str]:
        if not self.state_path.exists():
            return set()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return set(_normalize_record_ids(payload.get("processed_context_record_ids", [])))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise ValueError("Memory 自动提取状态无效") from error

    def _write_all(self) -> None:
        for item in self._items.values():
            self._write_item(item)
        self._write_state()
        self._write_index()

    def _write_item(self, item: MemoryItem) -> None:
        self.items_directory.mkdir(parents=True, exist_ok=True)
        _write_text_atomically(self.items_directory / f"{item.memory_id}.md", _format_item(item))

    def _write_state(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        _write_text_atomically(
            self.state_path,
            json.dumps({"processed_context_record_ids": sorted(self._processed_record_ids)}, ensure_ascii=False, separators=(",", ":")),
        )

    def _write_index(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        _write_text_atomically(self.path, _format_index(tuple(i for i in self._items.values() if i.status == "ACTIVE")))


def _format_item(item: MemoryItem) -> str:
    return "\n".join([
        "---", f"id: {item.memory_id}", f"category: {item.category}",
        f"keywords: {'、'.join(item.keywords)}", f"source: {item.source}",
        f"source_record_ids: {'、'.join(item.source_record_ids)}", f"updated_at: {item.updated_at}",
        "---", "", item.content, "",
    ])


def _parse_item(path: Path) -> MemoryItem:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RuntimeError(f"无法读取 Memory 条目：{path}") from error
    if len(lines) < 10 or lines[0] != "---":
        raise ValueError(f"Memory 条目格式无效：{path.name}")
    try:
        closing = lines.index("---", 1)
        fields = dict(line.split(": ", 1) for line in lines[1:closing])
        item = MemoryItem(
            fields["id"], _normalize_content("\n".join(lines[closing + 2:])),
            fields["category"], _normalize_keywords(fields["keywords"].split("、")),
            fields["source"], _normalize_record_ids(
                () if not fields["source_record_ids"] else fields["source_record_ids"].split("、")
            ), fields["updated_at"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Memory 条目格式无效：{path.name}") from error
    if item.category not in _VALID_CATEGORIES or item.source not in {"explicit", "automatic"}:
        raise ValueError(f"Memory 条目字段无效：{path.name}")
    if item.memory_id != _build_memory_id(item.category, item.content):
        raise ValueError(f"Memory 条目标识与内容不一致：{path.name}")
    if path.stem != item.memory_id:
        raise ValueError(f"Memory 条目文件名与标识不一致：{path.name}")
    return item


def _format_index(items: tuple[MemoryItem, ...]) -> str:
    lines = ["# Hexgent Memory", "", "<!-- 此文件是可再生索引；完整内容位于 items/。 -->", ""]
    for category, title in _CATEGORY_TITLES.items():
        lines.extend([f"## {title}", ""])
        for item in (entry for entry in items if entry.category == category):
            summary = item.content[:80] + ("..." if len(item.content) > 80 else "")
            lines.extend([f"- [{item.memory_id}](items/{item.memory_id}.md) — {summary}", f"  - 关键词：{'、'.join(item.keywords) or '无'}", ""])
    return "\n".join(lines)


def _looks_like_legacy_memory(path: Path) -> bool:
    return "### mem-" in path.read_text(encoding="utf-8")


def _load_legacy_memory(path: Path) -> tuple[dict[str, MemoryItem], set[str]]:
    """读取上一版单文件 Memory，迁移时不丢失已保存条目。"""
    import re
    text = path.read_text(encoding="utf-8")
    processed_match = re.search(r"<!-- hexgent-memory-state: (.+?) -->", text)
    processed = set()
    if processed_match:
        processed = set(_normalize_record_ids(json.loads(processed_match.group(1)).get("processed_context_record_ids", [])))
    titles = {title: category for category, title in _CATEGORY_TITLES.items()}
    items: dict[str, MemoryItem] = {}
    for category_title, block in re.findall(r"(?ms)^## (.+?)\n(.*?)(?=^## |\Z)", text):
        category = titles.get(category_title.strip())
        if category is None:
            continue
        for item_id, content, keywords, source, record_ids, updated_at in re.findall(
            r"(?ms)^### (mem-[^\n]+)\n- 内容：(.*?)\n- 关键词：(.*?)\n- 来源：(.*?)\n- ContextRecord：(.*?)\n- 更新时间：(.*?)$", block,
        ):
            normalized = _normalize_content(content)
            item = MemoryItem(_build_memory_id(category, normalized), normalized, category, _normalize_keywords(keywords.split("、")), "explicit" if source == "用户明确要求" else "automatic", _normalize_record_ids(() if record_ids == "无" else record_ids.split("、")), updated_at)
            items[item.memory_id] = item
    return items, processed


def _write_text_atomically(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _normalize_content(value: str) -> str:
    content = " ".join(value.split())
    if not content or len(content) > 500:
        raise ValueError("Memory 内容必须是 1 到 500 字符的非空文本")
    return content


def _normalize_keywords(values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({" ".join(value.split()).lower() for value in values if isinstance(value, str) and value.strip()}))
    if len(normalized) > 12 or any(len(value) > 64 for value in normalized):
        raise ValueError("Memory 关键词数量或长度超出限制")
    return normalized


def _normalize_record_ids(values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(sorted(set(values)))
    if any(not isinstance(value, str) or not value.startswith("ctx-") for value in normalized):
        raise ValueError("Memory ContextRecord 来源标识无效")
    return normalized


def _build_memory_id(category: MemoryCategory, content: str) -> str:
    digest = sha256(f"{category}\n{content}".encode("utf-8")).hexdigest()[:12]
    return f"mem-{digest}"


def _validate_memory_id(identity: str) -> None:
    if not isinstance(identity, str) or re.fullmatch(r"mem-[a-f0-9]{12}", identity) is None:
        raise ValueError("Memory 标识无效")


def _beijing_now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
