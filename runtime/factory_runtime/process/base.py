"""The process interpreter: runs a process file's steps inside a Temporal workflow.

`ProcessWorkflowBase` is not a workflow itself. A product declares its workflow classes on it, with its own step
types, and calls one of the run loops from `@workflow.run` (the SDK inherits the handlers, not the run method):

    @workflow.defn(name="LinearProcessWorkflow")
    class LinearProcessWorkflow(ProcessWorkflowBase):
        step_types = STEP_TYPES
        @workflow.run
        async def run(self, process_def: dict, input_data: dict | None = None, metadata: dict | None = None) -> dict:
            return await self.run_linear(process_def, input_data, metadata)

Run loops: `run_linear` (steps in order; `run_conditional` is the same loop under its old name) and `run_parallel`
(all steps at once; a step can't read another's result). In both, a step whose `condition:` is false is skipped and
absent from the results. Each returns the results by step id once every in-flight handler has finished.

A run first checks its definition (validation.py, with this class's step types): one that can't run, a None
definition included, fails the run at once with a non-retryable `ProcessDefinitionError` naming every problem (also
when the check itself crashes, e.g. on a product step type's `check`). So does a condition that can't be evaluated
(bad syntax, a dict or list compared; a comparison its values can't support is false instead), and a step that
finds its definition wrong while running; `on_error:
continue` does not apply to these. Any other error a step type raises that isn't a Temporal failure becomes a
non-retryable `ApplicationError` (type `ProcessStepError`), so a bug in a step fails the run instead of retrying its
workflow task forever; `on_error: continue` does apply to it.

Handlers: query `get_step_results`; update `resume_step(step_id, data)` (validated: only a step waiting now) and a
signal of the same name (unvalidated: data sent before the step waits is kept until it is reached; data for a pause
step that has already finished, resumed or expired, is dropped and logged).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Awaitable, Callable, ClassVar, Dict, Mapping, Optional, TypeVar

from temporalio import workflow
from temporalio.exceptions import ApplicationError, TemporalError

from factory_runtime.process.errors import definition_error, is_definition_error
from factory_runtime.process.steps import BUILTIN_STEP_TYPES, StepContext, StepType
from factory_runtime.process.templates import ConditionError, evaluate_condition
from factory_runtime.process.validation import process_problems

T = TypeVar("T")

STEP_ERROR = "ProcessStepError"


class ProcessWorkflowBase:
    """Handlers, step dispatch and run loops for process workflows (not a `@workflow.defn`)."""

    step_types: ClassVar[Mapping[str, StepType]] = BUILTIN_STEP_TYPES

    def __init__(self) -> None:
        self._pause_results: Dict[str, Any] = {}
        self._running_steps: list[str] = []  # top-level steps running now, in the order they started
        self._workflow_results: Dict[str, Any] = {}
        # Pause steps blocked in wait_for_resume; the resume_step validator admits only these.
        self._waiting_pause_steps: set[str] = set()
        # Pause steps that have finished (resumed or expired): a late signal for one is dropped.
        self._finished_pause_steps: set[str] = set()
        # Per-run state of step types (e.g. defaults fetched once per run). Never on a function, class or module.
        self.step_state: Dict[str, Any] = {}
        self._process_def: Dict[str, Any] = {}
        self._metadata: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Run loops
    # ------------------------------------------------------------------

    async def run_linear(
        self,
        process_def: Optional[Dict[str, Any]],
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Every step in order; a step whose `condition:` is false is skipped (absent from the results)."""
        return await self._run(process_def, input_data, metadata, parallel=False)

    async def run_conditional(
        self,
        process_def: Optional[Dict[str, Any]],
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """`run_linear` under the name the Conditional interpreter has always called (every loop honours
        `condition:` since v0.8)."""
        return await self._run(process_def, input_data, metadata, parallel=False)

    async def run_parallel(
        self,
        process_def: Optional[Dict[str, Any]],
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """All steps at once (each skipped when its `condition:` is false); no step reads another's result."""
        return await self._run(process_def, input_data, metadata, parallel=True)

    async def _run(
        self,
        process_def: Optional[Dict[str, Any]],
        input_data: Optional[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]],
        *,
        parallel: bool,
    ) -> Dict[str, Any]:
        input_data = input_data or {}
        self._metadata = metadata or {}
        workflow.logger.info(f"Starting {workflow.info().workflow_type} with metadata: {self._metadata}")

        try:
            problems = process_problems(process_def, self.step_types, parallel=parallel)
        except Exception as e:  # a malformed value the checks didn't foresee: fail the run, not the task (for ever)
            raise definition_error(
                f"Invalid process definition: checking it failed: {type(e).__name__}: {e}"
            ) from None
        if problems:
            raise definition_error("Invalid process definition: " + "; ".join(problems))
        assert process_def is not None  # checked above
        self._process_def = process_def
        steps: list[Dict[str, Any]] = process_def["steps"]

        if parallel:
            await asyncio.gather(*(self._run_step(step, input_data) for step in steps))
        else:
            for step in steps:
                await self._run_step(step, input_data)

        # Let in-flight resume_step updates/signals settle before closing.
        await workflow.wait_condition(workflow.all_handlers_finished)
        workflow.logger.info(f"{workflow.info().workflow_type} completed successfully")
        return self._workflow_results

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    async def _run_step(self, step: Dict[str, Any], input_data: Dict[str, Any]) -> None:
        """Run one top-level step unless its condition is false."""
        step_id = step["id"]
        condition = step.get("condition")
        if condition:
            try:
                passed = evaluate_condition(condition, self._workflow_results, input_data)
            except ConditionError as e:
                raise definition_error(f"Step '{step_id}': {e}") from None
            if not passed:
                workflow.logger.info(f"Skipping step {step_id}: condition is false")
                return
        await self._execute_step(step, input_data)

    async def _execute_step(self, step: Dict[str, Any], input_data: Dict[str, Any]) -> Any:
        """Run one step by its type and store its result under its id. With `on_error: continue` a failure stores
        None; a definition error always fails the run."""
        step_id = step["id"]
        step_type = step.get("type")
        workflow.logger.info(f"Executing step: {step_id} of type {step_type}")
        ctx = StepContext(
            base_id=step_id,
            results=self._workflow_results,
            input=input_data,
            process_def=self._process_def,
            metadata=self._metadata,
        )
        self._running_steps.append(step_id)
        try:
            known = self.step_types.get(step_type) if isinstance(step_type, str) else None
            if known is None:  # validated before the run: only a class whose map changed mid-run gets here
                raise definition_error(f"Step '{step_id}': unknown type '{step_type}'")
            try:
                result = await known.run(self, step, ctx)
            except TemporalError:
                raise
            except Exception as e:  # a bug in a step type: fail the run, not the workflow task (for ever)
                raise ApplicationError(
                    f"Step '{step_id}' failed: {type(e).__name__}: {e}", type=STEP_ERROR, non_retryable=True
                ) from e
        except Exception as e:
            if step.get("on_error") == "continue" and not is_definition_error(e):
                workflow.logger.warning(f"Step {step_id} failed with {e}, continuing (on_error=continue)")
                result = None
            else:
                raise
        finally:
            self._running_steps.remove(step_id)
        self._workflow_results[step_id] = result
        return result

    async def wait_for_resume(self, step: Dict[str, Any], step_id: str) -> Any:
        """Block until `resume_step` delivers data for `step_id` (the pause step's YAML id), or `timeout:` seconds
        pass (then `{"action": "expired", "timeout_seconds": ...}`). While blocked, the id is in
        `_waiting_pause_steps`, so the update validator admits resumes for it only."""
        workflow.logger.info(f"Waiting for resume_step for step {step_id}")
        timeout_seconds = step.get("timeout")
        self._waiting_pause_steps.add(step_id)

        try:
            if timeout_seconds:
                try:
                    await workflow.wait_condition(
                        lambda: step_id in self._pause_results,
                        timeout=timedelta(seconds=timeout_seconds),
                    )
                    return self._pause_results.pop(step_id)
                except asyncio.TimeoutError:
                    # Drop any resume that raced the deadline: it is not consumed.
                    self._pause_results.pop(step_id, None)
                    return {"action": "expired", "timeout_seconds": timeout_seconds}
            else:
                await workflow.wait_condition(
                    lambda: step_id in self._pause_results,
                )
                return self._pause_results.pop(step_id)
        finally:
            self._waiting_pause_steps.discard(step_id)
            self._finished_pause_steps.add(step_id)

    async def once(self, key: str, fetch: Callable[[], Awaitable[T]]) -> T:
        """`fetch()`'s result, fetched at most once per run under `key` in `step_state`: steps asking at the same time
        (a parallel process) await the same call. A call that failed is forgotten, so the next step asks again."""
        task = self.step_state.get(key)
        if task is None:
            task = asyncio.ensure_future(fetch())
            self.step_state[key] = task
        try:
            # shield: one waiting step being cancelled doesn't cancel the call the others wait on
            result: T = await asyncio.shield(task)
            return result
        except BaseException:
            if task.done() and not task.cancelled() and task.exception() is not None:
                if self.step_state.get(key) is task:
                    del self.step_state[key]
            raise

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    @workflow.query
    def get_step_results(self) -> Dict[str, Any]:
        """Progress: `{current_step, completed_steps: [ids], results: {id: result}}`. `current_step` is the step
        running now (in a parallel process the earliest started of those still running), None when none is."""
        return {
            "current_step": self._running_steps[0] if self._running_steps else None,
            "completed_steps": list(self._workflow_results.keys()),
            "results": self._workflow_results,
        }

    @workflow.update
    async def resume_step(self, step_id: str, data: Any) -> Dict[str, Any]:
        """Resume a waiting pause step with `data`; returns `{step_id, status: resumed|expired, result,
        completed_steps}` once the step has consumed it (or given up)."""
        workflow.logger.info(f"Received resume_step update for step {step_id}: {data}")
        self._pause_results[step_id] = data
        await workflow.wait_condition(
            lambda: (
                step_id not in self._pause_results or step_id not in self._waiting_pause_steps
            )
        )
        return self._step_status(step_id)

    @resume_step.validator
    def validate_resume_step(self, step_id: str, data: Any) -> None:
        """Reject a resume for a step that is not waiting now (the client gets WorkflowUpdateFailedError)."""
        if step_id not in self._waiting_pause_steps:
            waiting = sorted(self._waiting_pause_steps)
            raise ValueError(f"Step '{step_id}' is not waiting to be resumed (waiting steps: {waiting})")
        if step_id in self._pause_results:
            raise ValueError(f"Step '{step_id}' already has a pending resume")

    @workflow.signal(name="resume_step")
    async def resume_step_signal(self, step_id: str, data: Any) -> None:
        """The update's alias for callers that cannot await a result; unvalidated, so data sent before the step
        waits is picked up when it is reached. Data for a pause step that has finished is dropped and logged."""
        if step_id in self._finished_pause_steps:
            workflow.logger.warning(f"Dropped resume_step signal for step {step_id}: the step has already finished")
            return
        workflow.logger.info(f"Received resume_step signal for step {step_id}: {data}")
        self._pause_results[step_id] = data

    def _step_status(self, step_id: str) -> Dict[str, Any]:
        if step_id in self._waiting_pause_steps:
            status = "waiting"
        else:
            result = self._workflow_results.get(step_id)
            expired = isinstance(result, dict) and result.get("action") == "expired"
            status = "expired" if expired else "resumed"
        return {
            "step_id": step_id,
            "status": status,
            "result": self._workflow_results.get(step_id),
            "completed_steps": list(self._workflow_results.keys()),
        }
