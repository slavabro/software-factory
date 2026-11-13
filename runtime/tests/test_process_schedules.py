"""Schedules from process files: the definition model, the one builder (policy always explicit), convergent sync
against a fake client (create, skip, update in place keeping the operator's pause, retired ids), the create path,
the `initially_paused` hook, recent runs with their real status, and the sync round-trip against a real server."""

import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml
from pydantic import ValidationError
from temporalio.client import (
    ScheduleActionExecutionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    ScheduleState,
    ScheduleUpdateInput,
    WorkflowExecutionStatus,
)
from temporalio.service import RPCError, RPCStatusCode

from factory_runtime.process.files import parse_process_schedules
from factory_runtime.process.schedules import (
    ProcessScheduleConfig,
    ScheduleDefinition,
    ScheduleManager,
    WorkflowScheduleConfig,
    build_schedule,
)
from factory_runtime.process.settings import ProcessSettings
from factory_runtime.temporal import TemporalSettings, data_converter

PROCESS_PATH = "processes/test_process.yaml"
SETTINGS = ProcessSettings(project_root=Path("/nonexistent"))
TEMPORAL = TemporalSettings(task_queue="test-queue")


def sd(**kwargs):
    """A ScheduleDefinition as discovery builds it (the default time zone applied)."""
    return ScheduleDefinition(**{"timezone": SETTINGS.default_timezone, **kwargs})


def _process_config(schedule_def):
    return ProcessScheduleConfig(process_path=PROCESS_PATH, interpreter="LinearProcessWorkflow", schedules=[schedule_def])


def fo_style_memo(config, process_def):
    """A product's memo hook (the layout its pages read)."""
    if isinstance(config, ProcessScheduleConfig):
        return {
            "mode": "yaml_process",
            "process_path": config.process_path,
            "interpreter": config.interpreter,
            "department": process_def.get("metadata", {}).get("department", "unknown"),
            "process_name": process_def.get("name", "Unknown"),
        }
    return {"mode": "direct", "workflow_class": config.workflow_class}


# -----------------------------------------------------------------------------
# Fake Temporal client (only what ScheduleManager touches)
# -----------------------------------------------------------------------------


class _AsyncIter:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _Describe:
    def __init__(self, schedule, memo):
        self.schedule = schedule
        self._memo = memo
        self.info = SimpleNamespace(next_action_times=[], recent_actions=[])

    async def memo(self):
        return self._memo


class FakeHandle:
    def __init__(self, client, schedule_id):
        self.client = client
        self.schedule_id = schedule_id

    async def describe(self):
        if self.schedule_id not in self.client.store:
            raise RPCError("schedule not found", RPCStatusCode.NOT_FOUND, b"")
        schedule, memo = self.client.store[self.schedule_id]
        return _Describe(schedule, memo)

    async def update(self, updater):
        self.client.update_calls.append(self.schedule_id)
        result = updater(ScheduleUpdateInput(description=await self.describe()))
        if hasattr(result, "__await__"):
            result = await result
        _, memo = self.client.store[self.schedule_id]
        self.client.store[self.schedule_id] = (result.schedule, memo)

    async def pause(self, note=""):
        schedule, memo = self.client.store[self.schedule_id]
        schedule.state = ScheduleState(paused=True, note=note)

    async def delete(self):
        self.client.deleted.append(self.schedule_id)
        del self.client.store[self.schedule_id]


class FakeClient:
    def __init__(self):
        self.store: dict[str, tuple] = {}
        self.update_calls: list[str] = []
        self.create_calls: list[str] = []
        self.deleted: list[str] = []
        self.data_converter = data_converter()

    async def create_schedule(self, schedule_id, schedule, memo=None):
        self.create_calls.append(schedule_id)
        if schedule_id in self.store:
            raise ScheduleAlreadyRunningError()
        self.store[schedule_id] = (schedule, dict(memo or {}))

    def get_schedule_handle(self, schedule_id):
        return FakeHandle(self, schedule_id)

    async def list_schedules(self):
        return _AsyncIter(SimpleNamespace(id=sid) for sid in self.store)


class FakeProcess(unittest.TestCase):
    """Stand-in for the process loader, mutable so tests can change the 'file'."""

    def setUp(self):
        self.process_def = {"name": "Test Process", "metadata": {"department": "test"}, "steps": []}
        self.loaded: list[str] = []

    def load(self, path):
        self.loaded.append(path)
        return dict(self.process_def)

    def build(self, config, schedule_def, settings=SETTINGS, **hooks):
        hooks.setdefault("load_process", self.load)
        return build_schedule(config, schedule_def, settings, TEMPORAL, **hooks)


# -----------------------------------------------------------------------------
# ScheduleDefinition
# -----------------------------------------------------------------------------


class ScheduleDefinitionTest(unittest.TestCase):
    def test_defaults_no_overlap(self):
        d = sd(id="x", cron="0 * * * *")
        self.assertIsNone(d.overlap_policy)
        self.assertIsNone(d.execution_timeout_seconds)

    def test_with_overlap_policy(self):
        self.assertEqual(sd(id="x", cron="0 * * * *", overlap_policy="skip").overlap_policy, "skip")

    def test_all_overlap_policies(self):
        for policy in ["skip", "buffer_one", "buffer_all", "cancel_other", "terminate_other", "allow_all"]:
            with self.subTest(policy=policy):
                self.assertEqual(sd(id="x", cron="0 * * * *", overlap_policy=policy).overlap_policy, policy)

    def test_invalid_overlap_policy(self):
        with self.assertRaises(ValidationError):
            sd(id="x", cron="0 * * * *", overlap_policy="invalid")

    def test_with_execution_timeout(self):
        self.assertEqual(sd(id="x", cron="0 * * * *", execution_timeout_seconds=300).execution_timeout_seconds, 300)

    def test_backward_compatible(self):
        """File-style dicts without the newer fields still parse."""
        d = sd(**{"id": "test-schedule", "cron": "0 18 * * 1-5", "paused": False})
        self.assertEqual(d.id, "test-schedule")
        self.assertIsNone(d.overlap_policy)
        self.assertIsNone(d.execution_timeout_seconds)

    def test_time_zone_has_no_default_in_the_model(self):
        """The default time zone is the product's setting, applied by discovery, not a value baked into the model."""
        with self.assertRaises(ValidationError):
            ScheduleDefinition(id="x", cron="0 * * * *")


# -----------------------------------------------------------------------------
# build_schedule: policy is always explicit
# -----------------------------------------------------------------------------


class BuildScheduleTest(FakeProcess):
    def test_cron_schedule_gets_default_overlap_and_catchup(self):
        schedule, memo = self.build(
            _process_config(sd(id="s", cron="0 18 * * 1-5")), sd(id="s", cron="0 18 * * 1-5"), schedule_memo=fo_style_memo
        )
        self.assertEqual(schedule.policy.overlap, ScheduleOverlapPolicy.SKIP)
        self.assertEqual(schedule.policy.catchup_window, SETTINGS.catchup_window)
        self.assertEqual(schedule.spec.cron_expressions, ["0 18 * * 1-5"])
        self.assertEqual(schedule.spec.jitter, SETTINGS.cron_jitter)
        self.assertEqual(memo["cron_expression"], "0 18 * * 1-5")
        self.assertEqual(memo["mode"], "yaml_process")
        self.assertEqual(memo["process_name"], "Test Process")
        self.assertEqual(self.loaded, [PROCESS_PATH])

    def test_process_schedule_starts_the_interpreter_with_two_arguments(self):
        schedule_def = sd(id="s", cron="0 18 * * 1-5", input_data={"k": "v"})
        schedule, _ = self.build(_process_config(schedule_def), schedule_def)
        self.assertEqual(schedule.action.workflow, "LinearProcessWorkflow")
        self.assertEqual(schedule.action.args, [self.process_def, {"k": "v"}])
        self.assertEqual(schedule.action.id, "s")
        self.assertEqual(schedule.action.task_queue, TEMPORAL.task_queue)
        self.assertEqual(schedule.spec.time_zone_name, "UTC")

    def test_default_memo(self):
        schedule_def = sd(id="s", cron="0 18 * * 1-5")
        _, memo = self.build(_process_config(schedule_def), schedule_def)
        self.assertEqual(
            memo,
            {"process_path": PROCESS_PATH, "interpreter": "LinearProcessWorkflow", "cron_expression": "0 18 * * 1-5"},
        )

    def test_default_loader_reads_from_the_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "processes").mkdir()
            (root / PROCESS_PATH).write_text("name: From Disk\nsteps: []\n")
            schedule_def = sd(id="s", cron="0 18 * * 1-5")
            schedule, _ = build_schedule(
                _process_config(schedule_def), schedule_def, ProcessSettings(project_root=root), TEMPORAL
            )
        self.assertEqual(schedule.action.args[0], {"name": "From Disk", "steps": []})

    def test_interval_schedule_gets_policy_too(self):
        schedule_def = sd(id="s", interval_seconds=30)
        schedule, memo = self.build(_process_config(schedule_def), schedule_def)
        self.assertEqual([i.every for i in schedule.spec.intervals], [timedelta(seconds=30)])
        self.assertEqual(schedule.spec.cron_expressions, [])
        self.assertEqual(schedule.policy.overlap, ScheduleOverlapPolicy.SKIP)
        self.assertEqual(schedule.policy.catchup_window, SETTINGS.catchup_window)
        self.assertEqual(memo["interval_seconds"], 30)

    def test_execution_timeout(self):
        schedule_def = sd(id="s", cron="0 * * * *", execution_timeout_seconds=300)
        schedule, _ = self.build(_process_config(schedule_def), schedule_def)
        self.assertEqual(schedule.action.execution_timeout, timedelta(seconds=300))

    def test_explicit_overlap_policy_wins(self):
        schedule_def = sd(id="s", cron="0 * * * *", overlap_policy="buffer_one")
        schedule, _ = self.build(_process_config(schedule_def), schedule_def)
        self.assertEqual(schedule.policy.overlap, ScheduleOverlapPolicy.BUFFER_ONE)

    def test_default_overlap_comes_from_settings(self):
        schedule_def = sd(id="s", cron="0 * * * *")
        settings = ProcessSettings(project_root=Path("/nonexistent"), default_overlap_policy="allow_all")
        schedule, _ = self.build(_process_config(schedule_def), schedule_def, settings)
        self.assertEqual(schedule.policy.overlap, ScheduleOverlapPolicy.ALLOW_ALL)

    def test_unknown_default_overlap_is_rejected(self):
        schedule_def = sd(id="s", cron="0 * * * *")
        settings = ProcessSettings(project_root=Path("/nonexistent"), default_overlap_policy="bogus")
        with self.assertRaisesRegex(ValueError, "bogus"):
            self.build(_process_config(schedule_def), schedule_def, settings)

    def test_jitter_and_catchup_come_from_settings(self):
        schedule_def = sd(id="s", cron="0 * * * *")
        settings = ProcessSettings(
            project_root=Path("/nonexistent"), cron_jitter=timedelta(minutes=2), catchup_window=timedelta(minutes=7)
        )
        schedule, _ = self.build(_process_config(schedule_def), schedule_def, settings)
        self.assertEqual(schedule.spec.jitter, timedelta(minutes=2))
        self.assertEqual(schedule.policy.catchup_window, timedelta(minutes=7))

    def test_requires_cron_or_interval(self):
        schedule_def = sd(id="s")
        with self.assertRaisesRegex(ValueError, "cron or interval"):
            self.build(_process_config(schedule_def), schedule_def)

    def test_direct_workflow_mode(self):
        schedule_def = sd(id="s", cron="0 * * * *", workflow_args=[{"x": 1}])
        config = WorkflowScheduleConfig(workflow_class="MyWorkflow", schedule_file_path="x.schedule.yaml", schedules=[])
        schedule, memo = self.build(config, schedule_def, schedule_memo=fo_style_memo)
        self.assertEqual(schedule.action.workflow, "MyWorkflow")
        self.assertEqual(schedule.action.args, [{"x": 1}])
        self.assertEqual(memo, {"mode": "direct", "workflow_class": "MyWorkflow", "cron_expression": "0 * * * *"})
        self.assertEqual(schedule.policy.overlap, ScheduleOverlapPolicy.SKIP)
        self.assertEqual(self.loaded, [])  # a Python workflow's schedule loads no process

    def test_direct_workflow_default_memo(self):
        schedule_def = sd(id="s", cron="0 * * * *")
        config = WorkflowScheduleConfig(workflow_class="MyWorkflow", schedule_file_path="x.schedule.yaml")
        _, memo = self.build(config, schedule_def)
        self.assertEqual(memo, {"workflow_class": "MyWorkflow", "cron_expression": "0 * * * *"})

    def test_memo_hook_sees_config_and_process_def(self):
        seen = []

        def memo_hook(config, process_def):
            seen.append((config, process_def))
            return {"k": "v"}

        schedule_def = sd(id="s", cron="0 * * * *")
        config = _process_config(schedule_def)
        schedule, memo = self.build(config, schedule_def, schedule_memo=memo_hook)
        self.assertEqual(seen, [(config, self.process_def)])
        self.assertEqual(memo, {"k": "v", "cron_expression": "0 * * * *"})
        self.assertEqual(schedule.action.memo, memo)


# -----------------------------------------------------------------------------
# initially_paused: the product decides which new schedules start paused
# -----------------------------------------------------------------------------


class InitiallyPausedHookTest(FakeProcess):
    """A product keeps some schedules from starting in a fresh environment (e.g. LLM-backed ones while unattended
    use is off). Only the create path needs it: sync never unpauses an existing schedule."""

    def hook(self, *, switch_on: bool, backed: bool):
        calls = []

        def initially_paused(config, process_def):
            calls.append((config, process_def))
            return backed and not switch_on

        return initially_paused, calls

    def test_hooked_schedule_starts_paused_when_switch_off(self):
        schedule_def = sd(id="s", cron="0 18 * * 1-5", paused=False)
        hook, calls = self.hook(switch_on=False, backed=True)
        config = _process_config(schedule_def)
        schedule, _ = self.build(config, schedule_def, initially_paused=hook)
        self.assertIs(schedule.state.paused, True)
        self.assertEqual(calls, [(config, self.process_def)])

    def test_hooked_schedule_honours_yaml_when_switch_on(self):
        schedule_def = sd(id="s", cron="0 18 * * 1-5", paused=False)
        hook, _ = self.hook(switch_on=True, backed=True)
        schedule, _ = self.build(_process_config(schedule_def), schedule_def, initially_paused=hook)
        self.assertIs(schedule.state.paused, False)

    def test_unaffected_schedule_keeps_its_state(self):
        schedule_def = sd(id="s", cron="0 18 * * 1-5", paused=False)
        hook, _ = self.hook(switch_on=False, backed=False)
        schedule, _ = self.build(_process_config(schedule_def), schedule_def, initially_paused=hook)
        self.assertIs(schedule.state.paused, False)

    def test_yaml_paused_still_wins(self):
        """paused: true in the file is honoured without consulting the hook at all."""
        schedule_def = sd(id="s", cron="0 18 * * 1-5", paused=True)
        hook, calls = self.hook(switch_on=True, backed=False)
        schedule, _ = self.build(_process_config(schedule_def), schedule_def, initially_paused=hook)
        self.assertIs(schedule.state.paused, True)
        self.assertEqual(calls, [])

    def test_workflow_schedule_hook_gets_no_process(self):
        hook, calls = self.hook(switch_on=False, backed=True)
        config = WorkflowScheduleConfig(workflow_class="MyWorkflow", schedule_file_path="x.schedule.yaml")
        schedule, _ = self.build(config, sd(id="s", cron="0 * * * *"), initially_paused=hook)
        self.assertIs(schedule.state.paused, True)
        self.assertEqual(calls, [(config, None)])


# -----------------------------------------------------------------------------
# sync_all_from_files: idempotent and convergent
# -----------------------------------------------------------------------------


class SyncConvergenceTest(FakeProcess, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.client = FakeClient()
        self.configs: list = []

    def discover(self, *configs):
        self.configs = list(configs)

    def manager(self, client=None):
        return ScheduleManager(
            client or self.client,
            SETTINGS,
            TEMPORAL,
            discover=lambda: list(self.configs),
            load_process=self.load,
            schedule_memo=fo_style_memo,
        )

    async def test_creates_missing_schedule_with_policy(self):
        self.discover(_process_config(sd(id="s1", cron="0 18 * * 1-5")))

        result = await self.manager().sync_all_from_files()

        self.assertEqual(result.created, ["s1"])
        self.assertEqual((result.updated, result.skipped, result.errors), ([], [], []))
        schedule, memo = self.client.store["s1"]
        self.assertEqual(schedule.policy.overlap, ScheduleOverlapPolicy.SKIP)
        self.assertEqual(schedule.policy.catchup_window, SETTINGS.catchup_window)
        self.assertEqual(memo["cron_expression"], "0 18 * * 1-5")

    async def test_unchanged_schedule_is_left_alone(self):
        self.discover(_process_config(sd(id="s1", cron="0 18 * * 1-5")))
        manager = self.manager()
        await manager.sync_all_from_files()

        result = await manager.sync_all_from_files()

        self.assertEqual(result.skipped, ["s1"])
        self.assertEqual((result.created, result.updated), ([], []))
        self.assertEqual(self.client.update_calls, [])
        self.assertEqual(self.client.create_calls, ["s1"])

    async def test_changed_cron_updates_in_place_preserving_paused(self):
        self.discover(_process_config(sd(id="s1", cron="0 18 * * 1-5")))
        manager = self.manager()
        await manager.sync_all_from_files()
        # Operator paused it at runtime; config then changes the cron.
        await self.client.get_schedule_handle("s1").pause(note="holiday")
        self.discover(_process_config(sd(id="s1", cron="0 19 * * 1-5")))

        result = await manager.sync_all_from_files()

        self.assertEqual(result.updated, ["s1"])
        self.assertEqual((result.created, result.skipped), ([], []))
        self.assertEqual(self.client.update_calls, ["s1"])
        schedule, _ = self.client.store["s1"]
        self.assertEqual(schedule.spec.cron_expressions, ["0 19 * * 1-5"])
        self.assertEqual(schedule.action.memo["cron_expression"], "0 19 * * 1-5")
        self.assertIs(schedule.state.paused, True)
        self.assertEqual(schedule.state.note, "holiday")

        # And converged: a third sync is a no-op.
        again = await manager.sync_all_from_files()
        self.assertEqual((again.skipped, again.updated), (["s1"], []))

    async def test_changed_process_definition_updates_args(self):
        """The process is baked into the workflow args: an edit must be detected."""
        self.discover(_process_config(sd(id="s1", cron="0 18 * * 1-5")))
        manager = self.manager()
        await manager.sync_all_from_files()

        self.process_def["steps"] = [{"id": "new_step"}]
        result = await manager.sync_all_from_files()

        self.assertEqual(result.updated, ["s1"])
        schedule, _ = self.client.store["s1"]
        self.assertEqual(schedule.action.args[0]["steps"], [{"id": "new_step"}])

    async def test_changed_overlap_policy_updates(self):
        self.discover(_process_config(sd(id="s1", cron="0 18 * * 1-5")))
        manager = self.manager()
        await manager.sync_all_from_files()

        self.discover(_process_config(sd(id="s1", cron="0 18 * * 1-5", overlap_policy="buffer_all")))
        result = await manager.sync_all_from_files()

        self.assertEqual(result.updated, ["s1"])
        self.assertEqual(self.client.store["s1"][0].policy.overlap, ScheduleOverlapPolicy.BUFFER_ALL)

    async def test_mixed_batch_reports_each_bucket(self):
        self.discover(
            _process_config(sd(id="keep", cron="0 1 * * *")),
            _process_config(sd(id="change", cron="0 2 * * *")),
        )
        manager = self.manager()
        await manager.sync_all_from_files()

        self.discover(
            _process_config(sd(id="keep", cron="0 1 * * *")),
            _process_config(sd(id="change", cron="0 3 * * *")),
            _process_config(sd(id="new", interval_seconds=60)),
        )
        result = await manager.sync_all_from_files()

        self.assertEqual(result.skipped, ["keep"])
        self.assertEqual(result.updated, ["change"])
        self.assertEqual(result.created, ["new"])
        self.assertEqual(result.errors, [])

    async def test_lost_create_race_converges_instead_of_erroring(self):
        """describe() said missing, create() said exists (another worker won): fall through to the diff/update path
        rather than report an error."""
        self.discover(_process_config(sd(id="s1", cron="0 18 * * 1-5")))
        manager = self.manager()
        await manager.sync_all_from_files()

        real_describe = FakeHandle.describe
        state = {"first": True}

        async def racy_describe(handle):
            if state["first"]:
                state["first"] = False
                raise RPCError("schedule not found", RPCStatusCode.NOT_FOUND, b"")
            return await real_describe(handle)

        self.discover(_process_config(sd(id="s1", cron="0 19 * * 1-5")))
        with mock.patch.object(FakeHandle, "describe", racy_describe):
            result = await manager.sync_all_from_files()

        self.assertEqual(result.errors, [])
        self.assertEqual(result.updated, ["s1"])
        self.assertEqual(self.client.create_calls, ["s1", "s1"])

    async def test_bad_definition_is_reported_not_raised(self):
        self.discover(
            _process_config(sd(id="broken")),  # neither cron nor interval
            _process_config(sd(id="ok", cron="0 1 * * *")),
        )
        result = await self.manager().sync_all_from_files()

        self.assertEqual(result.created, ["ok"])
        self.assertEqual([e["schedule_id"] for e in result.errors], ["broken"])

    async def test_sync_deletes_existing_retired_schedule_only(self):
        self.client.store["old-present"] = (object(), {})
        self.discover(
            ProcessScheduleConfig(
                process_path=PROCESS_PATH,
                interpreter="LinearProcessWorkflow",
                schedules=[],
                retired_schedule_ids=["old-present", "old-absent"],
            )
        )

        result = await self.manager().sync_all_from_files()

        self.assertEqual(self.client.deleted, ["old-present"])
        self.assertEqual(result.deleted, ["old-present"])
        self.assertEqual(result.errors, [])

    async def test_default_discovery_reads_the_project_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "processes").mkdir()
            (root / "processes" / "p.yaml").write_text(
                "name: P\nsteps: []\nschedules:\n  - id: from-disk\n    cron: '0 9 * * *'\n"
            )
            manager = ScheduleManager(self.client, ProcessSettings(project_root=root), TEMPORAL)
            result = await manager.sync_all_from_files()
        self.assertEqual(result.created, ["from-disk"])


# -----------------------------------------------------------------------------
# Retired ids in process files
# -----------------------------------------------------------------------------


class RetiredSchedulesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def _write(self, data: dict) -> Path:
        path = self.tmp / "proc.yaml"
        path.write_text(yaml.safe_dump(data))
        return path

    def _parse(self, path):
        return parse_process_schedules(path, SETTINGS, ScheduleDefinition)

    def test_discovery_reads_retired_schedules(self):
        path = self._write(
            {
                "name": "P",
                "steps": [],
                "retired_schedules": ["old-a", "old-b"],
                "schedules": [{"id": "new", "cron": "0 * * * *"}],
            }
        )
        config = self._parse(path)
        self.assertEqual(config.retired_schedule_ids, ["old-a", "old-b"])
        self.assertEqual([s.id for s in config.schedules], ["new"])

        # A file that only retires ids is still a config (sync must see it).
        only_retired = self._parse(self._write({"name": "P", "retired_schedules": ["x"]}))
        self.assertIsNotNone(only_retired)
        self.assertEqual(only_retired.schedules, [])

    def test_live_and_retired_clash_is_rejected(self):
        path = self._write(
            {"name": "P", "retired_schedules": ["same"], "schedules": [{"id": "same", "cron": "0 * * * *"}]}
        )
        self.assertIsNone(self._parse(path))


# -----------------------------------------------------------------------------
# Create path: same builder as the file sync
# -----------------------------------------------------------------------------


class CreateScheduleTest(FakeProcess, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.client = FakeClient()
        self.manager = ScheduleManager(
            self.client, SETTINGS, TEMPORAL, load_process=self.load, schedule_memo=fo_style_memo
        )

    async def test_create_goes_through_builder(self):
        schedule_def = ScheduleDefinition(
            id="api-1", cron="0 9 * * 1-5", timezone="Europe/London", input_data={"k": "v"}, paused=True
        )
        view = await self.manager.create_schedule(_process_config(schedule_def), schedule_def)

        schedule, memo = self.client.store["api-1"]
        self.assertEqual(schedule.policy.overlap, ScheduleOverlapPolicy.SKIP)
        self.assertEqual(schedule.policy.catchup_window, SETTINGS.catchup_window)
        self.assertEqual(schedule.spec.time_zone_name, "Europe/London")
        self.assertEqual(schedule.action.args[1], {"k": "v"})
        self.assertIs(schedule.state.paused, True)
        self.assertEqual((memo["mode"], memo["process_path"]), ("yaml_process", PROCESS_PATH))
        self.assertEqual(view.schedule_id, "api-1")
        self.assertEqual(view.cron_expression, "0 9 * * 1-5")
        self.assertEqual(view.timezone, "Europe/London")
        self.assertIs(view.is_paused, True)

    async def test_create_direct_workflow(self):
        schedule_def = sd(id="api-2", cron="0 9 * * *", workflow_args=[{"a": 1}])
        config = WorkflowScheduleConfig(workflow_class="MyWorkflow", schedule_file_path="api")
        view = await self.manager.create_schedule(config, schedule_def)
        schedule, memo = self.client.store["api-2"]
        self.assertEqual(schedule.action.workflow, "MyWorkflow")
        self.assertEqual(schedule.action.args, [{"a": 1}])
        self.assertEqual(memo["mode"], "direct")
        self.assertEqual(view.memo["mode"], "direct")

    async def test_view_of_an_interval_schedule(self):
        schedule_def = sd(id="api-3", interval_seconds=90)
        view = await self.manager.create_schedule(_process_config(schedule_def), schedule_def)
        self.assertEqual(view.interval_seconds, 90)
        self.assertIsNone(view.cron_expression)
        self.assertEqual(view.timezone, SETTINGS.default_timezone)  # no time zone on an interval spec

    async def test_list_describes_each(self):
        for sid in ("a", "b"):
            d = sd(id=sid, cron="0 1 * * *")
            await self.manager.create_schedule(_process_config(d), d)
        views = await self.manager.list_schedules()
        self.assertEqual([v.schedule_id for v in views], ["a", "b"])
        self.assertEqual(views[0].memo["process_name"], "Test Process")


# -----------------------------------------------------------------------------
# get_schedule_runs: each run reports its real outcome, never assumed success
# -----------------------------------------------------------------------------


class _RunsClient:
    """A schedule with recent actions whose executions describe as given."""

    def __init__(self, executions):
        # executions: list of (workflow_id, WorkflowExecutionStatus | None | Exception, close_time)
        self._executions = {wf: (status, close) for wf, status, close in executions}
        started = datetime(2026, 10, 1, tzinfo=timezone.utc)
        actions = [
            SimpleNamespace(
                started_at=started + timedelta(hours=i),
                action=ScheduleActionExecutionStartWorkflow(workflow_id=wf, first_execution_run_id=f"run-{wf}"),
            )
            for i, (wf, _, _) in enumerate(executions)
        ]
        self._desc = SimpleNamespace(info=SimpleNamespace(recent_actions=actions))

    def get_schedule_handle(self, schedule_id):
        desc = self._desc

        class _H:
            async def describe(self):
                return desc

        return _H()

    def get_workflow_handle(self, workflow_id, run_id=None):
        status, close = self._executions[workflow_id]

        class _W:
            async def describe(self):
                if isinstance(status, Exception):
                    raise status
                return SimpleNamespace(status=status, close_time=close)

        return _W()


class ScheduleRunsTest(unittest.IsolatedAsyncioTestCase):
    async def test_runs_carry_real_status_and_close_time(self):
        closed = datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc)
        client = _RunsClient(
            [
                ("ok", WorkflowExecutionStatus.COMPLETED, closed),
                ("bad", WorkflowExecutionStatus.FAILED, closed),
                ("live", WorkflowExecutionStatus.RUNNING, None),
                ("gone", RuntimeError("describe failed"), None),
            ]
        )
        runs = await ScheduleManager(client, SETTINGS, TEMPORAL).get_schedule_runs("s", limit=10)
        by_id = {r.workflow_id: r for r in runs}
        self.assertEqual(by_id["ok"].status, WorkflowExecutionStatus.COMPLETED)
        self.assertEqual(by_id["ok"].completed_at, closed)
        self.assertEqual(by_id["ok"].run_id, "run-ok")
        self.assertEqual(by_id["bad"].status, WorkflowExecutionStatus.FAILED)
        self.assertEqual(by_id["live"].status, WorkflowExecutionStatus.RUNNING)
        self.assertIsNone(by_id["live"].completed_at)
        # An unreadable run is not reported as a success
        self.assertIsNone(by_id["gone"].status)
        # Newest first
        self.assertEqual([r.workflow_id for r in runs], ["gone", "live", "bad", "ok"])

    async def test_limit_takes_the_newest(self):
        client = _RunsClient([(f"w{i}", WorkflowExecutionStatus.COMPLETED, None) for i in range(5)])
        runs = await ScheduleManager(client, SETTINGS, TEMPORAL).get_schedule_runs("s", limit=2)
        self.assertEqual([r.workflow_id for r in runs], ["w4", "w3"])
        settings = ProcessSettings(project_root=Path("/nonexistent"), runs_limit=3)
        runs = await ScheduleManager(client, settings, TEMPORAL).get_schedule_runs("s")
        self.assertEqual(len(runs), 3)

    async def test_missing_schedule_is_a_value_error(self):
        class _Missing:
            def get_schedule_handle(self, schedule_id):
                class _H:
                    async def describe(self):
                        raise RPCError("schedule not found", RPCStatusCode.NOT_FOUND, b"")

                return _H()

        with self.assertRaisesRegex(ValueError, "not found"):
            await ScheduleManager(_Missing(), SETTINGS, TEMPORAL).get_schedule_runs("s")


# -----------------------------------------------------------------------------
# Against a real server: describe hands back compiled calendars and raw payloads
# -----------------------------------------------------------------------------


def _config(schedule_id: str, cron: str, args: list) -> WorkflowScheduleConfig:
    return WorkflowScheduleConfig(
        workflow_class="ScheduleSyncProbeWorkflow",
        schedule_file_path="probe.schedule.yaml",
        schedules=[ScheduleDefinition(id=schedule_id, cron=cron, timezone="Europe/London", workflow_args=args)],
    )


class SyncOnServerTest(unittest.IsolatedAsyncioTestCase):
    """Schedules need the dev server (the time-skipping one has no schedule API)."""

    async def asyncSetUp(self):
        from temporalio.testing import WorkflowEnvironment

        self.env = await WorkflowEnvironment.start_local(data_converter=data_converter())
        self.addAsyncCleanup(self.env.shutdown)

    async def test_sync_creates_then_converges_on_real_server(self):
        client = self.env.client
        schedule_id = f"test-sync-{uuid.uuid4()}"
        configs = [_config(schedule_id, "0 18 * * 1-5", [{"x": 1}])]
        manager = ScheduleManager(client, SETTINGS, TEMPORAL, discover=lambda: configs)
        handle = client.get_schedule_handle(schedule_id)
        try:
            created = await manager.sync_all_from_files()
            self.assertIn(schedule_id, created.created)

            # Same config again: nothing to do (cron/args/memo/policy all round-trip).
            unchanged = await manager.sync_all_from_files()
            self.assertIn(schedule_id, unchanged.skipped)
            self.assertNotIn(schedule_id, unchanged.updated)

            # Operator pauses; config then changes cron AND args.
            await handle.pause(note="holiday")
            configs[:] = [_config(schedule_id, "0 19 * * 1-5", [{"x": 2}])]
            updated = await manager.sync_all_from_files()
            self.assertIn(schedule_id, updated.updated)

            desc = await handle.describe()
            self.assertIs(desc.schedule.state.paused, True)
            self.assertEqual(desc.schedule.state.note, "holiday")
            args = await client.data_converter.decode(list(desc.schedule.action.args))
            self.assertEqual(args, [{"x": 2}])

            # Readers see the new cadence even though the schedule-level memo is frozen.
            view = await manager.get_schedule(schedule_id)
            self.assertEqual(view.cron_expression, "0 19 * * 1-5")
            self.assertIs(view.is_paused, True)
            self.assertEqual(view.timezone, "Europe/London")
            listed = {s.schedule_id: s for s in await manager.list_schedules()}
            self.assertEqual(listed[schedule_id].cron_expression, "0 19 * * 1-5")

            # Converged.
            again = await manager.sync_all_from_files()
            self.assertIn(schedule_id, again.skipped)
        finally:
            await handle.delete()


if __name__ == "__main__":
    unittest.main()
