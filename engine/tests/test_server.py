"""Against a real local Temporal server (the SDK's dev server, namespace `factory`, the search attributes
registered): schedules reconcile from config, a paused schedule holds wakes back, and a versioned worker runs a
team pinned to its Build ID."""

from __future__ import annotations

import asyncio
import os
import unittest
import uuid

from temporalio.client import Client
from temporalio.common import VersioningBehavior
from temporalio.testing import WorkflowEnvironment

from factory_runtime.temporal import data_converter
from engine import schedules
from engine.activities.teams import REPO_KEY, TEAM_KEY, schedule_paused, wake
from engine.cli import COMMANDS, parser
from engine.models import TeamRef, TeamResult, Wake, WakeTeamInput
from engine.settings import DEPLOYMENT, NAMESPACE, EngineSettings
from engine.worker import make_worker
from tests.fakes import Fakes
from tests.repo_fixture import make_repo


class ServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await WorkflowEnvironment.start_local(
            namespace=NAMESPACE, data_converter=data_converter(), search_attributes=[REPO_KEY, TEAM_KEY]
        )
        self.addAsyncCleanup(self.env.shutdown)
        self.client: Client = self.env.client
        self.repo, _, tmp = make_repo("true")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_ENGINE_STATE", None)
        self.settings = EngineSettings(repos=(str(self.repo),))
        self.ref = TeamRef(repo=str(self.repo.resolve()), repo_name="demo", team="qa")

    async def test_schedules_follow_the_config_and_keep_their_pause(self):
        report = await schedules.reconcile(self.client, self.settings)
        self.assertIn("created schedule/demo/qa", report[0])
        self.assertTrue(any("schedule/demo/upgrade" in line for line in report))  # [upgrade] is on by default
        paused, note = await schedule_paused(self.client, self.ref)
        self.assertTrue(paused)
        self.assertEqual(note, schedules.NEW_NOTE)
        await self.client.get_schedule_handle(self.ref.schedule_id).unpause(note="first run was good")
        await schedules.reconcile(self.client, self.settings)  # an update keeps the team's own state
        self.assertEqual(await schedule_paused(self.client, self.ref), (False, "first run was good"))
        desc = await self.client.get_schedule_handle(self.ref.schedule_id).describe()
        nxt = desc.info.next_action_times[0]  # the server keeps cron as calendars; 02:00 UTC plus up to 5 min jitter
        self.assertEqual((nxt.hour, nxt.minute < 6), (2, True))
        self.settings = EngineSettings(repos=())
        report = await schedules.reconcile(self.client, self.settings)
        self.assertIn("deleted schedule/demo/qa (no longer in config)", report)

    async def test_a_repo_without_a_factory_toml_gets_no_schedules(self):
        from tests.repo_fixture import git

        (self.repo / ".factory.toml").unlink()
        git("commit", "-qam", "opt out", cwd=self.repo)
        git("push", "-q", "origin", "main", cwd=self.repo)
        self.assertEqual(await schedules.reconcile(self.client, self.settings), [])

    async def test_a_paused_team_is_not_woken_except_by_hand(self):
        await schedules.reconcile(self.client, self.settings)  # created paused
        held = await wake(self.client, self.settings, WakeTeamInput(ref=self.ref, wake=Wake(reason="ci_red", event_id="e1")))
        self.assertFalse(held.woken)
        self.assertIn("paused", held.reason)
        by_hand = await wake(self.client, self.settings,
                             WakeTeamInput(ref=self.ref, wake=Wake(reason="manual", event_id="e2"), bypass_pause=True))
        self.assertTrue(by_hand.woken)
        desc = await self.client.get_workflow_handle(self.ref.workflow_id).describe()
        self.assertEqual(desc.typed_search_attributes.get(TEAM_KEY), "qa")
        await self.client.get_workflow_handle(self.ref.workflow_id).terminate("test over")

    async def test_a_versioned_worker_runs_the_team_pinned_to_its_build(self):
        fakes = Fakes()
        build = f"test-{uuid.uuid4().hex[:8]}"
        worker = make_worker(self.client, self.settings, build=build, activities=fakes.activities())
        async with worker:
            args = parser().parse_args(["set-current", "--build-id", build])
            for _ in range(50):  # the version exists once the worker has polled
                try:
                    await COMMANDS["set-current"](self.client, self.settings, args)
                    break
                except Exception:
                    await asyncio.sleep(0.2)
            await wake(self.client, self.settings,
                       WakeTeamInput(ref=self.ref, wake=Wake(reason="manual", event_id="m1"), bypass_pause=True))
            handle = self.client.get_workflow_handle(self.ref.workflow_id)
            result = TeamResult.model_validate(await handle.result())
            self.assertEqual(result.outcome, "done")
            desc = await handle.describe()
            info = desc.raw_description.workflow_execution_info.versioning_info
            self.assertEqual(info.behavior, VersioningBehavior.PINNED.value)
            self.assertEqual(info.deployment_version.build_id, build)
            self.assertEqual(info.deployment_version.deployment_name, DEPLOYMENT)


if __name__ == "__main__":
    unittest.main()
