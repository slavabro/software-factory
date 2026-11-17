"""The replay check: would the new worker keep every open run going? Run before every deploy and rollback.

`check_open_runs(client, definition)` reads every open run in the namespace (`ExecutionStatus = 'Running'`, no type
filter, so a run whose type was removed or renamed fails here instead of failing for ever after the deploy) and:

- for runs on the definition's task queues: the type must be registered; the history is streamed through the
  definition's `Replayer` (the commands each run already recorded must come out the same); every pending activity's
  type must be registered (or served elsewhere, `external_activity_names`); every signal and update the run has
  received must still have a handler on the new class (queries leave no trace in history, so the handlers a client
  queries are pinned by a product's own test);
- for runs on other queues: counted per queue and type, for information; a failure only when the definition
  registers that type by name (a run the worker would serve if it polled that queue);
- every schedule that starts a workflow on the definition's queues must start a registered type.

The report names each failure by kind, workflow id, type and run id with the error's message; it never holds payload
data. Replay does not check options, payloads or code past the end of a history: golden tests in CI cover those.

CLI: `python -m factory_runtime.temporal.replay_check MODULE:ATTR [--server HOST:PORT] [--namespace NS] [--tls]
[--json]`, where ATTR is a `WorkerDefinition` (or a function returning one). Connection details not given as flags
come from `definition.settings`, else `localhost:7233` / `default`; a Temporal Cloud API key comes from
`definition.settings.api_key` or `$TEMPORAL_API_KEY`. Exit 0: every open run passes; 1: runs would break (or the
definition did not load): reject the release; 2: couldn't check (Temporal or visibility unreachable): retry.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Sequence

from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import Client, ScheduleActionStartWorkflow, WorkflowExecution, WorkflowHistory
from temporalio.worker import WorkflowReplayResult
from temporalio.workflow import NondeterminismError

from factory_runtime.temporal.definition import WorkerDefinition

OPEN_RUNS_QUERY = "ExecutionStatus = 'Running'"

# Failure kinds.
NONDETERMINISM = "nondeterminism"  # replay took different steps than the run recorded
REPLAY_FAILURE = "replay_failure"  # replay raised (new code fails on the recorded history)
UNREGISTERED_TYPE = "unregistered_type"  # an open run's type is not registered on the new worker
MISSING_ACTIVITY = "missing_activity"  # a pending activity's type is not registered
MISSING_HANDLER = "missing_handler"  # a signal or update the run received has no handler on the new class
SCHEDULE_TYPE = "schedule_workflow_type"  # a schedule starts a type that is not registered
OTHER_QUEUE = "other_queue"  # a run of a registered type sits on a queue the worker doesn't serve

ERROR_CHARS = 500


@dataclass(frozen=True)
class RunFailure:
    """One reason the release would break a run. For `SCHEDULE_TYPE`, `workflow_id` is the schedule id and `run_id`
    is empty."""

    kind: str
    workflow_id: str
    workflow_type: str
    run_id: str
    error: str


@dataclass
class ReplayReport:
    namespace: str
    task_queues: tuple[str, ...]
    checked: dict[str, int] = field(default_factory=dict)  # open runs on the served queues, per type
    other_queues: dict[str, dict[str, int]] = field(default_factory=dict)  # queue -> type -> open runs
    schedules_checked: int = 0
    failures: list[RunFailure] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def nondeterminism(self) -> list[RunFailure]:
        return [f for f in self.failures if f.kind == NONDETERMINISM]

    @property
    def other_failures(self) -> list[RunFailure]:
        return [f for f in self.failures if f.kind != NONDETERMINISM]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "namespace": self.namespace,
            "task_queues": list(self.task_queues),
            "checked": dict(sorted(self.checked.items())),
            "other_queues": {q: dict(sorted(t.items())) for q, t in sorted(self.other_queues.items())},
            "schedules_checked": self.schedules_checked,
            "nondeterminism": [asdict(f) for f in self.nondeterminism],
            "failures": [asdict(f) for f in self.other_failures],
        }

    def render(self) -> str:
        """The report as text: a verdict word first, never colour alone."""
        verdict = "PASS" if self.passed else f"FAIL: {len(self.failures)} problem(s)"
        lines = [
            f"Replay check {verdict}",
            f"namespace {self.namespace}, task queues {', '.join(self.task_queues)}",
            f"open runs checked: {sum(self.checked.values())}, schedules checked: {self.schedules_checked}",
        ]
        lines += [f"  {t}: {n}" for t, n in sorted(self.checked.items())]
        if self.other_queues:
            lines.append("open runs on queues this worker doesn't serve:")
            for queue, types in sorted(self.other_queues.items()):
                lines += [f"  {queue} / {t}: {n}" for t, n in sorted(types.items())]
        for title, group in (("nondeterminism", self.nondeterminism), ("other failures", self.other_failures)):
            if group:
                lines.append(f"{title} ({len(group)}):")
                lines += [
                    f"  [{f.kind}] {f.workflow_type} {f.workflow_id} run {f.run_id or '-'}: {f.error}" for f in group
                ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------
# Shared with factory_runtime.temporal.patches
# ---------------------------------------------------------------------------------------------------------------


async def list_open_runs(client: Client) -> list[WorkflowExecution]:
    """Every open run in the client's namespace, of any type on any queue."""
    return [run async for run in client.list_workflows(OPEN_RUNS_QUERY)]


def failure_text(error: BaseException) -> str:
    """The message of a replay failure without stack traces: core wraps it in a `Failure { message: "…" }` dump."""
    text = str(error)
    match = re.search(r'message: "((?:[^"\\]|\\.)*)"', text)
    if match:
        text = re.sub(r"\\(.)", lambda m: {"n": "\n", "t": " "}.get(m.group(1), m.group(1)), match.group(1))
    return text.strip().splitlines()[0][:ERROR_CHARS] if text.strip() else type(error).__name__


def replay_failure(result: WorkflowReplayResult, workflow_type: str) -> RunFailure | None:
    error = result.replay_failure
    if error is None:
        return None
    kind = NONDETERMINISM if isinstance(error, NondeterminismError) else REPLAY_FAILURE
    return RunFailure(kind, result.history.workflow_id, workflow_type, result.history.run_id, failure_text(error))


async def fetch_histories(client: Client, runs: Sequence[WorkflowExecution]) -> AsyncIterator[WorkflowHistory]:
    for run in runs:
        yield await client.get_workflow_handle(run.id, run_id=run.run_id).fetch_history()


def served_runs(
    definition: WorkerDefinition, runs: Sequence[WorkflowExecution]
) -> tuple[list[WorkflowExecution], list[WorkflowExecution], list[WorkflowExecution]]:
    """Split open runs: (on a served queue and registered, on a served queue but unregistered, on other queues)."""
    registered, unregistered, elsewhere = [], [], []
    for run in runs:
        if run.task_queue not in definition.task_queues:
            elsewhere.append(run)
        elif definition.workflow_class(run.workflow_type) is None:
            unregistered.append(run)
        else:
            registered.append(run)
    return registered, unregistered, elsewhere


# ---------------------------------------------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------------------------------------------


def received_messages(events: Sequence[HistoryEvent]) -> tuple[set[str], set[str]]:
    """Names of the signals and updates a run's history shows it received."""
    signals: set[str] = set()
    updates: set[str] = set()
    for e in events:
        if e.HasField("workflow_execution_signaled_event_attributes"):
            signals.add(e.workflow_execution_signaled_event_attributes.signal_name)
        elif e.HasField("workflow_execution_update_accepted_event_attributes"):
            updates.add(e.workflow_execution_update_accepted_event_attributes.accepted_request.input.name)
        elif e.HasField("workflow_execution_update_admitted_event_attributes"):
            updates.add(e.workflow_execution_update_admitted_event_attributes.request.input.name)
    signals.discard("")
    updates.discard("")
    return signals, updates


def _handler_failures(
    definition: WorkerDefinition, run: WorkflowExecution, history: WorkflowHistory
) -> list[RunFailure]:
    cls = definition.workflow_class(run.workflow_type)
    assert cls is not None
    handlers = definition.handlers(cls)
    signals, updates = received_messages(history.events)
    failures = []
    if not handlers.dynamic_signal:
        for name in sorted(signals - handlers.signals):
            error = f"received signal {name!r}, which the new class doesn't handle"
            failures.append(_fail(MISSING_HANDLER, run, error))
    if not handlers.dynamic_update:
        for name in sorted(updates - handlers.updates):
            error = f"received update {name!r}, which the new class doesn't handle"
            failures.append(_fail(MISSING_HANDLER, run, error))
    return failures


async def _activity_failures(client: Client, definition: WorkerDefinition, run: WorkflowExecution) -> list[RunFailure]:
    description = await client.get_workflow_handle(run.id, run_id=run.run_id).describe()
    failures = []
    for pending in description.raw_description.pending_activities:
        name = pending.activity_type.name
        if not definition.serves_activity(name):
            failures.append(_fail(MISSING_ACTIVITY, run, f"pending activity {name!r} is not registered"))
    return failures


def _fail(kind: str, run: WorkflowExecution, error: str) -> RunFailure:
    return RunFailure(kind, run.id, run.workflow_type, run.run_id or "", error)


async def _schedule_failures(client: Client, definition: WorkerDefinition) -> tuple[int, list[RunFailure]]:
    checked = 0
    failures = []
    async for listed in await client.list_schedules():
        description = await client.get_schedule_handle(listed.id).describe()
        action = description.schedule.action
        if not isinstance(action, ScheduleActionStartWorkflow) or action.task_queue not in definition.task_queues:
            continue
        checked += 1
        if definition.workflow_class(action.workflow) is None:
            error = f"schedule starts workflow type {action.workflow!r}, which is not registered"
            failures.append(RunFailure(SCHEDULE_TYPE, listed.id, action.workflow, "", error))
    return checked, failures


async def check_open_runs(client: Client, definition: WorkerDefinition) -> ReplayReport:
    """Check every open run and schedule in the client's namespace against `definition` (module docstring). Raises
    when Temporal or visibility can't be read."""
    report = ReplayReport(namespace=client.namespace, task_queues=definition.task_queues)
    registered, unregistered, elsewhere = served_runs(definition, await list_open_runs(client))

    named_types = {name for name in definition.workflow_classes() if name is not None}
    other: dict[str, Counter[str]] = {}
    for run in elsewhere:
        other.setdefault(run.task_queue or "", Counter())[run.workflow_type] += 1
        if run.workflow_type in named_types:
            error = f"open on queue {run.task_queue!r}, which this worker doesn't serve, though it registers the type"
            report.failures.append(_fail(OTHER_QUEUE, run, error))
    report.other_queues = {q: dict(c) for q, c in other.items()}

    checked: Counter[str] = Counter()
    for run in unregistered:
        checked[run.workflow_type] += 1
        report.failures.append(_fail(UNREGISTERED_TYPE, run, "workflow type is not registered on the new worker"))

    by_run = {run.run_id: run for run in registered}

    async def histories() -> AsyncIterator[WorkflowHistory]:
        async for history in fetch_histories(client, registered):
            run = by_run[history.run_id]
            report.failures.extend(_handler_failures(definition, run, history))
            report.failures.extend(await _activity_failures(client, definition, run))
            yield history

    if registered:
        async with definition.replayer(namespace=client.namespace).workflow_replay_iterator(histories()) as results:
            async for result in results:
                run = by_run[result.history.run_id]
                checked[run.workflow_type] += 1
                if (failure := replay_failure(result, run.workflow_type)) is not None:
                    report.failures.append(failure)
    report.checked = dict(checked)

    report.schedules_checked, schedule_failures = await _schedule_failures(client, definition)
    report.failures.extend(schedule_failures)
    return report


# ---------------------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------------------


def load_definition(spec: str) -> WorkerDefinition:
    """`module:attr` (dots allowed in attr) naming a `WorkerDefinition` or a function returning one."""
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(f"expected MODULE:ATTR, got {spec!r}")
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    if callable(obj) and not isinstance(obj, WorkerDefinition):
        obj = obj()
    if not isinstance(obj, WorkerDefinition):
        raise TypeError(f"{spec} is a {type(obj).__name__}, not a WorkerDefinition")
    return obj


def cli_parser(prog: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("definition", help="MODULE:ATTR of the product's WorkerDefinition")
    parser.add_argument("--server", help="HOST:PORT of the Temporal frontend (default: the definition's settings)")
    parser.add_argument("--namespace", help="namespace (default: the definition's settings)")
    parser.add_argument("--tls", action="store_true", default=None, help="connect over TLS")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


async def connect(definition: WorkerDefinition, args: argparse.Namespace) -> Client:
    settings = definition.settings
    api_key = settings.api_key if settings is not None and settings.api_key else os.environ.get("TEMPORAL_API_KEY")
    tls = args.tls if args.tls is not None else bool(settings is not None and settings.tls) or api_key is not None
    return await Client.connect(
        args.server or (settings.server_url if settings else "localhost:7233"),
        namespace=args.namespace or (settings.namespace if settings else "default"),
        data_converter=definition.data_converter,
        tls=tls,
        api_key=api_key,
    )


def load_or_exit(spec: str) -> WorkerDefinition | int:
    try:
        return load_definition(spec)
    except Exception as e:  # the release's own code: a definition that doesn't load is a broken release
        print(f"The worker definition {spec} did not load: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    args = cli_parser(
        "python -m factory_runtime.temporal.replay_check", "Check that every open run replays on a new worker."
    ).parse_args(argv)
    definition = load_or_exit(args.definition)
    if isinstance(definition, int):
        return definition

    async def run() -> ReplayReport:
        return await check_open_runs(await connect(definition, args), definition)

    try:
        report = asyncio.run(run())
    except Exception as e:
        print(f"Replay check COULD NOT CHECK: {type(e).__name__}: {failure_text(e)}", file=sys.stderr)
        return 2
    print(json.dumps(report.as_dict(), indent=2) if args.json else report.render())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
