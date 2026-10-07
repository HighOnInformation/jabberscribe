"""HTTP client for the on-prem LiteLLM server.

LiteLLM exposes OpenAI-compatible routes, so STT and summary both speak plain
OpenAI JSON over httpx. The API key comes from the environment; a server
without auth simply leaves it unset.

Errors split in two. TransientError means LiteLLM is down or overloaded: the
job waits and retries for as long as that lasts. Everything else is a real
problem with the request or the data and fails the job after a few attempts.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from jabberscribe.config import LiteLLMConfig

KEY_ENV = "JABBERSCRIBE_LITELLM_KEY"


class TransientError(RuntimeError):
    """LiteLLM is unreachable, overloaded (429) or failing (5xx). Retry later; never give up."""


def make_client(cfg: LiteLLMConfig, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    key = os.environ.get(KEY_ENV)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    # The routes below already start with /v1; an OpenAI-style base_url ending in /v1 would double it.
    base_url = cfg.base_url.rstrip("/").removesuffix("/v1")
    return httpx.Client(base_url=base_url, headers=headers, timeout=cfg.timeout_seconds, transport=transport)


def post(client: httpx.Client, path: str, **kwargs: Any) -> httpx.Response:
    """POST and sort failures: TransientError for outages, HTTPStatusError for other 4xx.

    The caller wraps HTTPStatusError in its own permanent error type.
    """
    try:
        response = client.post(path, **kwargs)
    except httpx.TransportError as exc:
        raise TransientError(f"{path}: {exc}") from exc
    if response.status_code == 429 or response.status_code >= 500:
        raise TransientError(f"{path}: HTTP {response.status_code} {response.text[:200]}")
    response.raise_for_status()
    return response
