"""The process interpreter: runs a process file's steps inside a Temporal workflow.

`ProcessWorkflowBase` is not a workflow itself. A product declares its workflow classes on it, with its own step
types, and calls one of the run loops from `@workflow.run` (the SDK inherits the handlers, not the run method):

    @workflow.defn(name="LinearProcessWorkflow")
    class LinearProcessWorkflow(ProcessWorkflowBase):
        step_types = STEP_TYPES
        @workflow.run
        async def run(self, process_def: dict, input_data: dict | None = None, metadata: dict | None = None) -> dict:
            return await self.run_linear(process_def, input_data, metadata)

Run loops: `run_linear` (steps in order, `condition:` ignored), `run_conditional` (a step whose condition is false is
skipped and absent from the results), `run_parallel` (all steps at once, one shared results dict, `condition:`
ignored). Each returns the results by step id once every in-flight handler has finished.

Handlers: query `get_step_results`; update `resume_step(step_id, data)` (validated: only a step waiting now) and a
signal of the same name (unvalidated; data waits until the step is reached).

Long-running workflows replay their history against this code, so the commands each input produces (activities,
children, timers, `workflow.uuid4()` calls, their order and options) must not change without Worker Versioning.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, ClassVar, Dict, Mapping, Optional

from temporalio import workflow

from factory_runtime.process.steps import BUILTIN_STEP_TYPES, StepContext, StepType
from factory_runtime.process.templates import evaluate_condition


class ProcessWorkflowBase:
    """Handlers, step dispatch and run loops for process workflows (not a `@workflow.defn`)."""

    step_types: ClassVar[Mapping[str, StepType]] = BUILTIN_STEP_TYPES

    def __init__(self) -> None:
        self._pause_results: Dict[str, Any] = {}
        self._current_step_id: Optional[str] = None  # never set: the query always reports None (a known quirk)
        self._workflow_results: Dict[str, Any] = {}
        # Pause steps blocked in wait_for_resume; the resume_step validator admits only these.
        self._waiting_pause_steps: set[str] = set()
        # Per-run state of step types (e.g. defaults fetched once per run). Never on a function, class or module.
        self.step_state: Dict[str, Any] = {}
        self._process_def: Dict[str, Any] = {}
        self._metadata: Dict[str, Any] = {}

    def _begin_process(self, process_def: Optional[Dict[str, Any]]) -> None:
        """Note the run's definition for the step contexts (before any check of it, as always)."""
        self._process_def = process_def or {}

    # ------------------------------------------------------------------
    # Run loops
    # ------------------------------------------------------------------

    async def run_linear(
        self,
        process_def: Optional[Dict[str, Any]],
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Every step in order; `condition:` is ignored. A None definition raises ValueError."""
        input_data = input_data or {}
        self._begin_process(process_def)
        metadata = metadata or {}
        self._metadata = metadata

        workflow.logger.info(f"Starting {workflow.info().workflow_type} with metadata: {metadata}")

        if process_def is None:
            raise ValueError("Process definition is None")

        steps: list[Dict[str, Any]] = process_def.get("steps", [])
        workflow_results: Dict[str, Any] = {}
        self._workflow_results = workflow_results

        for step in steps:
            step_base_id = step.get("id")
            step_id = self._generate_step_id(step_base_id)
            await self._execute_step(step, step_base_id, step_id, workflow_results, input_data)

        # Let in-flight resume_step updates/signals settle before closing.
        await workflow.wait_condition(workflow.all_handlers_finished)
        workflow.logger.info(f"{workflow.info().workflow_type} completed successfully")
        return workflow_results

    async def run_conditional(
        self,
        process_def: Dict[str, Any],
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Steps in order; a step whose `condition:` is false is skipped (no uuid4, absent from the results)."""
        input_data = input_data or {}
        self._begin_process(process_def)
        metadata = metadata or {}
        self._metadata = metadata
        workflow_results: Dict[str, Any] = {}
        self._workflow_results = workflow_results

        for step in process_def.get("steps", []):
            condition = step.get("condition")
            if condition:
                if not evaluate_condition(condition, workflow_results, input_data):
                    continue

            step_base_id = step.get("id")
            step_id = self._generate_step_id(step_base_id)
            await self._execute_step(step, step_base_id, step_id, workflow_results, input_data)

        await workflow.wait_condition(workflow.all_handlers_finished)
        return workflow_results

    async def run_parallel(
        self,
        process_def: Dict[str, Any],
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """All steps at once, sharing one results dict; `condition:` is ignored."""
        input_data = input_data or {}
        self._begin_process(process_def)
        metadata = metadata or {}
        self._metadata = metadata
        workflow_results: Dict[str, Any] = {}
        self._workflow_results = workflow_results
        data = input_data

        async def execute_step(step: Dict[str, Any]) -> None:
            step_base_id = step.get("id")
            step_id = self._generate_step_id(step_base_id)
            await self._execute_step(step, step_base_id, step_id, workflow_results, data)

        await asyncio.gather(*(execute_step(step) for step in process_def.get("steps", [])))
        await workflow.wait_condition(workflow.all_handlers_finished)
        return workflow_results

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _generate_step_id(self, step_base_id: Any) -> str:
        """`{base}_{uuid4}`: one workflow.uuid4() per executed step (a known quirk: only for_each ids use it)."""
        return f"{step_base_id}_{workflow.uuid4()}"

    async def _execute_step(
        self,
        step: Dict[str, Any],
        step_base_id: Any,
        step_id: str,
        workflow_results: Dict[str, Any],
        input_data: Dict[str, Any],
    ) -> Any:
        """Run one step by its type and store its result under its base id. An unknown type logs a warning and
        stores None; with `on_error: continue` a failure stores None, otherwise the exception propagates as is."""
        step_type = step.get("type")
        on_error = step.get("on_error")
        workflow.logger.info(f"Executing step: {step_id} of type {step_type}")

        try:
            known = self.step_types.get(step_type) if isinstance(step_type, str) else None
            if known is not None:
                ctx = StepContext(
                    base_id=step_base_id,
                    unique_id=step_id,
                    results=workflow_results,
                    input=input_data,
                    process_def=self._process_def,
                    metadata=self._metadata,
                )
                result = await known.run(self, step, ctx)
                workflow_results[step_base_id] = result
                return result

            workflow.logger.warning(f"Unknown step type '{step_type}' for step {step_id}")
            workflow_results[step_base_id] = None
            return None
        except Exception as e:
            if on_error == "continue":
                workflow.logger.warning(f"Step {step_id} failed with {e}, continuing (on_error=continue)")
                workflow_results[step_base_id] = None
                return None
            raise

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

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    @workflow.query
    def get_step_results(self) -> Dict[str, Any]:
        """Progress: `{current_step: None, completed_steps: [ids], results: {id: result}}`."""
        return {
            "current_step": self._current_step_id,
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
        waits is picked up when it is reached."""
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
