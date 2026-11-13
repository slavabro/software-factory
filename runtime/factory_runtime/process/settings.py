"""Where a product keeps its process files and how its schedules run. The product builds this from its own config
(on every call, if its tests override settings); workflow code never sees it. The task queue is
`TemporalSettings.task_queue`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path


@dataclass(frozen=True)
class ProcessSettings:
    project_root: Path  # process paths are relative to it
    process_glob: str = "processes/*.yaml"  # process files, relative to project_root
    workflow_schedule_glob: str = "workflows/*.schedule.yaml"  # schedules of Python workflows, relative to it
    default_interpreter: str = "LinearProcessWorkflow"  # workflow type for a process that names no `interpreter`
    default_timezone: str = "UTC"  # for a schedule that names no `timezone`
    cron_jitter: timedelta = timedelta(minutes=5)  # cron schedules start up to this late, spreading load
    default_overlap_policy: str = "skip"  # skip | buffer_one | buffer_all | cancel_other | terminate_other | allow_all
    catchup_window: timedelta = timedelta(hours=1)  # how far back a run missed during downtime is still started
    runs_limit: int = 10  # recent runs listed per schedule
