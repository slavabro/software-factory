"""Where open runs stand on each patch marker: may a `patched(id)` be deprecated, may the call be removed?

The lifecycle of a behaviour change (docs/designs/safe-deploys.md §3): `workflow.patched(id)` while open runs are on
its old side, then `workflow.deprecate_patch(id)` once none are, then the call removed once no open run carries a
non-deprecated marker for it. History alone can't tell "passed the point on old code" from "never reached it", so
`patch_report(client, definition)` replays every open run on the definition's queues and records each patch call's
answer (`replay_check.ReplayRecorder`, at the workflow instance, so `workflow.patched`, `from temporalio.workflow
import patched`, a module passed through the sandbox and an unsandboxed runner are all seen), and reports per patch id:

- `old_side`: runs where `patched(id)` (or `deprecate_patch(id)`) answered False while replaying: they passed the
  point on the old code;
- `marked`: runs whose history carries a non-deprecated marker for the id (they took the new side; removing the
  call breaks them; deprecating is fine);
- `others`: the rest of the runs checked (never reached the point, or carry only a deprecated marker).

`can_deprecate` is "no run on the old side"; `can_remove` is that and "no non-deprecated marker". A patch id the
code calls but no open run reached does not appear. This report is the gate for each step. The replay check
(`replay_check`) also fails a premature step, but only when the two sides issue different commands; a patch that
changes only a value kept in workflow state (or anything else that issues no command) replays either way, so for it
the report is the only gate: deprecate or remove only when it says so.

CLI: `python -m factory_runtime.temporal.patches MODULE:ATTR [--server …] [--namespace …] [--tls | --no-tls]
[--json]`, the same options and connection rules as the replay check. Exit 0: every open run that could matter was
replayed and the report is complete; 1: some were left out (replay failed, type unregistered, or a run of a
registered type sits on a queue the definition doesn't serve), or the definition did not load or no replayer can be
built from it; 2: couldn't check; 64: bad command line.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Sequence

from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import Client, WorkflowExecution, WorkflowHistory

from factory_runtime.temporal.definition import WorkerDefinition
from factory_runtime.temporal.replay_check import (
    EXIT_PASS,
    EXIT_REJECT,
    EXIT_RETRY,
    UNREGISTERED_TYPE,
    ReplayRecorder,
    RunFailure,
    cli_parser,
    connect,
    failure_text,
    list_open_runs,
    load_or_exit,
    other_queue_failures,
    parse_args,
    replay_failure,
    replay_runs,
    run_failure,
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
    failures: list[RunFailure] = field(default_factory=list)  # runs left out: replay failed, unregistered, elsewhere

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
# What history says
# ---------------------------------------------------------------------------------------------------------------


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
    registered, unregistered, elsewhere = served_runs(definition, await list_open_runs(client))
    for run in unregistered:
        report.failures.append(run_failure(UNREGISTERED_TYPE, run, "workflow type is not registered on this worker"))
    report.failures.extend(other_queue_failures(definition, elsewhere))
    if not registered:
        return report

    markers: dict[str, dict[str, bool]] = {}  # workflow id -> patch id -> deprecated
    replayed: list[RunRef] = []

    async def read_markers(run: WorkflowExecution, history: WorkflowHistory) -> None:
        markers[run.id] = history_markers(history.events)

    recorder = ReplayRecorder()
    async for run, result in replay_runs(client, definition, registered, recorder, read_markers):
        if (failure := replay_failure(result, run)) is not None:
            report.failures.append(failure)
            continue
        replayed.append(RunRef(run.id, run.workflow_type, run.run_id or ""))

    report.runs_checked = len(replayed)
    for ref in replayed:
        answers = recorder.patches.get(ref.workflow_id, {})
        run_markers = markers.get(ref.workflow_id, {})
        old = {pid for pid, value in answers.items() if not value}
        marked = {pid for pid, deprecated in run_markers.items() if not deprecated}
        for pid in set(answers) | set(run_markers):
            report.patches.setdefault(pid, PatchStatus(pid))
        for pid in old:
            report.patches[pid].old_side.append(ref)
        for pid in marked - old:
            report.patches[pid].marked.append(ref)
    for status in report.patches.values():
        status.others = len(replayed) - len(status.old_side) - len(status.marked)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(
        cli_parser("python -m factory_runtime.temporal.patches", "Report where open runs stand on each patch marker."),
        argv,
    )
    if isinstance(args, int):
        return args
    definition = load_or_exit(args.definition)
    if isinstance(definition, int):
        return definition

    async def run() -> PatchReport:
        return await patch_report(await connect(definition, args), definition)

    try:
        report = asyncio.run(run())
    except Exception as e:
        print(f"Patch report COULD NOT CHECK: {type(e).__name__}: {failure_text(e)}", file=sys.stderr)
        return EXIT_RETRY
    print(json.dumps(report.as_dict(), indent=2) if args.json else report.render())
    return EXIT_PASS if report.complete else EXIT_REJECT


if __name__ == "__main__":
    sys.exit(main())
