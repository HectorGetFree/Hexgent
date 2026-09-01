"""Web Adapter 自身的网络协议配置。"""

from dataclasses import dataclass
from os import environ

from dotenv import load_dotenv


DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


@dataclass(frozen=True)
class WebSettings:
    """独立于 HexgentConfig 的 HTTP 部署设置。"""

    # 允许访问 API 的浏览器 Origin 白名单。
    cors_allowed_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS

    @classmethod
    def from_environment(cls) -> "WebSettings":
        """从环境变量加载逗号分隔的 CORS Origin。"""
        # 1. 开发环境允许从仓库 .env 加载，但不会覆盖进程显式变量。
        load_dotenv(override=False)

        # 2. 未配置时只允许本地 Vite；生产环境应显式配置部署域名。
        raw_origins = environ.get("HEXGENT_CORS_ALLOWED_ORIGINS")
        if raw_origins is None:
            return cls()
        origins = tuple(
            origin.strip().rstrip("/")
            for origin in raw_origins.split(",")
            if origin.strip()
        )
        if not origins:
            raise ValueError("HEXGENT_CORS_ALLOWED_ORIGINS 至少需要一个有效来源")
        return cls(origins)
