"""Temporal Schedules from process files: the definition model, the one builder, and `ScheduleManager`, which
converges Temporal on the files and reads, pauses, resumes and triggers schedules.

All schedule state lives in Temporal; nothing here stores any. A product passes in what is its own: the settings,
the process loader, the schedule memo its pages read, which new schedules start paused, and its schedule model.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Literal, Optional, Sequence, Union

from pydantic import BaseModel, Field
from temporalio.api.common.v1 import Payload
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionExecutionStartWorkflow,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleCalendarSpec,
    ScheduleDescription,
    ScheduleHandle,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
    WorkflowExecutionStatus,
)
from temporalio.service import RPCError, RPCStatusCode

from factory_runtime.process import files
from factory_runtime.process.settings import ProcessSettings
from factory_runtime.temporal.settings import TemporalSettings

logger = logging.getLogger(__name__)

OverlapPolicyName = Literal["skip", "buffer_one", "buffer_all", "cancel_other", "terminate_other", "allow_all"]

OVERLAP_MAP = {
    "skip": ScheduleOverlapPolicy.SKIP,
    "buffer_one": ScheduleOverlapPolicy.BUFFER_ONE,
    "buffer_all": ScheduleOverlapPolicy.BUFFER_ALL,
    "cancel_other": ScheduleOverlapPolicy.CANCEL_OTHER,
    "terminate_other": ScheduleOverlapPolicy.TERMINATE_OTHER,
    "allow_all": ScheduleOverlapPolicy.ALLOW_ALL,
}


# === Config models (parsed from files) ===


class ScheduleDefinition(BaseModel):
    """One schedule from a process file or a workflow's schedule file. A product may subclass it to add fields."""

    id: str = Field(..., description="Unique schedule identifier")
    # cron OR interval_seconds (the interval wins: sub-minute cadences)
    cron: Optional[str] = Field(default=None, description="Cron expression")
    interval_seconds: Optional[int] = Field(default=None, description="Interval in seconds (sub-minute schedules)")
    timezone: str  # discovery fills in ProcessSettings.default_timezone
    paused: bool = Field(default=False)
    overlap_policy: Optional[OverlapPolicyName] = None
    execution_timeout_seconds: Optional[int] = None

    # For processes
    input_data: Optional[dict] = Field(default_factory=dict)  # type: ignore[type-arg]

    # For Python workflows
    workflow_args: Optional[list[Any]] = Field(default_factory=list)


class ProcessScheduleConfig(BaseModel):
    """The schedules a process file declares."""

    process_path: str
    interpreter: str
    schedules: list[ScheduleDefinition] = Field(default_factory=list)
    # Ids the file used to declare (`retired_schedules:`); sync deletes them if present.
    retired_schedule_ids: list[str] = Field(default_factory=list)


class WorkflowScheduleConfig(BaseModel):
    """The schedules a Python workflow's schedule file declares."""

    workflow_class: str
    schedule_file_path: str  # for reference
    schedules: list[ScheduleDefinition] = Field(default_factory=list)


ScheduleConfig = Union[ProcessScheduleConfig, WorkflowScheduleConfig]


class SyncResult(BaseModel):
    """What a sync did, by schedule id."""

    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)  # existed, config differed: updated
    skipped: list[str] = Field(default_factory=list)  # existed, unchanged
    deleted: list[str] = Field(default_factory=list)  # retired by its file, still existed
    errors: list[dict] = Field(default_factory=list)  # type: ignore[type-arg]


# === What a product passes in ===

# The process definition at a path (default: files.load_process from the project root).
ProcessLoader = Callable[[str], dict[str, Any]]
# (config, process definition or None for a Python workflow) -> does a NEW schedule start paused?
InitiallyPaused = Callable[[ScheduleConfig, Optional[dict[str, Any]]], bool]
# (config, process definition or None) -> the schedule's memo (cron_expression / interval_seconds are added).
ScheduleMemo = Callable[[ScheduleConfig, Optional[dict[str, Any]]], dict[str, Any]]


def default_schedule_memo(config: ScheduleConfig, process_def: Optional[dict[str, Any]]) -> dict[str, Any]:
    if isinstance(config, ProcessScheduleConfig):
        return {"process_path": config.process_path, "interpreter": config.interpreter}
    return {"workflow_class": config.workflow_class}


def _overlap_policy(name: str | None, settings: ProcessSettings) -> ScheduleOverlapPolicy:
    key = name or settings.default_overlap_policy
    try:
        return OVERLAP_MAP[key]
    except KeyError:
        raise ValueError(f"Unknown overlap policy {key!r}; expected one of {', '.join(OVERLAP_MAP)}") from None


def build_schedule(
    config: ScheduleConfig,
    schedule_def: ScheduleDefinition,
    settings: ProcessSettings,
    temporal: TemporalSettings,
    *,
    load_process: ProcessLoader | None = None,
    initially_paused: InitiallyPaused | None = None,
    schedule_memo: ScheduleMemo | None = None,
) -> tuple[Schedule, dict[str, Any]]:
    """The Temporal `Schedule` and its create-time memo for one definition: the one place config becomes a schedule,
    so overlap, catchup, interval and jitter cannot be bypassed. Loads the process file for a process schedule.

    A process schedule starts the interpreter with two arguments, `[process_def, input_data]`; a Python workflow's
    starts it with `workflow_args`. The memo always carries `cron_expression` / `interval_seconds` (the server keeps
    compiled calendars and does not return cron strings)."""
    process_def: Optional[dict[str, Any]] = None
    if isinstance(config, ProcessScheduleConfig):
        loader = load_process or (lambda path: files.load_process(path, settings.project_root))
        process_def = loader(config.process_path)
        workflow_ref = config.interpreter
        workflow_args: list[Any] = [process_def, schedule_def.input_data or {}]
    else:
        workflow_ref = config.workflow_class
        workflow_args = schedule_def.workflow_args or []
    memo = (schedule_memo or default_schedule_memo)(config, process_def)

    if schedule_def.cron:
        memo["cron_expression"] = schedule_def.cron
    if schedule_def.interval_seconds:
        memo["interval_seconds"] = schedule_def.interval_seconds

    # Interval wins (sub-minute cadences); otherwise cron.
    if schedule_def.interval_seconds:
        spec = ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(seconds=schedule_def.interval_seconds))])
    elif schedule_def.cron:
        spec = ScheduleSpec(
            cron_expressions=[schedule_def.cron],
            jitter=settings.cron_jitter,
            time_zone_name=schedule_def.timezone,
        )
    else:
        raise ValueError(f"Schedule {schedule_def.id} needs either cron or interval_seconds")

    action_kwargs: dict[str, Any] = {}
    if schedule_def.execution_timeout_seconds is not None:
        action_kwargs["execution_timeout"] = timedelta(seconds=schedule_def.execution_timeout_seconds)

    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            workflow_ref,
            args=workflow_args,
            id=schedule_def.id,
            task_queue=temporal.task_queue,
            memo=memo,
            **action_kwargs,
        ),
        spec=spec,
        # Always explicit: the default overlap (skip) so a slow run never stacks, and a bounded catchup window so a
        # restart starts recent misses only.
        policy=SchedulePolicy(
            overlap=_overlap_policy(schedule_def.overlap_policy, settings),
            catchup_window=settings.catchup_window,
        ),
        # Only a create applies this: sync never unpauses an existing schedule (it carries live state over and the
        # signature leaves it out), so an operator's pause survives.
        state=ScheduleState(
            paused=schedule_def.paused or (initially_paused is not None and initially_paused(config, process_def))
        ),
    )
    return schedule, memo


@dataclasses.dataclass(frozen=True)
class ScheduleView:
    """A schedule as Temporal has it. `memo` is the create-time memo updated by the action memo (which every
    in-place update refreshes); `calendars` are the server's compiled cron, for a product's own wording."""

    schedule_id: str
    memo: dict[str, Any]
    cron_expression: str | None
    interval_seconds: int | None
    timezone: str
    is_paused: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    calendars: Sequence[ScheduleCalendarSpec]


@dataclasses.dataclass(frozen=True)
class ScheduleRun:
    """One recent run of a schedule; `status` is None when the run could not be described."""

    workflow_id: str
    run_id: str
    started_at: datetime
    status: WorkflowExecutionStatus | None
    completed_at: datetime | None


class ScheduleManager:
    """Converges Temporal's schedules on the files, and reads and changes them. Owns no state."""

    def __init__(
        self,
        client: Client,
        settings: ProcessSettings,
        temporal: TemporalSettings,
        *,
        discover: Callable[[], Sequence[ScheduleConfig]] | None = None,
        load_process: ProcessLoader | None = None,
        initially_paused: InitiallyPaused | None = None,
        schedule_memo: ScheduleMemo | None = None,
    ) -> None:
        """`discover` gives the configs to sync (default: `files.discover_schedules(settings)`)."""
        self.client = client
        self.settings = settings
        self.temporal = temporal
        self._discover = discover or (lambda: files.discover_schedules(settings))
        self._load_process = load_process
        self._initially_paused = initially_paused
        self._schedule_memo = schedule_memo

    def build(self, config: ScheduleConfig, schedule_def: ScheduleDefinition) -> tuple[Schedule, dict[str, Any]]:
        """`build_schedule` with this manager's settings and hooks."""
        return build_schedule(
            config,
            schedule_def,
            self.settings,
            self.temporal,
            load_process=self._load_process,
            initially_paused=self._initially_paused,
            schedule_memo=self._schedule_memo,
        )

    # === Sync from files ===

    async def sync_all_from_files(self) -> SyncResult:
        """Create missing schedules, update changed ones in place (paused state kept), leave unchanged ones alone,
        and delete the ids a process file retired. Idempotent and convergent; one schedule's failure is reported in
        `errors`, not raised."""
        result = SyncResult()
        configs = self._discover()

        for config in configs:
            for schedule_def in config.schedules:
                try:
                    outcome = await self._sync_one(config, schedule_def)
                    getattr(result, outcome).append(schedule_def.id)
                except Exception as e:
                    logger.error(f"Failed to sync schedule {schedule_def.id}: {e}")
                    result.errors.append({"schedule_id": schedule_def.id, "error": str(e)})

        # Sync never deletes on its own; a file declares the ids it used to own, so a renamed schedule does not keep
        # firing the old definition for ever.
        for config in configs:
            if not isinstance(config, ProcessScheduleConfig):
                continue
            for schedule_id in config.retired_schedule_ids:
                try:
                    handle = self.client.get_schedule_handle(schedule_id)
                    if await self._describe_or_none(handle) is None:
                        continue
                    await self.delete_schedule(schedule_id)
                    result.deleted.append(schedule_id)
                except Exception as e:
                    logger.error(f"Failed to delete retired schedule {schedule_id}: {e}")
                    result.errors.append({"schedule_id": schedule_id, "error": str(e)})

        logger.info(
            f"Schedule sync: created={len(result.created)}, updated={len(result.updated)}, "
            f"unchanged={len(result.skipped)}, deleted={len(result.deleted)}, errors={len(result.errors)}"
        )
        return result

    async def _sync_one(self, config: ScheduleConfig, schedule_def: ScheduleDefinition) -> str:
        """Converge one schedule; returns its SyncResult bucket (created, updated, skipped). Existence is decided by
        describe (strongly consistent), not the list API, whose index lags a fresh create."""
        desired, memo = self.build(config, schedule_def)
        handle = self.client.get_schedule_handle(schedule_def.id)

        desc = await self._describe_or_none(handle)
        if desc is None:
            try:
                await self.client.create_schedule(schedule_def.id, desired, memo=memo)
                logger.info(f"Created schedule: {schedule_def.id}")
                return "created"
            except ScheduleAlreadyRunningError:
                desc = await handle.describe()  # lost a race: converge instead

        if await self._signature(desc.schedule) == await self._signature(desired):
            return "skipped"

        async def _apply(update_input: ScheduleUpdateInput) -> ScheduleUpdate:
            # Runtime state (paused, note, remaining actions) is the operator's, not the file's: carry it over.
            current_state = update_input.description.schedule.state
            return ScheduleUpdate(schedule=dataclasses.replace(desired, state=current_state))

        await handle.update(_apply)
        logger.info(f"Updated schedule: {schedule_def.id} (config changed)")
        return "updated"

    @staticmethod
    async def _describe_or_none(handle: ScheduleHandle) -> ScheduleDescription | None:
        try:
            return await handle.describe()
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                return None
            raise

    async def _signature(self, schedule: Schedule) -> dict[str, Any]:
        """A comparable view of a schedule, the same for a built one and a described one: workflow, id, task queue,
        execution timeout, decoded args and memo, cron (from the memo), intervals, jitter, time zone, overlap,
        catchup window, pause-on-failure. Paused state is left out."""
        action = schedule.action
        if not isinstance(action, ScheduleActionStartWorkflow):
            return {"action": type(action).__name__}
        args = list(action.args)
        if args and isinstance(args[0], Payload):
            args = await self.client.data_converter.decode(args)
        memo = await self._decode_memo(action.memo)
        spec = schedule.spec
        policy = schedule.policy
        return {
            "workflow": action.workflow,
            "workflow_id": action.id,
            "task_queue": action.task_queue,
            "execution_timeout": action.execution_timeout or None,
            "args": args,
            "memo": memo,
            "cron": memo.get("cron_expression"),
            "intervals": [(i.every, i.offset or timedelta(0)) for i in spec.intervals],
            "jitter": spec.jitter or timedelta(0),
            "time_zone": spec.time_zone_name or "",
            "overlap": policy.overlap,
            "catchup_window": policy.catchup_window,
            "pause_on_failure": policy.pause_on_failure,
        }

    async def _decode_memo(self, memo: Any) -> dict[str, Any]:
        """An action memo as plain values (describe hands back raw payloads)."""
        if not memo:
            return {}
        decoded = {}
        for key, value in dict(memo).items():
            if isinstance(value, Payload):
                value = (await self.client.data_converter.decode([value]))[0]
            decoded[key] = value
        return decoded

    # === Read ===

    async def list_schedules(self) -> list[ScheduleView]:
        """Every schedule in the namespace, each described (a list entry carries only the create-time memo)."""
        schedules = []
        async for entry in await self.client.list_schedules():
            schedules.append(await self.get_schedule(entry.id))
        return schedules

    async def get_schedule(self, schedule_id: str) -> ScheduleView:
        handle = self.client.get_schedule_handle(schedule_id)
        desc = await handle.describe()
        return await self._view(schedule_id, desc)

    async def get_schedule_runs(self, schedule_id: str, limit: int | None = None) -> list[ScheduleRun]:
        """The newest `limit` runs (default `settings.runs_limit`), newest first, each with its real status and
        close time. Raises ValueError when the schedule is missing or cannot be described."""
        if limit is None:
            limit = self.settings.runs_limit

        try:
            handle = self.client.get_schedule_handle(schedule_id)
            desc = await handle.describe()
        except Exception as e:
            error_msg = str(e).lower()
            if any(phrase in error_msg for phrase in ["not found", "does not exist", "not_found"]):
                logger.warning(f"Schedule {schedule_id} not found in Temporal")
                raise ValueError(f"Schedule {schedule_id} not found")
            logger.error(f"Failed to get schedule handle or describe for {schedule_id}: {e}", exc_info=True)
            raise ValueError(f"Schedule {schedule_id} not accessible: {e}")

        runs = []
        # recent_actions is oldest first: take the newest `limit`
        for action in desc.info.recent_actions[-limit:]:
            started = action.action
            if not isinstance(started, ScheduleActionExecutionStartWorkflow):
                continue
            workflow_id = started.workflow_id
            run_id = started.first_execution_run_id

            status: WorkflowExecutionStatus | None = None
            completed_at = None
            try:
                workflow_desc = await self.client.get_workflow_handle(workflow_id, run_id=run_id).describe()
                status = workflow_desc.status
                completed_at = workflow_desc.close_time
            except Exception as e:
                logger.debug(f"Could not describe run {workflow_id}/{run_id}: {e}")

            runs.append(ScheduleRun(workflow_id, run_id, action.started_at, status, completed_at))

        runs.sort(key=lambda r: r.started_at, reverse=True)
        return runs[:limit]

    # === Change ===

    async def create_schedule(self, config: ScheduleConfig, schedule_def: ScheduleDefinition) -> ScheduleView:
        """Create one schedule outside the files (e.g. from an API), through the same builder as the sync."""
        schedule, memo = self.build(config, schedule_def)
        await self.client.create_schedule(schedule_def.id, schedule, memo=memo)
        logger.info(f"Created schedule: {schedule_def.id}")
        return await self.get_schedule(schedule_def.id)

    async def delete_schedule(self, schedule_id: str) -> bool:
        handle = self.client.get_schedule_handle(schedule_id)
        await handle.delete()
        logger.info(f"Deleted schedule: {schedule_id}")
        return True

    async def pause_schedule(self, schedule_id: str, note: str = "") -> ScheduleView:
        handle = self.client.get_schedule_handle(schedule_id)
        await handle.pause(note=note)
        logger.info(f"Paused schedule: {schedule_id}")
        return await self.get_schedule(schedule_id)

    async def resume_schedule(self, schedule_id: str, note: str = "") -> ScheduleView:
        handle = self.client.get_schedule_handle(schedule_id)
        await handle.unpause(note=note)
        logger.info(f"Resumed schedule: {schedule_id}")
        return await self.get_schedule(schedule_id)

    async def trigger_schedule(self, schedule_id: str) -> dict[str, str]:
        """Start a run now."""
        handle = self.client.get_schedule_handle(schedule_id)
        await handle.trigger()
        logger.info(f"Triggered schedule: {schedule_id}")
        return {"status": "triggered", "schedule_id": schedule_id}

    async def _view(self, schedule_id: str, desc: ScheduleDescription) -> ScheduleView:
        memo = dict(await desc.memo() or {})
        memo.update(await self._decode_memo(getattr(desc.schedule.action, "memo", None)))
        spec = desc.schedule.spec

        cron = spec.cron_expressions[0] if spec.cron_expressions else memo.get("cron_expression") or None
        interval_seconds: int | None = None
        if spec.intervals:
            interval_seconds = int(spec.intervals[0].every.total_seconds())
        elif memo.get("interval_seconds"):
            interval_seconds = memo.get("interval_seconds")

        return ScheduleView(
            schedule_id=schedule_id,
            memo=memo,
            cron_expression=cron,
            interval_seconds=interval_seconds,
            timezone=spec.time_zone_name or self.settings.default_timezone,
            is_paused=desc.schedule.state.paused,
            next_run_at=desc.info.next_action_times[0] if desc.info.next_action_times else None,
            last_run_at=desc.info.recent_actions[-1].started_at if desc.info.recent_actions else None,
            calendars=list(spec.calendars),
        )
