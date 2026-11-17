"""Step types: what one YAML step does when the run reaches it.

A step type is a plain async function `step(wf, step, ctx) -> result` plus what the validator needs to know about
it: whether it may run inside `for_each`, the keys a step of the type must have (`required`) and may have
(`optional`; None: its keys aren't checked), and an optional `check(step) -> [problems]` for its values. Every step
may also have `id`, `type`, `condition`, `on_error` and `description` (`COMMON_STEP_KEYS`). The built-in map is
read-only; a product adds its own types in a map of its own on its workflow classes:

    STEP_TYPES = {**BUILTIN_STEP_TYPES, "review": StepType(review_step, for_each=True, required=frozenset({"subject"}),
                                                           optional=frozenset({"notes"}))}

A step raises `definition_error(...)` (errors.py) for a definition it can't run; that fails the run at once, whatever
its `on_error`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from temporalio import workflow
from temporalio.common import RetryPolicy

from factory_runtime.process.errors import definition_error, is_definition_error
from factory_runtime.process.templates import resolve_argument

if TYPE_CHECKING:
    from factory_runtime.process.base import ProcessWorkflowBase

# Default attempts for every YAML activity step; override per step with `retry.max_attempts`. A product's own step
# types use it for their housekeeping activities too.
DEFAULT_STEP_MAX_ATTEMPTS = 3

# start_to_close for an activity step that sets no `timeout:` (seconds)
DEFAULT_ACTIVITY_TIMEOUT_SECONDS = 60

# Keys every top-level step may have; a `for_each` inner step may have `type` and `description` only of these.
COMMON_STEP_KEYS = frozenset({"id", "type", "condition", "on_error", "description"})
INNER_STEP_KEYS = frozenset({"type", "description"})
ON_ERROR_VALUES = ("continue",)

# Keys that were removed or renamed, with what to write instead (the validator's message).
RETIRED_KEYS: Mapping[str, str] = MappingProxyType(
    {
        "kwargs": "`kwargs:` was renamed `options:` (they are execute_activity options, not activity arguments)",
        "signal_name": "`signal_name:` was removed: a pause step is resumed by `resume_step(step_id, data)` under "
        "its own id",
    }
)

# `options:` of an activity step: the execute_activity options a YAML step may set (timeouts in seconds).
ACTIVITY_OPTIONS = frozenset(
    {"summary", "activity_id", "task_queue", "schedule_to_close_timeout", "schedule_to_start_timeout"}
)
_SECONDS_OPTIONS = frozenset({"schedule_to_close_timeout", "schedule_to_start_timeout"})
RETRY_KEYS = frozenset({"max_attempts", "initial_interval_seconds", "non_retryable_error_types"})


class _LazyUuid:
    """One `workflow.uuid4()`, drawn the first time a step asks for its unique id (never for a step that doesn't)."""

    def __init__(self, value: str | None = None) -> None:
        self._value = value

    def get(self) -> str:
        if self._value is None:
            self._value = str(workflow.uuid4())
        return self._value


@dataclass(frozen=True)
class StepContext:
    """Where the run stands when a step starts.

    `base_id` is the step's YAML id (results, pauses and resumes use it). `unique_id` is `{base_id}_{uuid4}`, and
    `{base_id}_{uuid4}[i]` for iteration i inside `for_each` (where `base_id` is the `for_each` step's id); its
    uuid4 is drawn the first time a step reads it, once per step (every iteration shares it). `results` is the
    run's live results dict (step id -> result), `input` its input data, `item` the current `for_each` item (None
    outside one), `index` the iteration, `process_def` and `metadata` the run's arguments."""

    base_id: Any
    results: dict[str, Any]
    input: dict[str, Any]
    process_def: dict[str, Any]
    metadata: dict[str, Any]
    item: Any = None
    index: int | None = None
    uuid: _LazyUuid = field(default_factory=_LazyUuid, repr=False, compare=False)

    @property
    def unique_id(self) -> str:
        unique = f"{self.base_id}_{self.uuid.get()}"
        return unique if self.index is None else f"{unique}[{self.index}]"

    def resolve(self, arg: Any, *, strict: bool = False, log_missing: bool = True) -> Any:
        """`arg` with its placeholders resolved against this context (templates.resolve_argument)."""
        return resolve_argument(arg, self.results, self.input, strict, self.item, log_missing)

    def iteration(self, index: int, item: Any) -> StepContext:
        """The context of iteration `index` of a `for_each` step, over `item`."""
        return replace(self, index=index, item=item)


StepFn = Callable[["ProcessWorkflowBase", dict[str, Any], StepContext], Awaitable[Any]]
StepCheck = Callable[[dict[str, Any]], list[str]]


@dataclass(frozen=True)
class StepType:
    run: StepFn
    for_each: bool = False  # may run as a `for_each` inner step
    required: frozenset[str] = frozenset()  # keys a step of this type must have
    optional: frozenset[str] | None = None  # keys it may have besides those and COMMON_STEP_KEYS; None: unchecked
    check: StepCheck | None = None  # problems with the step's values (the keys are checked already)


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


def _is_seconds(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _check_activity(step: dict[str, Any]) -> list[str]:
    problems = []
    if not isinstance(step.get("activity"), str) or not step["activity"]:
        problems.append("`activity:` must name the activity")
    args = step.get("args")
    if isinstance(args, list) and len(args) > 1:
        problems.append(
            f"`args:` has {len(args)} elements; an activity takes one argument: pass a dict (or one value)"
        )
    options = step.get("options")
    if options is not None:
        if not isinstance(options, dict):
            problems.append("`options:` must be a mapping of execute_activity options")
        else:
            unknown = sorted(set(options) - ACTIVITY_OPTIONS)
            if unknown:
                problems.append(f"unknown `options:` {unknown} (known: {sorted(ACTIVITY_OPTIONS)})")
            for key in sorted(set(options) & _SECONDS_OPTIONS):
                if not _is_seconds(options[key]):
                    problems.append(f"`options.{key}` must be seconds")
    for key in ("timeout", "heartbeat_timeout_seconds"):
        if key in step and not _is_seconds(step[key]):
            problems.append(f"`{key}:` must be seconds")
    retry = step.get("retry")
    if retry is not None:
        if not isinstance(retry, dict):
            problems.append("`retry:` must be a mapping")
        else:
            unknown = sorted(set(retry) - RETRY_KEYS)
            if unknown:
                problems.append(f"unknown `retry:` keys {unknown} (known: {sorted(RETRY_KEYS)})")
    return problems


def _activity_args(step: dict[str, Any], ctx: StepContext) -> list[Any]:
    """The activity's arguments: none when `args:` is absent or [], the element of a one-element list, otherwise
    the value itself (a dict is passed whole)."""
    args = step.get("args")
    if args is None or args == []:
        return []
    if isinstance(args, list):
        if len(args) > 1:
            raise definition_error(f"Step '{ctx.base_id}': `args:` has {len(args)} elements; an activity takes one")
        return [ctx.resolve(args[0])]
    return [ctx.resolve(args)]


def _activity_options(step: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
    options = step.get("options") or {}
    unknown = sorted(set(options) - ACTIVITY_OPTIONS)
    if unknown:
        raise definition_error(f"Step '{ctx.base_id}': unknown `options:` {unknown}")
    resolved: dict[str, Any] = {}
    for key, value in options.items():
        value = ctx.resolve(value)
        resolved[key] = timedelta(seconds=value) if key in _SECONDS_OPTIONS else value
    return resolved


async def activity_step(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> Any:
    """Run the activity named `activity:` with `args:` resolved as its one argument (a one-element list, or a dict
    or other value passed whole), a bounded retry policy (`retry:`), `timeout:` (start_to_close seconds, default
    60), `heartbeat_timeout_seconds:`, and `options:` (execute_activity options: summary, activity_id, task_queue,
    schedule_to_close_timeout and schedule_to_start_timeout in seconds)."""
    activity_name = step.get("activity")
    if not activity_name:
        raise definition_error(f"Activity step '{ctx.base_id}' missing 'activity' key")

    timeout_seconds = step.get("timeout", DEFAULT_ACTIVITY_TIMEOUT_SECONDS)
    args = _activity_args(step, ctx)

    # Explicit, bounded retries on every step (never the SDK's unlimited default), optional heartbeat deadline.
    activity_options: dict[str, Any] = {
        **_activity_options(step, ctx),
        "start_to_close_timeout": timedelta(seconds=timeout_seconds),
        "retry_policy": build_step_retry_policy(step.get("retry")),
    }
    heartbeat_timeout_seconds = step.get("heartbeat_timeout_seconds")
    if heartbeat_timeout_seconds:
        activity_options["heartbeat_timeout"] = timedelta(seconds=heartbeat_timeout_seconds)

    execute: Callable[..., Awaitable[Any]] = workflow.execute_activity
    return await execute(activity_name, *args, **activity_options)


_PARENT_CLOSE_POLICIES = {
    "abandon": workflow.ParentClosePolicy.ABANDON,
    "terminate": workflow.ParentClosePolicy.TERMINATE,
    "request_cancel": workflow.ParentClosePolicy.REQUEST_CANCEL,
}


def _check_workflow(step: dict[str, Any]) -> list[str]:
    problems = []
    if not isinstance(step.get("workflow_name"), str) or not step["workflow_name"]:
        problems.append("`workflow_name:` must name the child workflow")
    policy = step.get("parent_close_policy")
    if policy is not None and policy not in _PARENT_CLOSE_POLICIES:
        problems.append(f"unknown `parent_close_policy: {policy}` (known: {sorted(_PARENT_CLOSE_POLICIES)})")
    if "execution_timeout" in step and not _is_seconds(step["execution_timeout"]):
        problems.append("`execution_timeout:` must be seconds")
    return problems


async def workflow_step(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> Any:
    """Run the child workflow `workflow_name:` with `args:` resolved as its one argument.

    Options: `workflow_id` (default `{workflow_name}_{uuid4}`), `task_queue` (default the parent's),
    `wait_for_result` (default true; false starts it and gives `{"child_workflow_id": ...}`), `parent_close_policy`
    (`abandon`, `terminate`, `request_cancel`), `execution_timeout` (seconds)."""
    workflow_name = step.get("workflow_name")
    policy_str = step.get("parent_close_policy")
    parent_close_policy = _PARENT_CLOSE_POLICIES.get(policy_str) if isinstance(policy_str, str) else None
    if policy_str is not None and parent_close_policy is None:
        raise definition_error(f"Step '{ctx.base_id}': unknown parent_close_policy '{policy_str}'")

    workflow_args = ctx.resolve(step.get("args", {}))
    child_id = step.get("workflow_id") or f"{workflow_name}_{workflow.uuid4()}"
    task_queue = step.get("task_queue", workflow.info().task_queue)
    wait_for_result = step.get("wait_for_result", True)

    workflow.logger.info(f"Starting child workflow '{workflow_name}' as step '{ctx.base_id}'")

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


def _check_pause(step: dict[str, Any]) -> list[str]:
    if "timeout" in step and not _is_seconds(step["timeout"]):
        return ["`timeout:` must be seconds"]
    return []


async def pause_step(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> Any:
    """Wait for `resume_step` under the step's YAML id; `timeout:` (seconds) gives `{"action": "expired", ...}`."""
    return await wf.wait_for_resume(step, ctx.base_id)


async def for_each_step(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> list[Any]:
    """Run the inner `step:` once per element of `items:` (a list; None gives [], anything else is one item).

    Only types flagged `for_each` run inside (the validator rejects any other). `on_error: continue` on the for_each
    step gives None for a failed iteration. The result is the list of iteration results."""
    inner_step = step.get("step")
    on_error = step.get("on_error")
    inner_type = inner_step.get("type") if isinstance(inner_step, dict) else None
    inner = wf.step_types.get(inner_type) if isinstance(inner_type, str) else None
    if inner is None or not inner.for_each or not isinstance(inner_step, dict):
        raise definition_error(f"Step '{ctx.base_id}': for_each can't run an inner step of type '{inner_type}'")

    items = ctx.resolve(step.get("items"))
    if not isinstance(items, list):
        if items is None:
            workflow.logger.info(f"for_each step {ctx.base_id}: items resolved to None, skipping")
            return []
        items = [items]  # a single item is a list of one

    workflow.logger.info(f"for_each step {ctx.base_id}: iterating over {len(items)} items")

    results: list[Any] = []
    for i, loop_item in enumerate(items):
        try:
            results.append(await inner.run(wf, inner_step, ctx.iteration(i, loop_item)))
        except Exception as e:
            if on_error == "continue" and not is_definition_error(e):
                workflow.logger.warning(f"for_each {ctx.base_id}[{i}] failed: {e}, continuing")
                results.append(None)
            else:
                raise
    return results


BUILTIN_STEP_TYPES: Mapping[str, StepType] = MappingProxyType(
    {
        "activity": StepType(
            activity_step,
            for_each=True,
            required=frozenset({"activity"}),
            optional=frozenset({"args", "options", "timeout", "heartbeat_timeout_seconds", "retry"}),
            check=_check_activity,
        ),
        "workflow": StepType(
            workflow_step,
            for_each=True,
            required=frozenset({"workflow_name"}),
            optional=frozenset(
                {"args", "workflow_id", "task_queue", "wait_for_result", "parent_close_policy", "execution_timeout"}
            ),
            check=_check_workflow,
        ),
        "pause": StepType(pause_step, optional=frozenset({"timeout"}), check=_check_pause),
        "approval": StepType(pause_step, optional=frozenset({"timeout"}), check=_check_pause),
        # `step:` is checked by the validator as an inner step
        "for_each": StepType(for_each_step, required=frozenset({"items", "step"}), optional=frozenset()),
    }
)
