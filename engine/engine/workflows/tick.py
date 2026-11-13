"""ScheduleTick: the bridge from a Temporal Schedule to a team (docs/designs/engine.md, section 3).

A Schedule can only start a workflow, and it appends the time to that workflow's ID, so it can't start
`team/<repo>/<team>` itself. Each tick wakes the team with its own workflow ID as the event id: unique per scheduled
time, so a retried tick is deduped by the team."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, VersioningBehavior

with workflow.unsafe.imports_passed_through():
    from engine.models import TickInput, Wake, WakeTeamInput, WakeTeamResult


@workflow.defn(name="ScheduleTick", versioning_behavior=VersioningBehavior.PINNED)
class ScheduleTick:
    @workflow.run
    async def run(self, inp: TickInput) -> WakeTeamResult:
        result: WakeTeamResult = await workflow.execute_activity(
            "wake_team",
            WakeTeamInput(ref=inp.ref, wake=Wake(reason="schedule", event_id=workflow.info().workflow_id)),
            result_type=WakeTeamResult,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
            summary="wake the team",
        )
        return result
