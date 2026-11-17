"""The replay check: would the new worker keep every open run going? Run before every deploy and rollback.

`check_open_runs(client, definition)` reads every open run in the namespace (`ExecutionStatus = 'Running'`, no type
filter, so a run whose type was removed or renamed fails here instead of failing for ever after the deploy) and:

- for runs on the definition's task queues: the type must be registered; the history is streamed through the
  definition's `Replayer` (the commands each run already recorded must come out the same); every pending activity's
  type must be registered (or served elsewhere, `external_activity_names`); every local activity the code starts
  while replaying, one in flight at the end of the history included, must be in `activity_names` itself (a local
  activity runs on this worker; an interceptor records them); every signal and update the run has received must
  still have a handler on the new class (queries leave no trace in history, so the handlers a client queries are
  pinned by a product's own test);
- for runs on other queues: counted per queue and type, for information; a failure only when the definition
  registers that type by name (a run the worker would serve if it polled that queue);
- every schedule that starts a workflow on the definition's queues must start a registered type; a schedule on
  another queue that starts a type the definition registers by name fails, as a run there would.

Open runs are listed until two listings agree (visibility lags behind runs that just started or closed); if they keep
differing, the check couldn't check. Runs are matched to their replay by workflow id (one open run per id): a reset
run's history names the run it was reset from, so the report takes the run id from the listing.

The report names each failure by kind, workflow id, type and run id with the error's message; it never holds payload
data. Replay does not check options, payloads or code past the end of a history: golden tests in CI cover those.

CLI: `python -m factory_runtime.temporal.replay_check MODULE:ATTR [--server HOST:PORT] [--namespace NS]
[--tls | --no-tls] [--json]`, where ATTR is a `WorkerDefinition` (or a function returning one). Connection details
not given as flags come from `definition.settings`, else `localhost:7233` / `default`; a Temporal Cloud API key comes
from `definition.settings.api_key` or a non-empty `$TEMPORAL_API_KEY`, and turns TLS on unless `--no-tls`. The
replayer is built from the definition before connecting. Exit 0: every open run passes; 1: runs would break, or the
definition did not load or no replayer can be built from it: reject the release; 2: couldn't check (Temporal or
visibility unreachable, or visibility kept changing): retry; 64: bad command line.
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
from typing import Any, AsyncIterator, Awaitable, Callable, NoReturn, Sequence

from temporalio import workflow
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import Client, ScheduleActionStartWorkflow, WorkflowExecution, WorkflowHistory
from temporalio.worker import (
    Interceptor,
    StartLocalActivityInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
    WorkflowReplayResult,
)
from temporalio.workflow import NondeterminismError

from factory_runtime.temporal.definition import WorkerDefinition

OPEN_RUNS_QUERY = "ExecutionStatus = 'Running'"

# Failure kinds.
NONDETERMINISM = "nondeterminism"  # replay took different steps than the run recorded
REPLAY_FAILURE = "replay_failure"  # replay raised (new code fails on the recorded history)
UNREGISTERED_TYPE = "unregistered_type"  # an open run's type is not registered on the new worker
MISSING_ACTIVITY = "missing_activity"  # a pending activity's or a local activity's type is not registered
MISSING_HANDLER = "missing_handler"  # a signal or update the run received has no handler on the new class
SCHEDULE_TYPE = "schedule_workflow_type"  # a schedule starts a type that is not registered
OTHER_QUEUE = "other_queue"  # a run or schedule of a registered type sits on a queue the worker doesn't serve

ERROR_CHARS = 500

# Exit codes of the CLIs.
EXIT_PASS, EXIT_REJECT, EXIT_RETRY, EXIT_USAGE = 0, 1, 2, 64

# Listing open runs: up to this many listings after the first, this long apart, until two agree.
LIST_ATTEMPTS = 5
LIST_PAUSE_SECONDS = 1.0


@dataclass(frozen=True)
class RunFailure:
    """One reason the release would break a run. For a schedule's failure, `workflow_id` is the schedule id and
    `run_id` is empty."""

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
# Recording what replayed code asks for (shared with factory_runtime.temporal.patches)
# ---------------------------------------------------------------------------------------------------------------


class ReplayRecorder(Interceptor):
    """A worker interceptor, added last to the check's replayer, that records per workflow id what the replayed code
    asked for and replay itself doesn't check: the local activity types it started (`local_activities`) and each
    patch id it asked about, with the answer (`patches`; False: the run is on the old side).

    Both are recorded where every spelling of the call ends up, so the import style doesn't matter (`workflow.patched`,
    `from temporalio.workflow import patched`, a module passed through the sandbox or run unsandboxed): local
    activities at the outbound interceptor, patches at the workflow instance's `workflow_patch`, which
    `patched` and `deprecate_patch` both call."""

    def __init__(self) -> None:
        self.local_activities: dict[str, set[str]] = {}
        self.patches: dict[str, dict[str, bool]] = {}

    def workflow_interceptor_class(self, input: WorkflowInterceptorClassInput) -> type[WorkflowInboundInterceptor]:
        recorder = self

        class Outbound(WorkflowOutboundInterceptor):
            def start_local_activity(self, input: StartLocalActivityInput) -> workflow.ActivityHandle[Any]:
                recorder.local_activities.setdefault(workflow.info().workflow_id, set()).add(input.activity)
                return super().start_local_activity(input)

        class Inbound(WorkflowInboundInterceptor):
            def init(self, outbound: WorkflowOutboundInterceptor) -> None:
                recorder._record_patches()
                super().init(Outbound(outbound))

        return Inbound

    def _record_patches(self) -> None:
        """Wrap the starting workflow instance's `workflow_patch` (the instance is the current runtime during
        interceptor init)."""
        runtime: Any = getattr(workflow, "_Runtime").current()
        original = getattr(runtime, "workflow_patch", None)
        if original is None:  # the SDK moved it: fail every replay rather than report no patches
            raise RuntimeError("the workflow runtime has no workflow_patch: patch calls can't be recorded")

        def workflow_patch(id: str, *, deprecated: bool, **kwargs: Any) -> bool:
            value = bool(original(id, deprecated=deprecated, **kwargs))
            self.patches.setdefault(workflow.info().workflow_id, {}).setdefault(id, value)
            return value

        runtime.workflow_patch = workflow_patch


# ---------------------------------------------------------------------------------------------------------------
# Shared with factory_runtime.temporal.patches
# ---------------------------------------------------------------------------------------------------------------


class VisibilityUnsettled(RuntimeError):
    """Listings of the open runs kept differing: the check couldn't check."""


async def list_open_runs(
    client: Client, *, attempts: int = LIST_ATTEMPTS, pause: float = LIST_PAUSE_SECONDS
) -> list[WorkflowExecution]:
    """Every open run in the client's namespace, of any type on any queue: listed until two listings `pause` apart
    agree on the open runs, at most `attempts` listings after the first (raises `VisibilityUnsettled`)."""

    async def listing() -> list[WorkflowExecution]:
        return [run async for run in client.list_workflows(OPEN_RUNS_QUERY)]

    def ids(runs: list[WorkflowExecution]) -> set[tuple[str, str | None]]:
        return {(run.id, run.run_id) for run in runs}

    runs = await listing()
    for _ in range(attempts):
        await asyncio.sleep(pause)
        again = await listing()
        if ids(again) == ids(runs):
            return again
        runs = again
    raise VisibilityUnsettled(f"the open runs listed kept changing over {attempts + 1} listings")


def failure_text(error: BaseException) -> str:
    """The message of a replay failure without stack traces: core wraps it in a `Failure { message: "…" }` dump."""
    text = str(error)
    match = re.search(r'message: "((?:[^"\\]|\\.)*)"', text)
    if match:
        text = re.sub(r"\\(.)", lambda m: {"n": "\n", "t": " "}.get(m.group(1), m.group(1)), match.group(1))
    return text.strip().splitlines()[0][:ERROR_CHARS] if text.strip() else type(error).__name__


def replay_failure(result: WorkflowReplayResult, run: WorkflowExecution) -> RunFailure | None:
    error = result.replay_failure
    if error is None:
        return None
    kind = NONDETERMINISM if isinstance(error, NondeterminismError) else REPLAY_FAILURE
    return run_failure(kind, run, failure_text(error))


def run_failure(kind: str, run: WorkflowExecution, error: str) -> RunFailure:
    """A failure of `run`, named by the listing (a reset run's history names the run it was reset from)."""
    return RunFailure(kind, run.id, run.workflow_type, run.run_id or "", error)


async def replay_runs(
    client: Client,
    definition: WorkerDefinition,
    runs: Sequence[WorkflowExecution],
    recorder: ReplayRecorder,
    on_history: Callable[[WorkflowExecution, WorkflowHistory], Awaitable[None]] | None = None,
) -> AsyncIterator[tuple[WorkflowExecution, WorkflowReplayResult]]:
    """Replay each run's history through the definition's replayer with `recorder`, yielding each run with its
    result. Results are matched by workflow id (one open run per id). `on_history(run, history)` is awaited for each
    history before it is replayed."""
    by_id = {run.id: run for run in runs}

    async def histories() -> AsyncIterator[WorkflowHistory]:
        for run in runs:
            history = await client.get_workflow_handle(run.id, run_id=run.run_id).fetch_history()
            if on_history is not None:
                await on_history(run, history)
            yield history

    replayer = definition.replayer(namespace=client.namespace, extra_interceptors=[recorder])
    async with replayer.workflow_replay_iterator(histories()) as results:
        async for result in results:
            yield by_id[result.history.workflow_id], result


async def build_replayer(definition: WorkerDefinition) -> None:
    """Build the definition's replayer as the check will, workflows prepared in their runner, without a server:
    raises when it can't be built (a broken release, not a reason to retry)."""

    async def no_histories() -> AsyncIterator[WorkflowHistory]:
        return
        yield

    replayer = definition.replayer(extra_interceptors=[ReplayRecorder()])
    async with replayer.workflow_replay_iterator(no_histories()) as results:
        async for _ in results:
            pass


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


def named_types(definition: WorkerDefinition) -> set[str]:
    """The workflow types the definition registers by name (not through a dynamic workflow)."""
    return {name for name in definition.workflow_classes() if name is not None}


def other_queue_failures(definition: WorkerDefinition, elsewhere: Sequence[WorkflowExecution]) -> list[RunFailure]:
    """Runs on queues the definition doesn't serve, of a type it registers by name."""
    names = named_types(definition)
    error = "open on queue {!r}, which this worker doesn't serve, though it registers the type"
    return [
        run_failure(OTHER_QUEUE, run, error.format(run.task_queue)) for run in elsewhere if run.workflow_type in names
    ]


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
            failures.append(run_failure(MISSING_HANDLER, run, error))
    if not handlers.dynamic_update:
        for name in sorted(updates - handlers.updates):
            error = f"received update {name!r}, which the new class doesn't handle"
            failures.append(run_failure(MISSING_HANDLER, run, error))
    return failures


async def _activity_failures(client: Client, definition: WorkerDefinition, run: WorkflowExecution) -> list[RunFailure]:
    description = await client.get_workflow_handle(run.id, run_id=run.run_id).describe()
    failures = []
    for pending in description.raw_description.pending_activities:
        name = pending.activity_type.name
        if not definition.serves_activity(name):
            failures.append(run_failure(MISSING_ACTIVITY, run, f"pending activity {name!r} is not registered"))
    return failures


def _local_activity_failures(
    definition: WorkerDefinition, run: WorkflowExecution, recorder: ReplayRecorder
) -> list[RunFailure]:
    error = "local activity {!r} is not registered (a local activity runs on this worker)"
    return [
        run_failure(MISSING_ACTIVITY, run, error.format(name))
        for name in sorted(recorder.local_activities.get(run.id, ()))
        if not definition.runs_local_activity(name)
    ]


async def _schedule_failures(client: Client, definition: WorkerDefinition) -> tuple[int, list[RunFailure]]:
    checked = 0
    failures = []
    names = named_types(definition)
    async for listed in await client.list_schedules():
        description = await client.get_schedule_handle(listed.id).describe()
        action = description.schedule.action
        if not isinstance(action, ScheduleActionStartWorkflow):
            continue
        if action.task_queue in definition.task_queues:
            checked += 1
            if definition.workflow_class(action.workflow) is None:
                error = f"schedule starts workflow type {action.workflow!r}, which is not registered"
                failures.append(RunFailure(SCHEDULE_TYPE, listed.id, action.workflow, "", error))
        elif action.workflow in names:
            checked += 1
            error = (
                f"schedule starts it on queue {action.task_queue!r}, which this worker doesn't serve, "
                "though it registers the type"
            )
            failures.append(RunFailure(OTHER_QUEUE, listed.id, action.workflow, "", error))
    return checked, failures


async def check_open_runs(client: Client, definition: WorkerDefinition) -> ReplayReport:
    """Check every open run and schedule in the client's namespace against `definition` (module docstring). Raises
    when Temporal or visibility can't be read, or visibility keeps changing."""
    report = ReplayReport(namespace=client.namespace, task_queues=definition.task_queues)
    registered, unregistered, elsewhere = served_runs(definition, await list_open_runs(client))

    other: dict[str, Counter[str]] = {}
    for run in elsewhere:
        other.setdefault(run.task_queue or "", Counter())[run.workflow_type] += 1
    report.other_queues = {q: dict(c) for q, c in other.items()}
    report.failures.extend(other_queue_failures(definition, elsewhere))

    checked: Counter[str] = Counter()
    for run in unregistered:
        checked[run.workflow_type] += 1
        report.failures.append(run_failure(UNREGISTERED_TYPE, run, "workflow type is not registered on the new worker"))

    async def before_replay(run: WorkflowExecution, history: WorkflowHistory) -> None:
        report.failures.extend(_handler_failures(definition, run, history))
        report.failures.extend(await _activity_failures(client, definition, run))

    recorder = ReplayRecorder()
    if registered:
        async for run, result in replay_runs(client, definition, registered, recorder, before_replay):
            checked[run.workflow_type] += 1
            if (failure := replay_failure(result, run)) is not None:
                report.failures.append(failure)
            report.failures.extend(_local_activity_failures(definition, run, recorder))
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


class _Parser(argparse.ArgumentParser):
    """Exits `EXIT_USAGE` on a bad command line: argparse's own 2 means "retry" to a deployer."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def cli_parser(prog: str, description: str) -> argparse.ArgumentParser:
    parser = _Parser(prog=prog, description=description)
    parser.add_argument("definition", help="MODULE:ATTR of the product's WorkerDefinition")
    parser.add_argument("--server", help="HOST:PORT of the Temporal frontend (default: the definition's settings)")
    parser.add_argument("--namespace", help="namespace (default: the definition's settings)")
    parser.add_argument(
        "--tls",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="connect over TLS, or not (default: the settings' tls, or on when there is an API key)",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def parse_args(parser: argparse.ArgumentParser, argv: Sequence[str] | None) -> argparse.Namespace | int:
    try:
        return parser.parse_args(argv)
    except SystemExit as e:  # --help exits 0; errors exit EXIT_USAGE
        return e.code if isinstance(e.code, int) else EXIT_USAGE


async def connect(definition: WorkerDefinition, args: argparse.Namespace) -> Client:
    settings = definition.settings
    api_key = (settings.api_key if settings is not None else None) or os.environ.get("TEMPORAL_API_KEY") or None
    if args.tls is not None:
        tls = bool(args.tls)
    else:
        tls = bool(settings is not None and settings.tls) or api_key is not None
    return await definition.connect(args.server, namespace=args.namespace, tls=tls, api_key=api_key)


def load_or_exit(spec: str) -> WorkerDefinition | int:
    """The definition, with its replayer built, or `EXIT_REJECT`: a release whose definition doesn't load, or from
    which no replayer can be built, is broken; checked before connecting."""
    try:
        definition = load_definition(spec)
    except Exception as e:  # the release's own code
        print(f"The worker definition {spec} did not load: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_REJECT
    try:
        asyncio.run(build_replayer(definition))
    except Exception as e:
        cause = f" ({type(e.__cause__).__name__}: {failure_text(e.__cause__)})" if e.__cause__ else ""
        print(
            f"No replayer can be built from the worker definition {spec}: {type(e).__name__}: {failure_text(e)}{cause}",
            file=sys.stderr,
        )
        return EXIT_REJECT
    return definition


def main(argv: Sequence[str] | None = None) -> int:
    parser = cli_parser("python -m factory_runtime.temporal.replay_check", "Check that every open run replays.")
    args = parse_args(parser, argv)
    if isinstance(args, int):
        return args
    definition = load_or_exit(args.definition)
    if isinstance(definition, int):
        return definition

    async def run() -> ReplayReport:
        return await check_open_runs(await connect(definition, args), definition)

    try:
        report = asyncio.run(run())
    except Exception as e:
        print(f"Replay check COULD NOT CHECK: {type(e).__name__}: {failure_text(e)}", file=sys.stderr)
        return EXIT_RETRY
    print(json.dumps(report.as_dict(), indent=2) if args.json else report.render())
    return EXIT_PASS if report.passed else EXIT_REJECT


if __name__ == "__main__":
    sys.exit(main())
