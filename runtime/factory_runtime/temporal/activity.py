"""Helpers for code that runs inside Temporal activities.

- `activity_idempotency_key()`: a key for side effects that must not be duplicated when an activity attempt is
  retried (notifications, audit rows, records).
- `DATA_SHAPE_ERRORS`: exceptions that mean "the input is wrong". A retry cannot fix them, so activities wrap them as
  non-retryable `ApplicationError` and let everything else (connection resets, timeouts, provider 5xx) retry under
  the workflow's retry policy.
"""

from pydantic import ValidationError
from temporalio import activity

DATA_SHAPE_ERRORS: tuple[type[Exception], ...] = (
    ValueError,  # includes Decimal and UUID parse errors
    KeyError,
    TypeError,
    ValidationError,
)


def activity_idempotency_key(*parts: object) -> str:
    """Key identifying THIS activity invocation, stable across retry attempts.

    `workflow_id:run_id:activity_id`: the activity id is fixed when the workflow schedules the activity and shared by
    every retry, so a side effect tagged with it is written once however many attempts run. The run id is included
    because activity ids restart on continue-as-new and long-lived loops reuse workflow ids, so
    `workflow_id:activity_id` alone would collide across cycles. Extra `parts` are appended for activities that write
    several rows per attempt. Must be called inside an activity (`activity.info()` raises otherwise).
    """
    info = activity.info()
    key = f"{info.workflow_id}:{info.workflow_run_id}:{info.activity_id}"
    if parts:
        key = ":".join([key, *(str(p) for p in parts)])
    return key
