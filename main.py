"""Hexgent 的向后兼容启动入口。"""

from hexgent.config import ConfigError

from hexgent.agent_engine import main


if __name__ == "__main__":
    try:
        main()
    except ConfigError as error:
        raise SystemExit(f"配置错误：{error}") from error
