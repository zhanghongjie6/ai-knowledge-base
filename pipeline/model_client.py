"""LLM model client with retry logic, multi-provider support, and cost tracking.

Provides:
    create_provider: Create a provider config from environment variables.
    chat_with_retry: Send a chat completion request with automatic retries.
    CostTracker: Track token usage and estimate costs across providers.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
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

PRICE_TABLE: dict[str, dict[str, float]] = {
    "deepseek": {"input": 1.0, "output": 2.0},
    "qwen": {"input": 4.0, "output": 12.0},
    "openai": {"input": 150.0, "output": 600.0},
}

DEFAULT_TIMEOUT = 60.0
MAX_RETRIES = 3
RETRY_DELAY = 2.0


@dataclass
class UsageRecord:
    """Token usage for a single API call.

    Attributes:
        provider: Provider name (e.g. deepseek, qwen).
        prompt_tokens: Number of input tokens.
        completion_tokens: Number of output tokens.
        cost: Estimated cost in CNY.
    """
    provider: str
    prompt_tokens: int
    completion_tokens: int
    cost: float


class CostTracker:
    """Track token usage and estimate costs across LLM providers.

    Uses the PRICE_TABLE to calculate costs based on actual token usage
    reported by the API response.

    Usage:
        tracker = CostTracker()
        # after each API call:
        tracker.record(usage={"prompt_tokens": 100, "completion_tokens": 50}, provider="deepseek")
        # at the end:
        print(tracker.report())
    """

    def __init__(self) -> None:
        self._records: list[UsageRecord] = []

    def record(
        self,
        usage: dict[str, int] | None,
        provider: str,
    ) -> None:
        """Record token usage from a single API call.

        Args:
            usage: Dict with prompt_tokens and completion_tokens from API response.
            provider: Provider name matching PRICE_TABLE keys.
        """
        if usage is None:
            return

        prompt = usage.get("prompt_tokens", 0) or 0
        completion = usage.get("completion_tokens", 0) or 0
        cost = self._calculate_cost(prompt, completion, provider)

        self._records.append(UsageRecord(
            provider=provider,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cost=cost,
        ))

    def estimated_cost(self, provider: str | None = None) -> float:
        """Return total estimated cost in CNY, optionally filtered by provider.

        Args:
            provider: If set, only count calls for this provider.

        Returns:
            Total cost in CNY (yuan).
        """
        records = self._records
        if provider:
            records = [r for r in records if r.provider == provider]
        return round(sum(r.cost for r in records), 4)

    def report(self, provider: str | None = None) -> str:
        """Print a formatted cost report.

        Args:
            provider: If set, only show details for this provider.
        """
        records = self._records
        if provider:
            records = [r for r in records if r.provider == provider]

        if not records:
            return "No API calls recorded."

        total_cost = 0.0
        total_prompt = 0
        total_completion = 0
        per_provider: dict[str, dict[str, float | int]] = {}

        for r in records:
            total_cost += r.cost
            total_prompt += r.prompt_tokens
            total_completion += r.completion_tokens
            pp = per_provider.setdefault(r.provider, {
                "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0,
            })
            pp["calls"] += 1
            pp["prompt_tokens"] += r.prompt_tokens
            pp["completion_tokens"] += r.completion_tokens
            pp["cost"] += r.cost

        lines: list[str] = []
        lines.append("=" * 50)
        lines.append("  CostTracker Report")
        lines.append("=" * 50)

        for prov, stats in sorted(per_provider.items()):
            p_input = PRICE_TABLE.get(prov, {}).get("input", 0)
            p_output = PRICE_TABLE.get(prov, {}).get("output", 0)
            lines.append(
                f"  {prov}:"
                f"  {stats['calls']} call(s)"
                f"  |  input {stats['prompt_tokens']:>6} tokens"
                f"  |  output {stats['completion_tokens']:>6} tokens"
                f"  |  ¥{stats['cost']:.4f}"
            )
            lines.append(
                f"    (price: ¥{p_input}/M input  ¥{p_output}/M output)"
            )

        lines.append("-" * 50)
        lines.append(
            f"  TOTAL:"
            f"  {len(records)} call(s)"
            f"  |  {total_prompt + total_completion} tokens"
            f"  |  ¥{total_cost:.4f}"
        )
        lines.append("=" * 50)

        report = "\n".join(lines)
        logger.info("\n" + report)
        print(report)
        return report

    def _calculate_cost(self, prompt: int, completion: int, provider: str) -> float:
        """Calculate cost in CNY for given token counts.

        Args:
            prompt: Number of input tokens.
            completion: Number of output tokens.
            provider: Provider name.

        Returns:
            Cost in CNY.
        """
        prices = PRICE_TABLE.get(provider, {"input": 0, "output": 0})
        return (prompt / 1_000_000 * prices["input"]
                + completion / 1_000_000 * prices["output"])


# ── Global tracker instance ────────────────────────────────────────────

TRACKER: CostTracker = CostTracker()


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
    tracker: CostTracker | None = None,
) -> str:
    """Send a chat completion request with retry logic.

    Args:
        provider: Provider config from create_provider().
        messages: List of message dicts with 'role' and 'content'.
        max_retries: Maximum number of retry attempts.
        tracker: Optional CostTracker for recording token usage.

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
                if tracker:
                    tracker.record(data.get("usage"), provider["provider"])
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


# ── Convenience API ────────────────────────────────────────────────────

tracker: CostTracker = TRACKER

_PROVIDER_CACHE: dict[str, Any] | None = None


def chat(prompt: str) -> dict[str, str]:
    """Send a single prompt to the default LLM and return the response.

    Args:
        prompt: The user message text.

    Returns:
        A dict with key 'content' containing the response text.

    Raises:
        RuntimeError: If the API call fails after all retries.
    """
    global _PROVIDER_CACHE
    if _PROVIDER_CACHE is None:
        _PROVIDER_CACHE = create_provider()

    content = chat_with_retry(
        _PROVIDER_CACHE,
        [{"role": "user", "content": prompt}],
        tracker=TRACKER,
    )
    return {"content": content}
