"""Versions of one long-running workflow, `Order`, as successive releases of a product would ship it, and the worker
definitions the replay check and patch report tests build from them.

An order waits for the signal `go`, reserves (an activity), then stays open. Releases:

- `OrderV1`: reserves with `reserve`; also handles the signal `nudge`.
- `OrderReordered`: ships before reserving: a run already past the reservation no longer replays.
- `OrderNoNudge`: V1 without the `nudge` handler.
- `OrderPatched`: `patched("reserve-v2")` picks `reserve_v2`; runs already past the point keep `reserve`.
- `OrderDeprecated`: `deprecate_patch("reserve-v2")`, then `reserve_v2`.
- `OrderPatchRemoved`: `reserve_v2` with no patch call.
- `OrderStamped`: stamps with the local activity `stamp` instead of reserving.
"""

from typing import Callable, Sequence

from datetime import timedelta

from temporalio import activity, workflow

from factory_runtime.temporal import WorkerDefinition, activity_names
from temporalio.worker import WorkflowRunner

from factory_runtime.testing.golden import default_workflow_runner

QUEUE = "replay-check"
PATCH = "reserve-v2"
TIMEOUT = timedelta(seconds=10)


@activity.defn
async def reserve() -> None:
    pass


@activity.defn
async def reserve_v2() -> None:
    pass


@activity.defn
async def ship() -> None:
    pass


@activity.defn
async def stamp() -> None:
    pass


ACTIVITIES = [reserve, reserve_v2, ship, stamp]


class _Order:
    def __init__(self) -> None:
        self.go_ahead = False
        self.stage = "waiting"

    @workflow.signal
    def go(self) -> None:
        self.go_ahead = True

    @workflow.query
    def current_stage(self) -> str:
        return self.stage

    async def wait_then(self, *activities: str) -> None:
        await workflow.wait_condition(lambda: self.go_ahead)
        for name in activities:
            await workflow.execute_activity(name, start_to_close_timeout=TIMEOUT)
        self.stage = "reserved"
        await workflow.wait_condition(lambda: False)


@workflow.defn(name="Order")
class OrderV1(_Order):
    @workflow.run
    async def run(self) -> None:
        await self.wait_then("reserve")

    @workflow.signal
    def nudge(self) -> None:
        pass


@workflow.defn(name="Order")
class OrderReordered(_Order):
    @workflow.run
    async def run(self) -> None:
        await self.wait_then("ship", "reserve")

    @workflow.signal
    def nudge(self) -> None:
        pass


@workflow.defn(name="Order")
class OrderNoNudge(_Order):
    @workflow.run
    async def run(self) -> None:
        await self.wait_then("reserve")


@workflow.defn(name="Order")
class OrderPatched(_Order):
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self.go_ahead)
        if workflow.patched(PATCH):
            await self.wait_then("reserve_v2")
        else:
            await self.wait_then("reserve")


@workflow.defn(name="Order")
class OrderDeprecated(_Order):
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self.go_ahead)
        workflow.deprecate_patch(PATCH)
        await self.wait_then("reserve_v2")


@workflow.defn(name="Order")
class OrderPatchRemoved(_Order):
    @workflow.run
    async def run(self) -> None:
        await self.wait_then("reserve_v2")


@workflow.defn(name="Order")
class OrderStamped(_Order):
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self.go_ahead)
        await workflow.execute_local_activity("stamp", start_to_close_timeout=TIMEOUT)
        self.stage = "reserved"
        await workflow.wait_condition(lambda: False)


@workflow.defn(name="Billing")
class Billing:
    """Calls an activity on a queue no worker polls, so it stays pending."""

    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity("charge", task_queue="billing", start_to_close_timeout=timedelta(days=1))


def definition(
    *workflows: type,
    external: frozenset[str] = frozenset(),
    activities: Sequence[Callable[..., object]] = ACTIVITIES,
    runner: WorkflowRunner | None = None,
) -> WorkerDefinition:
    return WorkerDefinition(
        task_queues=(QUEUE,),
        workflows=workflows,
        activity_names=activity_names(activities),
        external_activity_names=external,
        workflow_runner=runner or default_workflow_runner(),
    )


# Module-level definitions, as a product exposes them for the CLI.
CURRENT = definition(OrderV1)
REORDERED = definition(OrderReordered)
PATCHED = definition(OrderPatched)


def patched_definition() -> WorkerDefinition:
    return PATCHED
