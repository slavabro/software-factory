"""The process-wide Temporal client.

The Python client is backed by the Rust core, is safe to share across coroutines on one event loop and cleans up
when dereferenced (there is no close()). Each process (API server, worker) creates one per settings on first use
and reuses it.
"""

from __future__ import annotations

import asyncio
import logging
import os

from temporalio.client import Client

from factory_runtime.temporal.converter import data_converter
from factory_runtime.temporal.settings import TemporalSettings

logger = logging.getLogger(__name__)

Key = tuple[str, str, int, int]

_clients: dict[Key, Client] = {}
_lock = asyncio.Lock()


def _key(settings: TemporalSettings) -> Key:
    return (settings.server_url, settings.namespace, settings.compress_min_bytes, settings.warn_bytes)


async def get_client(settings: TemporalSettings) -> Client:
    """The connected client for `settings`, created on first use (raises if the server can't be reached)."""
    key = _key(settings)
    if (client := _clients.get(key)) is not None:
        return client
    async with _lock:
        if (client := _clients.get(key)) is not None:
            return client
        logger.info(f"Creating Temporal client for process {os.getpid()}: {settings.server_url}/{settings.namespace}")
        client = await Client.connect(
            settings.server_url,
            namespace=settings.namespace,
            data_converter=data_converter(settings.compress_min_bytes, settings.warn_bytes),
        )
        _clients[key] = client
        return client


def reset_clients() -> None:
    """Forget every client (tests only; production never needs to)."""
    _clients.clear()
