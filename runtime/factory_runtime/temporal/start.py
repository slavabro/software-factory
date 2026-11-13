"""Start a workflow or join its run, delivering a signal in the same call."""

from typing import Any

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy


async def signal_with_start(
    client: Client,
    workflow: str,
    workflow_id: str,
    *,
    task_queue: str,
    signal: str,
    signal_arg: Any,
    arg: Any = None,
) -> None:
    """Start `workflow` (by name, so the caller needn't import it) under `workflow_id` with `arg` as its one input
    (none when None), or join the run already going, and deliver `signal` with `signal_arg`. Temporal's
    SignalWithStart is atomic, so an event is never lost between a "not running" check and a start. `arg` is used
    only when a run starts; a joined run keeps its own input. A running id is joined (USE_EXISTING, the only conflict
    policy SignalWithStart accepts besides the default); a closed id starts a new run (server default reuse policy).
    Raises RPCError when the server is unreachable."""
    await client.start_workflow(
        workflow,
        *([arg] if arg is not None else []),
        id=workflow_id,
        task_queue=task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal=signal,
        start_signal_args=[signal_arg],
    )
