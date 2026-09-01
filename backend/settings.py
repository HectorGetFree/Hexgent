"""Hexgent Web 层的环境配置。"""

from dataclasses import dataclass
from os import environ

from dotenv import load_dotenv


DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


@dataclass(frozen=True)
class WebSettings:
    """与 Agent 领域配置隔离的 Web 协议层设置。"""

    cors_allowed_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS

    @classmethod
    def from_environment(cls) -> "WebSettings":
        """从逗号分隔的环境变量读取允许跨域访问的前端来源。"""
        # 1. 与 Hexgent 客户端保持一致，开发环境可以在仓库根目录使用 .env。
        load_dotenv(override=False)
        # 2. 未配置时只开放本地 Vite 来源，生产环境必须显式提供部署域名。
        raw_origins = environ.get("HEXGENT_CORS_ALLOWED_ORIGINS")
        if raw_origins is None:
            return cls()
        # 3. 清理空项和末尾斜杠，避免浏览器 Origin 精确匹配失败。
        origins = tuple(
            origin.strip().rstrip("/")
            for origin in raw_origins.split(",")
            if origin.strip()
        )
        if not origins:
            raise ValueError("HEXGENT_CORS_ALLOWED_ORIGINS 至少需要一个有效来源")
        return cls(origins)
