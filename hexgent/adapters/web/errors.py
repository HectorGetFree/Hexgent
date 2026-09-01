"""Web Adapter 的可序列化业务错误。"""

from typing import Any


class WebAdapterError(RuntimeError):
    """网络调用无法按当前 Session 或 Run 状态完成。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
