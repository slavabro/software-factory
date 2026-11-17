"""Where a product keeps its process files, what they may contain and how its schedules run. The product builds
this from its own config (on every call, if its tests override settings); workflow code never sees it. The task queue
is `TemporalSettings.task_queue`.

`step_types` is required: the product's own map (the one its workflow classes run), so a file is validated where it
loads (`files.load_process`, the schedule builder) against the types that will run it, never the built-ins alone."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from factory_runtime.process.validation import PARALLEL_INTERPRETERS

if TYPE_CHECKING:
    from factory_runtime.process.steps import StepType


@dataclass(frozen=True)
class ProcessSettings:
    project_root: Path  # process paths are relative to it
    step_types: Mapping[str, StepType]  # the product's step types (its workflow classes' `step_types`)
    # Workflow types a process's `interpreter:` may name (the product's registered interpreters); None: unchecked.
    interpreters: tuple[str, ...] | None = None
    parallel_interpreters: tuple[str, ...] = PARALLEL_INTERPRETERS  # those that run a process's steps at once
    process_glob: str = "processes/*.yaml"  # process files, relative to project_root
    workflow_schedule_glob: str = "workflows/*.schedule.yaml"  # schedules of Python workflows, relative to it
    default_interpreter: str = "LinearProcessWorkflow"  # workflow type for a process that names no `interpreter`
    default_timezone: str = "UTC"  # for a schedule that names no `timezone`
    cron_jitter: timedelta = timedelta(minutes=5)  # cron schedules start up to this late, spreading load
    default_overlap_policy: str = "skip"  # skip | buffer_one | buffer_all | cancel_other | terminate_other | allow_all
    catchup_window: timedelta = timedelta(hours=1)  # how far back a run missed during downtime is still started
    runs_limit: int = 10  # recent runs listed per schedule
