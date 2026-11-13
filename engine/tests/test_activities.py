"""Activities for real: a git repo with an origin, an agent that is a shell command, real process groups."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from engine import settings as settings_mod
from engine.activities import agent, repo as repo_acts
from engine.activities.teams import TeamActivities
from engine.models import OutcomeInput, ResolveInput, StepInput, TeamRef
from engine.settings import EngineSettings
from engine.worker import build_id
from tests.repo_fixture import make_repo


def alive_group(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


class RepoActivitiesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo, self.state, tmp = make_repo('echo "agent ran: $FACTORY_BRANCH" > .factory/report.md')
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_ENGINE_STATE", None)
        self.ref = TeamRef(repo=str(self.repo), repo_name="demo", team="qa")
        self.env = ActivityEnvironment()

    async def plan(self):
        return await self.env.run(repo_acts.resolve_team, ResolveInput(ref=self.ref, run_id="abcdef1234", day="20261113"))

    async def test_a_whole_run_prepare_session_report_clean(self):
        plan = await self.plan()
        self.assertTrue(plan.enabled)
        self.assertEqual(plan.paths.branch, "factory/qa/20261113-abcdef12")
        self.assertTrue(plan.paths.worktree.startswith(str(self.state)))
        step = StepInput(ref=self.ref, paths=plan.paths, prompt=plan.prompt)
        self.assertTrue((await self.env.run(repo_acts.prepare_worktree, step)).ok)
        self.assertTrue((await self.env.run(repo_acts.prepare_worktree, step)).ok)  # repeat: reused
        self.assertTrue((await self.env.run(agent.run_agent_session, step)).ok)
        delivered = await self.env.run(repo_acts.deliver_report, step)
        self.assertIn("agent ran: factory/qa/20261113-abcdef12", Path(plan.paths.report).read_text())
        self.assertIn("report", delivered.message)
        cleaned = await self.env.run(repo_acts.remove_worktree, step)
        self.assertFalse(Path(plan.paths.worktree).exists(), cleaned.message)
        self.assertTrue((await self.env.run(repo_acts.remove_worktree, step)).ok)  # repeat: nothing left is fine

    async def test_an_unknown_team_is_not_enabled(self):
        ref = TeamRef(repo=str(self.repo), repo_name="demo", team="devops")
        plan = await self.env.run(repo_acts.resolve_team, ResolveInput(ref=ref, run_id="r", day="20261113"))
        self.assertFalse(plan.enabled)
        self.assertIn("no team 'devops'", plan.reason)

    async def test_upgrade_is_a_team_from_the_upgrade_section(self):
        ref = TeamRef(repo=str(self.repo), repo_name="demo", team="upgrade")
        plan = await self.env.run(repo_acts.resolve_team, ResolveInput(ref=ref, run_id="r", day="20261113"))
        self.assertEqual((plan.enabled, plan.prompt), (True, "/factory:upgrade"))


class AgentSessionTest(unittest.IsolatedAsyncioTestCase):
    async def setup_repo(self, script: str):
        self.repo, self.state, tmp = make_repo(script)
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_ENGINE_STATE", None)
        self.ref = TeamRef(repo=str(self.repo), repo_name="demo", team="qa")
        self.env = ActivityEnvironment()
        plan = await self.env.run(repo_acts.resolve_team, ResolveInput(ref=self.ref, run_id="0123456789", day="20261113"))
        self.step = StepInput(ref=self.ref, paths=plan.paths, prompt=plan.prompt)
        await self.env.run(repo_acts.prepare_worktree, self.step)
        return Path(plan.paths.worktree)

    async def test_a_failing_agent_is_not_retried(self):
        await self.setup_repo("exit 3")
        with self.assertRaises(ApplicationError) as caught:
            await self.env.run(agent.run_agent_session, self.step)
        self.assertEqual((caught.exception.type, caught.exception.non_retryable), ("AgentFailed", True))

    async def test_heartbeats_while_running_and_cancel_kills_the_whole_group(self):
        worktree = await self.setup_repo("sleep 60 & sleep 60; wait")
        beats = []
        self.env.on_heartbeat = lambda *d: beats.append(d)
        with mock.patch.object(agent, "HEARTBEAT_EVERY", 0.2), mock.patch.object(agent, "KILL_GRACE", 2.0):
            task = asyncio.create_task(self.env.run(agent.run_agent_session, self.step))
            for _ in range(50):
                await asyncio.sleep(0.1)
                if (worktree / agent.PIDFILE).exists() and beats:
                    break
            pid = int((worktree / agent.PIDFILE).read_text())
            self.assertTrue(alive_group(pid))
            self.env.cancel()
            with self.assertRaises(BaseException):
                await task
        self.assertFalse(alive_group(pid), "the agent's process group outlived the cancel")
        self.assertFalse((worktree / agent.PIDFILE).exists())
        self.assertIn("pid", beats[0][0])

    async def test_a_stale_agent_from_a_dead_worker_is_stopped_first(self):
        worktree = await self.setup_repo("true")
        stale = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: stale.poll() is None and stale.kill())
        (worktree / agent.PIDFILE).parent.mkdir(parents=True, exist_ok=True)
        (worktree / agent.PIDFILE).write_text(str(stale.pid))
        with mock.patch.object(agent, "KILL_GRACE", 2.0):
            await self.env.run(agent.run_agent_session, self.step)
        stale.wait(timeout=5)
        self.assertIn("stopped a stale agent", Path(self.step.paths.log).read_text())


class RecordOutcomeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.handle = mock.MagicMock()
        self.handle.pause = mock.AsyncMock()
        client = mock.MagicMock()
        client.get_schedule_handle.return_value = self.handle
        self.acts = TeamActivities(client, EngineSettings(state=Path(tmp.name)))
        self.ref = TeamRef(repo="/r", repo_name="demo", team="qa")

    async def record(self, outcome: str, activity_id: str) -> object:
        env = ActivityEnvironment()
        env.info = env.info.__class__(**{**env.info.__dict__, "activity_id": activity_id})
        return await env.run(self.acts.record_outcome, OutcomeInput(ref=self.ref, run_id="run", outcome=outcome,
                                                                     summary="s", reasons=["schedule"]))

    async def test_three_failures_in_a_row_pause_the_schedule_and_others_do_not_count(self):
        for i, outcome in enumerate(["failed", "interrupted", "failed", "skipped"]):
            await self.record(outcome, str(i))
        self.handle.pause.assert_not_awaited()
        result = await self.record("failed", "9")
        self.assertEqual((result.consecutive_failures, result.paused), (3, True))
        self.assertIn("3 failed runs", self.handle.pause.await_args.kwargs["note"])

    async def test_a_retried_record_is_written_once(self):
        await self.record("failed", "same")
        again = await self.record("failed", "same")
        self.assertEqual(again.consecutive_failures, 1)
        notebook = (self.acts._team_dir(self.ref) / "notebook.md").read_text()
        self.assertEqual(notebook.count("## run"), 1)

    async def test_a_success_resets_the_count(self):
        await self.record("failed", "1")
        self.assertEqual((await self.record("done", "2")).consecutive_failures, 0)


class SpendSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def test_unattended_off_stops_runs(self):
        acts = TeamActivities(mock.MagicMock(), EngineSettings(unattended=False))
        answer = await ActivityEnvironment().run(acts.check_budget, TeamRef(repo="/r", repo_name="d", team="qa"))
        self.assertEqual(answer.verdict, "stop")


class BuildIdTest(unittest.TestCase):
    def test_stable_and_blind_to_tests_and_caches(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("engine/engine/a.py", "runtime/factory_runtime/b.py", "engine/tests/t.py", "engine/engine/__pycache__/a.pyc"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(rel)
            first = build_id(root)
            (root / "engine/tests/t.py").write_text("changed")
            (root / "docs").mkdir()
            (root / "docs/x.md").write_text("docs")
            self.assertEqual(build_id(root), first)
            (root / "engine/engine/a.py").write_text("changed")
            self.assertNotEqual(build_id(root), first)


class SettingsTest(unittest.TestCase):
    def test_unknown_keys_are_refused(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('repos = ["~/x"]\nserver_prot = 1\n')
        self.addCleanup(os.unlink, f.name)
        with self.assertRaises(ValueError):
            settings_mod.load(Path(f.name))


if __name__ == "__main__":
    unittest.main()
