"""使用终端协议驱动同一个 AgentEngine Core。"""

from pathlib import Path

from ...agent_engine import AgentEngine, RunRequest
from ...config import load_config
from ...run_control import RunControl
from ...session import SessionStore
from ...workspace import find_workspace_root
from .event_renderer import TerminalEventRenderer
from .terminal_io import TerminalIO
from .pinned_commands import handle_pinned_command
from .interactive_run import run_interactive


def main(config_path: Path | None = None) -> None:
    """启动 Hexgent 命令行交互会话。"""
    # 1. CLI Adapter 负责把工作区、配置和终端能力组装到 Core 外部。
    terminal_io = TerminalIO()
    workdir = find_workspace_root()
    config = load_config(workdir, config_path)
    engine = AgentEngine.create(config, workdir)

    print("\033[36mHexgent：\033[0m你好！有什么可以帮你的？")
    print(
        "输入内容后按两次回车或输入 /send 发送消息；"
        "输入 /restore 恢复历史会话，/continue 续跑暂停任务；exit 或 q 可以结束对话。\n"
        "约束管理：/pin <原文>，/replace-pin <id> <version> <新原文>，/revoke-pin <id> <version>，/pins。\n"
        "运行中可输入新要求（空行或 /send 提交）中断式纠偏；/queue <要求> 边界式排队；/pause、/cancel 控制运行。\n"
    )

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
        if handle_pinned_command(engine, query):
            continue

        # 3. 恢复命令由 CLI Adapter 消费，不得作为普通 Query 写入 Context。
        if normalized_query == "/restore" or normalized_query.startswith(
            "/restore "
        ):
            session_id = query.strip()[len("/restore") :].strip()
            try:
                if not session_id:
                    sessions = SessionStore.list_restorable_session_details(workdir)
                    if not sessions:
                        print("没有找到采用当前恢复协议的历史会话。")
                        continue
                    session_id = terminal_io.select_restore_session(sessions) or ""
                    if not session_id:
                        print("已取消会话恢复。")
                        continue
                engine = AgentEngine.restore(config, workdir, session_id)
            except (FileNotFoundError, OSError, UnicodeError, ValueError) as error:
                print(f"会话恢复失败：{error}")
                continue
            print(f"已恢复会话：{session_id}")
            continue

        if normalized_query == "/continue":
            request = RunRequest.continue_run()
        else:
            request = RunRequest.submit(query)

        print("\033[90m正在处理，请稍候……\033[0m")

        # 4. 将终端控制和权限确认转换为 Core 端口，并消费结构化事件。
        run_control = RunControl()
        renderer = TerminalEventRenderer(engine.tool_context.plan_store.get_plan)
        try:
            run_interactive(terminal_io, engine, request, run_control, renderer)
        except (RuntimeError, ValueError) as error:
            print(f"运行请求未执行：{error}")
