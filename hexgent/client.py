"""OpenAI 客户端与模型配置。"""

import os
from functools import lru_cache

from dotenv import load_dotenv
from openai import OpenAI


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    """根据本地环境配置创建并缓存 OpenAI 客户端。"""
    load_dotenv(override=True)
    return OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ["OPENAI_BASE_URL"],
    )


def get_model() -> str:
    """返回配置的模型 ID。"""
    load_dotenv(override=True)
    return os.environ["MODEL_ID"]
