"""Process workflows as a product declares them: the four interpreters on `ProcessWorkflowBase`, with one extra
step type plugged in. `fetch_once` stands in for a product's decision step: it fetches per-run defaults through an
activity at most once per run (in `wf.step_state`), then starts a child whose id is built from the step's unique id,
so it runs inside `for_each` with `{base}_{uuid}[i]` ids."""

from datetime import timedelta
from typing import Any, Dict, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from factory_runtime.process import (
    BUILTIN_STEP_TYPES,
    DEFAULT_STEP_MAX_ATTEMPTS,
    ProcessWorkflowBase,
    StepContext,
    StepType,
)

DEFAULTS_ACTIVITY = "fetch_defaults_activity"
RECORD_CHILD = "RecordingChild"


async def fetch_once_step(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> Any:
    if wf.step_state.get("defaults") is None:
        wf.step_state["defaults"] = await workflow.execute_activity(
            DEFAULTS_ACTIVITY,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=DEFAULT_STEP_MAX_ATTEMPTS),
            result_type=dict,
        )
    defaults = wf.step_state["defaults"]
    department = ((ctx.process_def or {}).get("metadata") or {}).get("department")
    payload = {
        "label": ctx.resolve(step.get("label", defaults.get("label"))),
        "department": department,
        "item": ctx.item,
        "base_id": ctx.base_id,
        "run_metadata": ctx.metadata,
    }
    child_id = f"{ctx.unique_id}-{workflow.info().workflow_id[-8:]}"
    result = await workflow.execute_child_workflow(
        RECORD_CHILD,
        payload,
        id=child_id,
        task_queue=workflow.info().task_queue,
        parent_close_policy=workflow.ParentClosePolicy.REQUEST_CANCEL,
    )
    return {"child_id": child_id, **result}


STEP_TYPES = {**BUILTIN_STEP_TYPES, "fetch_once": StepType(fetch_once_step, for_each=True)}


@workflow.defn(name="LinearProcessWorkflow")
class LinearProcessWorkflow(ProcessWorkflowBase):
    step_types = STEP_TYPES

    @workflow.run
    async def run(
        self,
        process_def: Dict[str, Any],
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self.run_linear(process_def, input_data, metadata)


@workflow.defn(name="ConditionalProcessWorkflow")
class ConditionalProcessWorkflow(ProcessWorkflowBase):
    step_types = STEP_TYPES

    @workflow.run
    async def run(
        self,
        process_def: Dict[str, Any],
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self.run_conditional(process_def, input_data, metadata)


@workflow.defn(name="HybridProcessWorkflow")
class HybridProcessWorkflow(ProcessWorkflowBase):
    step_types = STEP_TYPES

    @workflow.run
    async def run(
        self,
        process_def: Dict[str, Any],
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self.run_conditional(process_def, input_data, metadata)


@workflow.defn(name="ParallelProcessWorkflow")
class ParallelProcessWorkflow(ProcessWorkflowBase):
    step_types = STEP_TYPES

    @workflow.run
    async def run(
        self,
        process_def: Dict[str, Any],
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self.run_parallel(process_def, input_data, metadata)


@workflow.defn(name=RECORD_CHILD)
class RecordingChild:
    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"seen": payload}


@workflow.defn(name="EchoChild")
class EchoChild:
    @workflow.run
    async def run(self, args: Any = None) -> Dict[str, Any]:
        return {"child_echo": args}


@workflow.defn(name="SlowChild")
class SlowChild:
    @workflow.run
    async def run(self, args: Any = None) -> Dict[str, Any]:
        await workflow.sleep(timedelta(hours=1))
        return {"slow": args}


PROCESS_WORKFLOWS = [
    LinearProcessWorkflow,
    ConditionalProcessWorkflow,
    HybridProcessWorkflow,
    ParallelProcessWorkflow,
    RecordingChild,
    EchoChild,
    SlowChild,
]
