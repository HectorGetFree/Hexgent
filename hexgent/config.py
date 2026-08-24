"""Hexgent 普通运行配置的 TOML 加载入口。"""

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, TypeVar, get_type_hints

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 使用兼容实现。
    import tomli as tomllib

from .context.context_budget import ContextLimits
from .context.session_store import ToolResultLimits
from .loop.limits import LoopLimits


ConfigSection = TypeVar("ConfigSection", LoopLimits, ContextLimits, ToolResultLimits)


class ConfigError(ValueError):
    """配置文件无法安全转换为 Hexgent 运行配置。"""


@dataclass(frozen=True)
class HexgentConfig:
    """一次 Hexgent 进程使用的完整普通运行配置。"""

    # Agent Loop 的跨轮次资源预算配置。
    loop: LoopLimits = LoopLimits()

    # 模型活动上下文容量和压缩策略配置。
    context: ContextLimits = ContextLimits()

    # 工具结果内联、预览和外置落盘配置。
    tool_result: ToolResultLimits = ToolResultLimits()


def load_config(
    workdir: Path,
    config_path: Path | None = None,
) -> HexgentConfig:
    """加载 TOML 配置；默认文件不存在时使用全部代码默认值。"""
    # 1. 解析默认配置路径或调用方显式指定的配置路径。
    resolved_workdir = workdir.resolve()
    path = config_path or resolved_workdir / "hexgent.toml"
    if not path.is_absolute():
        path = resolved_workdir / path
    path = path.resolve()
    if not path.exists():
        if config_path is None:
            return HexgentConfig()
        raise ConfigError(f"配置文件不存在：{path}")

    # 2. 读取 TOML，并把语法错误转换为中文配置错误。
    try:
        with path.open("rb") as config_file:
            raw_config = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"无法读取配置文件 {path}：{error}") from error

    # 3. 拒绝未知顶层配置，避免拼写错误被静默忽略。
    known_sections = {"loop", "context", "tool_result"}
    unknown_sections = set(raw_config) - known_sections
    if unknown_sections:
        names = "、".join(sorted(unknown_sections))
        raise ConfigError(f"配置文件包含未知配置段：{names}")

    # 4. 分别构造强类型配置对象，缺失字段继续使用代码默认值。
    config = HexgentConfig(
        loop=_load_section("loop", raw_config.get("loop"), LoopLimits),
        context=_load_section(
            "context",
            raw_config.get("context"),
            ContextLimits,
        ),
        tool_result=_load_section(
            "tool_result",
            raw_config.get("tool_result"),
            ToolResultLimits,
        ),
    )
    _validate_relationships(config)
    return config


def _load_section(
    section_name: str,
    values: Any,
    section_type: type[ConfigSection],
) -> ConfigSection:
    """校验一个配置段并构造对应的冻结数据对象。"""
    if values is None:
        return section_type()
    if not isinstance(values, dict):
        raise ConfigError(f"配置段 [{section_name}] 必须是 TOML 表")

    resolved_types = get_type_hints(section_type)
    field_types = {
        field.name: resolved_types[field.name]
        for field in fields(section_type)
    }
    unknown_fields = set(values) - set(field_types)
    if unknown_fields:
        names = "、".join(sorted(unknown_fields))
        raise ConfigError(f"配置段 [{section_name}] 包含未知字段：{names}")

    for field_name, value in values.items():
        _validate_field(section_name, field_name, field_types[field_name], value)
    return section_type(**values)


def _validate_field(
    section_name: str,
    field_name: str,
    expected_type: Any,
    value: Any,
) -> None:
    """校验配置字段类型及正数约束。"""
    full_name = f"[{section_name}].{field_name}"
    if expected_type is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"配置字段 {full_name} 必须是布尔值")
        return
    if expected_type is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"配置字段 {full_name} 必须是整数")
        minimum = 0 if field_name == "max_plan_completion_nudges" else 1
        if value < minimum:
            comparator = "大于或等于 0" if minimum == 0 else "大于 0"
            raise ConfigError(f"配置字段 {full_name} 必须{comparator}")
        return
    if expected_type is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"配置字段 {full_name} 必须是数字")
        if value <= 0:
            raise ConfigError(f"配置字段 {full_name} 必须大于 0")


def _validate_relationships(config: HexgentConfig) -> None:
    """校验需要结合多个字段才能判断的配置约束。"""
    context = config.context
    reserved_tokens = (
        context.reserved_output_tokens + context.safety_margin_tokens
    )
    if reserved_tokens >= context.context_window_tokens:
        raise ConfigError(
            "[context] 的输出预留与安全余量之和必须小于上下文窗口"
        )
    if context.recent_context_tokens >= context.compaction_threshold_tokens:
        raise ConfigError(
            "[context].recent_context_tokens 必须小于主动压缩阈值"
        )
    if not 0 < context.min_compaction_savings_ratio <= 1:
        raise ConfigError(
            "[context].min_compaction_savings_ratio 必须大于 0 且小于或等于 1"
        )
    if (
        config.tool_result.preview_characters
        > config.tool_result.max_inline_characters
    ):
        raise ConfigError(
            "[tool_result].preview_characters 不能大于 max_inline_characters"
        )
