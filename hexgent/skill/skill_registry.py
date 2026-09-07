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
    """只发现工作区 `skills` 下的目录式 Skill。"""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir.resolve()
        # 项目 Skill 属于可版本管理的工作流资产，不应写入被 Git 忽略的运行时目录。
        self.directory = self.workdir / "skills"
        self._skills = self._load()

    @property
    def skills(self) -> tuple[SkillDefinition, ...]:
        """按名称稳定返回当前项目可激活的 Skill。"""
        return tuple(self._skills[name] for name in sorted(self._skills))

    def get(self, name: str) -> SkillDefinition | None:
        """按稳定名称查找已发现的 Skill。"""
        return self._skills.get(name)

    def refresh(self) -> None:
        """重新扫描项目目录，使新创建的 Skill 在下一轮可被发现。"""
        self._skills = self._load()

    def create(
        self,
        name: str,
        description: str,
        allowed_tools: tuple[str, ...],
        instructions: str,
    ) -> SkillDefinition:
        """原子创建新的目录式 Skill，拒绝覆盖已经存在的文件。"""
        if not _SKILL_NAME_PATTERN.fullmatch(name):
            raise ValueError("Skill name 必须由小写字母、数字和短横线组成")
        target = self.directory / name / "SKILL.md"
        if target.exists():
            raise FileExistsError(f"Skill 已存在，不能覆盖：{name}")
        candidate = SkillDefinition(
            name,
            " ".join(description.split()),
            instructions.strip(),
            tuple(dict.fromkeys(allowed_tools)),
            target.relative_to(self.workdir).as_posix(),
        )
        _validate_skill(candidate, target)

        # 1. 只在项目级目标目录中创建文件，临时文件替换避免半份 SKILL.md。
        target.parent.mkdir(parents=True, exist_ok=False)
        temporary = target.with_suffix(".md.tmp")
        try:
            temporary.write_text(_format_skill(candidate), encoding="utf-8")
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            target.parent.rmdir()
            raise

        # 2. 创建成功后刷新目录；后续模型轮次即可通过 Catalog 看到新 Skill。
        self.refresh()
        created = self.get(name)
        if created is None:
            raise RuntimeError(f"Skill 创建后未被注册表发现：{name}")
        return created

    def catalog(self) -> str:
        """构造小型目录上下文，不在未激活时注入完整正文。"""
        if not self._skills:
            return ""
        lines = ["<available-skills>", "以下是可按需调用 activate_skill 激活的项目 Skill："]
        for skill in self.skills:
            lines.append(f"- {skill.name}: {skill.description}")
        lines.extend([
            "当用户目标与某个 Skill 描述明显匹配时，应先调用 activate_skill，再执行其工作流。",
            "不匹配时不要激活；激活后必须遵守其完整说明和工具限制。",
            "</available-skills>",
        ])
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
    definition = SkillDefinition(
        name,
        " ".join(description.split()),
        instructions.strip(),
        tuple(dict.fromkeys(allowed_tools)),
        path.relative_to(workdir).as_posix(),
    )
    _validate_skill(definition, path)
    return definition


def _validate_skill(skill: SkillDefinition, path: Path) -> None:
    """校验从文件读取或即将写入的同一份 Skill 定义。"""
    if not isinstance(skill.name, str) or not _SKILL_NAME_PATTERN.fullmatch(skill.name):
        raise ValueError(f"Skill name 无效：{path}")
    if not isinstance(skill.description, str) or not skill.description.strip():
        raise ValueError(f"Skill description 不能为空：{path}")
    if not isinstance(skill.allowed_tools, tuple) or not all(
        isinstance(tool, str) and tool.strip() for tool in skill.allowed_tools
    ):
        raise ValueError(f"Skill allowed_tools 无效：{path}")
    if not isinstance(skill.instructions, str) or not skill.instructions.strip():
        raise ValueError(f"Skill 正文不能为空：{path}")


def _format_skill(skill: SkillDefinition) -> str:
    """使用 V1 固定格式生成可编辑、可再次解析的 SKILL.md。"""
    lines = ["---", f"name: {skill.name}", f"description: {skill.description}"]
    if skill.allowed_tools:
        lines.append("allowed_tools:")
        lines.extend(f"  - {tool}" for tool in skill.allowed_tools)
    lines.extend(["---", "", skill.instructions, ""])
    return "\n".join(lines)


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
