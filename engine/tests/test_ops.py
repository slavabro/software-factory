"""The engine's operations (engine/ops.py) against a real local Temporal server: each action's effect is checked
through `snapshot()`, the read the CLI and the desk share, and each change lands in the action log."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from temporalio.client import Client
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment

from factory_runtime.temporal import data_converter
from engine import cli, ops, schedules, settings as settings_mod
from engine.activities.teams import REPO_KEY, TEAM_KEY
from engine.factory import install
from engine.models import TeamRef
from engine.settings import NAMESPACE, EngineSettings
from engine.worker import make_worker
from tests.fakes import Fakes
from tests.repo_fixture import git, make_repo


class Isolated:
    """engine.toml, the builds and the state in a temp dir, never the machine's own install."""

    def isolate(self, case: unittest.TestCase) -> Path:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        for target, name, value in ((install, "SETTINGS", base / "engine.toml"),
                                    (install, "BUILDS", base / "builds"), (install, "CURRENT", base / "current")):
            patch = mock.patch.object(target, name, value)
            patch.start()
            case.addCleanup(patch.stop)
        return base


class OpsServerTest(unittest.IsolatedAsyncioTestCase, Isolated):
    async def asyncSetUp(self):
        self.env = await WorkflowEnvironment.start_local(
            namespace=NAMESPACE, data_converter=data_converter(), search_attributes=[REPO_KEY, TEAM_KEY]
        )
        self.addAsyncCleanup(self.env.shutdown)
        self.client: Client = self.env.client
        self.base = self.isolate(self)
        self.repo, self.state, tmp = make_repo("true")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_ENGINE_STATE", None)
        install.SETTINGS.write_text(f'repos = ["{self.repo}"]\ntime_zone = "UTC"\n')
        self.settings = settings_mod.load()
        self.ref = TeamRef(repo=str(self.repo.resolve()), repo_name="demo", team="qa")

    def actions(self) -> list[ops.Change]:
        return ops.read_actions(self.settings)

    async def test_a_snapshot_shows_the_repos_teams_schedules_and_builds(self):
        await ops.reconcile(self.client, self.settings)
        build = install.BUILDS / "abc123"
        build.mkdir(parents=True)
        (build / install.COMMIT).write_text("f00d\n")
        install.CURRENT.write_text("abc123")
        snap = await ops.snapshot(self.client, self.settings)
        (repo,) = snap.repos
        self.assertEqual((repo.name, repo.problem), ("demo", ""))
        qa = next(t for t in repo.teams if t.ref.team == "qa")
        self.assertEqual((qa.ref, qa.schedule, qa.wake_on, qa.failures), (self.ref, "0 2 * * *", ("ci_red",), 0))
        assert qa.scheduled is not None
        self.assertEqual((qa.scheduled.paused, qa.scheduled.note), (True, schedules.NEW_NOTE))
        self.assertIsNotNone(qa.scheduled.next_run)
        self.assertIn("schedule/demo/qa", [s.schedule_id for s in snap.schedules])
        self.assertEqual((snap.open_runs, snap.recent_runs), ((), ()))
        (b,) = snap.builds
        self.assertEqual((b.build_id, b.commit, b.current, snap.current_build), ("abc123", "f00d", True, "abc123"))
        assert b.server is not None
        self.assertFalse(b.server.known)

    async def test_a_read_never_fetches(self):
        await ops.reconcile(self.client, self.settings)
        with mock.patch.object(schedules, "fetch_default", side_effect=AssertionError("a read fetched")), \
             mock.patch.object(schedules.subprocess, "run", wraps=schedules.subprocess.run) as run:
            snap = await ops.snapshot(self.client, self.settings)
        self.assertEqual(snap.repos[0].problem, "")
        self.assertFalse([c for c in run.call_args_list if "fetch" in c.args[0]])

    async def test_pause_and_resume_show_in_the_snapshot_and_the_log(self):
        await ops.reconcile(self.client, self.settings)
        await ops.resume(self.client, self.settings, self.ref, "first run was good")
        snap = await ops.snapshot(self.client, self.settings)
        qa = next(s for s in snap.schedules if s.schedule_id == self.ref.schedule_id)
        self.assertEqual((qa.paused, qa.note), (False, "first run was good"))
        await ops.pause(self.client, self.settings, self.ref, who="desk")
        snap = await ops.snapshot(self.client, self.settings)
        qa = next(s for s in snap.schedules if s.schedule_id == self.ref.schedule_id)
        self.assertEqual((qa.paused, qa.note), (True, "paused by hand"))
        last = self.actions()[-1]
        self.assertEqual((last.who, last.action, last.target), ("desk", "pause", "schedule/demo/qa"))
        self.assertEqual((last.before, last.after), ({"paused": False, "note": "first run was good"},
                                                     {"paused": True, "note": "paused by hand"}))

    async def test_pausing_a_team_without_a_schedule_is_refused(self):
        with self.assertRaises(ops.OpError) as caught:
            await ops.pause(self.client, self.settings, self.ref)
        self.assertIn("no such schedule", str(caught.exception))

    async def test_run_now_opens_a_run_even_while_paused_and_says_when_spend_is_off(self):
        await ops.reconcile(self.client, self.settings)  # created paused
        ops.set_unattended(self.settings, False)
        res = await ops.run_now(self.client, self.settings, ops.team_ref(str(self.repo), "qa"))
        self.addAsyncCleanup(self.client.get_workflow_handle(self.ref.workflow_id).terminate, "test over")
        self.assertFalse(res.unattended)
        snap = await ops.snapshot(self.client, self.settings)
        self.assertEqual([(r.workflow_id, r.status) for r in snap.open_runs], [("team/demo/qa", "running")])
        self.assertEqual([a.action for a in self.actions()], ["reconcile", "spend", "run-now"])

    async def test_a_finished_run_shows_its_outcome_and_summary(self):
        build = f"test-{uuid.uuid4().hex[:8]}"
        async with make_worker(self.client, self.settings, build=build, activities=Fakes().activities()):
            for _ in range(50):  # the version exists once the worker has polled
                try:
                    change = await ops.set_current(self.client, self.settings, build)
                    break
                except RPCError:
                    await asyncio.sleep(0.2)
            self.assertEqual((change.action, change.before, change.after), ("set-current", None, build))
            await ops.run_now(self.client, self.settings, self.ref)
            await self.client.get_workflow_handle(self.ref.workflow_id).result()
        snap = await ops.snapshot(self.client, self.settings)
        (run,) = snap.recent_runs
        self.assertEqual((run.repo_name, run.team, run.status, run.outcome), ("demo", "qa", "completed", "done"))
        self.assertTrue(cli.run_line(run).startswith("team/demo/qa: ✓ Done "))

    async def test_a_terminated_run_shows_why(self):
        await ops.run_now(self.client, self.settings, self.ref)
        await self.client.get_workflow_handle(self.ref.workflow_id).terminate("stopped for the test")
        snap = await ops.snapshot(self.client, self.settings)
        (run,) = snap.recent_runs
        self.assertEqual((run.status, run.outcome), ("terminated", None))
        self.assertIn("The run was terminated", run.failure or "")
        self.assertTrue(cli.run_line(run).startswith("team/demo/qa: ✕ Terminated "))

    async def test_remove_then_add_a_repo(self):
        await ops.reconcile(self.client, self.settings)
        await ops.run_now(self.client, self.settings, self.ref)
        self.addAsyncCleanup(self.client.get_workflow_handle(self.ref.workflow_id).terminate, "test over")
        removed = await ops.remove_repo(self.client, self.settings, str(self.repo))
        self.assertIn("deleted schedule/demo/qa (no longer in config)", removed.report)
        self.assertEqual([r.workflow_id for r in removed.open_runs], ["team/demo/qa"])  # it finishes on its own
        self.assertEqual(settings_mod.load().repos, ())
        snap = await ops.snapshot(self.client, settings_mod.load())
        self.assertEqual((snap.repos, snap.schedules), ((), ()))

        added = await ops.add_repo(self.client, settings_mod.load(), str(self.repo), who="desk")
        self.assertFalse(added.unchanged)
        self.assertIn("created schedule/demo/qa (0 2 * * *, paused until a first run by hand)", added.report)
        snap = await ops.snapshot(self.client, settings_mod.load())
        self.assertEqual([r.name for r in snap.repos], ["demo"])
        log = self.actions()
        self.assertEqual([(a.who, a.action) for a in log if a.action.endswith("-repo")],
                         [("cli", "remove-repo"), ("desk", "add-repo")])
        self.assertEqual(log[-1].after, [str(self.repo.resolve())])

        again = await ops.add_repo(self.client, settings_mod.load(), str(self.repo))
        self.assertTrue(again.unchanged)
        self.assertEqual(len(self.actions()), len(log))  # nothing changed, nothing logged

    async def test_a_repo_without_a_factory_toml_on_its_default_branch_is_not_added(self):
        other, _, tmp = make_repo("true")
        self.addCleanup(tmp.cleanup)
        (other / ".factory.toml").unlink()
        git("commit", "-qam", "opt out", cwd=other)
        git("push", "-q", "origin", "main", cwd=other)
        with self.assertRaises(ops.OpError) as caught:
            await ops.add_repo(self.client, self.settings, str(other))
        self.assertIn("not opted in", str(caught.exception))
        with self.assertRaises(ops.OpError):
            await ops.add_repo(self.client, self.settings, str(other / "missing"))
        self.assertEqual(settings_mod.load().repos, (str(self.repo),))

    async def test_status_prints_one_line_per_schedule_and_run(self):
        await ops.reconcile(self.client, self.settings)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await cli.cmd_status(self.client, self.settings, cli.parser().parse_args(["status"]))
        lines = out.getvalue().splitlines()
        self.assertTrue(any(line.startswith(f"schedule/demo/qa: ‖ Paused ({schedules.NEW_NOTE}), next ") for line in lines))


class SpendSwitchCliTest(unittest.TestCase, Isolated):
    def test_spend_off_and_on_write_the_file_and_the_log_without_a_server(self):
        self.isolate(self)
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        with mock.patch.dict(os.environ, {"FACTORY_ENGINE_STATE": state.name}), \
             mock.patch.object(cli, "get_client", side_effect=AssertionError("no server needed")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(asyncio_run(cli.run(["spend", "off"])), 0)
                self.assertEqual(asyncio_run(cli.run(["spend", "off"])), 0)
            self.assertFalse(settings_mod.load().unattended)
            self.assertIn("spend switch off: no team starts a new agent session", out.getvalue())
            self.assertIn("(it already was)", out.getvalue())
            asyncio_run(cli.run(["spend", "on"]))
            self.assertTrue(settings_mod.load().unattended)
            log = [json.loads(line) for line in (Path(state.name) / ops.ACTIONS_LOG).read_text().splitlines()]
        self.assertEqual([(e["who"], e["action"], e["before"], e["after"]) for e in log],
                         [("cli", "spend", True, False), ("cli", "spend", False, True)])  # the no-op is not logged


def asyncio_run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class SettingsKeysTest(unittest.TestCase):
    def test_the_desk_port_has_a_default_and_loads(self):
        self.assertEqual(EngineSettings().desk_port, 8245)
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write("desk_port = 9000\n")
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(settings_mod.load(Path(f.name)).desk_port, 9000)

    def test_the_settings_path_has_one_owner(self):
        with mock.patch.object(install, "SETTINGS", Path("/elsewhere/engine.toml")):
            self.assertEqual(settings_mod.config_path(), Path("/elsewhere/engine.toml"))

    def test_a_corrupt_log_line_is_skipped(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        settings = EngineSettings(state=Path(state.name))
        ops.log_action(settings, "cli", "spend", "unattended", True, False)
        with open(Path(state.name) / ops.ACTIONS_LOG, "a") as log:
            log.write('{"time": "torn\n[1, 2]\n')
        ops.log_action(settings, "desk", "spend", "unattended", False, True)
        self.assertEqual([c.who for c in ops.read_actions(settings)], ["cli", "desk"])


if __name__ == "__main__":
    unittest.main()
