"""The replay check and the patch report against a real local server: runs start under one release, the worker
stops, and the check (or report) runs with the next release's definition, as a deploy would."""

import asyncio
import contextlib
import io
import os
import subprocess
import sys
import unittest
import uuid
from typing import Callable

from temporalio.api.common.v1 import WorkflowExecution as ExecutionRef
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import PollWorkflowTaskQueueRequest, ResetWorkflowExecutionRequest
from temporalio.client import Client, Schedule, ScheduleActionStartWorkflow, ScheduleSpec, ScheduleState
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner

from factory_runtime.temporal import build_workflow_runner, data_converter
from factory_runtime.temporal.sandbox import STRICT_IMPORT_POLICY
from factory_runtime.temporal.patches import patch_report
from factory_runtime.temporal.patches import main as patches_main
from factory_runtime.temporal.replay_check import (
    MISSING_ACTIVITY,
    MISSING_HANDLER,
    NONDETERMINISM,
    OTHER_QUEUE,
    SCHEDULE_TYPE,
    UNREGISTERED_TYPE,
    ReplayReport,
    check_open_runs,
)
from factory_runtime.temporal.replay_check import main as check_main
from factory_runtime.testing import TEST_PASSTHROUGH_MODULES, query_eventually
from tests.patch_import_workflows import VALUE_PATCH, ValueDeprecated, ValueOld, ValuePatched
from tests.replay_workflows import (
    ACTIVITIES,
    PATCH,
    QUEUE,
    Billing,
    OrderDeprecated,
    OrderNoNudge,
    OrderPatched,
    OrderPatchRemoved,
    OrderReordered,
    OrderStamped,
    OrderV1,
    definition,
    reserve,
    reserve_v2,
    ship,
)

VISIBILITY_SECONDS = 10


class LocalServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.env = await WorkflowEnvironment.start_local(data_converter=data_converter())
        self.addAsyncCleanup(self.env.shutdown)
        self.client: Client = self.env.client
        self.server = self.client.service_client.config.target_host
        # Workers connect as a product's do: the test environment's client carries a worker interceptor of its own,
        # which the replayer wouldn't have, so `WorkerDefinition.worker` refuses it.
        self.worker_client = await definition(OrderV1).connect(self.server)

    async def start(self, workflow_class: type, *, go: bool, nudge: bool = False, **kwargs: object) -> str:
        """Start an order under the release `workflow_class`, optionally let it reserve; returns the run id once the
        worker has stopped."""
        defn = definition(workflow_class, Billing, **kwargs)  # type: ignore[arg-type]
        async with defn.worker(self.worker_client, activities=ACTIVITIES):
            handle = await self.client.start_workflow("Order", id=f"order-{uuid.uuid4()}", task_queue=QUEUE)
            await query_eventually(handle, "current_stage", lambda s: s == "waiting")
            if nudge:
                await handle.signal("nudge")
            if go:
                await handle.signal("go")
                await query_eventually(handle, "current_stage", lambda s: s == "reserved")
            elif nudge:
                await query_eventually(handle, "current_stage", lambda s: s == "waiting")
        assert handle.result_run_id is not None
        return handle.result_run_id

    async def visible(self, count: int) -> None:
        """Wait until visibility lists `count` open runs."""
        deadline = asyncio.get_running_loop().time() + VISIBILITY_SECONDS
        while True:
            n = len([r async for r in self.client.list_workflows("ExecutionStatus = 'Running'")])
            if n == count or asyncio.get_running_loop().time() > deadline:
                self.assertEqual(n, count)
                return
            await asyncio.sleep(0.2)

    async def terminate(self, run_id: str) -> None:
        async for run in self.client.list_workflows("ExecutionStatus = 'Running'"):
            if run.run_id == run_id:
                await self.client.get_workflow_handle(run.id, run_id=run_id).terminate("test")

    async def check(self, *workflows: type, **kwargs: object) -> ReplayReport:
        return await check_open_runs(self.client, definition(*workflows, **kwargs))  # type: ignore[arg-type]

    def kinds(self, report: ReplayReport) -> list[tuple[str, str]]:
        return sorted((f.kind, f.run_id) for f in report.failures)

    async def main(self, main: Callable[[list[str]], int], spec: str) -> tuple[int, str]:
        """A CLI's main in a thread (it runs its own event loop), with its output."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = await asyncio.to_thread(main, [spec, "--server", self.server])
        return code, out.getvalue()

    def cli(self, module: str, spec: str) -> subprocess.CompletedProcess[str]:
        """The CLI in its own process, from the directory holding `tests`."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run(
            [sys.executable, "-m", module, spec, "--server", self.server],
            cwd=here,
            capture_output=True,
            text=True,
            timeout=120,
        )


class ReplayCheckTest(LocalServerTest):
    async def test_open_runs_that_replay_pass(self) -> None:
        await self.start(OrderV1, go=True)
        await self.start(OrderV1, go=False)
        await self.visible(2)
        report = await self.check(OrderV1)
        self.assertEqual(report.failures, [])
        self.assertEqual(report.checked, {"Order": 2})
        self.assertIn("Replay check PASS", report.render())
        code, out = await self.main(check_main, "tests.replay_workflows:CURRENT")
        self.assertEqual((code, out.splitlines()[0]), (0, "Replay check PASS"))

    async def test_a_code_change_that_alters_commands_is_nondeterminism(self) -> None:
        past = await self.start(OrderV1, go=True)
        await self.start(OrderV1, go=False)  # not yet at the changed step: replays
        await self.visible(2)
        report = await self.check(OrderReordered)
        self.assertEqual(self.kinds(report), [(NONDETERMINISM, past)])
        self.assertEqual(report.nondeterminism[0].workflow_type, "Order")
        self.assertIn("Nondeterminism", report.nondeterminism[0].error)
        self.assertEqual(report.other_failures, [])
        done = self.cli("factory_runtime.temporal.replay_check", "tests.replay_workflows:REORDERED")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("FAIL", done.stdout)
        self.assertIn(past, done.stdout)

    async def test_an_open_run_of_a_type_no_longer_registered_fails(self) -> None:
        await self.start(OrderV1, go=False)
        handle = await self.client.start_workflow("Legacy", id="legacy-1", task_queue=QUEUE)
        await self.visible(2)
        report = await self.check(OrderV1)
        self.assertEqual(self.kinds(report), [(UNREGISTERED_TYPE, handle.result_run_id)])
        self.assertEqual(report.checked, {"Legacy": 1, "Order": 1})

    async def test_a_pending_activity_whose_type_was_removed_fails(self) -> None:
        async with definition(OrderV1, Billing).worker(self.worker_client, activities=ACTIVITIES):
            handle = await self.client.start_workflow(Billing.run, id="billing-1", task_queue=QUEUE)
            for _ in range(50):  # until the activity is scheduled
                if (await handle.describe()).raw_description.pending_activities:
                    break
                await asyncio.sleep(0.1)
        await self.visible(1)
        self.assertEqual((await self.check(OrderV1, Billing, external=frozenset({"charge"}))).failures, [])
        report = await self.check(OrderV1, Billing)
        self.assertEqual(self.kinds(report), [(MISSING_ACTIVITY, handle.result_run_id)])
        self.assertIn("'charge'", report.failures[0].error)

    async def test_a_schedule_whose_workflow_type_was_removed_fails(self) -> None:
        for schedule_id, workflow_type, queue in (
            ("orders", "Order", QUEUE),
            ("gone", "Gone", QUEUE),
            ("theirs", "Gone", "another-worker"),
        ):
            await self.client.create_schedule(
                schedule_id,
                Schedule(
                    action=ScheduleActionStartWorkflow(workflow_type, id=f"{schedule_id}-run", task_queue=queue),
                    spec=ScheduleSpec(intervals=[]),
                    state=ScheduleState(paused=True),
                ),
            )
        report = await self.check(OrderV1)
        self.assertEqual(report.schedules_checked, 2)
        failures = [(f.kind, f.workflow_id, f.workflow_type) for f in report.failures]
        self.assertEqual(failures, [(SCHEDULE_TYPE, "gone", "Gone")])

    async def test_a_removed_signal_handler_that_a_run_received_fails(self) -> None:
        nudged = await self.start(OrderV1, go=False, nudge=True)
        await self.start(OrderV1, go=True)
        await self.visible(2)
        report = await self.check(OrderNoNudge)
        self.assertEqual(self.kinds(report), [(MISSING_HANDLER, nudged)])
        self.assertIn("'nudge'", report.failures[0].error)

    async def test_runs_on_queues_the_worker_does_not_serve_are_reported(self) -> None:
        await self.client.start_workflow("Unrelated", id="unrelated-1", task_queue="another-worker")
        stray = await self.client.start_workflow("Order", id="stray-1", task_queue="old-queue")
        await self.visible(2)
        report = await self.check(OrderV1)
        self.assertEqual(report.other_queues, {"another-worker": {"Unrelated": 1}, "old-queue": {"Order": 1}})
        self.assertEqual(self.kinds(report), [(OTHER_QUEUE, stray.result_run_id)])
        self.assertEqual(report.checked, {})

        # The patch report leaves that run out too, and so isn't complete.
        patches = await patch_report(self.client, definition(OrderV1))
        self.assertFalse(patches.complete)
        self.assertEqual([(f.kind, f.run_id) for f in patches.failures], [(OTHER_QUEUE, stray.result_run_id)])

    async def test_a_schedule_on_another_queue_that_starts_a_registered_type_fails(self) -> None:
        for schedule_id, workflow_type, queue in (("stray", "Order", "old-queue"), ("theirs", "Gone", "elsewhere")):
            await self.client.create_schedule(
                schedule_id,
                Schedule(
                    action=ScheduleActionStartWorkflow(workflow_type, id=f"{schedule_id}-run", task_queue=queue),
                    spec=ScheduleSpec(intervals=[]),
                    state=ScheduleState(paused=True),
                ),
            )
        report = await self.check(OrderV1)
        self.assertEqual([(f.kind, f.workflow_id, f.workflow_type) for f in report.failures], [
            (OTHER_QUEUE, "stray", "Order")
        ])

    async def test_a_removed_local_activity_fails(self) -> None:
        stamped = await self.start(OrderStamped, go=True)
        await self.start(OrderStamped, go=False)  # not yet at the local activity: fine either way
        await self.visible(2)
        self.assertEqual((await self.check(OrderStamped)).failures, [])
        without_stamp = [reserve, reserve_v2, ship]
        report = await self.check(OrderStamped, activities=without_stamp)
        self.assertEqual(self.kinds(report), [(MISSING_ACTIVITY, stamped)])
        self.assertIn("local activity 'stamp'", report.failures[0].error)
        # A local activity runs on this worker: another worker serving the type doesn't help.
        report = await self.check(OrderStamped, activities=without_stamp, external=frozenset({"stamp"}))
        self.assertEqual(self.kinds(report), [(MISSING_ACTIVITY, stamped)])

    async def test_a_local_activity_in_flight_at_the_end_of_the_history_is_checked(self) -> None:
        """A worker took the task that starts the local activity and died: the history ends there, with no marker."""
        run_id = await self.start(OrderStamped, go=False)
        handle = self.client.get_workflow_handle([r async for r in self.client.list_workflows()][0].id, run_id=run_id)
        await handle.signal("go")
        task = await self.client.workflow_service.poll_workflow_task_queue(
            PollWorkflowTaskQueueRequest(namespace=self.client.namespace, task_queue=TaskQueue(name=QUEUE))
        )
        self.assertEqual(task.workflow_execution.run_id, run_id)
        await self.visible(1)
        report = await self.check(OrderStamped, activities=[reserve, reserve_v2, ship])
        self.assertEqual(self.kinds(report), [(MISSING_ACTIVITY, run_id)])
        self.assertIn("local activity 'stamp'", report.failures[0].error)

    async def test_a_reset_run_is_checked_under_its_own_run_id(self) -> None:
        old = await self.start(OrderV1, go=True)
        handle = self.client.get_workflow_handle(
            [r async for r in self.client.list_workflows("ExecutionStatus = 'Running'")][0].id, run_id=old
        )
        history = await handle.fetch_history()
        last_completed = [e.event_id for e in history.events if e.HasField("workflow_task_completed_event_attributes")]
        response = await self.client.workflow_service.reset_workflow_execution(
            ResetWorkflowExecutionRequest(
                namespace=self.client.namespace,
                workflow_execution=ExecutionRef(workflow_id=handle.id, run_id=old),
                reason="test",
                workflow_task_finish_event_id=last_completed[-1],
                request_id=uuid.uuid4().hex,
            )
        )
        reset = response.run_id
        self.assertNotEqual(reset, old)
        await self.visible(1)

        report = await self.check(OrderV1)
        self.assertEqual(report.failures, [])
        self.assertEqual(report.checked, {"Order": 1})
        self.assertEqual(self.kinds(await self.check(OrderReordered)), [(NONDETERMINISM, reset)])
        patches = await patch_report(self.client, definition(OrderPatched))
        self.assertTrue(patches.complete)
        self.assertEqual([r.run_id for r in patches.patches[PATCH].old_side], [reset])


class PatchLifecycleTest(LocalServerTest):
    async def test_a_patch_from_introduction_to_removal(self) -> None:
        old_side = await self.start(OrderV1, go=True)  # passed the point on the old code
        not_reached = await self.start(OrderV1, go=False)
        marked = await self.start(OrderPatched, go=True)  # took the new side: non-deprecated marker
        await self.visible(3)

        # Patched: every run replays; the report names the run on the old side and the marked one.
        self.assertEqual((await self.check(OrderPatched)).failures, [])
        report = await patch_report(self.client, definition(OrderPatched))
        self.assertTrue(report.complete)
        self.assertEqual(report.runs_checked, 3)
        status = report.patches[PATCH]
        self.assertEqual([r.run_id for r in status.old_side], [old_side])
        self.assertEqual([r.run_id for r in status.marked], [marked])
        self.assertEqual(status.others, 1)
        self.assertFalse(status.can_deprecate)
        code, out = await self.main(patches_main, "tests.replay_workflows:patched_definition")
        self.assertEqual(code, 0)
        self.assertIn(f"{PATCH}: old side 1, non-deprecated marker 1, others 1 -> keep patched()", out)

        # Deprecating while a run is still on the old side fails the check.
        self.assertEqual(self.kinds(await self.check(OrderDeprecated)), [(NONDETERMINISM, old_side)])

        # Once that run is gone the patch may be deprecated but not removed: a run carries its marker.
        await self.terminate(old_side)
        await self.visible(2)
        status = (await patch_report(self.client, definition(OrderPatched))).patches[PATCH]
        self.assertTrue(status.can_deprecate)
        self.assertFalse(status.can_remove)
        self.assertEqual((await self.check(OrderDeprecated)).failures, [])
        self.assertEqual(self.kinds(await self.check(OrderPatchRemoved)), [(NONDETERMINISM, marked)])

        # A run under the deprecated release carries a deprecated marker: removal is safe for it.
        await self.terminate(marked)
        await self.start(OrderDeprecated, go=True)
        await self.visible(2)
        report = await patch_report(self.client, definition(OrderDeprecated))
        self.assertTrue(report.patches[PATCH].can_remove)
        self.assertEqual((await self.check(OrderPatchRemoved)).failures, [])
        self.assertNotIn(not_reached, [f.run_id for f in report.failures])


class ValueOnlyPatchTest(LocalServerTest):
    """A patch that changes only a value: replay can't block a premature deprecation (no command differs), so the
    patch report is the gate, and it must see `patched` however the workflow module imports it and runs."""

    RUNNERS = {
        "sandboxed": lambda: build_workflow_runner(TEST_PASSTHROUGH_MODULES, import_policy=STRICT_IMPORT_POLICY),
        "passed through": lambda: build_workflow_runner(
            (*TEST_PASSTHROUGH_MODULES, "tests.patch_import_workflows"), import_policy=STRICT_IMPORT_POLICY
        ),
        "unsandboxed": UnsandboxedWorkflowRunner,
    }

    async def test_the_patch_report_keeps_a_value_only_patch_with_a_run_on_its_old_side(self) -> None:
        old_side = await self.start(ValueOld, go=True)
        await self.start(ValueOld, go=False)
        await self.visible(2)

        # Deprecating early replays: replay is no gate for a value-only patch.
        self.assertEqual((await self.check(ValueDeprecated)).failures, [])

        for name, runner in self.RUNNERS.items():
            for release in (ValuePatched, ValueDeprecated):
                with self.subTest(runner=name, release=release.__name__):
                    report = await patch_report(self.client, definition(release, runner=runner()))
                    self.assertTrue(report.complete, report.render())
                    status = report.patches[VALUE_PATCH]
                    self.assertEqual([r.run_id for r in status.old_side], [old_side])
                    self.assertFalse(status.can_deprecate)
                    self.assertIn(f"{VALUE_PATCH}: old side 1, non-deprecated marker 0, others 1 -> keep patched()",
                                  report.render())


class UnreachableTest(unittest.TestCase):
    def test_an_unreachable_server_exits_2(self) -> None:
        for main in (check_main, patches_main):
            with self.subTest(main=main.__module__):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(main(["tests.replay_workflows:CURRENT", "--server", "127.0.0.1:1"]), 2)
                self.assertIn("COULD NOT CHECK", err.getvalue())

    def test_a_definition_that_does_not_load_exits_1(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(check_main(["tests.replay_workflows:MISSING"]), 1)
            self.assertEqual(check_main(["tests.replay_workflows:QUEUE"]), 1)

    def test_a_definition_whose_replayer_cant_be_built_exits_1_before_connecting(self) -> None:
        for main in (check_main, patches_main):
            with self.subTest(main=main.__module__):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = main(["tests.bad_sandbox_workflows:DEFINITION", "--server", "127.0.0.1:1"])
                self.assertEqual(code, 1, err.getvalue())
                self.assertIn("replayer", err.getvalue())

    def test_a_bad_command_line_exits_64(self) -> None:
        for main in (check_main, patches_main):
            with self.subTest(main=main.__module__), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main([]), 64)
                self.assertEqual(main(["tests.replay_workflows:CURRENT", "--bogus"]), 64)


if __name__ == "__main__":
    unittest.main()
