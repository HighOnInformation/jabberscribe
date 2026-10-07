"""HTTP client for the on-prem LiteLLM server.

LiteLLM exposes OpenAI-compatible routes, so STT and summary both speak plain
OpenAI JSON over httpx. The API key comes from the environment; a server
without auth simply leaves it unset.
"""

from __future__ import annotations

import os

import httpx

from jabberscribe.config import LiteLLMConfig

KEY_ENV = "JABBERSCRIBE_LITELLM_KEY"


def make_client(cfg: LiteLLMConfig, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    key = os.environ.get(KEY_ENV)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.Client(base_url=cfg.base_url, headers=headers, timeout=cfg.timeout_seconds, transport=transport)
