"""Web 层统一错误，不伪装成 Agent 工具失败。"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class WebError(Exception):
    """携带 HTTP 状态、稳定错误码和可选详情的 Web 业务异常。"""

    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        """初始化一条不会进入 Agent 工具失败通道的 Web 异常。"""
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


def install_error_handlers(app: FastAPI) -> None:
    """为应用安装统一的 WebError JSON 响应转换器。"""

    @app.exception_handler(WebError)
    async def handle_web_error(_request: Request, error: WebError) -> JSONResponse:
        """把 WebError 转换为稳定的 error 包装结构。"""
        body: dict[str, Any] = {
            "error": {"code": error.code, "message": error.message}
        }
        if error.details is not None:
            body["error"]["details"] = error.details
        return JSONResponse(status_code=error.status_code, content=body)
