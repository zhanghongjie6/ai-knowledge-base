"""LLM model client with retry logic and multi-provider support.

Provides:
    create_provider: Create a provider config from environment variables.
    chat_with_retry: Send a chat completion request with automatic retries.
"""

import logging
import os
import time
from typing import Any

import httpx


logger = logging.getLogger(__name__)

PROVIDER_CONFIGS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "env_key": "DEEPSEEK_API_KEY",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "env_key": "QWEN_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "env_key": "OPENAI_API_KEY",
    },
}

DEFAULT_TIMEOUT = 60.0
MAX_RETRIES = 3
RETRY_DELAY = 2.0


def create_provider() -> dict[str, Any]:
    """Create a provider config from the LLM_PROVIDER env var.

    Returns:
        A dict with keys: provider, base_url, model, api_key.

    Raises:
        ValueError: If LLM_PROVIDER is unset or unknown.
    """
    provider_name = os.environ.get("LLM_PROVIDER", "deepseek").lower()
    config = PROVIDER_CONFIGS.get(provider_name)
    if config is None:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{provider_name}'. "
            f"Supported: {', '.join(PROVIDER_CONFIGS)}"
        )

    api_key = os.environ.get(config["env_key"])
    if not api_key:
        raise ValueError(
            f"{config['env_key']} is not set for provider '{provider_name}'"
        )

    return {
        "provider": provider_name,
        "base_url": config["base_url"],
        "model": config["model"],
        "api_key": api_key,
    }


def chat_with_retry(
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    max_retries: int = MAX_RETRIES,
) -> str:
    """Send a chat completion request with retry logic.

    Args:
        provider: Provider config from create_provider().
        messages: List of message dicts with 'role' and 'content'.
        max_retries: Maximum number of retry attempts.

    Returns:
        The response text content.

    Raises:
        RuntimeError: If all retry attempts fail.
    """
    url = f"{provider['base_url'].rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": provider["model"],
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 1024,
    }

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                if content:
                    return content
                logger.warning("Attempt %d: empty response, retrying...", attempt)
        except httpx.HTTPStatusError as e:
            last_error = e
            logger.warning(
                "Attempt %d: HTTP %d - %s", attempt, e.response.status_code, e.response.text[:200]
            )
        except httpx.RequestError as e:
            last_error = e
            logger.warning("Attempt %d: request failed - %s", attempt, e)
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            last_error = e
            logger.warning("Attempt %d: bad response format - %s", attempt, e)

        if attempt < max_retries:
            time.sleep(RETRY_DELAY * attempt)

    raise RuntimeError(
        f"chat_with_retry failed after {max_retries} attempts"
    ) from last_error
