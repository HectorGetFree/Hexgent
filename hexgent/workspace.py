"""发现 Hexgent 当前会话使用的工作区根目录。"""

from pathlib import Path


def find_workspace_root(start: Path | None = None) -> Path:
    """从起始目录向上查找最近的项目根目录。"""
    current = (start or Path.cwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() or (candidate / ".git").exists():
            return candidate
    return current
