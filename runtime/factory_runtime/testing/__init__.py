"""Test Temporal code exactly the way production runs it.

- `make_worker(client, passthrough=…, **kwargs)`: a drop-in for `Worker(...)` built with the production sandbox
  runner and the strict import policy, so a workflow that pulls settings or I/O into the sandbox is reported in
  tests, not in production. Pass `workflow_runner=` to override.
- `time_skipping_env(...)`: the SDK's time-skipping test server with the shared data converter.
- `query_eventually(...)`: poll a workflow query until a predicate holds.
- `factory_runtime.testing.golden`: golden runs of the process engine (`GoldenHarness`), with the generic stubs in
  `factory_runtime.testing.golden_stubs`; imported by its own name.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Iterable

from temporalio.client import Client, WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from factory_runtime.temporal.converter import data_converter
from factory_runtime.temporal.sandbox import STRICT_IMPORT_POLICY, build_workflow_runner
from factory_runtime.temporal.settings import DEFAULT_COMPRESS_MIN_BYTES, DEFAULT_WARN_BYTES

# Workflow classes defined in test modules (a product's, and this kit's golden stubs), and the test runners' own
# machinery those modules import.
TEST_PASSTHROUGH_MODULES = ("tests", "factory_runtime.testing", "_pytest", "pytest", "unittest")


def make_worker(client: Client, passthrough: Iterable[str] = (), **kwargs: Any) -> Worker:
    if "workflow_runner" not in kwargs:
        kwargs["workflow_runner"] = build_workflow_runner(
            (*passthrough, *TEST_PASSTHROUGH_MODULES), import_policy=STRICT_IMPORT_POLICY
        )
    return Worker(client, **kwargs)


async def time_skipping_env(
    min_bytes: int = DEFAULT_COMPRESS_MIN_BYTES, warn_bytes: int = DEFAULT_WARN_BYTES
) -> WorkflowEnvironment:
    """Start the time-skipping test server (downloaded on first use); `async with` it, or call `shutdown()`."""
    return await WorkflowEnvironment.start_time_skipping(data_converter=data_converter(min_bytes, warn_bytes))


async def query_eventually(
    handle: WorkflowHandle[Any, Any],
    query_fn: Any,
    predicate: Callable[[Any], bool],
    *,
    timeout: float = 10,
    interval: float = 0.2,
) -> Any:
    """Poll a workflow query until `predicate` holds (the time-skipping env does not advance timers by itself)."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = await handle.query(query_fn)
            if predicate(last):
                return last
        except Exception:
            pass
        await asyncio.sleep(interval)
    raise AssertionError(f"{query_fn} never satisfied predicate; last={last}")
