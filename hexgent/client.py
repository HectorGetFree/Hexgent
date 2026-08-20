"""OpenAI client and model configuration."""

import os
from functools import lru_cache

from dotenv import load_dotenv
from openai import OpenAI


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    """Build and cache the OpenAI client from local environment settings."""
    load_dotenv(override=True)
    return OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ["OPENAI_BASE_URL"],
    )


def get_model() -> str:
    """Return the configured model ID."""
    load_dotenv(override=True)
    return os.environ["MODEL_ID"]
