"""支持通过 ``python -m hexgent.adapters.web`` 启动网络适配层。"""

import argparse
from pathlib import Path

import uvicorn

from .app import create_app


def _parse_arguments() -> argparse.Namespace:
    """解析 Web Adapter 的监听参数。"""
    parser = argparse.ArgumentParser(description="启动 Hexgent Web Adapter")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--workdir", type=Path, help="指定 Agent 工作区")
    parser.add_argument("--config", type=Path, help="指定 TOML 配置文件")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = _parse_arguments()
    uvicorn.run(
        create_app(
            workdir=arguments.workdir,
            config_path=arguments.config,
        ),
        host=arguments.host,
        port=arguments.port,
    )
