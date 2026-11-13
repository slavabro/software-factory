"""Step types: what one YAML step does when the run reaches it.

A step type is a plain async function `step(wf, step, ctx) -> result` plus a flag whether it may run inside
`for_each`. `wf` is the running workflow (its `step_state` holds per-run state a step type needs, its `step_types` the
map it dispatches from), `step` the step's dict from the process file, `ctx` where the run stands (`StepContext`).
The built-in map is read-only; a product adds its own types in a map of its own on its workflow classes:

    STEP_TYPES = {**BUILTIN_STEP_TYPES, "review": StepType(review_step, for_each=True)}

Each built-in step issues exactly the commands it always has (see the README's known quirks before changing one).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from temporalio import workflow
from temporalio.common import RetryPolicy

from factory_runtime.process.templates import resolve_argument

if TYPE_CHECKING:
    from factory_runtime.process.base import ProcessWorkflowBase

# Default attempts for every YAML activity step; override per step with `retry.max_attempts`. A product's own step
# types use it for their housekeeping activities too.
DEFAULT_STEP_MAX_ATTEMPTS = 3

# start_to_close for an activity step that sets no `timeout:` (seconds)
DEFAULT_ACTIVITY_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class StepContext:
    """Where the run stands when a step starts.

    `base_id` is the step's YAML id (results, pauses and resumes use it); `unique_id` is `{base_id}_{uuid4}`, and
    `{base_id}_{uuid4}[i]` for iteration i inside `for_each` (where `base_id` is the `for_each` step's id). `results`
    is the run's live results dict (step id -> result), `input` its input data, `item` the current `for_each` item
    (None outside one), `process_def` and `metadata` the run's arguments."""

    base_id: Any
    unique_id: str
    results: dict[str, Any]
    input: dict[str, Any]
    process_def: dict[str, Any]
    metadata: dict[str, Any]
    item: Any = None

    def resolve(self, arg: Any, *, strict: bool = False, log_missing: bool = True) -> Any:
        """`arg` with its placeholders resolved against this context (templates.resolve_argument)."""
        return resolve_argument(arg, self.results, self.input, strict, self.item, log_missing)

    def iteration(self, index: int, item: Any) -> StepContext:
        """The context of iteration `index` of a `for_each` step, over `item`."""
        return replace(self, unique_id=f"{self.unique_id}[{index}]", item=item)


StepFn = Callable[["ProcessWorkflowBase", dict[str, Any], StepContext], Awaitable[Any]]


@dataclass(frozen=True)
class StepType:
    run: StepFn
    for_each: bool = False  # may run as a `for_each` inner step


def build_step_retry_policy(retry: dict[str, Any] | None) -> RetryPolicy:
    """A RetryPolicy from a YAML step's optional `retry:` block: `max_attempts` (default DEFAULT_STEP_MAX_ATTEMPTS),
    `initial_interval_seconds` (SDK default when omitted), `non_retryable_error_types` (fail at once)."""
    retry = retry or {}
    kwargs: dict[str, Any] = {
        "maximum_attempts": int(retry.get("max_attempts", DEFAULT_STEP_MAX_ATTEMPTS)),
    }
    initial_interval = retry.get("initial_interval_seconds")
    if initial_interval is not None:
        kwargs["initial_interval"] = timedelta(seconds=float(initial_interval))
    non_retryable = retry.get("non_retryable_error_types")
    if non_retryable:
        kwargs["non_retryable_error_types"] = [str(t) for t in non_retryable]
    return RetryPolicy(**kwargs)


async def activity_step(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> Any:
    """Run the activity named `activity:` with `args:` (each resolved) and a bounded retry policy.

    `kwargs:` are passed to `execute_activity` as options, not to the activity, and more than one `args:` element
    fails the workflow task (the SDK takes one `arg`): both known quirks, kept so running workflows replay."""
    activity_name = step.get("activity")
    if not activity_name:
        raise ValueError(f"Activity step {ctx.unique_id} missing 'activity' key")

    timeout_seconds = step.get("timeout", DEFAULT_ACTIVITY_TIMEOUT_SECONDS)
    resolved_args = [ctx.resolve(a) for a in step.get("args", [])]
    resolved_kwargs = {k: ctx.resolve(v) for k, v in step.get("kwargs", {}).items()}

    # Explicit, bounded retries on every step (never the SDK's unlimited default), optional heartbeat deadline.
    activity_options: dict[str, Any] = {
        "start_to_close_timeout": timedelta(seconds=timeout_seconds),
        "retry_policy": build_step_retry_policy(step.get("retry")),
    }
    heartbeat_timeout_seconds = step.get("heartbeat_timeout_seconds")
    if heartbeat_timeout_seconds:
        activity_options["heartbeat_timeout"] = timedelta(seconds=heartbeat_timeout_seconds)

    execute: Callable[..., Awaitable[Any]] = workflow.execute_activity
    return await execute(activity_name, *resolved_args, **resolved_kwargs, **activity_options)


_PARENT_CLOSE_POLICIES = {
    "abandon": workflow.ParentClosePolicy.ABANDON,
    "terminate": workflow.ParentClosePolicy.TERMINATE,
    "request_cancel": workflow.ParentClosePolicy.REQUEST_CANCEL,
}


async def workflow_step(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> Any:
    """Run the child workflow `workflow_name:` with `args:` resolved as its one argument.

    Options: `workflow_id` (default `{workflow_name}_{uuid4}`), `task_queue` (default the parent's),
    `wait_for_result` (default true; false starts it and gives `{"child_workflow_id": ...}`), `parent_close_policy`
    (`abandon`, `terminate`, `request_cancel`; anything else is logged and the server default used),
    `execution_timeout` (seconds)."""
    workflow_name = step.get("workflow_name")
    workflow_args = ctx.resolve(step.get("args", {}))

    child_id = step.get("workflow_id") or f"{workflow_name}_{workflow.uuid4()}"
    task_queue = step.get("task_queue", workflow.info().task_queue)
    wait_for_result = step.get("wait_for_result", True)

    policy_str = step.get("parent_close_policy")
    parent_close_policy = _PARENT_CLOSE_POLICIES.get(policy_str) if isinstance(policy_str, str) else None
    if policy_str is not None and parent_close_policy is None:
        workflow.logger.warning(f"Unknown parent_close_policy '{policy_str}' for step '{ctx.unique_id}', using default")

    workflow.logger.info(f"Starting child workflow '{workflow_name}' as step '{ctx.unique_id}'")

    child_kwargs: dict[str, Any] = dict(id=child_id, task_queue=task_queue)
    if parent_close_policy:
        child_kwargs["parent_close_policy"] = parent_close_policy
    execution_timeout_seconds = step.get("execution_timeout")
    if execution_timeout_seconds:
        child_kwargs["execution_timeout"] = timedelta(seconds=execution_timeout_seconds)

    if wait_for_result:
        execute: Callable[..., Awaitable[Any]] = workflow.execute_child_workflow
        return await execute(workflow_name, workflow_args, **child_kwargs)
    start: Callable[..., Awaitable[workflow.ChildWorkflowHandle[Any, Any]]] = workflow.start_child_workflow
    child_handle = await start(workflow_name, workflow_args, **child_kwargs)
    return {"child_workflow_id": child_handle.id}


async def pause_step(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> Any:
    """Wait for `resume_step` under the step's YAML id; `timeout:` (seconds) gives `{"action": "expired", ...}`.
    `signal_name:` is ignored (a known quirk)."""
    return await wf.wait_for_resume(step, ctx.base_id)


async def for_each_step(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> list[Any]:
    """Run the inner `step:` once per element of `items:` (a list; None gives [], anything else is one item).

    Only types flagged `for_each` run inside; any other inner type (pause, approval, a nested for_each, an unknown
    one) logs a warning and gives None. `on_error: continue` on the for_each step gives None for a failed iteration;
    an inner step's own `on_error` is ignored with a warning. The result is the list of iteration results."""
    raw_items = step.get("items")
    inner_step = step.get("step")
    on_error = step.get("on_error")

    if not inner_step:
        workflow.logger.warning(f"for_each step {ctx.unique_id} has no inner 'step' definition")
        return []

    if inner_step.get("on_error"):
        workflow.logger.warning(
            f"for_each step {ctx.unique_id}: inner step has 'on_error' which is ignored — "
            f"use on_error on the for_each step instead"
        )

    items = ctx.resolve(raw_items)
    if not isinstance(items, list):
        if items is None:
            workflow.logger.info(f"for_each step {ctx.unique_id}: items resolved to None, skipping")
            return []
        items = [items]  # a single item is a list of one

    workflow.logger.info(f"for_each step {ctx.unique_id}: iterating over {len(items)} items")

    results: list[Any] = []
    inner_step_type = inner_step.get("type")
    inner = wf.step_types.get(inner_step_type) if isinstance(inner_step_type, str) else None

    for i, loop_item in enumerate(items):
        workflow.logger.info(f"for_each {ctx.unique_id}: iteration {i}/{len(items)}")
        try:
            if inner is not None and inner.for_each:
                result = await inner.run(wf, inner_step, ctx.iteration(i, loop_item))
            else:
                workflow.logger.warning(f"for_each {ctx.unique_id}: unsupported inner step type '{inner_step_type}'")
                result = None
            results.append(result)
        except Exception as e:
            if on_error == "continue":
                workflow.logger.warning(f"for_each {ctx.unique_id}[{i}] failed: {e}, continuing")
                results.append(None)
            else:
                raise

    return results


BUILTIN_STEP_TYPES: Mapping[str, StepType] = MappingProxyType(
    {
        "activity": StepType(activity_step, for_each=True),
        "workflow": StepType(workflow_step, for_each=True),
        "pause": StepType(pause_step),
        "approval": StepType(pause_step),
        "for_each": StepType(for_each_step),
    }
)
