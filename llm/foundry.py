"""Shared Microsoft Foundry client and Agent Framework helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import os
import weakref
from threading import Lock
from typing import Any
import logging

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from telemetry import traced


load_dotenv()

_client: FoundryChatClient | None = None
_client_lock = Lock()
_semaphores: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()
_semaphore_limits: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, int
] = weakref.WeakKeyDictionary()
_semaphore_lock = Lock()
logger = logging.getLogger(__name__)


def _llm_semaphore() -> asyncio.Semaphore:
    limit_value = os.getenv("LLM_MAX_CONCURRENCY", "2")
    try:
        limit = int(limit_value)
    except ValueError as error:
        raise ValueError("LLM_MAX_CONCURRENCY must be a positive integer") from error
    if limit < 1:
        raise ValueError("LLM_MAX_CONCURRENCY must be a positive integer")
    loop = asyncio.get_running_loop()
    with _semaphore_lock:
        semaphore = _semaphores.get(loop)
        if semaphore is None or _semaphore_limits.get(loop) != limit:
            semaphore = asyncio.Semaphore(limit)
            _semaphores[loop] = semaphore
            _semaphore_limits[loop] = limit
        return semaphore


def _is_rate_limited(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    return (
        status_code == 429
        or getattr(response, "status_code", None) == 429
        or "TooManyRequests" in error.__class__.__name__
        or "RateLimit" in error.__class__.__name__
    )


def get_client() -> FoundryChatClient:
    """Lazily create the process-wide Foundry chat client."""
    global _client
    if _client is not None:
        return _client

    with _client_lock:
        if _client is not None:
            return _client
        project_endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
        model = os.getenv("FOUNDRY_MODEL", "").strip()
        if not project_endpoint:
            raise RuntimeError("FOUNDRY_PROJECT_ENDPOINT must be configured")
        if not project_endpoint.startswith("https://"):
            raise ValueError("FOUNDRY_PROJECT_ENDPOINT must start with 'https://'")
        if not model:
            raise RuntimeError(
                "FOUNDRY_MODEL must be configured with the Foundry deployment name"
            )

        _client = FoundryChatClient(
            project_endpoint=project_endpoint,
            model=model,
            credential=DefaultAzureCredential(),
        )
        return _client


def make_agent(
    name: str,
    instructions: str,
    tools: Sequence[Any] | None = None,
) -> Agent:
    """Create an Agent Framework agent backed by the shared Foundry client."""
    return Agent(
        client=get_client(),
        name=name,
        instructions=instructions,
        tools=tools or [],
    )


async def ask(
    agent: Any,
    prompt: str,
    timeout: float = 6.0,
    fallback: str = "",
    *,
    agent_name: str | None = None,
    fact_ids: Sequence[str] = (),
) -> str:
    """Run an agent with a deadline, returning its text or the fallback."""
    name = agent_name or str(getattr(agent, "name", "agent"))
    for attempt in range(2):
        try:
            async def run_agent() -> Any:
                return await agent.run(prompt)

            async with _llm_semaphore():
                result = await asyncio.wait_for(
                    traced(name, fact_ids, run_agent()),
                    timeout=timeout,
                )
            return str(result)
        except Exception as error:
            if attempt == 0 and _is_rate_limited(error):
                await asyncio.sleep(0.25)
                continue
            logger.warning(
                "Foundry agent %s fell back (%s)",
                name,
                error.__class__.__name__,
            )
            return fallback
    return fallback
