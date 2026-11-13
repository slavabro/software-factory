"""Activities for real: a git repo with an origin, an agent that is a shell command, real process groups."""

from __future__ import annotations

import asyncio
import os
import tempfile
import json
import signal
import time
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from engine import process
from engine import settings as settings_mod
from engine.activities import agent, repo as repo_acts
from engine.activities.teams import TeamActivities
from engine.models import OutcomeInput, ResolveInput, StepInput, TeamRef
from engine.activities.agent import slept_marker
from engine.settings import EngineSettings
from engine.worker import build_id
from tests.repo_fixture import make_repo


def json_pid(worktree: Path):
    try:
        import json

        return json.loads((worktree / process.PIDFILE).read_text())["pid"]
    except (OSError, ValueError):
        return None


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


class PrepareTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_kills_the_setup_even_if_it_ignores_term(self):
        os.environ["FACTORY_STOP_GRACE"] = "1"
        self.addCleanup(os.environ.pop, "FACTORY_STOP_GRACE", None)
        marker = Path(tempfile.mkdtemp()) / "setup-pid"
        self.addCleanup(lambda: marker.parent.exists() and __import__("shutil").rmtree(marker.parent))
        repo, _, tmp = make_repo("true", extra_toml=f'\n[worktree]\nsetup = ["trap \\"\\" TERM; echo $$ > {marker}; sleep 60"]\n')
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_ENGINE_STATE", None)
        ref = TeamRef(repo=str(repo), repo_name="demo", team="qa")
        env = ActivityEnvironment()
        plan = await env.run(repo_acts.resolve_team, ResolveInput(ref=ref, run_id="cafe1234", day="20261113"))
        step = StepInput(ref=ref, paths=plan.paths, prompt=plan.prompt)
        with mock.patch.object(process, "KILL_GRACE", 2.0):
            task = asyncio.create_task(env.run(repo_acts.prepare_worktree, step))
            for _ in range(100):
                await asyncio.sleep(0.1)
                if marker.exists() and marker.read_text().strip():
                    break
            setup_pid = int(marker.read_text())
            env.cancel()
            with self.assertRaises(BaseException):
                await task
        await asyncio.sleep(0.5)
        self.assertIsNone(process.started_at(setup_pid), "the setup kept running after the cancel")

    async def test_a_failed_fetch_raises_to_be_retried_and_not_opted_in_does_not(self):
        repo, _, tmp = make_repo("true")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_ENGINE_STATE", None)
        ref = TeamRef(repo=str(repo), repo_name="demo", team="qa")
        env = ActivityEnvironment()
        plan = await env.run(repo_acts.resolve_team, ResolveInput(ref=ref, run_id="beef5678", day="20261113"))
        step = StepInput(ref=ref, paths=plan.paths, prompt=plan.prompt)
        subprocess.run(["git", "remote", "set-url", "origin", "/nonexistent"], cwd=repo, check=True)
        with self.assertRaises(ApplicationError) as caught:
            await env.run(repo_acts.prepare_worktree, step)
        self.assertEqual((caught.exception.type, caught.exception.non_retryable), ("PrepareFailed", False))

    async def test_an_incomplete_worktree_is_replaced_not_reused(self):
        repo, _, tmp = make_repo("true")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_ENGINE_STATE", None)
        ref = TeamRef(repo=str(repo), repo_name="demo", team="qa")
        env = ActivityEnvironment()
        plan = await env.run(repo_acts.resolve_team, ResolveInput(ref=ref, run_id="f00d9abc", day="20261113"))
        Path(plan.paths.worktree).mkdir(parents=True)  # what a worktree add that died half way leaves
        (Path(plan.paths.worktree) / "junk").write_text("x")
        result = await env.run(repo_acts.prepare_worktree, StepInput(ref=ref, paths=plan.paths, prompt=plan.prompt))
        self.assertTrue(result.ok)
        self.assertTrue((Path(plan.paths.worktree) / ".factory.toml").is_file())
        self.assertFalse((Path(plan.paths.worktree) / "junk").exists())


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

    async def test_an_agent_stopped_by_a_signal_is_retried_not_failed(self):
        await self.setup_repo("kill -TERM $$")
        with self.assertRaises(ApplicationError) as caught:
            await self.env.run(agent.run_agent_session, self.step)
        self.assertEqual((caught.exception.type, caught.exception.non_retryable), ("AgentInterrupted", False))

    async def test_a_failing_agent_is_not_retried(self):
        await self.setup_repo("exit 3")
        with self.assertRaises(ApplicationError) as caught:
            await self.env.run(agent.run_agent_session, self.step)
        self.assertEqual((caught.exception.type, caught.exception.non_retryable), ("AgentFailed", True))

    async def test_heartbeats_while_running_and_cancel_kills_the_whole_group(self):
        worktree = await self.setup_repo("sleep 60 & sleep 60; wait")
        beats = []
        self.env.on_heartbeat = lambda *d: beats.append(d)
        with mock.patch.object(agent, "HEARTBEAT_EVERY", 0.2), mock.patch.object(process, "KILL_GRACE", 2.0):
            task = asyncio.create_task(self.env.run(agent.run_agent_session, self.step))
            for _ in range(50):
                await asyncio.sleep(0.1)
                if (worktree / process.PIDFILE).exists() and beats:
                    break
            pid = json_pid(worktree)
            self.assertTrue(alive_group(pid))
            self.env.cancel()
            with self.assertRaises(BaseException):
                await task
        self.assertFalse(alive_group(pid), "the agent's process group outlived the cancel")
        self.assertEqual(json_pid(worktree), None)
        self.assertFalse((worktree / process.PIDFILE).exists())
        self.assertIn("pid", beats[0][0])

    async def test_a_stale_agent_from_a_dead_worker_is_stopped_first(self):
        worktree = await self.setup_repo("true")
        stale = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: stale.poll() is None and stale.kill())
        process.write_pidfile(worktree, stale.pid)
        with mock.patch.object(process, "KILL_GRACE", 2.0):
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

    async def record(self, outcome: str, activity_id: str, **extra) -> object:
        env = ActivityEnvironment()
        env.info = env.info.__class__(**{**env.info.__dict__, "activity_id": activity_id})
        return await env.run(self.acts.record_outcome, OutcomeInput(ref=self.ref, run_id="run", outcome=outcome,
                                                                     summary="s", reasons=["schedule"], **extra))

    async def test_three_failures_in_a_row_pause_the_schedule_and_others_do_not_count(self):
        for i, outcome in enumerate(["failed", "interrupted", "failed", "skipped"]):
            await self.record(outcome, str(i))
        self.handle.pause.assert_not_awaited()
        result = await self.record("failed", "9")
        self.assertEqual((result.consecutive_failures, result.paused), (3, True))
        self.assertIn("3 failed runs", self.handle.pause.await_args.kwargs["note"])

    async def test_a_pause_that_failed_is_done_by_the_retry(self):
        from temporalio.service import RPCError, RPCStatusCode

        for i in range(2):
            await self.record("failed", str(i))
        self.handle.pause.side_effect = [RPCError("busy", RPCStatusCode.UNAVAILABLE, b""), None]
        with self.assertRaises(RPCError):
            await self.record("failed", "third")
        result = await self.record("failed", "third")  # the activity's retry: same key
        self.assertEqual((result.consecutive_failures, result.paused), (3, True))

    async def test_a_timeout_is_interrupted_only_if_the_machine_slept(self):
        log = str(self.acts.settings.state / "run.log")
        plain = await self.record("failed", "a", timed_out=True, log=log, budget_minutes=90)
        self.assertEqual((plain.outcome, plain.consecutive_failures), ("failed", 1))
        slept_marker(log).write_text(json.dumps({"slept": 1800, "awake": 3600}))
        slept = await self.record("failed", "b", timed_out=True, log=log, budget_minutes=90)
        self.assertEqual((slept.outcome, slept.consecutive_failures), ("interrupted", 1))

    async def test_a_retried_record_is_written_once(self):
        await self.record("failed", "same")
        again = await self.record("failed", "same")
        self.assertEqual(again.consecutive_failures, 1)
        notebook = (self.acts._team_dir(self.ref) / "notebook.md").read_text()
        self.assertEqual(notebook.count("## run"), 1)

    async def test_a_success_resets_the_count(self):
        await self.record("failed", "1")
        self.assertEqual((await self.record("done", "2")).consecutive_failures, 0)


class SleepEvidenceTest(unittest.TestCase):
    def test_a_noticed_sleep_is_written_with_the_awake_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = str(Path(tmp) / "run.log")
            watch = process.SleepWatch()
            watch.wall -= 900  # as if 15 minutes of wall time passed while the monotonic clock stood still
            agent._note_sleep(watch, log)
            seen = json.loads(slept_marker(log).read_text())
            self.assertGreater(seen["slept"], 800)
            self.assertLess(seen["awake"], 5)

    def test_a_retry_in_the_same_worker_sees_a_sleep_the_killed_attempt_missed(self):
        env = ActivityEnvironment()
        last = {"worker": os.getpid(), "wall": time.time() - 900, "mono": time.monotonic()}
        env.info = env.info.__class__(**{**env.info.__dict__, "heartbeat_details": [last]})
        watch = env.run(agent._resume_watch)
        self.assertGreater(watch.tick(), 800)
        other = {**last, "worker": os.getpid() + 1}  # another process: its monotonic clock means nothing here
        env.info = env.info.__class__(**{**env.info.__dict__, "heartbeat_details": [other]})
        self.assertEqual(env.run(agent._resume_watch).tick(), 0.0)

    def test_a_sleep_excuses_a_timeout_only_if_it_ate_into_the_budget(self):
        from engine.activities.teams import slept_in_budget

        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "m"
            marker.write_text(json.dumps({"slept": 1200, "awake": 60 * 80}))
            self.assertTrue(slept_in_budget(marker, 90))  # awake 80 of 90 minutes: the sleep made it time out
            marker.write_text(json.dumps({"slept": 120, "awake": 60 * 95}))
            self.assertFalse(slept_in_budget(marker, 90))  # awake past the budget: a hang, not the nap
            self.assertFalse(slept_in_budget(Path(tmp) / "missing", 90))


class ProcessTest(unittest.TestCase):
    def test_a_group_whose_leader_exited_is_still_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            leader = subprocess.Popen(["sh", "-c", "sleep 60 & echo $!; exit 0"], stdout=subprocess.PIPE, text=True,
                                      start_new_session=True)
            child = int(leader.stdout.readline())
            leader.wait()
            self.addCleanup(lambda: process.group_alive(leader.pid) and os.killpg(leader.pid, signal.SIGKILL))
            (worktree / process.PIDFILE).parent.mkdir(parents=True)
            (worktree / process.PIDFILE).write_text(json.dumps({"pid": leader.pid, "started": "Fri Nov 13 00:00:00 2026"}))
            self.assertEqual(process.kill_stale(worktree, grace=2), leader.pid)
            time.sleep(0.3)
            self.assertIsNone(process.started_at(child), "the leader's child kept running")

    def test_a_reused_pid_is_not_killed(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)
            self.addCleanup(lambda: bystander.poll() is None and bystander.kill())
            (worktree / process.PIDFILE).parent.mkdir(parents=True)
            (worktree / process.PIDFILE).write_text(json.dumps({"pid": bystander.pid, "started": "Mon Jan  1 00:00:00 2001"}))
            self.assertIsNone(process.kill_stale(worktree, grace=1))
            self.assertIsNone(bystander.poll(), "an unrelated process with a reused pid was killed")

    def test_only_its_own_pidfile_is_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            process.write_pidfile(worktree, os.getpid())
            process.clear_pidfile(worktree, 1)  # an older attempt's cleanup
            self.assertTrue((worktree / process.PIDFILE).exists())
            process.clear_pidfile(worktree, os.getpid())
            self.assertFalse((worktree / process.PIDFILE).exists())

    def test_one_session_per_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = process.WorktreeLock(Path(tmp)), process.WorktreeLock(Path(tmp))
            self.assertTrue(first.acquire(timeout=1))
            self.assertFalse(second.acquire(timeout=0.5))
            first.release()
            self.assertTrue(second.acquire(timeout=1))
            second.release()

    def test_a_sleep_is_wall_time_the_monotonic_clock_did_not_see(self):
        watch = process.SleepWatch(threshold=60)
        with mock.patch.object(process.time, "time", return_value=watch.wall + 900), \
             mock.patch.object(process.time, "monotonic", return_value=watch.mono + 30):
            self.assertGreater(watch.tick(), 800)
        self.assertEqual(process.SleepWatch().tick(), 0.0)


class SpendSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_switch_is_read_on_every_run(self):
        acts = TeamActivities(mock.MagicMock(), EngineSettings())
        ref = TeamRef(repo="/r", repo_name="d", team="qa")
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write("unattended = false\n")
        self.addCleanup(os.unlink, f.name)
        with mock.patch.object(settings_mod, "CONFIG_PATH", Path(f.name)):
            self.assertEqual((await ActivityEnvironment().run(acts.check_budget, ref)).verdict, "stop")
            Path(f.name).write_text("unattended = true\n")
            self.assertEqual((await ActivityEnvironment().run(acts.check_budget, ref)).verdict, "go")


class BuildIdTest(unittest.TestCase):
    def test_stable_and_blind_to_tests_and_caches(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("engine/engine/a.py", "runtime/factory_runtime/b.py", "plugins/factory/sfactory/c.py",
                        "engine/tests/t.py", "engine/engine/__pycache__/a.pyc", "engine/infra/x.yml"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(rel)
            first = build_id(root)
            (root / "engine/tests/t.py").write_text("changed")
            (root / "docs").mkdir()
            (root / "docs/x.md").write_text("docs")
            self.assertEqual(build_id(root), first)
            (root / "engine/engine/a.py").write_text("changed")
            second = build_id(root)
            self.assertNotEqual(second, first)
            (root / "plugins/factory/sfactory/c.py").write_text("changed")  # the runner the activities use
            self.assertNotEqual(build_id(root), second)


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
