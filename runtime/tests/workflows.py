"""Workflows for the server tests, in their own module as a product would have them."""

from datetime import timedelta
from decimal import Decimal

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from tests.models import Order


@activity.defn
async def price(order: Order) -> Order:
    return order.model_copy(update={"total": order.qty * Decimal("2.50")})


@activity.defn
async def explode(_: str) -> None:
    raise RuntimeError("the broker said no")


@workflow.defn
class PriceOrder:
    @workflow.run
    async def run(self, order: Order) -> Order:
        return await workflow.execute_activity(price, order, start_to_close_timeout=timedelta(seconds=10))


@workflow.defn
class BigResult:
    @workflow.run
    async def run(self, rows: int) -> dict:
        return {"rows": [{"id": i, "note": "x" * 100} for i in range(rows)]}


@workflow.defn
class Failing:
    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(
            explode,
            "x",
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@workflow.defn
class Inbox:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.done = False

    @workflow.run
    async def run(self, first: str) -> list[str]:
        self.events.append(f"start:{first}")
        await workflow.wait_condition(lambda: self.done)
        return self.events

    @workflow.signal
    def event(self, name: str) -> None:
        if name == "stop":
            self.done = True
        else:
            self.events.append(name)

    @workflow.query
    def seen(self) -> list[str]:
        return self.events


@workflow.defn
class Waiting:
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)
