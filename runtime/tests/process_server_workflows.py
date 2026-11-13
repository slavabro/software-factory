"""Child workflows the process server tests start (module level: the sandbox imports workflow classes by module)."""

import asyncio
from datetime import timedelta
from typing import Any, Dict

from temporalio import workflow

from tests.process_workflows import RECORD_CHILD


@workflow.defn(name="MockChildWorkflow")
class MockChildWorkflow:
    """Returns what it was given."""

    @workflow.run
    async def run(self, input_data: Any) -> Dict[str, Any]:
        return {"child_received": input_data}


@workflow.defn(name="MockSlowChildWorkflow")
class MockSlowChildWorkflow:
    """Never completes (for fire-and-forget and abandon tests)."""

    @workflow.run
    async def run(self, input_data: Any) -> Dict[str, Any]:
        await workflow.wait_condition(lambda: False)
        return {}


@workflow.defn(name=RECORD_CHILD)
class CancellableRecordingChild:
    """Stands in for the plug-in step's child: reports it started, blocks until cancelled, then records the cancel
    on its way out (as a product's long-lived child does)."""

    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        await workflow.execute_activity(
            "child_started_activity",
            {"child_id": workflow.info().workflow_id},
            start_to_close_timeout=timedelta(seconds=5),
        )
        try:
            await workflow.wait_condition(lambda: False)
        except asyncio.CancelledError:
            await workflow.execute_activity(
                "child_cancelled_activity",
                {"child_id": workflow.info().workflow_id, "status": "CANCELLED"},
                start_to_close_timeout=timedelta(seconds=5),
            )
            raise
        return {}
