"""Start a workflow or join its run, delivering a signal in the same call."""

from typing import Any, Sequence

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy


async def signal_with_start(
    client: Client,
    workflow: str,
    workflow_id: str,
    *,
    task_queue: str,
    signal: str,
    signal_args: Sequence[Any] = (),
    args: Sequence[Any] = (),
    **start_options: Any,
) -> WorkflowHandle[Any, Any]:
    """Start `workflow` (by name, so the caller needn't import it) under `workflow_id` with `args`, or join the run
    already going, and deliver `signal` with `signal_args`. Temporal's SignalWithStart is atomic, so an event is never
    lost between a "not running" check and a start. `args` are used only when a run starts; a joined run keeps its
    own input. A running id is joined (USE_EXISTING, the only conflict policy SignalWithStart accepts besides the
    default); `start_options` go to `start_workflow` (execution_timeout, memo, id_reuse_policy, ...). Returns the
    run's handle. Raises RPCError when the server is unreachable."""
    return await client.start_workflow(
        workflow,
        args=list(args),
        id=workflow_id,
        task_queue=task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal=signal,
        start_signal_args=list(signal_args),
        **start_options,
    )
