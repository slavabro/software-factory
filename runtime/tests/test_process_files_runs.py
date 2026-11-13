"""Process files and on-demand runs: loading a file, the one rule that picks a process's interpreter, discovering
processes and schedules, starting or joining a run and reading how it stands, and the package boundary (the
workflow-side package imports no client code)."""

import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from factory_runtime.process.files import (
    discover_processes,
    discover_schedules,
    load_process,
    parse_process_schedules,
    process_interpreter,
)
from factory_runtime.process.runs import RunState, describe_process_run, start_process_run
from factory_runtime.process.schedules import ProcessScheduleConfig, ScheduleDefinition, WorkflowScheduleConfig
from factory_runtime.process.settings import ProcessSettings
from factory_runtime.temporal import TemporalSettings

DEFAULT = "LinearProcessWorkflow"
TEMPORAL = TemporalSettings(task_queue="test-queue")


class TempRoot(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = ProcessSettings(project_root=self.root, default_timezone="Europe/London")

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


# -----------------------------------------------------------------------------
# One rule picks the interpreter, for every way a process starts
# -----------------------------------------------------------------------------


class ProcessInterpreterTest(TempRoot):
    def test_process_interpreter(self):
        cases = [
            ({"interpreter": "ConditionalProcessWorkflow"}, "ConditionalProcessWorkflow"),
            ({}, DEFAULT),
            ({"interpreter": None}, DEFAULT),
            ({"interpreter": ""}, DEFAULT),
        ]
        for process_def, expected in cases:
            with self.subTest(process_def=process_def):
                self.assertEqual(process_interpreter(process_def, DEFAULT), expected)

    def test_schedule_discovery_uses_the_declared_interpreter(self):
        path = self.write(
            "p.yaml",
            "name: P\ninterpreter: ConditionalProcessWorkflow\nsteps: []\n"
            "schedules:\n  - id: p-daily\n    cron: '0 9 * * *'\n",
        )
        self.assertEqual(
            parse_process_schedules(path, self.settings, ScheduleDefinition).interpreter, "ConditionalProcessWorkflow"
        )

    def test_schedule_discovery_falls_back_to_the_default_interpreter(self):
        path = self.write("p.yaml", "name: P\nsteps: []\nschedules:\n  - id: p-daily\n    cron: '0 9 * * *'\n")
        settings = ProcessSettings(project_root=self.root, default_interpreter="HybridProcessWorkflow")
        self.assertEqual(parse_process_schedules(path, settings, ScheduleDefinition).interpreter, "HybridProcessWorkflow")


# -----------------------------------------------------------------------------
# load_process
# -----------------------------------------------------------------------------


class LoadProcessTest(TempRoot):
    def test_loads_relative_to_the_root(self):
        self.write("processes/p.yaml", "name: P\nsteps:\n  - id: a\n    type: activity\n")
        self.assertEqual(
            load_process("processes/p.yaml", self.root), {"name": "P", "steps": [{"id": "a", "type": "activity"}]}
        )
        self.assertEqual(load_process(Path("processes/p.yaml"), self.root)["name"], "P")

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_process("processes/missing.yaml", self.root)

    def test_empty_file_raises(self):
        self.write("processes/empty.yaml", "  \n\n")
        with self.assertRaisesRegex(ValueError, "empty or contains only whitespace"):
            load_process("processes/empty.yaml", self.root)

    def test_file_that_parses_to_nothing_raises(self):
        self.write("processes/null.yaml", "# only a comment\n")
        with self.assertRaisesRegex(ValueError, "parsed to None"):
            load_process("processes/null.yaml", self.root)


# -----------------------------------------------------------------------------
# Discovery
# -----------------------------------------------------------------------------


class DiscoverProcessesTest(TempRoot):
    def test_finds_processes_in_path_order(self):
        self.write("processes/b_second.yaml", "name: Second\ndescription: two\nsteps: []\n")
        self.write("processes/a_first_one.yaml", "steps: []\n")
        self.write("processes/empty.yaml", "")
        self.write("processes/broken.yaml", "name: [unclosed\n")
        self.write("elsewhere/x.yaml", "name: Not Matched\n")

        with self.assertLogs("factory_runtime.process.files", "WARNING") as logs:
            found = discover_processes(self.settings)

        self.assertEqual([p.path for p in found], ["processes/a_first_one.yaml", "processes/b_second.yaml"])
        first, second = found
        self.assertEqual((first.name, first.description), ("A First One", ""))
        self.assertEqual((second.name, second.description), ("Second", "two"))
        self.assertEqual(second.definition, {"name": "Second", "description": "two", "steps": []})
        self.assertTrue(any("broken.yaml" in m for m in logs.output))

    def test_glob_comes_from_settings(self):
        self.write("org/a/processes/p.yaml", "name: P\n")
        settings = ProcessSettings(project_root=self.root, process_glob="org/*/processes/*.yaml")
        self.assertEqual([p.path for p in discover_processes(settings)], ["org/a/processes/p.yaml"])


class ProductScheduleDefinition(ScheduleDefinition):
    """A product's schedule model with a field of its own."""

    rerunnable: bool = False


class DiscoverSchedulesTest(TempRoot):
    def setUp(self):
        super().setUp()
        self.write(
            "processes/p.yaml",
            "name: P\nsteps: []\nschedules:\n"
            "  - id: p-daily\n    cron: '0 9 * * *'\n    rerunnable: true\n"
            "  - id: p-tokyo\n    cron: '0 9 * * *'\n    timezone: Asia/Tokyo\n",
        )
        self.write("processes/retired_only.yaml", "name: R\nsteps: []\nretired_schedules: [old-id]\n")
        self.write("processes/no_schedules.yaml", "name: N\nsteps: []\n")
        self.write(
            "workflows/w.schedule.yaml",
            "workflow_class: MyWorkflow\nschedules:\n  - id: w-hourly\n    cron: '0 * * * *'\n"
            "    workflow_args: [{x: 1}]\n",
        )
        self.write("workflows/no_class.schedule.yaml", "schedules:\n  - id: lost\n    cron: '0 * * * *'\n")

    def by_id(self, configs):
        return {s.id: (c, s) for c in configs for s in c.schedules}

    def test_default_time_zone_is_applied_at_load_time(self):
        found = self.by_id(discover_schedules(self.settings))
        self.assertEqual(found["p-daily"][1].timezone, "Europe/London")
        self.assertEqual(found["p-tokyo"][1].timezone, "Asia/Tokyo")
        self.assertEqual(found["w-hourly"][1].timezone, "Europe/London")

    def test_process_and_workflow_configs(self):
        configs = discover_schedules(self.settings)
        found = self.by_id(configs)
        self.assertEqual(set(found), {"p-daily", "p-tokyo", "w-hourly"})
        config, _ = found["p-daily"]
        self.assertIsInstance(config, ProcessScheduleConfig)
        self.assertEqual(config.process_path, str(self.root / "processes/p.yaml"))
        self.assertEqual(config.interpreter, DEFAULT)
        wconfig, w = found["w-hourly"]
        self.assertIsInstance(wconfig, WorkflowScheduleConfig)
        self.assertEqual(wconfig.workflow_class, "MyWorkflow")
        self.assertEqual(w.workflow_args, [{"x": 1}])
        # A file that only retires ids is still a config; one with neither is not.
        retired = [c for c in configs if isinstance(c, ProcessScheduleConfig) and c.retired_schedule_ids]
        self.assertEqual([c.retired_schedule_ids for c in retired], [["old-id"]])
        self.assertEqual(len(configs), 3)

    def test_schedule_model_subclass(self):
        found = self.by_id(discover_schedules(self.settings, ProductScheduleDefinition))
        daily = found["p-daily"][1]
        self.assertIsInstance(daily, ProductScheduleDefinition)
        self.assertIs(daily.rerunnable, True)
        self.assertIs(found["w-hourly"][1].rerunnable, False)

    def test_base_model_ignores_product_fields(self):
        found = self.by_id(discover_schedules(self.settings))
        self.assertNotIn("rerunnable", found["p-daily"][1].model_dump())


# -----------------------------------------------------------------------------
# On-demand runs
# -----------------------------------------------------------------------------

STARTED = datetime(2026, 10, 27, 14, 0, tzinfo=UTC)
CLOSED = datetime(2026, 10, 27, 14, 1, tzinfo=UTC)
INPUT = {"asset_id": "a1", "start_date": "2026-10-01", "end_date": "2026-10-02"}
PROCESS_DEF = {"name": "Refetch", "interpreter": "ConditionalProcessWorkflow", "metadata": {"department": "data"}}
METADATA = {"department": "data", "process_name": "Refetch"}
SETTINGS = ProcessSettings(project_root=Path("/nonexistent"))


def _client(start_error=None, describe=None, result=None) -> MagicMock:
    client = MagicMock()
    client.start_workflow = AsyncMock(side_effect=start_error)
    handle = MagicMock()
    handle.describe = AsyncMock(**describe) if describe else AsyncMock()
    handle.result = AsyncMock(return_value=result)
    client.get_workflow_handle = MagicMock(return_value=handle)
    return client


async def _start(client, process_def=PROCESS_DEF, **kwargs):
    await start_process_run(
        client,
        process_def,
        INPUT,
        "run-1",
        settings=SETTINGS,
        temporal=TEMPORAL,
        metadata=METADATA,
        execution_timeout=timedelta(minutes=7),
        **kwargs,
    )


class ProcessRunsTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_uses_interpreter_and_joins(self):
        client = _client()
        memo = {"mode": "on_demand", "department": "data", "process_name": "Refetch", "incident_id": "i1"}

        await _start(client, summary="A summary", memo=memo)

        call = client.start_workflow.await_args
        self.assertEqual(call.args[0], process_interpreter(PROCESS_DEF, SETTINGS.default_interpreter))
        self.assertEqual(call.args[0], "ConditionalProcessWorkflow")
        self.assertEqual(call.kwargs["args"], [PROCESS_DEF, INPUT, METADATA])
        self.assertEqual(call.kwargs["id"], "run-1")
        self.assertEqual(call.kwargs["task_queue"], TEMPORAL.task_queue)
        self.assertEqual(call.kwargs["id_conflict_policy"], WorkflowIDConflictPolicy.USE_EXISTING)
        self.assertEqual(call.kwargs["id_reuse_policy"], WorkflowIDReusePolicy.ALLOW_DUPLICATE)
        self.assertEqual(call.kwargs["execution_timeout"], timedelta(minutes=7))
        self.assertEqual(call.kwargs["memo"], memo)
        self.assertEqual(call.kwargs["static_summary"], "A summary")

    async def test_metadata_is_the_third_argument_exactly(self):
        client = _client()
        await _start(client, process_def={"name": "No Interpreter"})
        call = client.start_workflow.await_args
        self.assertEqual(call.args[0], SETTINGS.default_interpreter)
        self.assertEqual(call.kwargs["args"][2], {"department": "data", "process_name": "Refetch"})
        self.assertIsNone(call.kwargs["memo"])

    async def test_rerun_closed_false_counts_closed_as_started(self):
        closed = WorkflowAlreadyStartedError("run-1", "LinearProcessWorkflow")

        client = _client(start_error=closed)
        await _start(client, rerun_closed=False)  # swallowed: it ran once
        self.assertEqual(client.start_workflow.await_args.kwargs["id_reuse_policy"], WorkflowIDReusePolicy.REJECT_DUPLICATE)

        with self.assertRaises(WorkflowAlreadyStartedError):
            await _start(_client(start_error=closed), rerun_closed=True)

    async def test_describe_maps_states(self):
        cases = [
            (WorkflowExecutionStatus.RUNNING, RunState.RUNNING),
            (WorkflowExecutionStatus.COMPLETED, RunState.COMPLETED),
            (WorkflowExecutionStatus.CONTINUED_AS_NEW, RunState.COMPLETED),
            (WorkflowExecutionStatus.FAILED, RunState.FAILED),
            (WorkflowExecutionStatus.TERMINATED, RunState.FAILED),
            (WorkflowExecutionStatus.TIMED_OUT, RunState.FAILED),
            (WorkflowExecutionStatus.CANCELED, RunState.FAILED),
            (None, RunState.FAILED),  # unreadable: never assumed successful
        ]
        for temporal, expected in cases:
            with self.subTest(temporal=temporal):
                desc = SimpleNamespace(status=temporal, start_time=STARTED, close_time=CLOSED)
                results = {"refetch": {"written_count": 3}}
                client = _client(describe={"return_value": desc}, result=results)

                run = await describe_process_run(client, "run-1")

                handle = client.get_workflow_handle.return_value
                client.get_workflow_handle.assert_called_once_with("run-1")
                self.assertEqual(run.state, expected)
                self.assertEqual((run.workflow_id, run.started_at, run.closed_at), ("run-1", STARTED, CLOSED))
                if expected == RunState.COMPLETED:
                    self.assertEqual(run.results, results)
                else:  # never blocks on a running run, never reads a failed one's result
                    handle.result.assert_not_awaited()
                    self.assertEqual(run.results, {})

    async def test_describe_not_found_is_none_other_rpc_errors_raise(self):
        missing = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        run = await describe_process_run(_client(describe={"side_effect": missing}), "run-1")
        self.assertEqual(run.state, RunState.NONE)
        self.assertEqual(run.results, {})

        down = RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")
        with self.assertRaises(RPCError):
            await describe_process_run(_client(describe={"side_effect": down}), "run-1")


# -----------------------------------------------------------------------------
# Package boundary
# -----------------------------------------------------------------------------


class PackageBoundaryTest(unittest.TestCase):
    def test_workflow_package_imports_no_client_code(self):
        """The sandbox and the strict import policy must see only workflow code."""
        probe = (
            "import sys, factory_runtime.process\n"
            "client = ['factory_runtime.process.files', 'factory_runtime.process.schedules',"
            " 'factory_runtime.process.runs', 'factory_runtime.process.settings', 'yaml']\n"
            "print(','.join(m for m in client if m in sys.modules))\n"
        )
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
