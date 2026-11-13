"""The process-wide Temporal client, one per settings.

The Python client is backed by the Rust core, is safe to share across coroutines on one event loop and cleans up
when dereferenced (there is no close()). A process creates it on first use and reuses it. `cached_client` answers
"is one connected?" without connecting (health checks); `set_client` puts one in place (tests, or a client a product
built itself); `reset_clients` forgets them all (tests).
"""

from __future__ import annotations

import asyncio
import logging
import os
import weakref
from typing import Any

from temporalio.client import Client

from factory_runtime.temporal.converter import data_converter
from factory_runtime.temporal.settings import TemporalSettings

logger = logging.getLogger(__name__)

Key = tuple[str, str, int, int, bool, str | None]

_clients: dict[Key, Client] = {}
# One lock per event loop: an asyncio.Lock belongs to the loop that first waits on it, and test suites run a fresh
# loop per test.
_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def _key(settings: TemporalSettings) -> Key:
    return (
        settings.server_url,
        settings.namespace,
        settings.compress_min_bytes,
        settings.warn_bytes,
        settings.tls,
        settings.api_key,
    )


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Lock()
    return lock


async def get_client(settings: TemporalSettings, **connect_options: Any) -> Client:
    """The connected client for `settings`, created on first use (raises if the server can't be reached).
    `connect_options` go to `Client.connect` on that first use (e.g. `tls=TLSConfig(...)` for mTLS, `identity=`)."""
    key = _key(settings)
    if (client := _clients.get(key)) is not None:
        return client
    async with _lock():
        if (client := _clients.get(key)) is not None:
            return client
        logger.info(f"Creating Temporal client for process {os.getpid()}: {settings.server_url}/{settings.namespace}")
        options: dict[str, Any] = {"tls": settings.tls or settings.api_key is not None, **connect_options}
        if settings.api_key is not None:
            options.setdefault("api_key", settings.api_key)
        client = await Client.connect(
            settings.server_url,
            namespace=settings.namespace,
            data_converter=data_converter(settings.compress_min_bytes, settings.warn_bytes),
            **options,
        )
        _clients[key] = client
        return client


def cached_client(settings: TemporalSettings) -> Client | None:
    """The client already connected for `settings`, or None; never connects."""
    return _clients.get(_key(settings))


def set_client(settings: TemporalSettings, client: Client | None) -> None:
    """Use `client` for `settings` from now on (None forgets it)."""
    if client is None:
        _clients.pop(_key(settings), None)
    else:
        _clients[_key(settings)] = client


def reset_clients() -> None:
    """Forget every client (tests only; production never needs to)."""
    _clients.clear()
