"""TeamWorkflow and ScheduleTick on Temporal's time-skipping test server, with stand-in activities."""

from __future__ import annotations

import asyncio
import unittest
import uuid
from datetime import timedelta

from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError, RetryState, TimeoutError, TimeoutType

from factory_runtime.temporal import signal_with_start
from factory_runtime.testing import make_worker, time_skipping_env
from engine.models import BudgetAnswer, TeamInput, TeamRef, TeamResult, TickInput, Wake
from engine.settings import TASK_QUEUE
from engine.workflows import WORKFLOWS, ScheduleTick, TeamWorkflow
from engine.workflows.team import classify
from tests.fakes import Fakes, later

RUN = ["resolve_team", "check_budget", "prepare_worktree", "run_agent_session", "deliver_report", "remove_worktree",
       "record_outcome"]


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await time_skipping_env()
        self.addAsyncCleanup(self.env.shutdown)
        self.client = self.env.client
        self.fakes = Fakes()
        self.worker = make_worker(
            self.client, passthrough=["engine.models"], task_queue=TASK_QUEUE, workflows=WORKFLOWS,
            activities=self.fakes.activities(),
        )
        await self.worker.__aenter__()
        self.addAsyncCleanup(self.worker.__aexit__, None, None, None)
        self.ref = TeamRef(repo="/repo", repo_name=f"repo-{uuid.uuid4().hex[:6]}", team="qa")

    async def result(self, handle) -> TeamResult:
        return TeamResult.model_validate(await handle.result())

    async def wake(self, event_id: str, reason: str = "schedule"):
        return await signal_with_start(
            self.client, "TeamWorkflow", self.ref.workflow_id, task_queue=TASK_QUEUE, signal="wake",
            signal_args=[Wake(reason=reason, event_id=event_id)], args=[TeamInput(ref=self.ref)],
        )


class WorkflowTest(Base):
    async def test_one_wake_is_one_run_in_order(self):
        result = await self.result(await self.wake("tick-1"))
        self.assertEqual(result.outcome, "done")
        self.assertEqual(self.fakes.calls, RUN)
        self.assertEqual(self.fakes.outcomes[0].reasons, ["schedule"])

    async def test_wakes_during_a_run_merge_into_one_more_run_and_repeats_are_dropped(self):
        self.fakes.holding = True
        await self.wake("tick-1")
        await self.fakes.agent_started.wait()
        for event, reason in (("tick-1", "schedule"), ("ci-9", "ci_red"), ("ci-9", "ci_red"), ("merge-3", "merged")):
            await self.wake(event, reason)
        self.fakes.hold.set()
        handle = self.client.get_workflow_handle(self.ref.workflow_id)
        await handle.result()  # follows continue-as-new to the last run
        self.assertEqual(self.fakes.calls.count("record_outcome"), 2)
        self.assertEqual(self.fakes.outcomes[1].reasons, ["ci_red", "merged"])

    async def test_an_idle_team_completes_and_a_later_wake_starts_it_again(self):
        await (await self.wake("tick-1")).result()
        await (await self.wake("tick-2")).result()
        self.assertEqual(self.fakes.calls.count("record_outcome"), 2)

    async def test_budget_wait_is_a_durable_timer(self):
        self.fakes.budget = [later(hours=5)]
        result = await self.result(await self.wake("tick-1"))
        self.assertEqual(result.outcome, "done")
        self.assertEqual(self.fakes.calls.count("check_budget"), 2)

    async def test_no_budget_skips_without_a_worktree(self):
        self.fakes.budget = [BudgetAnswer(verdict="stop", reason="unattended runs are off")]
        result = await self.result(await self.wake("tick-1"))
        self.assertEqual((result.outcome, result.summary), ("skipped", "unattended runs are off"))
        self.assertNotIn("prepare_worktree", self.fakes.calls)

    async def test_a_disabled_team_is_skipped(self):
        self.fakes.enabled = False
        result = await self.result(await self.wake("tick-1"))
        self.assertEqual(result.outcome, "skipped")
        self.assertEqual(self.fakes.calls, ["resolve_team", "record_outcome"])

    async def test_a_failed_agent_is_recorded_and_the_worktree_still_removed(self):
        self.fakes.agent_error = "AgentFailed"
        result = await self.result(await self.wake("tick-1"))
        self.assertEqual(result.outcome, "failed")
        self.assertIn("agent said no", result.summary)
        self.assertEqual(self.fakes.calls[-2:], ["remove_worktree", "record_outcome"])

    async def test_cancel_reaches_the_agent_and_cleanup_runs_after_it(self):
        self.fakes.holding = True
        handle = await self.wake("tick-1")
        await self.fakes.agent_started.wait()
        await handle.cancel()
        with self.assertRaises(WorkflowFailureError):
            await handle.result()
        self.assertTrue(self.fakes.agent_cancelled)
        self.assertEqual(self.fakes.calls[-2:], ["remove_worktree", "record_outcome"])
        self.assertEqual((self.fakes.outcomes[0].outcome, self.fakes.outcomes[0].summary), ("skipped", "cancelled"))
        self.assertEqual((await handle.describe()).status, WorkflowExecutionStatus.CANCELED)

    async def test_schedule_tick_wakes_the_team_with_its_own_id(self):
        tick_id = f"tick/{self.ref.repo_name}/qa-2026-11-13T02:00:00Z"
        await self.client.execute_workflow(ScheduleTick.run, TickInput(ref=self.ref), id=tick_id, task_queue=TASK_QUEUE)
        (woken,) = self.fakes.woken
        self.assertEqual((woken.wake.reason, woken.wake.event_id, woken.bypass_pause), ("schedule", tick_id, False))


class MoreWorkflowTest(Base):
    async def test_a_run_that_breaks_outside_the_agent_is_recorded_and_pending_wakes_still_run(self):
        self.fakes.resolve_errors = 1
        self.fakes.holding = True
        handle = await self.wake("tick-1")
        for _ in range(100):  # the first run is recording its failure; wake again meanwhile
            if "resolve_team" in self.fakes.calls:
                break
            await asyncio.sleep(0.05)
        await self.wake("ci-1", "ci_red")
        self.fakes.hold.set()
        await self.client.get_workflow_handle(self.ref.workflow_id).result()
        outcomes = [(o.outcome, o.reasons) for o in self.fakes.outcomes]
        self.assertEqual(outcomes[0][0], "failed")
        self.assertIn("run stopped", self.fakes.outcomes[0].summary)
        self.assertEqual(len(outcomes), 2, outcomes)

    async def test_cancel_during_prepare_waits_for_it_then_cleans_up(self):
        self.fakes.prepare_holding = True
        handle = await self.wake("tick-1")
        await self.fakes.prepare_started.wait()
        await handle.cancel()
        with self.assertRaises(WorkflowFailureError):
            await handle.result()
        self.assertTrue(self.fakes.prepare_cancelled)
        self.assertNotIn("run_agent_session", self.fakes.calls)
        self.assertEqual(self.fakes.calls[-2:], ["remove_worktree", "record_outcome"])

    async def test_a_cancel_while_resolving_ends_cancelled(self):
        self.fakes.resolve_holding = True
        handle = await self.wake("tick-1")
        await self.fakes.resolve_started.wait()
        await handle.cancel()
        with self.assertRaises(WorkflowFailureError):
            await handle.result()
        self.assertEqual((await handle.describe()).status, WorkflowExecutionStatus.CANCELED)
        self.assertEqual([(o.outcome, o.summary) for o in self.fakes.outcomes], [("skipped", "cancelled")])

    async def test_seen_event_ids_survive_continue_as_new(self):
        self.fakes.holding = True
        await self.wake("tick-1")
        await self.fakes.agent_started.wait()
        await self.wake("ci-1", "ci_red")  # → a second run, after continue-as-new
        self.fakes.hold.set()
        await self.client.get_workflow_handle(self.ref.workflow_id).result()
        self.assertEqual(self.fakes.calls.count("record_outcome"), 2)


class ReplayTest(Base):
    async def test_histories_replay_against_the_current_code(self):
        """Each workflow's history replays on the code that produced it: workflow code that isn't deterministic
        (clock, randomness, I/O) fails here. Across builds Pinned versioning means an old history never meets new
        code (docs/designs/engine.md, sections 6 and 9)."""
        from temporalio.worker import Replayer

        from factory_runtime.temporal import build_workflow_runner, data_converter

        self.fakes.holding = True
        first = await self.wake("tick-1")
        await self.fakes.agent_started.wait()
        await self.wake("ci-1", "ci_red")
        self.fakes.hold.set()
        await self.client.get_workflow_handle(self.ref.workflow_id).result()
        tick_id = f"tick/{self.ref.repo_name}/qa-x"
        await self.client.execute_workflow(ScheduleTick.run, TickInput(ref=self.ref), id=tick_id, task_queue=TASK_QUEUE)
        replayer = Replayer(workflows=WORKFLOWS, data_converter=data_converter(),
                            workflow_runner=build_workflow_runner(["engine.models"]))
        histories = [
            await self.client.get_workflow_handle(self.ref.workflow_id, run_id=first.result_run_id).fetch_history(),
            await self.client.get_workflow_handle(self.ref.workflow_id).fetch_history(),  # after continue-as-new
            await self.client.get_workflow_handle(tick_id).fetch_history(),
        ]
        self.assertGreaterEqual(len(histories), 3)
        async with replayer.workflow_replay_iterator(_aiter(histories)) as results:
            async for result in results:
                self.assertIsNone(result.replay_failure)


async def _aiter(items):
    for item in items:
        yield item


class ClassifyTest(unittest.TestCase):
    def error(self, cause: BaseException) -> ActivityError:
        err = ActivityError("activity failed", scheduled_event_id=1, started_event_id=2, identity="w",
                            activity_type="run_agent_session", activity_id="5", retry_state=RetryState.TIMEOUT)
        err.__cause__ = cause
        return err

    def test_a_timeout_is_a_failure_flagged_as_timed_out(self):
        """record_outcome decides: interrupted only if the machine slept (tests/test_activities.py)."""
        timeout = TimeoutError("heartbeat", type=TimeoutType.HEARTBEAT, last_heartbeat_details=[])
        self.assertEqual(classify(self.error(timeout)), ("failed", "timed out (heartbeat)", True))

    def test_an_agent_stopped_by_a_signal_is_interrupted(self):
        cause = ApplicationError("the agent was stopped by signal 15", type="AgentInterrupted")
        self.assertEqual(classify(self.error(cause))[0], "interrupted")

    def test_anything_else_is_failed_with_its_message(self):
        self.assertEqual(classify(self.error(ApplicationError("the agent exited with 2"))),
                         ("failed", "the agent exited with 2", False))


if __name__ == "__main__":
    unittest.main()
