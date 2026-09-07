"""项目级 Skill 的发现、校验和可持久化快照。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


_SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class SkillDefinition:
    """从项目 SKILL.md 解析并可冻结到当前 Run 的不可变定义。"""

    # 项目内唯一的 Skill 名称。
    name: str
    # 用于目录展示与模型自动选择的简短描述。
    description: str
    # Skill 正文，不包含 YAML frontmatter。
    instructions: str
    # 可选的工具白名单；为空表示不额外限制。
    allowed_tools: tuple[str, ...]
    # Skill 文件相对于工作区的稳定位置。
    relative_path: str

    def to_dict(self) -> dict[str, Any]:
        """转换为工具结果和 Checkpoint 使用的稳定 JSON 对象。"""
        return {
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "allowed_tools": list(self.allowed_tools),
            "relative_path": self.relative_path,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SkillDefinition":
        """严格恢复已激活的 Skill 快照。"""
        try:
            skill = cls(
                name=payload["name"],
                description=payload["description"],
                instructions=payload["instructions"],
                allowed_tools=tuple(payload["allowed_tools"]),
                relative_path=payload["relative_path"],
            )
        except (KeyError, TypeError) as error:
            raise ValueError("Checkpoint Skill 结构无效") from error
        if (
            not _SKILL_NAME_PATTERN.fullmatch(skill.name)
            or not isinstance(skill.description, str)
            or not skill.description.strip()
            or not isinstance(skill.instructions, str)
            or not skill.instructions.strip()
            or not isinstance(skill.relative_path, str)
            or not skill.relative_path.endswith("/SKILL.md")
            or not all(isinstance(tool, str) for tool in skill.allowed_tools)
        ):
            raise ValueError("Checkpoint Skill 字段无效")
        return skill


class SkillRegistry:
    """只发现工作区 `.hexgent/skills` 下的目录式 Skill。"""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir.resolve()
        self.directory = self.workdir / ".hexgent" / "skills"
        self._skills = self._load()

    @property
    def skills(self) -> tuple[SkillDefinition, ...]:
        """按名称稳定返回当前项目可激活的 Skill。"""
        return tuple(self._skills[name] for name in sorted(self._skills))

    def get(self, name: str) -> SkillDefinition | None:
        """按稳定名称查找已发现的 Skill。"""
        return self._skills.get(name)

    def catalog(self) -> str:
        """构造小型目录上下文，不在未激活时注入完整正文。"""
        if not self._skills:
            return ""
        lines = ["<available-skills>", "以下是可按需调用 activate_skill 激活的项目 Skill："]
        for skill in self.skills:
            lines.append(f"- {skill.name}: {skill.description}")
        lines.extend(["激活后必须遵守其完整说明和工具限制。", "</available-skills>"])
        return "\n".join(lines)

    def _load(self) -> dict[str, SkillDefinition]:
        if not self.directory.is_dir():
            return {}
        skills: dict[str, SkillDefinition] = {}
        for skill_dir in sorted(self.directory.iterdir()):
            if not skill_dir.is_dir() or not _SKILL_NAME_PATTERN.fullmatch(skill_dir.name):
                continue
            path = skill_dir / "SKILL.md"
            if not path.is_file():
                continue
            definition = _parse_skill(path, self.workdir)
            if definition.name != skill_dir.name:
                raise ValueError(f"Skill 名称必须与目录名一致：{path}")
            if definition.name in skills:
                raise ValueError(f"Skill 名称重复：{definition.name}")
            skills[definition.name] = definition
        return skills


def _parse_skill(path: Path, workdir: Path) -> SkillDefinition:
    """解析 V1 支持的最小 frontmatter，拒绝歧义和未声明字段。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"无法读取 Skill 文件：{path}") from error
    frontmatter, instructions = _split_frontmatter(text, path)
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    allowed_tools = frontmatter.get("allowed_tools", ())
    if not isinstance(name, str) or not _SKILL_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Skill name 无效：{path}")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"Skill description 不能为空：{path}")
    if not isinstance(allowed_tools, tuple) or not all(
        isinstance(tool, str) and tool.strip() for tool in allowed_tools
    ):
        raise ValueError(f"Skill allowed_tools 无效：{path}")
    if not instructions.strip():
        raise ValueError(f"Skill 正文不能为空：{path}")
    return SkillDefinition(
        name,
        " ".join(description.split()),
        instructions.strip(),
        tuple(dict.fromkeys(allowed_tools)),
        path.relative_to(workdir).as_posix(),
    )


def _split_frontmatter(text: str, path: Path) -> tuple[dict[str, object], str]:
    """仅支持 name、description 和 allowed_tools，避免引入 YAML 运行依赖。"""
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"Skill 缺少 frontmatter：{path}")
    try:
        closing = lines.index("---", 1)
    except ValueError as error:
        raise ValueError(f"Skill frontmatter 未闭合：{path}") from error
    values: dict[str, object] = {}
    index = 1
    while index < closing:
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith("  - "):
            raise ValueError(f"Skill frontmatter 缺少列表字段：{path}")
        if ":" not in line:
            raise ValueError(f"Skill frontmatter 格式无效：{path}")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key not in {"name", "description", "allowed_tools"} or key in values:
            raise ValueError(f"Skill frontmatter 字段无效：{path}")
        if key != "allowed_tools":
            if not value:
                raise ValueError(f"Skill frontmatter 字段不能为空：{path}")
            values[key] = value
            index += 1
            continue
        tools: list[str] = []
        if value:
            tools = [item.strip() for item in value.split(",") if item.strip()]
            index += 1
        else:
            index += 1
            while index < closing and lines[index].startswith("  - "):
                tool = lines[index][4:].strip()
                if not tool:
                    raise ValueError(f"Skill allowed_tools 包含空值：{path}")
                tools.append(tool)
                index += 1
        values[key] = tuple(tools)
    return values, "\n".join(lines[closing + 1:])
