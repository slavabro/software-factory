"""Where open runs stand on each patch marker: may a `patched(id)` be deprecated, may the call be removed?

The lifecycle of a behaviour change (docs/designs/safe-deploys.md §3): `workflow.patched(id)` while open runs are on
its old side, then `workflow.deprecate_patch(id)` once none are, then the call removed once no open run carries a
non-deprecated marker for it. History alone can't tell "passed the point on old code" from "never reached it", so
`patch_report(client, definition)` replays every open run on the definition's queues with `workflow.patched`
instrumented, and reports per patch id:

- `old_side`: runs where `patched(id)` returned False while replaying (they passed the point on the old code);
- `marked`: runs whose history carries a non-deprecated marker for the id (they took the new side; removing the
  call breaks them; deprecating is fine);
- `others`: the rest of the runs checked (never reached the point, or carry only a deprecated marker).

`can_deprecate` is "no run on the old side"; `can_remove` is that and "no non-deprecated marker". A patch id the
code calls but no open run reached does not appear. The replay check (`replay_check`) blocks a premature step on its
own: deprecating or removing too early fails replay.

CLI: `python -m factory_runtime.temporal.patches MODULE:ATTR [--server …] [--namespace …] [--tls] [--json]`, the
same options as the replay check. Exit 0: every run replayed and the report is complete; 1: some runs failed to
replay or are of a type the definition doesn't register (the report leaves them out); 2: couldn't check.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterator, Sequence

from temporalio import workflow
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import Client, WorkflowHistory

from factory_runtime.temporal.definition import WorkerDefinition
from factory_runtime.temporal.replay_check import (
    UNREGISTERED_TYPE,
    RunFailure,
    cli_parser,
    connect,
    failure_text,
    fetch_histories,
    list_open_runs,
    load_or_exit,
    replay_failure,
    served_runs,
)

PATCH_MARKER = "core_patch"
PATCH_DATA = "patch-data"


@dataclass(frozen=True)
class RunRef:
    workflow_id: str
    workflow_type: str
    run_id: str


@dataclass
class PatchStatus:
    patch_id: str
    old_side: list[RunRef] = field(default_factory=list)
    marked: list[RunRef] = field(default_factory=list)
    others: int = 0

    @property
    def can_deprecate(self) -> bool:
        return not self.old_side

    @property
    def can_remove(self) -> bool:
        return not self.old_side and not self.marked


@dataclass
class PatchReport:
    runs_checked: int = 0
    patches: dict[str, PatchStatus] = field(default_factory=dict)
    failures: list[RunFailure] = field(default_factory=list)  # runs left out: replay failed or type unregistered

    @property
    def complete(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "runs_checked": self.runs_checked,
            "patches": {
                pid: {
                    "old_side": [vars(r) for r in p.old_side],
                    "marked": [vars(r) for r in p.marked],
                    "others": p.others,
                    "can_deprecate": p.can_deprecate,
                    "can_remove": p.can_remove,
                }
                for pid, p in sorted(self.patches.items())
            },
            "failures": [vars(f) for f in self.failures],
        }

    def render(self) -> str:
        head = "complete" if self.complete else f"INCOMPLETE: {len(self.failures)} run(s) left out"
        lines = [f"Patch report {head}; open runs replayed: {self.runs_checked}"]
        if not self.patches:
            lines.append("no open run reached a patch")
        for pid, p in sorted(self.patches.items()):
            steps = "may be removed" if p.can_remove else "may be deprecated" if p.can_deprecate else "keep patched()"
            lines.append(
                f"{pid}: old side {len(p.old_side)}, non-deprecated marker {len(p.marked)}, others {p.others}"
                f" -> {steps}"
            )
            lines += [f"  old side: {r.workflow_type} {r.workflow_id} run {r.run_id}" for r in p.old_side]
        for f in self.failures:
            lines.append(f"left out [{f.kind}] {f.workflow_type} {f.workflow_id} run {f.run_id}: {f.error}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------
# Instrumenting workflow.patched
# ---------------------------------------------------------------------------------------------------------------

# run id -> [(patch id, returned value, replaying)]
_calls: dict[str, list[tuple[str, bool, bool]]] = {}
_lock = threading.Lock()  # one report at a time per process: the instrument is module-wide


@contextlib.contextmanager
def _instrumented_patched() -> Iterator[dict[str, list[tuple[str, bool, bool]]]]:
    """Swap `temporalio.workflow.patched` (shared with the sandbox, which passes temporalio through) for a recorder.
    A workflow module imported in the sandbox during replay binds the recorder, even with `from … import patched`."""
    original = workflow.patched

    def recording_patched(id: str, **kwargs: Any) -> bool:
        value = original(id, **kwargs)
        replaying = workflow.unsafe.is_replaying()
        _calls.setdefault(workflow.info().run_id, []).append((id, value, replaying))
        return value

    if not _lock.acquire(blocking=False):
        raise RuntimeError("a patch report is already running in this process")
    try:
        _calls.clear()
        workflow.patched = recording_patched
        try:
            yield _calls
        finally:
            workflow.patched = original
    finally:
        _lock.release()


def history_markers(events: Sequence[HistoryEvent]) -> dict[str, bool]:
    """Patch ids whose marker the history holds, to whether the marker is deprecated."""
    markers: dict[str, bool] = {}
    for e in events:
        if not e.HasField("marker_recorded_event_attributes"):
            continue
        attrs = e.marker_recorded_event_attributes
        if attrs.marker_name != PATCH_MARKER or PATCH_DATA not in attrs.details:
            continue
        for payload in attrs.details[PATCH_DATA].payloads:
            data = json.loads(payload.data)
            markers[str(data["id"])] = bool(data.get("deprecated", False))
    return markers


async def patch_report(client: Client, definition: WorkerDefinition) -> PatchReport:
    """Replay every open run on the definition's queues and report each patch id's runs (module docstring)."""
    report = PatchReport()
    registered, unregistered, _ = served_runs(definition, await list_open_runs(client))
    for run in unregistered:
        error = "workflow type is not registered on this worker"
        report.failures.append(RunFailure(UNREGISTERED_TYPE, run.id, run.workflow_type, run.run_id or "", error))
    if not registered:
        return report

    by_run = {run.run_id: run for run in registered}
    markers: dict[str, dict[str, bool]] = {}
    replayed: list[RunRef] = []

    async def histories() -> AsyncIterator[WorkflowHistory]:
        async for history in fetch_histories(client, registered):
            markers[history.run_id] = history_markers(history.events)
            yield history

    with _instrumented_patched() as calls:
        async with definition.replayer(namespace=client.namespace).workflow_replay_iterator(histories()) as results:
            async for result in results:
                run = by_run[result.history.run_id]
                if (failure := replay_failure(result, run.workflow_type)) is not None:
                    report.failures.append(failure)
                    continue
                replayed.append(RunRef(run.id, run.workflow_type, result.history.run_id))
        recorded = {run_id: list(c) for run_id, c in calls.items()}

    report.runs_checked = len(replayed)
    for ref in replayed:
        old = {pid for pid, value, replaying in recorded.get(ref.run_id, []) if replaying and not value}
        marked = {pid for pid, deprecated in markers.get(ref.run_id, {}).items() if not deprecated}
        for pid in old | marked | set(markers.get(ref.run_id, {})) | {c[0] for c in recorded.get(ref.run_id, [])}:
            report.patches.setdefault(pid, PatchStatus(pid))
        for pid in old:
            report.patches[pid].old_side.append(ref)
        for pid in marked - old:
            report.patches[pid].marked.append(ref)
    for status in report.patches.values():
        status.others = len(replayed) - len(status.old_side) - len(status.marked)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    args = cli_parser(
        "python -m factory_runtime.temporal.patches", "Report where open runs stand on each patch marker."
    ).parse_args(argv)
    definition = load_or_exit(args.definition)
    if isinstance(definition, int):
        return definition

    async def run() -> PatchReport:
        return await patch_report(await connect(definition, args), definition)

    try:
        report = asyncio.run(run())
    except Exception as e:
        print(f"Patch report COULD NOT CHECK: {type(e).__name__}: {failure_text(e)}", file=sys.stderr)
        return 2
    print(json.dumps(report.as_dict(), indent=2) if args.json else report.render())
    return 0 if report.complete else 1


if __name__ == "__main__":
    sys.exit(main())
