"""Phase 5C Batch 1 的 eval-only 受限 Bash 执行器。

它不是 Production bash 的替代实现。仅在 benchmark 进程内临时替换同名工具的
handler，保留工具名、参数 schema 和既有 Permission 管线，避免 Agent 获得任意 Shell。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import selectors
import subprocess
import sys
from time import monotonic
from typing import Iterator

from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_registry import TOOL_REGISTRY
from hexgent.tool.tool_result import ToolHandlerResult


PROFILE_NAME = "eval_restricted_bash_v1"
ALLOWED_COMMAND = "python -m unittest discover -s tests"
_ALLOWED_ARGV = (sys.executable, "-m", "unittest", "discover", "-s", "tests")
_TIMEOUT_SECONDS = 20.0
_MAX_OUTPUT_BYTES = 16 * 1024


def _minimal_environment() -> dict[str, str]:
    """不给 fixture test 传递 API Key、代理或其他宿主环境变量。"""

    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _terminate(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def _run_fixed_unittest(workspace: Path) -> tuple[int, bytes] | ToolHandlerResult:
    """以 shell=False 执行固定 argv，并在时间/输出越界时终止子进程。"""

    try:
        process = subprocess.Popen(
            _ALLOWED_ARGV,
            shell=False,
            cwd=workspace,
            env=_minimal_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as error:
        return ToolHandlerResult.failed(
            "external_dependency_failed", str(error), retryable=True,
        )

    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    chunks: list[bytes] = []
    output_size = 0
    deadline = monotonic() + _TIMEOUT_SECONDS
    try:
        while selector.get_map():
            remaining = deadline - monotonic()
            if remaining <= 0:
                _terminate(process)
                return ToolHandlerResult.failed(
                    "tool_timeout", f"受限 unittest 超时（{int(_TIMEOUT_SECONDS)} 秒）",
                    side_effect_state="none",
                )
            events = selector.select(remaining)
            if not events:
                continue
            for key, _ in events:
                block = os.read(key.fd, min(4_096, _MAX_OUTPUT_BYTES - output_size + 1))
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                output_size += len(block)
                if output_size > _MAX_OUTPUT_BYTES:
                    _terminate(process)
                    return ToolHandlerResult.failed(
                        "tool_output_limit_exceeded",
                        f"受限 unittest 输出超过 {_MAX_OUTPUT_BYTES} 字节",
                        side_effect_state="none",
                    )
                chunks.append(block)
        return_code = process.wait(timeout=1)
        return return_code, b"".join(chunks)
    finally:
        selector.close()
        process.stdout.close()


def restricted_bash(command: str, context: ToolExecutionContext, *, workspace: Path) -> ToolHandlerResult:
    """只接受完全等于 allowlist 的 T1 unittest 命令。"""

    trusted_workspace = workspace.resolve()
    if context.workdir.resolve() != trusted_workspace:
        return ToolHandlerResult.failed(
            "eval_tool_policy_denied", "受限 bash 仅允许可信 T1 fixture workspace",
            phase="permission", side_effect_state="not_started", execution_status="denied",
        )
    if command != ALLOWED_COMMAND:
        return ToolHandlerResult.failed(
            "eval_tool_policy_denied", "受限 bash 仅允许固定 unittest 验证命令",
            phase="permission", side_effect_state="not_started", execution_status="denied",
        )
    result = _run_fixed_unittest(trusted_workspace)
    if isinstance(result, ToolHandlerResult):
        return result
    return_code, raw_output = result
    output = raw_output.decode("utf-8", errors="replace").strip()
    if return_code != 0:
        return ToolHandlerResult.failed(
            "business_failure",
            f"固定 unittest 退出码为 {return_code}：{output or '无输出'}",
            side_effect_state="none",
        )
    return ToolHandlerResult.success(
        output or "（无输出）",
        {
            "exit_code": return_code,
            "stdout_characters": len(raw_output),
            "stderr_characters": 0,
        },
    )


@contextmanager
def restricted_bash_profile(workspace: Path) -> Iterator[None]:
    """临时替换全局 registry 中 bash 的 handler，退出时无条件恢复 Production handler。"""

    original = TOOL_REGISTRY["bash"]

    def handler(command: str, context: ToolExecutionContext) -> ToolHandlerResult:
        return restricted_bash(command, context, workspace=workspace)

    TOOL_REGISTRY["bash"] = replace(original, function=handler)
    try:
        yield
    finally:
        TOOL_REGISTRY["bash"] = original
