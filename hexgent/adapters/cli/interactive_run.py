"""终端主线程独占输入；工作线程仅执行 Core 和提交事件/审批请求。"""
import os
import select
import sys
from queue import Empty, Queue
from threading import Thread
from concurrent.futures import Future


def submit_control(control, content):
    """运行中普通消息默认纠偏；/queue 显式保留边界式行为。"""
    stripped = content.strip()
    if stripped == "/pause":
        control.pause()
    elif stripped in {"/cancel", "exit", "q"}:
        control.cancel()
    elif stripped.startswith("/queue "):
        control.steer(content[content.index("/queue") + 7:], mode="boundary")
    elif stripped.startswith("/steer "):
        control.steer(content[content.index("/steer") + 7:], mode="interrupt")
    elif stripped.startswith("/"):
        raise ValueError("运行中仅支持 /pause、/cancel、/steer <要求>、/queue <要求>")
    elif stripped:
        control.steer(content, mode="interrupt")


def run_interactive(terminal, engine, request, control, renderer):
    """TTY 使用单输入所有者；管道模式保留原有同步消费语义。"""
    if not sys.stdin.isatty():
        with terminal.capture_run_interrupt(control):
            for event in engine.run(request, run_control=control,
                                    user_confirmation=terminal.request_confirmation):
                renderer.render(event)
        return

    messages = Queue()
    control.bind_steering_session(engine.session)
    def confirm(name, message, call_id=None):
        result = Future()
        messages.put(("permission", (name, message, result)))
        return result.result()

    def worker():
        try:
            for event in engine.run(request, run_control=control, user_confirmation=confirm):
                messages.put(("event", event))
        except BaseException as error:
            messages.put(("error", error))
        finally:
            messages.put(("done", None))

    # 1. 不启动后台 stdin 读取线程，结束后不存在遗留 reader 抢占下一条输入。
    lines, buffered, permission = [], b"", None
    input_closed = False
    error = None
    thread = Thread(target=worker, name="hexgent-cli-run")
    with terminal.capture_run_interrupt(control):
        thread.start()
        done = False
        while not done:
            try:
                while True:
                    kind, value = messages.get_nowait()
                    if kind == "event":
                        try:
                            renderer.render(value)
                        except Exception as failure:
                            error = failure
                            control.pause()
                    elif kind == "permission":
                        permission = value[2]
                        print(f"\n权限确认：{value[1]}（工具：{value[0]}）[y/N]；也可提交 /steer <要求>", flush=True)
                    elif kind == "error":
                        error = value
                    elif kind == "done":
                        done = True
            except Empty:
                pass
            if permission is not None and (control.interrupt_controller.interrupted
                    or control.steering_mailbox.has_interrupting_input):
                permission.set_result("unavailable")
                permission = None
            if done:
                break
            if input_closed:
                thread.join(0.05)
                continue
            readable, _, _ = select.select([sys.stdin.fileno()], [], [], 0.05)
            if not readable:
                continue
            chunk = os.read(sys.stdin.fileno(), 4096)
            if not chunk:
                # 2. EOF 安全暂停，并唤醒审批；不把关闭输入解释为允许执行。
                control.pause()
                input_closed = True
                if permission is not None:
                    permission.set_result("unavailable")
                    permission = None
                continue
            buffered += chunk
            while b"\n" in buffered:
                raw, buffered = buffered.split(b"\n", 1)
                line = raw.decode("utf-8", errors="replace").rstrip("\r")
                if permission is not None and not line.startswith("/"):
                    permission.set_result("allow" if line.strip().lower() in {"y", "yes", "是", "允许"} else "deny")
                    permission = None
                    continue
                immediate = not lines and line.startswith("/")
                if line and line != "/send" and not immediate:
                    lines.append(line)
                    continue
                content = line if immediate else "\n".join(lines)
                lines.clear()
                try:
                    submit_control(control, content)
                    if content.strip():
                        print("\n控制请求已登记；等待当前执行安全收束。", flush=True)
                        if permission is not None and not content.strip().startswith("/queue "):
                            permission.set_result("unavailable")
                            permission = None
                except (ValueError, RuntimeError, OSError) as failure:
                    print(f"控制请求未接受：{failure}", flush=True)
        thread.join()
    # 3. 已键入但未提交的内容保留到下一次输入，不能静默丢弃。
    terminal.pending_lines = lines + ([buffered.decode("utf-8", errors="replace")] if buffered else [])
    if error is not None:
        raise error
