"""智能体可以调用的本地工具。"""

from pathlib import Path
import subprocess

from .tool_context import ToolExecutionContext


MAX_OUTPUT_LENGTH = 50_000


def bash(command: str, context: ToolExecutionContext) -> str:
    """执行 Shell 命令。"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=context.workdir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = (result.stdout + result.stderr).strip()
        return output[:MAX_OUTPUT_LENGTH] if output else "（无输出）"
    except subprocess.TimeoutExpired:
        return "错误：命令执行超时（120 秒）"
    except (FileNotFoundError, OSError) as error:
        return f"错误：{error}"


def _resolve_path(path: str, context: ToolExecutionContext) -> Path:
    """相对于当前工具工作区解析文件路径。"""
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = context.workdir / target
    return target.resolve()


def read_file(path: str, context: ToolExecutionContext) -> str:
    """读取 UTF-8 文本文件。"""
    content = _resolve_path(path, context).read_text(encoding="utf-8")
    return content[:MAX_OUTPUT_LENGTH]


def write_file(path: str, content: str, context: ToolExecutionContext) -> str:
    """写入 UTF-8 文本文件，并在需要时创建父目录。"""
    target = _resolve_path(path, context)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"已向 {target} 写入 {len(content)} 个字符"


def edit_file(
    path: str,
    old_text: str,
    new_text: str,
    context: ToolExecutionContext,
) -> str:
    """仅当原文本在 UTF-8 文件中出现一次时执行替换。"""
    if not old_text:
        return "错误：原文本不能为空"

    target = _resolve_path(path, context)
    content = target.read_text(encoding="utf-8")
    occurrences = content.count(old_text)
    if occurrences == 0:
        return "错误：未找到原文本"
    if occurrences > 1:
        return f"错误：原文本出现了 {occurrences} 次，请提供更多上下文以确保唯一匹配"

    target.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
    return f"已编辑 {target}"


def glob(pattern: str, context: ToolExecutionContext) -> str:
    """返回工作目录中符合 Glob 模式的路径。"""
    matches = sorted(context.workdir.glob(pattern))
    if not matches:
        return "（没有匹配项）"

    visible_matches = matches[:1000]
    output = "\n".join(str(path) for path in visible_matches)
    if len(matches) > len(visible_matches):
        output += f"\n……另有 {len(matches) - len(visible_matches)} 个匹配项"
    return output[:MAX_OUTPUT_LENGTH]
