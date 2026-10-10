"""冻结 fixture 的完整性校验、隔离 clone 与 Stage A snapshot。"""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import Iterator


FIXTURE_ROOT = Path(__file__).with_name("fixtures")
FIXTURE_IDS = frozenset({"research-synthesis", "document-operations", "project-workflow", "technical-mini-service"})


def baseline_path(fixture_id: str) -> Path:
    if fixture_id not in FIXTURE_IDS:
        raise KeyError(f"未知 fixture：{fixture_id}")
    path = FIXTURE_ROOT / fixture_id
    if not path.is_dir():
        raise FileNotFoundError(f"fixture baseline 不存在：{path}")
    return path


@contextmanager
def cloned_workspace(fixture_id: str) -> Iterator[Path]:
    """为每一次 Agent run 产生独立工作区，绝不复用已修改目录。"""

    with TemporaryDirectory(prefix=f"hexgent-phase5c-{fixture_id}-") as directory:
        target = Path(directory) / "workspace"
        shutil.copytree(baseline_path(fixture_id), target)
        yield target


def freeze_snapshot(workspace: Path, destination: Path) -> Path:
    """冻结完整 Stage A workspace；L1 明确要求连同 .hexgent durable state 保存。"""

    if destination.exists():
        raise FileExistsError(f"snapshot 已存在：{destination}")
    shutil.copytree(workspace, destination)
    return destination


@contextmanager
def cloned_snapshot(snapshot: Path) -> Iterator[Path]:
    """由同一 Stage A snapshot 派生 ON/OFF 的两个互不污染的 Stage B 副本。"""

    if not snapshot.is_dir():
        raise FileNotFoundError(f"snapshot 不存在：{snapshot}")
    with TemporaryDirectory(prefix="hexgent-phase5c-stage-b-") as directory:
        target = Path(directory) / "workspace"
        shutil.copytree(snapshot, target)
        yield target


def workspace_manifest(workspace: Path, *, include_hexgent: bool = False) -> dict[str, str]:
    """返回内容寻址 manifest；默认忽略 Runtime 自身写入的 .hexgent 工件。"""

    manifest: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if not include_hexgent and relative.parts and relative.parts[0] == ".hexgent":
            continue
        manifest[str(relative)] = sha256(path.read_bytes()).hexdigest()
    return manifest


def changed_paths(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
