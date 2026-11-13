"""Process files on disk: load one, choose the workflow type that runs it, discover processes and the schedules
they (and Python workflows' `.schedule.yaml` files) declare. Paths and globs come from `ProcessSettings`."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

import yaml

from factory_runtime.process.settings import ProcessSettings

if TYPE_CHECKING:
    from factory_runtime.process.schedules import (
        ProcessScheduleConfig,
        ScheduleConfig,
        ScheduleDefinition,
        WorkflowScheduleConfig,
    )

logger = logging.getLogger(__name__)


def load_process(process_path: str | Path, root: Path) -> dict[str, Any]:
    """The process definition in `root / process_path`. Raises FileNotFoundError for a missing file and ValueError
    for an empty one."""
    path = root / process_path
    with open(path, "r") as f:
        content = f.read()
        if not content.strip():
            raise ValueError(f"YAML file {path} is empty or contains only whitespace")
        process_def = yaml.safe_load(content)
        if process_def is None:
            raise ValueError(f"YAML file {path} parsed to None - check file content")
    return process_def  # type: ignore[no-any-return]


def process_interpreter(process_def: Mapping[str, Any], default: str) -> str:
    """The workflow type that runs a process: its declared `interpreter`, else `default`. The one rule for every
    start (schedules and on-demand runs alike); another type would ignore the process's `condition:` steps."""
    return process_def.get("interpreter") or default


@dataclass(frozen=True)
class DiscoveredProcess:
    path: str  # relative to the project root
    name: str  # `name:`, else the file stem in title case
    description: str
    definition: dict[str, Any]


def discover_processes(settings: ProcessSettings) -> list[DiscoveredProcess]:
    """Every process file matching `settings.process_glob`, in path order; an empty file is skipped, an unreadable
    one logged and skipped."""
    processes = []
    for process_file in sorted(settings.project_root.glob(settings.process_glob)):
        try:
            with open(process_file) as f:
                data = yaml.safe_load(f)
            if not data:
                continue
            processes.append(
                DiscoveredProcess(
                    path=str(process_file.relative_to(settings.project_root)),
                    name=data.get("name", process_file.stem.replace("_", " ").title()),
                    description=data.get("description", ""),
                    definition=data,
                )
            )
        except Exception as e:
            logger.warning(f"Failed to parse process file {process_file}: {e}")
            continue
    return processes


def discover_schedules(
    settings: ProcessSettings, schedule_model: type[ScheduleDefinition] | None = None
) -> list[ScheduleConfig]:
    """The schedules declared by process files (`schedules:`, `retired_schedules:`) and by Python workflows'
    schedule files (`workflow_class:`, `schedules:`), parsed with `schedule_model` (a product may subclass
    `ScheduleDefinition`); a schedule without `timezone` gets `settings.default_timezone`. A file that fails to
    parse is logged and skipped."""
    from factory_runtime.process.schedules import ScheduleDefinition

    model = schedule_model or ScheduleDefinition
    configs: list[ScheduleConfig] = []
    root = settings.project_root

    for process_file in root.glob(settings.process_glob):
        config = parse_process_schedules(process_file, settings, model)
        if config and (config.schedules or config.retired_schedule_ids):
            configs.append(config)
            logger.debug(f"Found {len(config.schedules)} schedule(s) in {process_file}")

    for schedule_file in root.glob(settings.workflow_schedule_glob):
        workflow_config = parse_workflow_schedules(schedule_file, settings, model)
        if workflow_config and workflow_config.schedules:
            configs.append(workflow_config)
            logger.debug(f"Found {len(workflow_config.schedules)} schedule(s) in {schedule_file}")

    logger.info(f"Discovered {len(configs)} workflow(s) with schedules from {root}")
    return configs


def _schedule(model: type[ScheduleDefinition], data: Any, settings: ProcessSettings) -> ScheduleDefinition:
    return model(**{"timezone": settings.default_timezone, **data})


def parse_process_schedules(
    path: Path, settings: ProcessSettings, schedule_model: type[ScheduleDefinition]
) -> ProcessScheduleConfig | None:
    """The schedules a process file declares, or None (no schedules, or a file that fails to parse: logged).
    `process_path` is `str(path)`; an id both live and retired is refused."""
    from factory_runtime.process.schedules import ProcessScheduleConfig

    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except Exception as e:
        logger.warning(f"Failed to parse process file {path}: {e}")
        return None

    if not data:
        return None
    schedules_data = data.get("schedules") or []
    retired = data.get("retired_schedules") or []
    if not schedules_data and not retired:
        return None

    try:
        schedules = [_schedule(schedule_model, s, settings) for s in schedules_data]
        both = {s.id for s in schedules} & set(retired)
        if both:
            raise ValueError(f"declared both live and retired: {sorted(both)}")
        return ProcessScheduleConfig(
            process_path=str(path),
            interpreter=process_interpreter(data, settings.default_interpreter),
            schedules=schedules,
            retired_schedule_ids=[str(r) for r in retired],
        )
    except Exception as e:
        logger.warning(f"Failed to parse schedules in {path}: {e}")
        return None


def parse_workflow_schedules(
    path: Path, settings: ProcessSettings, schedule_model: type[ScheduleDefinition]
) -> WorkflowScheduleConfig | None:
    """The schedules a Python workflow's schedule file declares, or None (logged when it fails to parse or names no
    `workflow_class`)."""
    from factory_runtime.process.schedules import WorkflowScheduleConfig

    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except Exception as e:
        logger.warning(f"Failed to parse schedule file {path}: {e}")
        return None

    if not data or "workflow_class" not in data:
        logger.warning(f"Schedule file {path} missing workflow_class")
        return None

    schedules_data = data.get("schedules", [])
    if not schedules_data:
        return None

    try:
        return WorkflowScheduleConfig(
            workflow_class=data["workflow_class"],
            schedule_file_path=str(path),
            schedules=[_schedule(schedule_model, s, settings) for s in schedules_data],
        )
    except Exception as e:
        logger.warning(f"Failed to parse schedules in {path}: {e}")
        return None
