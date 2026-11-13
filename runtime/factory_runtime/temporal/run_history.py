"""Why a closed workflow run failed, read from its history."""

from temporalio.api.failure.v1 import Failure
from temporalio.client import Client, WorkflowHistoryEventFilterType


def _failure_chain(failure: Failure | None) -> str:
    """The failure message and every cause beneath it: 'outer ← inner ← root'."""
    messages: list[str] = []
    while failure is not None:
        if failure.message:
            messages.append(failure.message)
        failure = failure.cause if failure.HasField("cause") else None
    return " ← ".join(messages)


async def get_run_failure(client: Client, workflow_id: str, run_id: str | None, max_chars: int) -> str | None:
    """A one-line reason a closed run did not complete, or None when it did (or none is recorded)."""
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)
    async for event in handle.fetch_history_events(event_filter_type=WorkflowHistoryEventFilterType.CLOSE_EVENT):
        if event.HasField("workflow_execution_failed_event_attributes"):
            text = _failure_chain(event.workflow_execution_failed_event_attributes.failure)
            return text[:max_chars] or "The run failed without a message."
        if event.HasField("workflow_execution_timed_out_event_attributes"):
            return "The run timed out."
        if event.HasField("workflow_execution_terminated_event_attributes"):
            reason = event.workflow_execution_terminated_event_attributes.reason
            return f"The run was terminated: {reason}"[:max_chars] if reason else "The run was terminated."
    return None
