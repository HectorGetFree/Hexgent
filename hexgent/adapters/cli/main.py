"""使用终端协议驱动同一个 AgentEngine Core。"""

from pathlib import Path

from ...agent_engine import AgentEngine
from ...config import load_config
from ...run_control import RunControl
from ...workspace import find_workspace_root
from .event_renderer import TerminalEventRenderer
from .terminal_io import TerminalIO


def main(config_path: Path | None = None) -> None:
    """启动 Hexgent 命令行交互会话。"""
    # 1. CLI Adapter 负责把工作区、配置和终端能力组装到 Core 外部。
    terminal_io = TerminalIO()
    workdir = find_workspace_root()
    engine = AgentEngine(load_config(workdir, config_path), workdir)

    print("\033[36mHexgent：\033[0m你好！有什么可以帮你的？")
    print("输入内容后按两次回车或输入 /send 发送消息；exit 或 q 可以结束对话。\n")

    # 2. 每条终端消息启动独立 Run，并跨 Run 复用同一个 AgentEngine。
    while True:
        try:
            query = terminal_io.read_message()
        except EOFError:
            print("\n输入流已关闭，会话结束。")
            break
        except KeyboardInterrupt:
            print("\n会话已结束。")
            break

        normalized_query = query.strip().lower()
        if normalized_query in {"q", "exit"}:
            break
        if not normalized_query:
            continue

        print("\033[90m正在处理，请稍候……\033[0m")

        # 3. 将终端控制和权限确认转换为 Core 端口，并消费结构化事件。
        run_control = RunControl()
        renderer = TerminalEventRenderer(engine.tool_context.plan_store.get_plan)
        event_stream = engine.run(
            query,
            user_confirmation=terminal_io.request_confirmation,
            run_control=run_control,
        )
        with terminal_io.capture_run_interrupt(run_control):
            for event in event_stream:
                renderer.render(event)
