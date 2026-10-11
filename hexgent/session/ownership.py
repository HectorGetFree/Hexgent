"""本地 Session 写入所有权：进程间排他，进程崩溃由操作系统释放。"""
from contextlib import contextmanager
import fcntl
from functools import wraps
from pathlib import Path
from threading import RLock


class SessionOwnership:
    def __init__(self):
        self.lock = RLock()
        self.depth = 0
        self.file = None

    @contextmanager
    def hold(self, directory: Path):
        # 1. 同一个 Store 的控制/工具线程共享所有权，不跨 Store 重入。
        with self.lock:
            if self.depth == 0:
                directory.mkdir(parents=True, exist_ok=True)
                handle = (directory / "writer.lock").open("a+")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    raise RuntimeError("SESSION_BUSY：Session 已被其他写入者占用") from None
                self.file = handle
            self.depth += 1
        try:
            yield
        finally:
            with self.lock:
                self.depth -= 1
                if self.depth == 0:
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
                    self.file.close()
                    self.file = None


def owned_write(method):
    """Store 工件和 Transcript 使用同一所有权协议。"""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.write_guard():
            return method(self, *args, **kwargs)
    return wrapped


def session_write(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if self.store is None:
            return method(self, *args, **kwargs)
        with self.store.write_guard():
            return method(self, *args, **kwargs)
    return wrapped
