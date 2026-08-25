"""智能体可以调用的本地工具。"""

from hashlib import sha256
from pathlib import Path
import subprocess

from .tool_context import (
    ReadFileCacheEntry,
    ReadFileCacheKey,
    ToolExecutionContext,
)


DEFAULT_READ_LINE_COUNT = 200
MAX_READ_LINE_COUNT = 500


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
        return output if output else "（无输出）"
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


def validate_read_file(
    arguments: dict[str, object],
    _context: ToolExecutionContext,
) -> str | None:
    """校验分段读取范围，避免一次恢复过多文件内容。"""
    start_line = arguments.get("start_line")
    line_count = arguments.get("line_count")
    if isinstance(start_line, int) and start_line < 1:
        return "参数“start_line”必须大于或等于 1"
    if isinstance(line_count, int) and not 1 <= line_count <= MAX_READ_LINE_COUNT:
        return f"参数“line_count”必须在 1 到 {MAX_READ_LINE_COUNT} 之间"
    return None


def read_file(
    path: str,
    context: ToolExecutionContext,
    start_line: int | None = None,
    line_count: int | None = None,
) -> str:
    """读取完整或指定行范围的 UTF-8 文件，内容未变化时返回占位结果。"""
    # 1. 读取原始字节并计算内容 Hash，避免依赖修改时间和文件大小。
    target = _resolve_path(path, context)
    raw_content = target.read_bytes()
    content_hash = sha256(raw_content).hexdigest()

    # 2. 规范化可选范围；出现任一范围参数时默认从首行读取 200 行。
    is_partial = start_line is not None or line_count is not None
    if is_partial:
        effective_start = start_line if start_line is not None else 1
        effective_count = (
            line_count if line_count is not None else DEFAULT_READ_LINE_COUNT
        )
    else:
        effective_start = None
        effective_count = None
    # 3. 只有 UTF-8 解码成功后才更新缓存，保证缓存代表成功返回的正文。
    content = raw_content.decode("utf-8")
    if not is_partial:
        cache_key = ReadFileCacheKey(target)
        cached = context.read_file_cache.get(cache_key)
        if cached is not None and cached.content_hash == content_hash:
            return _unchanged_file_result(target, content_hash)
        context.read_file_cache[cache_key] = ReadFileCacheEntry(content_hash)
        return content

    # 4. 覆盖整个文件的首段请求按完整读取处理，避免模型默认 1-500 污染缓存。
    assert effective_start is not None and effective_count is not None
    lines = content.splitlines()
    total_lines = len(lines)
    if effective_start == 1 and effective_count >= total_lines:
        cache_key = ReadFileCacheKey(target)
        cached = context.read_file_cache.get(cache_key)
        if cached is not None and cached.content_hash == content_hash:
            return _unchanged_file_result(target, content_hash)
        context.read_file_cache[cache_key] = ReadFileCacheEntry(content_hash)
        return content

    # 5. 真正的分段请求按具体范围去重，只返回范围内的内容和行数元数据。
    cache_key = ReadFileCacheKey(target, effective_start, effective_count)
    cached = context.read_file_cache.get(cache_key)
    if cached is not None and cached.content_hash == content_hash:
        return _unchanged_file_result(
            target,
            content_hash,
            effective_start,
            effective_count,
        )
    if total_lines > 0 and effective_start > total_lines:
        return f"错误：起始行 {effective_start} 超出文件总行数 {total_lines}"
    start_index = effective_start - 1
    selected_lines = lines[start_index : start_index + effective_count]
    actual_start = effective_start if total_lines > 0 else 0
    actual_end = start_index + len(selected_lines) if total_lines > 0 else 0
    body = "\n".join(selected_lines)
    context.read_file_cache[cache_key] = ReadFileCacheEntry(content_hash)
    return (
        f'<file-content path="{target}" lines="{actual_start}-{actual_end}" '
        f'total_lines="{total_lines}">\n'
        f"{body}\n"
        "</file-content>"
    )


def _unchanged_file_result(
    target: Path,
    content_hash: str,
    start_line: int | None = None,
    line_count: int | None = None,
) -> str:
    """构造完整文件或指定范围未变化时的统一占位结果。"""
    range_attributes = (
        f' start_line="{start_line}" line_count="{line_count}"'
        if start_line is not None and line_count is not None
        else ""
    )
    return (
        f'<file-unchanged path="{target}" sha256="{content_hash}"'
        f"{range_attributes}>\n"
        "文件内容与本会话上次读取时一致，未重复返回完整内容。\n"
        "</file-unchanged>"
    )


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
    return output
