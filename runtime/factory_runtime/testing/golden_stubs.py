"""The generic stubs golden process files call (factory_runtime.testing.golden): children and activities whose result
depends only on their input. A product adds its own step types' stubs next to these.

Imports only temporalio and the standard library: the workflow sandbox may re-import it on every run.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError

GATE_ACTIVITY = "golden_gate_activity"

# ---------------------------------------------------------------------------
# Gates: an activity that waits on one does not finish until the script releases it (`release: <gate>`)
# ---------------------------------------------------------------------------

_GATES: dict[str, asyncio.Event] = {}


def gate(name: str) -> asyncio.Event:
    """The gate `name` of the case running now (a product's stub activity may wait on its own gates)."""
    return _GATES.setdefault(name, asyncio.Event())


def reset_gates() -> None:
    _GATES.clear()


# ---------------------------------------------------------------------------
# Children
# ---------------------------------------------------------------------------


@workflow.defn(name="GoldenChildWorkflow")
class GoldenChildWorkflow:
    """Echoes its input. With a dict input carrying `gate`, first waits on that gate (an activity the script
    releases), so a Parallel run decides when the child finishes."""

    @workflow.run
    async def run(self, arg: Any = None) -> dict[str, Any]:
        if isinstance(arg, dict) and arg.get("gate"):
            await workflow.execute_activity(
                GATE_ACTIVITY, {"gate": arg["gate"]}, start_to_close_timeout=timedelta(hours=1)
            )
        return {"child": "GoldenChildWorkflow", "received": arg}


@workflow.defn(name="GoldenFailingChildWorkflow")
class GoldenFailingChildWorkflow:
    @workflow.run
    async def run(self, arg: Any = None) -> dict[str, Any]:
        raise ApplicationError("golden child failed", type="GoldenChildError", non_retryable=True)


@workflow.defn(name="GoldenSlowChildWorkflow")
class GoldenSlowChildWorkflow:
    """Sleeps an hour: outlives a short `execution_timeout`."""

    @workflow.run
    async def run(self, arg: Any = None) -> dict[str, Any]:
        await workflow.sleep(timedelta(hours=1))
        return {"child": "GoldenSlowChildWorkflow", "received": arg}


STUB_WORKFLOWS: tuple[type, ...] = (GoldenChildWorkflow, GoldenFailingChildWorkflow, GoldenSlowChildWorkflow)


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


@activity.defn(name="golden_echo_activity")
async def golden_echo_activity(*args: Any) -> dict[str, Any]:
    return {"activity": "golden_echo_activity", "args": list(args)}


@activity.defn(name="golden_value_activity")
async def golden_value_activity(*args: Any) -> Any:
    return args[0] if args else None


@activity.defn(name="golden_fail_activity")
async def golden_fail_activity(*args: Any) -> dict[str, Any]:
    raise ApplicationError("golden activity failed", type="GoldenFatalError")


@activity.defn(name="golden_maybe_fail_activity")
async def golden_maybe_fail_activity(item: Any = None) -> dict[str, Any]:
    if isinstance(item, dict) and item.get("fail"):
        raise ApplicationError(f"golden item failed: {item.get('name')}", type="GoldenItemError")
    return {"ok": item}


@activity.defn(name=GATE_ACTIVITY)
async def golden_gate_activity(params: dict[str, Any]) -> dict[str, Any]:
    await gate(params["gate"]).wait()
    return {"gate": params["gate"], "params": params}


STUB_ACTIVITIES: tuple[Any, ...] = (
    golden_echo_activity,
    golden_value_activity,
    golden_fail_activity,
    golden_maybe_fail_activity,
    golden_gate_activity,
)
