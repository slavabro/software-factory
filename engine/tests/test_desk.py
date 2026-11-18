"""The Factory Desk (engine/desk): every state as a word and a shape, agent text escaped, the safety checks on every
request, logs and notebooks by ID only, `gh` failures that never fail a page, and the pages against a real local
Temporal server."""

from __future__ import annotations

import asyncio
import http.client
import os
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from temporalio.client import Client
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment

from factory_runtime.temporal import data_converter
from engine import ops, schedules, settings as settings_mod
from engine.activities.teams import REPO_KEY, TEAM_KEY
from engine.desk import pages, repos
from engine.desk.server import Desk, make_server
from engine.factory import install
from engine.models import TeamRef
from engine.settings import NAMESPACE, EngineSettings
from engine.worker import make_worker
from tests.fakes import Fakes
from tests.repo_fixture import make_repo

SCRIPT = "<script>alert(1)</script>"
REF = TeamRef(repo="/r/demo", repo_name="demo", team="qa")
NOW = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)


def run(status: str = "completed", outcome: str | None = "done", summary: str = "", **kw: object) -> ops.RunView:
    fields: dict[str, object] = dict(workflow_id=REF.workflow_id, run_id=str(uuid.uuid4()), repo_name="demo", team="qa",
                                     status=status, started=NOW, closed=NOW, outcome=outcome, summary=summary)
    fields.update(kw)
    return ops.RunView(**fields)  # type: ignore[arg-type]


def team(paused: bool, note: str = "", failures: int = 0) -> ops.TeamView:
    sched = ops.ScheduleView(REF.schedule_id, "demo", "qa", paused, note, NOW + timedelta(hours=1))
    return ops.TeamView(REF, "0 2 * * *", ("ci_red",), failures, sched, prompt="/factory:qa", budget_minutes=5)


def snapshot(teams: tuple[ops.TeamView, ...] = (), runs: tuple[ops.RunView, ...] = (),
             settings: EngineSettings | None = None) -> ops.EngineSnapshot:
    settings = settings or EngineSettings(time_zone="UTC")
    repo = ops.RepoView("/r/demo", "/r/demo", "demo", teams)
    build = ops.BuildView("b1", "f00d", True, ops.BuildStatus("b1", True, True, "unspecified", False))
    return ops.EngineSnapshot(settings, (repo,), (), (), runs, (build,), "b1")


class RenderTest(unittest.TestCase):
    settings = EngineSettings(time_zone="UTC")

    def test_every_run_state_shows_a_word_and_a_shape(self):
        cases = [
            (run(outcome="done"), "done", "Done"), (run(outcome="failed"), "failed", "Failed"),
            (run(outcome="interrupted"), "interrupted", "Interrupted"), (run(outcome="skipped"), "skipped", "Skipped"),
            (run(status="running", outcome=None), "running", "Running"),
            (run(status="continued_as_new", outcome=None), "continued", "Continued"),
            (run(status="terminated", outcome=None, failure="stopped"), "failed", "Terminated"),
            (run(status="completed", outcome=None, failure="unreadable"), "unknown", "Completed"),
        ]
        for view, key, word in cases:
            html = pages.run_item(view, self.settings)
            self.assertIn(f'data-state="{key}"', html, word)
            self.assertIn("<svg", html, word)
            self.assertIn(f"<span>{word}</span>", html)

    def test_engine_health_is_a_word_and_a_shape(self):
        snap = snapshot()
        cases = [
            (pages.Live(None, "connection refused", reachable=False), "stopped", "Engine stopped"),
            (pages.Live(None, "boom"), "degraded", "Engine degraded"),
            (pages.Live(snap), "degraded", "Engine degraded"),  # no worker polling
            (pages.Live(snap, pollers={"b1": NOW}), "running", "Engine running"),
        ]
        for live, key, word in cases:
            self.assertEqual(pages.health(live)[:2], (key, word))
            html = pages.overview(self.settings, live, {}, {})
            self.assertIn(f'data-state="{key}"><svg', html)
            self.assertIn(f"<span>{word}</span>", html)

    def test_schedule_and_spend_states_are_words_and_shapes(self):
        self.assertIn('data-state="active"', pages.schedule_mark(team(paused=False)))
        paused = pages.schedule_mark(team(paused=True, note="by hand"))
        self.assertIn('data-state="paused"', paused)
        self.assertIn("by hand", paused)
        off = pages.spend_card(EngineSettings(unattended=False))
        self.assertIn('data-state="paused"', off)
        self.assertIn("<span>Off</span>", off)
        self.assertIn("stops runs that have not started", off)

    def test_agent_written_text_renders_escaped(self):
        live = pages.Live(snapshot((team(True, note=SCRIPT, failures=3),),
                                   (run(summary=SCRIPT), run(status="failed", outcome=None, failure=SCRIPT))),
                          pollers={"b1": NOW})
        gh = repos.RepoGitHub(repos.Read(repos.CI("failing", "abc", "https://x/1", (SCRIPT,)), 1.0),
                              repos.Read((repos.PR(7, SCRIPT, "https://x/7", "factory/qa/x", False),), 1.0))
        for html in (pages.overview(self.settings, live, {"/r/demo": gh}, {}),
                     pages.projects(self.settings, live, {"/r/demo": gh}, {}),
                     pages.team_page(self.settings, replace(live, team_runs={REF.workflow_id: live.snap.recent_runs}),
                                     team(True, note=SCRIPT, failures=3), REF.workflow_id),
                     pages.runs_page(self.settings, live),
                     pages.text_page(self.settings, "Log", "/", SCRIPT)):
            self.assertNotIn("<script", html)
            self.assertIn("&lt;script&gt;", html)

    def test_needs_you_lists_new_teams_and_teams_paused_after_failures_with_the_note(self):
        new = team(True, note=schedules.NEW_NOTE)
        failing = replace(team(True, note="paused after 3 failed runs in a row; last: setup broke", failures=3),
                          ref=REF.model_copy(update={"team": "upgrade"}))
        by_hand = replace(team(True, note="holiday"), ref=REF.model_copy(update={"team": "docs"}))
        html = pages.needs_you(snapshot((new, failing, by_hand)))
        self.assertIn("demo · qa", html)
        self.assertIn("until its first run by hand", html)
        self.assertIn("demo · upgrade", html)
        self.assertIn("paused after 3 failed runs in a row; last: setup broke", html)
        self.assertNotIn("demo · docs", html)  # paused by hand: not something that needs the principal
        self.assertIn("Nothing needs you", pages.needs_you(snapshot((team(False),))))

    def test_a_gh_failure_reads_unknown_with_the_reason_and_the_last_value(self):
        never = pages.ci_mark(repos.Read(error="gh failed: no network"))
        self.assertIn('data-state="unknown"', never)
        self.assertIn("gh failed: no network", never)
        import time
        stale = pages.ci_mark(repos.Read(repos.CI("passing"), time.time() - 1500, "gh took longer than 15 s"))
        self.assertIn('data-state="unknown"', stale)
        self.assertIn("last: passing, 25 min ago", stale)
        self.assertIn('data-state="unknown"', pages.prs_html(repos.Read(error="gh failed")))

    def test_who_woke_a_run_and_the_schedule_in_words(self):
        self.assertIn("woken by its schedule, hand", pages.run_item(run(reasons=("schedule", "manual")), self.settings))
        self.assertEqual(pages.cron_words("0 6 * * 1"), "Mondays 06:00")
        self.assertEqual(pages.cron_words("30 2 * * *"), "Daily 02:30")
        self.assertEqual(pages.cron_words("*/5 * * * *"), "*/5 * * * *")
        self.assertIn("Mon 16 Nov 06:00", pages.when(datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc),
                                                    EngineSettings(time_zone="Asia/Jerusalem")))


class GitHubReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_ci_counts_each_workflow_of_the_latest_commit_once(self):
        rows = [{"headSha": "b", "workflowName": "CI", "status": "completed", "conclusion": "success", "url": "u1"},
                {"headSha": "b", "workflowName": "Lint", "status": "in_progress", "conclusion": "", "url": "u2"},
                {"headSha": "a", "workflowName": "CI", "status": "completed", "conclusion": "failure", "url": "u3"}]
        self.assertEqual(repos.ci_from_runs(rows).state, "running")
        rows[1]["conclusion"], rows[1]["status"] = "failure", "completed"
        ci = repos.ci_from_runs(rows)
        self.assertEqual((ci.state, ci.failing, ci.url), ("failing", ("Lint",), "u2"))
        self.assertEqual(repos.ci_from_runs([]).state, "none")

    def test_a_failed_read_keeps_the_last_value_and_is_cached(self):
        gh = repos.GitHub(fresh=0, retry=3600)
        with mock.patch.object(repos, "run_gh", return_value=[]), \
             mock.patch.object(repos, "default_branch", return_value="main"):
            first = gh.repo(self.root)
        self.assertEqual((first.ci.value, first.prs.value, first.ci.error), (repos.CI("none"), (), ""))
        with mock.patch.object(repos, "run_gh", side_effect=repos.GhError("gh failed: offline")) as gh_call, \
             mock.patch.object(repos, "default_branch", return_value="main"):
            second = gh.repo(self.root)
            third = gh.repo(self.root)
        self.assertEqual((second.ci.value, second.ci.error), (repos.CI("none"), "gh failed: offline"))
        self.assertIsNotNone(second.ci.read_at)
        self.assertEqual(third, second)
        self.assertEqual(gh_call.call_count, 2)  # one try per read; the failure is not retried at once

    def test_gh_has_a_timeout_and_a_missing_gh_is_a_reason(self):
        with mock.patch.object(repos.subprocess, "run", side_effect=FileNotFoundError) as call:
            with self.assertRaisesRegex(repos.GhError, "not installed"):
                repos.run_gh(self.root, "pr", "list")
        self.assertEqual(call.call_args.kwargs["timeout"], repos.GH_TIMEOUT)

    def test_only_factory_branches_are_factory_prs(self):
        rows = [{"number": 1, "title": "t", "url": "u", "headRefName": "factory/upgrade-1", "isDraft": False},
                {"number": 2, "title": "t", "url": "u", "headRefName": "feature/x", "isDraft": False}]
        with mock.patch.object(repos, "run_gh", return_value=rows):
            self.assertEqual([p.number for p in repos.read_prs(self.root)], [1])


class Isolated:
    """The desk's token, engine.toml and builds in a temp dir, never this machine's own install."""

    def isolate(self, case: unittest.TestCase) -> Path:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        for name, value in (("DESK_TOKEN", base / "desk.token"), ("SETTINGS", base / "engine.toml"),
                            ("BUILDS", base / "builds"), ("CURRENT", base / "current"), ("SOURCE", base / "source"),
                            ("DEPLOY_LOCK", base / "deploy.lock")):
            patch = mock.patch.object(install, name, value)
            patch.start()
            case.addCleanup(patch.stop)
        return base


class Served:
    """A desk on a free port in this process, and a client that can set Host and Cookie."""

    def serve(self, case: unittest.TestCase, settings: EngineSettings) -> None:
        self.desk = Desk(load_settings=lambda: settings, github=repos.GitHub())
        self.server = make_server(self.desk, 0)
        self.port = self.server.server_port
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        case.addCleanup(self.desk.close)
        case.addCleanup(self.server.server_close)
        case.addCleanup(self.server.shutdown)
        self.cookie = ""

    def get(self, target: str, host: str | None = None, cookie: str | None = None,
            method: str = "GET") -> tuple[int, dict[str, str], str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        conn.putrequest(method, target, skip_host=True)
        conn.putheader("Host", host or f"localhost:{self.port}")
        if cookie if cookie is not None else self.cookie:
            conn.putheader("Cookie", cookie if cookie is not None else self.cookie)
        conn.endheaders()
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body

    def sign_in(self) -> None:
        status, headers, _ = self.get(f"/?token={install.desk_token()}", cookie="")
        assert status == 303, status
        self.cookie = headers["set-cookie"].split(";")[0]


class SafetyTest(unittest.TestCase, Isolated, Served):
    """Every request passes Host and token checks; the server's Temporal is down here, so pages still render."""

    def setUp(self):
        self.base = self.isolate(self)
        self.repo, self.state, tmp = make_repo("true")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_ENGINE_STATE", None)
        self.settings = EngineSettings(repos=(str(self.repo),), time_zone="UTC", server_port=1)  # nothing listens
        self.serve(self, self.settings)

    def test_a_wrong_host_is_refused_before_anything_else(self):
        token = install.desk_token()
        for host in ("evil.example:%d" % self.port, f"localhost:{self.port + 1}", "localhost", "127.0.0.1"):
            status, headers, body = self.get(f"/?token={token}", host=host)
            self.assertEqual(status, 403, host)
            self.assertNotIn("set-cookie", headers)
        self.assertEqual(self.get("/", host=f"127.0.0.1:{self.port}")[0], 403)  # right host, no cookie yet

    def test_without_the_cookie_every_page_is_refused_with_how_to_get_in(self):
        for path in ("/", "/projects", "/runs", "/engine", "/desk.css", "/notebook?team=team/demo/qa", "/nope"):
            status, headers, body = self.get(path)
            self.assertEqual(status, 403, path)
            self.assertIn("factory engine status", body)
            self.assertTrue(headers["content-type"].startswith("text/plain"))
        self.assertEqual(self.get("/", cookie=f"factory_desk_{self.port}=wrong")[0], 403)

    def test_the_token_is_traded_for_a_strict_cookie_and_a_clean_url(self):
        token = install.desk_token()
        self.assertEqual(os.stat(install.DESK_TOKEN).st_mode & 0o777, 0o600)
        status, headers, _ = self.get(f"/team?id=team%2Fdemo%2Fqa&token={token}")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/team?id=team%2Fdemo%2Fqa")
        cookie = headers["set-cookie"]
        for part in ("HttpOnly", "SameSite=Strict", "Path=/", f"factory_desk_{self.port}={token}"):
            self.assertIn(part, cookie)
        self.assertEqual(self.get("/?token=not-it")[0], 403)
        self.cookie = cookie.split(";")[0]
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-security-policy"].split(";")[0], "default-src 'self'")
        self.assertFalse([h for h in headers if h.startswith("access-control-")])
        self.assertIn('data-state="stopped"', body)  # the server is down: the page says so, in a word and a shape
        self.assertIn("<span>Engine stopped</span>", body)
        self.assertEqual(self.get("/", method="POST")[0], 405)  # actions are step 2

    def test_logs_and_notebooks_are_served_by_id_only(self):
        self.sign_in()
        notes = self.state / "teams" / "demo" / "qa"
        notes.mkdir(parents=True)
        (notes / "notebook.md").write_text(f"## run · done\n\n{SCRIPT}\n")
        logs = self.state / "logs" / "demo" / "qa"
        logs.mkdir(parents=True)
        run_id = "abcdef12-0000-4000-8000-000000000000"
        (logs / "20261118-abcdef12.log").write_text("the agent's log\n")
        (self.base / "secret.txt").write_text("not for the desk")

        status, _, body = self.get("/notebook?team=team/demo/qa")
        self.assertEqual(status, 200)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn(SCRIPT, body)
        status, _, body = self.get(f"/log?team=team/demo/qa&run={run_id}")
        self.assertEqual(status, 200)
        self.assertIn("the agent&#x27;s log", body)

        for target in ("/notebook?team=team/demo/../../..", "/notebook?team=team/../secret.txt",
                       f"/notebook?team={self.base}/secret.txt", "/notebook?team=team/demo/nobody",
                       "/log?team=team/demo/qa&run=../../secret.txt", "/log?team=team/demo/qa&run=abcdef12*",
                       f"/log?team=team/other/qa&run={run_id}", "/notebook?team=team/demo/qa/../../../secret.txt",
                       "/notebook?file=../secret.txt", f"/log?path={self.base}/secret.txt"):
            status, _, body = self.get(target.replace(" ", "%20"))
            self.assertEqual(status, 404, target)
            self.assertNotIn("not for the desk", body)


class DeskServerTest(unittest.IsolatedAsyncioTestCase, Isolated, Served):
    """The pages against a real local Temporal server (the SDK's dev server, as test_ops uses)."""

    async def asyncSetUp(self):
        self.env = await WorkflowEnvironment.start_local(
            namespace=NAMESPACE, data_converter=data_converter(), search_attributes=[REPO_KEY, TEAM_KEY]
        )
        self.addAsyncCleanup(self.env.shutdown)
        self.client: Client = self.env.client
        self.isolate(self)
        self.repo, self.state, tmp = make_repo("true")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_ENGINE_STATE", None)
        host, port = self.client.service_client.config.target_host.rsplit(":", 1)
        install.SETTINGS.write_text(f'repos = ["{self.repo}"]\ntime_zone = "UTC"\nserver_host = "{host}"\n'
                                    f"server_port = {port}\n")
        self.settings = settings_mod.load()
        self.ref = TeamRef(repo=str(self.repo.resolve()), repo_name="demo", team="qa")
        self.serve(self, self.settings)
        await asyncio.to_thread(self.sign_in)

    async def page(self, target: str) -> str:
        status, _, body = await asyncio.to_thread(self.get, target)
        self.assertEqual(status, 200, target)
        return body

    async def test_every_page_renders_from_the_real_engine(self):
        await ops.reconcile(self.client, self.settings)
        build = f"test-{uuid.uuid4().hex[:8]}"
        (install.BUILDS / build).mkdir(parents=True)
        (install.BUILDS / build / install.COMMIT).write_text("c0ffee\n")
        install.CURRENT.write_text(build)
        with mock.patch.object(repos, "run_gh", side_effect=repos.GhError("gh failed: no network")):
            async with make_worker(self.client, self.settings, build=build, activities=Fakes().activities()):
                for _ in range(50):  # the version exists once the worker has polled
                    try:
                        await ops.set_current(self.client, self.settings, build)
                        break
                    except RPCError:
                        await asyncio.sleep(0.2)
                await ops.run_now(self.client, self.settings, self.ref)
                await self.client.get_workflow_handle(self.ref.workflow_id).result()

                overview = await self.page("/")
                self.assertIn("<span>Engine running</span>", overview)
                self.assertIn("demo · qa", overview)  # needs you: new, paused until a first run by hand
                self.assertIn("until its first run by hand", overview)
                self.assertIn('data-state="done"', overview)
                self.assertIn("woken by hand", overview)
                self.assertIn("report sent", overview)
                self.assertIn("gh failed: no network", overview)  # CI and PRs unknown, the page still renders

                projects = await self.page("/projects")
                self.assertIn('data-state="paused"', projects)
                self.assertIn(schedules.NEW_NOTE, projects)
                self.assertIn("Daily 02:00", projects)

                team_page = await self.page("/team?id=team%2Fdemo%2Fqa")
                self.assertIn("/factory:qa", team_page)
                self.assertIn("report sent", team_page)
                self.assertIn("(UTC; cron <code>0 2 * * *</code>)", team_page)
                self.assertIn('data-state="paused"', team_page)  # its schedule's state, from the snapshot
                self.assertIn(schedules.NEW_NOTE, team_page)

                runs = await self.page("/runs")
                self.assertIn('data-state="done"', runs)
                self.assertIn("/namespaces/factory/workflows/team%2Fdemo%2Fqa/", runs)

                engine = await self.page("/engine")
                self.assertIn("<span>Reachable</span>", engine)
                self.assertIn(build, engine)
                self.assertIn("c0ffee", engine)
                self.assertIn("worker polling", engine)

        status, _, _ = await asyncio.to_thread(self.get, "/team?id=team%2Fdemo%2Fnobody")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
