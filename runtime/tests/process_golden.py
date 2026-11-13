"""Golden runs of the process engine's built-in steps: the interpreters, stubs, uuid4 tap, normalization and replay
that tests/test_process_golden.py uses. The reference recordings, and where they come from: that test's docstring.

A golden case is one synthetic process file (fixtures/process_golden/processes/<name>.yaml) run by one interpreter
class in the time-skipping test server, with the stub activities and children below registered under the names the
files call. Each case has two recordings under fixtures/process_golden/recorded/:

- `<name>.<Interpreter>.history.json`: the full event history, for `Replayer`;
- `<name>.<Interpreter>.golden.json`: the normalized commands read from that history (every activity, child, timer,
  marker and the run's end, in order, with their options and decoded payloads), the number of `workflow.uuid4()`
  draws, and the answers to the queries, updates and signals the script sends.

UUIDs: `workflow.uuid4()` is seeded per run from the run id, so its values differ between fresh runs. A worker
interceptor notes the workflow's random state when the run starts and when it ends; the harness regenerates the
run's uuid4 sequence from it and writes each uuid as `<uuid4#k>`, its draw number in the run (k counts every draw,
used or not). A uuid not drawn by the workflow becomes `<uuid?n>`, numbered by first appearance. So a golden pins
which draw named which child, and how many draws the run made.

A process file has these keys:

    scope: generic                               # built-in step types only (the only scope kept here)
    description: what the case pins
    interpreters: [LinearProcessWorkflow, ...]   # one case per interpreter
    process: {...}                               # the process definition (the workflow's first argument)
    input: {...}                                 # second argument
    metadata: {...}                              # third argument; omit to start with two arguments
    start_signal: {step_id, data}                # optional: signal-with-start `resume_step`
    script: [...]                                # actions, in order (below)
    outcome: completed | failed | task_failed    # default completed

Script actions: `probe_waiting: [step ids]` (wait until those pause steps are waiting; records nothing),
`wait_completed: step_id` (poll the query until the step is in `completed_steps`; records nothing),
`release: gate`, `query: label`, `update: {step_id, data}`, `signal: {step_id, data}`, `skip_seconds: n`.
After the script the case awaits the result (time skips), then records a final query.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType, ParentClosePolicy, WorkflowIdReusePolicy
from temporalio.client import WorkflowFailureError, WorkflowHistory, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    ExecuteWorkflowInput,
    Interceptor,
    Replayer,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from factory_runtime.process import BUILTIN_STEP_TYPES, ProcessWorkflowBase
from factory_runtime.temporal.converter import data_converter
from factory_runtime.temporal.sandbox import STRICT_IMPORT_POLICY, build_workflow_runner
from factory_runtime.testing import TEST_PASSTHROUGH_MODULES, make_worker, query_eventually

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "process_golden"
PROCESS_DIR = GOLDEN_DIR / "processes"
RECORDED_DIR = GOLDEN_DIR / "recorded"

TASK_QUEUE = "process-engine-golden"
ALT_TASK_QUEUE = "process-engine-golden-alt"  # a `task_queue:` a workflow step names explicitly
PROBE_STEP = "__golden_probe__"
POLL_SECONDS = 0.05
WAIT_LIMIT_SECONDS = 30
GATE_ACTIVITY = "golden_gate_activity"


# ---------------------------------------------------------------------------
# The interpreters, declared as a product declares them, with the built-in step types only
# ---------------------------------------------------------------------------


@workflow.defn(name="LinearProcessWorkflow")
class LinearProcessWorkflow(ProcessWorkflowBase):
    step_types = BUILTIN_STEP_TYPES

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
    step_types = BUILTIN_STEP_TYPES

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
    step_types = BUILTIN_STEP_TYPES

    @workflow.run
    async def run(
        self,
        process_def: Dict[str, Any],
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self.run_parallel(process_def, input_data, metadata)


@workflow.defn(name="HybridProcessWorkflow")
class HybridProcessWorkflow(ProcessWorkflowBase):
    step_types = BUILTIN_STEP_TYPES

    @workflow.run
    async def run(
        self,
        process_def: Dict[str, Any],
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self.run_conditional(process_def, input_data, metadata)


INTERPRETERS: dict[str, type] = {
    cls.__name__: cls
    for cls in (LinearProcessWorkflow, ConditionalProcessWorkflow, ParallelProcessWorkflow, HybridProcessWorkflow)
}


# ---------------------------------------------------------------------------
# Stub children (deterministic: a result depends only on the input)
# ---------------------------------------------------------------------------


@workflow.defn(name="GoldenChildWorkflow")
class GoldenChildWorkflow:
    """Echoes its input. With a dict input carrying `gate`, first waits on that gate (an activity the script
    releases), so a Parallel run decides when the child finishes."""

    @workflow.run
    async def run(self, arg: Any = None) -> dict:
        if isinstance(arg, dict) and arg.get("gate"):
            await workflow.execute_activity(
                GATE_ACTIVITY, {"gate": arg["gate"]}, start_to_close_timeout=timedelta(hours=1)
            )
        return {"child": "GoldenChildWorkflow", "received": arg}


@workflow.defn(name="GoldenFailingChildWorkflow")
class GoldenFailingChildWorkflow:
    @workflow.run
    async def run(self, arg: Any = None) -> dict:
        raise ApplicationError("golden child failed", type="GoldenChildError", non_retryable=True)


@workflow.defn(name="GoldenSlowChildWorkflow")
class GoldenSlowChildWorkflow:
    """Sleeps an hour: outlives a short `execution_timeout`."""

    @workflow.run
    async def run(self, arg: Any = None) -> dict:
        await workflow.sleep(timedelta(hours=1))
        return {"child": "GoldenSlowChildWorkflow", "received": arg}


STUB_WORKFLOWS = [GoldenChildWorkflow, GoldenFailingChildWorkflow, GoldenSlowChildWorkflow]


# ---------------------------------------------------------------------------
# Stub activities
# ---------------------------------------------------------------------------

_GATES: dict[str, asyncio.Event] = {}


def _gate(name: str) -> asyncio.Event:
    return _GATES.setdefault(name, asyncio.Event())


@activity.defn(name="golden_echo_activity")
async def golden_echo_activity(*args: Any) -> dict:
    return {"activity": "golden_echo_activity", "args": list(args)}


@activity.defn(name="golden_value_activity")
async def golden_value_activity(*args: Any) -> Any:
    return args[0] if args else None


@activity.defn(name="golden_fail_activity")
async def golden_fail_activity(*args: Any) -> dict:
    raise ApplicationError("golden activity failed", type="GoldenFatalError")


@activity.defn(name="golden_maybe_fail_activity")
async def golden_maybe_fail_activity(item: Any = None) -> dict:
    if isinstance(item, dict) and item.get("fail"):
        raise ApplicationError(f"golden item failed: {item.get('name')}", type="GoldenItemError")
    return {"ok": item}


@activity.defn(name=GATE_ACTIVITY)
async def golden_gate_activity(params: dict) -> dict:
    await _gate(params["gate"]).wait()
    return {"gate": params["gate"], "params": params}


STUB_ACTIVITIES = [
    golden_echo_activity,
    golden_value_activity,
    golden_fail_activity,
    golden_maybe_fail_activity,
    golden_gate_activity,
]


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def load_process_files() -> dict[str, dict]:
    return {path.stem: yaml.safe_load(path.read_text()) for path in sorted(PROCESS_DIR.glob("*.yaml"))}


def golden_cases() -> list[tuple[str, str]]:
    return [(name, interpreter) for name, spec in load_process_files().items() for interpreter in spec["interpreters"]]


def case_id(name: str, interpreter: str) -> str:
    return f"{name}.{interpreter}"


def history_path(name: str, interpreter: str) -> Path:
    return RECORDED_DIR / f"{case_id(name, interpreter)}.history.json"


def golden_path(name: str, interpreter: str) -> Path:
    return RECORDED_DIR / f"{case_id(name, interpreter)}.golden.json"


def workflow_id_for(name: str, interpreter: str) -> str:
    return f"golden-{name}-{interpreter.removesuffix('ProcessWorkflow').lower()}"


# ---------------------------------------------------------------------------
# uuid4 tap: the workflow's random state at the start and end of each run
# ---------------------------------------------------------------------------

_RANDOM_STATES: dict[str, dict[str, Any]] = {}


class _RandomTapInbound(WorkflowInboundInterceptor):
    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        workflow_id = workflow.info().workflow_id
        _RANDOM_STATES[workflow_id] = {"start": workflow.random().getstate(), "end": None}
        try:
            return await super().execute_workflow(input)
        finally:
            try:
                _RANDOM_STATES[workflow_id]["end"] = workflow.random().getstate()
            except Exception:  # torn down outside the workflow context (eviction)
                pass


class RandomTap(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[type[WorkflowInboundInterceptor]]:
        return _RandomTapInbound


def _uuid_sequence(states: dict[str, Any]) -> tuple[list[str], int]:
    """The run's uuid4 draws (as workflow.uuid4 makes them) and how many it made (-1: the end state is not reached by
    uuid4 draws alone)."""
    rng = random.Random()
    rng.setstate(states["start"])
    end = states["end"]
    drawn: list[str] = []
    draws = -1
    for i in range(10_000):
        if end is not None and rng.getstate() == end:
            draws = i
            break
        drawn.append(str(uuid.UUID(bytes=rng.getrandbits(128).to_bytes(16, "big"), version=4)))
    return drawn, draws


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _replace_uuids(value: Any, sequence: list[str], unknown: dict[str, str]) -> Any:
    if isinstance(value, str):

        def name(match: re.Match[str]) -> str:
            text = match.group(0)
            if text in sequence:
                return f"<uuid4#{sequence.index(text) + 1}>"
            return unknown.setdefault(text, f"<uuid?{len(unknown) + 1}>")

        return _UUID_RE.sub(name, value)
    if isinstance(value, dict):
        return {_replace_uuids(k, sequence, unknown): _replace_uuids(v, sequence, unknown) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_uuids(v, sequence, unknown) for v in value]
    return value


# ---------------------------------------------------------------------------
# History -> normalized commands
# ---------------------------------------------------------------------------


def _seconds(message: Any, field: str) -> Optional[float]:
    return getattr(message, field).ToTimedelta().total_seconds() if message.HasField(field) else None


def _retry(message: Any) -> Optional[dict]:
    if not message.HasField("retry_policy"):
        return None
    policy = message.retry_policy
    return {
        "initial_interval": _seconds(policy, "initial_interval"),
        "backoff_coefficient": policy.backoff_coefficient,
        "maximum_interval": _seconds(policy, "maximum_interval"),
        "maximum_attempts": policy.maximum_attempts,
        "non_retryable_error_types": list(policy.non_retryable_error_types),
    }


def _failure(failure: Any) -> Optional[dict]:
    if failure is None or not failure.ByteSize():
        return None
    info = failure.WhichOneof("failure_info")
    out: dict[str, Any] = {"message": failure.message, "info": info}
    if info == "application_failure_info":
        out["type"] = failure.application_failure_info.type
        out["non_retryable"] = failure.application_failure_info.non_retryable
    if failure.HasField("cause"):
        out["cause"] = _failure(failure.cause)
    return out


class _Decoder:
    def __init__(self, converter: Any) -> None:
        self._converter = converter

    async def values(self, payloads: Any) -> list:
        items = list(payloads.payloads)
        if not items:
            return []
        return list(await self._converter.decode(items))

    async def value(self, payload: Any) -> Any:
        if not payload.ByteSize():
            return None
        return (await self._converter.decode([payload]))[0]


async def normalize_history(history: WorkflowHistory, converter: Any) -> list[dict]:
    """Every command the workflow produced, in history order, with its options and decoded payloads."""
    decode = _Decoder(converter)
    commands: list[dict] = []
    task_failure_seen = False
    for event in history.events:
        kind = event.event_type
        summary = await decode.value(event.user_metadata.summary) if event.HasField("user_metadata") else None
        if kind == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            a = event.activity_task_scheduled_event_attributes
            commands.append(
                {
                    "command": "schedule_activity",
                    "activity_type": a.activity_type.name,
                    "activity_id": a.activity_id,
                    "task_queue": a.task_queue.name,
                    "input": await decode.values(a.input),
                    "schedule_to_close_timeout": _seconds(a, "schedule_to_close_timeout"),
                    "schedule_to_start_timeout": _seconds(a, "schedule_to_start_timeout"),
                    "start_to_close_timeout": _seconds(a, "start_to_close_timeout"),
                    "heartbeat_timeout": _seconds(a, "heartbeat_timeout"),
                    "retry_policy": _retry(a),
                    "summary": summary,
                }
            )
        elif kind == EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED:
            c = event.start_child_workflow_execution_initiated_event_attributes
            commands.append(
                {
                    "command": "start_child_workflow",
                    "workflow_type": c.workflow_type.name,
                    "workflow_id": c.workflow_id,
                    "task_queue": c.task_queue.name,
                    "input": await decode.values(c.input),
                    "parent_close_policy": ParentClosePolicy.Name(c.parent_close_policy),
                    "workflow_execution_timeout": _seconds(c, "workflow_execution_timeout"),
                    "workflow_run_timeout": _seconds(c, "workflow_run_timeout"),
                    "workflow_task_timeout": _seconds(c, "workflow_task_timeout"),
                    "workflow_id_reuse_policy": WorkflowIdReusePolicy.Name(c.workflow_id_reuse_policy),
                    "retry_policy": _retry(c),
                    "cron_schedule": c.cron_schedule,
                    "memo": sorted(c.memo.fields.keys()),
                    "search_attributes": sorted(c.search_attributes.indexed_fields.keys()),
                    "summary": summary,
                }
            )
        elif kind == EventType.EVENT_TYPE_TIMER_STARTED:
            t = event.timer_started_event_attributes
            commands.append(
                {
                    "command": "start_timer",
                    "timer_id": t.timer_id,
                    "start_to_fire_timeout": _seconds(t, "start_to_fire_timeout"),
                    "summary": summary,
                }
            )
        elif kind == EventType.EVENT_TYPE_TIMER_CANCELED:
            commands.append({"command": "cancel_timer", "timer_id": event.timer_canceled_event_attributes.timer_id})
        elif kind == EventType.EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED:
            commands.append({"command": "request_cancel_activity"})
        elif kind == EventType.EVENT_TYPE_MARKER_RECORDED:
            m = event.marker_recorded_event_attributes
            commands.append(
                {
                    "command": "record_marker",
                    "marker_name": m.marker_name,
                    "details": {k: await decode.values(v) for k, v in sorted(m.details.items())},
                }
            )
        elif kind in (
            EventType.EVENT_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED,
            EventType.EVENT_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED,
            EventType.EVENT_TYPE_UPSERT_WORKFLOW_SEARCH_ATTRIBUTES,
            EventType.EVENT_TYPE_WORKFLOW_PROPERTIES_MODIFIED,
            EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED,
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW,
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED,
        ):
            commands.append({"command": EventType.Name(kind)})
        elif kind == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED:
            result = event.workflow_execution_completed_event_attributes.result
            commands.append({"command": "complete_workflow", "result": await decode.values(result)})
        elif kind == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED:
            failure = event.workflow_execution_failed_event_attributes.failure
            commands.append({"command": "fail_workflow", "failure": _failure(failure)})
        elif kind == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED and not task_failure_seen:
            # A workflow task that raised a non-Temporal error: the SDK retries it forever; the first is kept
            task_failure_seen = True
            f = event.workflow_task_failed_event_attributes.failure
            commands.append(
                {"command": "workflow_task_failed", "failure": {"message": f.message, "info": f.WhichOneof("failure_info")}}
            )
        elif kind == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED:
            commands.append({"command": "terminated_by_harness"})
    return commands


# ---------------------------------------------------------------------------
# Running and replaying a case
# ---------------------------------------------------------------------------


def sandbox_runner() -> SandboxedWorkflowRunner:
    return build_workflow_runner(TEST_PASSTHROUGH_MODULES, import_policy=STRICT_IMPORT_POLICY)


async def _until(check: Any, what: str) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_LIMIT_SECONDS
    while True:
        value = await check()
        if value:
            return value
        if loop.time() > deadline:
            raise TimeoutError(f"golden run: gave up waiting for {what}")
        await asyncio.sleep(POLL_SECONDS)


async def _probe_waiting(handle: Any, step_ids: list[str]) -> None:
    async def all_waiting() -> bool:
        try:
            await handle.execute_update("resume_step", args=[PROBE_STEP, None])
        except WorkflowUpdateFailedError as e:
            message = getattr(e.cause, "message", str(e.cause))
            return all(f"'{sid}'" in message.split("waiting steps:", 1)[-1] for sid in step_ids)
        raise AssertionError("the probe step was resumed")

    await _until(all_waiting, f"steps {step_ids} to wait")


async def _wait_completed(handle: Any, step_id: str) -> None:
    await query_eventually(
        handle,
        "get_step_results",
        lambda answer: step_id in answer["completed_steps"],
        timeout=WAIT_LIMIT_SECONDS,
        interval=POLL_SECONDS,
    )


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


async def run_case(env: WorkflowEnvironment, name: str, spec: dict, interpreter: str) -> dict:
    """Run one case on the current code; return its normalized golden."""
    _GATES.clear()
    workflow_id = workflow_id_for(name, interpreter)
    args = [spec.get("process"), spec.get("input")]
    if "metadata" in spec:
        args.append(spec["metadata"])
    start_options: dict[str, Any] = {}
    if spec.get("start_signal"):
        signal = spec["start_signal"]
        start_options = {"start_signal": "resume_step", "start_signal_args": [signal["step_id"], signal.get("data")]}
    answers: list[dict] = []
    client = env.client
    common: dict[str, Any] = {"workflows": [*INTERPRETERS.values(), *STUB_WORKFLOWS], "activities": STUB_ACTIVITIES}
    async with (
        make_worker(client, task_queue=TASK_QUEUE, interceptors=[RandomTap()], **common),
        make_worker(client, task_queue=ALT_TASK_QUEUE, **common),
    ):
        handle = await client.start_workflow(
            interpreter, args=args, id=workflow_id, task_queue=TASK_QUEUE, **start_options
        )
        for action in spec.get("script") or []:
            (verb, arg), *_ = action.items()
            if verb == "probe_waiting":
                await _probe_waiting(handle, list(arg))
            elif verb == "wait_completed":
                await _wait_completed(handle, arg)
            elif verb == "release":
                _gate(arg).set()
            elif verb == "skip_seconds":
                await env.sleep(timedelta(seconds=arg))
            elif verb == "query":
                answers.append({"query": arg, "answer": await handle.query("get_step_results")})
            elif verb == "update":
                try:
                    result = await handle.execute_update("resume_step", args=[arg["step_id"], arg.get("data")])
                    answers.append({"update": arg["step_id"], "answer": result})
                except WorkflowUpdateFailedError as e:
                    answers.append(
                        {
                            "update": arg["step_id"],
                            "rejected": {
                                "type": getattr(e.cause, "type", None),
                                "message": getattr(e.cause, "message", str(e.cause)),
                            },
                        }
                    )
            elif verb == "signal":
                await handle.signal("resume_step", args=[arg["step_id"], arg.get("data")])
                answers.append({"signal": arg["step_id"]})
            else:
                raise ValueError(f"unknown golden script action {verb!r}")

        outcome = spec.get("outcome", "completed")
        if outcome == "task_failed":

            async def task_failed() -> bool:
                history = await handle.fetch_history()
                return any(e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED for e in history.events)

            await _until(task_failed, "a workflow task failure")
            await handle.terminate(reason="golden run: workflow task failure recorded")
        else:
            try:
                await asyncio.wait_for(handle.result(), WAIT_LIMIT_SECONDS)
                ended = "completed"
            except WorkflowFailureError:
                ended = "failed"
            assert ended == outcome, f"{name} under {interpreter}: run {ended}, expected {outcome}"
            answers.append({"query": "final", "answer": await handle.query("get_step_results")})

        history = await handle.fetch_history()
        commands = await normalize_history(history, client.data_converter)

    sequence, draws = _uuid_sequence(_RANDOM_STATES[workflow_id])
    assert draws >= 0, f"{name} under {interpreter}: the run's final random state was not captured"
    unknown: dict[str, str] = {}
    return {
        "case": case_id(name, interpreter),
        "workflow_id": workflow_id,
        "uuid4_draws": draws,
        "commands": _replace_uuids(_jsonable(commands), sequence, unknown),
        "answers": _replace_uuids(_jsonable(answers), sequence, unknown),
    }


async def replay_case(name: str, interpreter: str, history_json: str) -> Any:
    """Replay a saved history against the current interpreter classes; return the replay result."""
    replayer = Replayer(
        workflows=list(INTERPRETERS.values()), workflow_runner=sandbox_runner(), data_converter=data_converter()
    )
    history = WorkflowHistory.from_json(workflow_id_for(name, interpreter), history_json)
    return await replayer.replay_workflow(history, raise_on_replay_failure=False)


def dump_golden(golden: dict) -> str:
    return json.dumps(golden, indent=2, sort_keys=False) + "\n"
