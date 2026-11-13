"""On-demand runs of a process: start one under a workflow id (or join the run already going), then read how it
stands. (Starting a Python workflow with a signal is `factory_runtime.temporal.signal_with_start`.)"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Mapping

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from factory_runtime.process.files import process_interpreter
from factory_runtime.process.settings import ProcessSettings
from factory_runtime.temporal.settings import TemporalSettings


class RunState(StrEnum):
    """Where the latest run under a workflow id stands."""

    NONE = "none"  # never started
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"  # failed, timed out, terminated, cancelled, or its status could not be read


_STATE_BY_STATUS = {
    WorkflowExecutionStatus.RUNNING: RunState.RUNNING,
    WorkflowExecutionStatus.COMPLETED: RunState.COMPLETED,
    WorkflowExecutionStatus.CONTINUED_AS_NEW: RunState.COMPLETED,
}


@dataclass(frozen=True)
class ProcessRun:
    state: RunState
    workflow_id: str
    started_at: datetime | None = None
    closed_at: datetime | None = None
    results: dict[str, Any] = field(default_factory=dict)  # step id -> result (completed runs only)


async def start_process_run(
    client: Client,
    process_def: dict[str, Any],
    input_data: dict[str, Any],
    workflow_id: str,
    *,
    settings: ProcessSettings,
    temporal: TemporalSettings,
    metadata: Mapping[str, Any],
    execution_timeout: timedelta,
    memo: Mapping[str, Any] | None = None,
    summary: str | None = None,
    rerun_closed: bool = True,
) -> None:
    """Start `process_def` under `workflow_id` with three arguments, `[process_def, input_data, metadata]`, on the
    interpreter its schedules use, or join the run already going (USE_EXISTING).

    `rerun_closed=False`: an id whose run already closed is not started again, and that counts as started. `memo`
    labels the run for the product's pages. Raises RPCError when the server is unreachable."""
    try:
        await client.start_workflow(
            process_interpreter(process_def, settings.default_interpreter),
            args=[process_def, input_data, dict(metadata)],
            id=workflow_id,
            task_queue=temporal.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=(
                WorkflowIDReusePolicy.ALLOW_DUPLICATE if rerun_closed else WorkflowIDReusePolicy.REJECT_DUPLICATE
            ),
            execution_timeout=execution_timeout,
            memo=dict(memo) if memo is not None else None,
            static_summary=summary,
        )
    except WorkflowAlreadyStartedError:
        if rerun_closed:
            raise
        # REJECT_DUPLICATE on a closed run: it ran once, which is what the caller asked


async def describe_process_run(client: Client, workflow_id: str) -> ProcessRun:
    """The latest run under `workflow_id`: NONE, RUNNING, COMPLETED (with its step results) or FAILED. Raises
    RPCError other than not-found."""
    handle = client.get_workflow_handle(workflow_id)
    try:
        desc = await handle.describe()
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            return ProcessRun(RunState.NONE, workflow_id)
        raise
    state = _STATE_BY_STATUS.get(desc.status, RunState.FAILED) if desc.status is not None else RunState.FAILED
    results = (await handle.result() or {}) if state == RunState.COMPLETED else {}
    return ProcessRun(state, workflow_id, desc.start_time, desc.close_time, results)
