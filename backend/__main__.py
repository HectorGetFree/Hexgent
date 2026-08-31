"""允许通过 ``python -m backend`` 启动开发服务器。"""

import uvicorn


if __name__ == "__main__":
    uvicorn.run("backend.app:create_app", factory=True, host="127.0.0.1", port=8000)
