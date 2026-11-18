"""The Factory Desk (engine/desk): every state as a word and a shape, agent text escaped, the safety checks on every
request, logs and notebooks by ID only, `gh` failures that never fail a page, and the pages against a real local
Temporal server."""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import http.server
import io
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from temporalio.client import Client
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment

from factory_runtime.temporal import data_converter
from engine import ops, schedules, settings as settings_mod
from engine.activities.teams import REPO_KEY, TEAM_KEY
from engine.desk import pages, repos
from engine import cli
from engine.desk import server as desk_server
from engine.desk.server import Desk, make_server
from engine.factory import install
from engine.models import TeamRef
from engine.settings import NAMESPACE, EngineSettings
from engine.worker import make_worker
from tests.fakes import Fakes
from tests.repo_fixture import git, make_repo

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

    def test_change_pages_escape_what_they_show_and_need_no_script(self):
        deploy = ops.DeployRun("20261118T090000Z", SCRIPT, "failed", 2, {"source": SCRIPT, "branch": SCRIPT})
        plan = ops.DeployPlan(SCRIPT, "a" * 40, SCRIPT, "b1", "c" * 40, None)
        live = pages.Live(snapshot((team(True, note=SCRIPT),)), pollers={"b1": NOW})
        for html in (pages.deploy_page(self.settings, deploy), pages.confirm_deploy(self.settings, plan, "f"),
                     pages.engine_page(self.settings, live, plan, [deploy], "f"),
                     pages.confirm_remove_repo(self.settings, live, SCRIPT, "f"),
                     pages.error_page(self.settings, "Not done", SCRIPT),
                     pages.team_page(self.settings, live, team(True, note=SCRIPT), REF.workflow_id, "f",
                                     pages.team_notice("pause", team(True, note=SCRIPT), live, self.settings))):
            self.assertNotIn("<script", html)
            self.assertIn("&lt;script&gt;", html)
        self.assertIn('data-state="failed"><svg', pages.deploy_page(self.settings, deploy))
        self.assertIn("<span>Failed (exit 2)</span>", pages.deploy_page(self.settings, deploy))

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

    def test_pages_loading_at_once_run_gh_once(self):
        gh, calls = repos.GitHub(), []

        def slow(root: Path) -> repos.CI:
            calls.append(root)
            time.sleep(0.2)
            return repos.CI("passing")

        threads = [threading.Thread(target=gh._get, args=(self.root, "ci", slow)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(calls), 1)

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

    def serve(self, case: unittest.TestCase, settings: EngineSettings | None = None) -> None:
        """With `settings` fixed, else read from engine.toml at each request (as the desk does)."""
        load = settings_mod.load if settings is None else (lambda: settings)
        self.desk = Desk(load_settings=load, github=repos.GitHub())
        self.server = make_server(self.desk, 0)
        self.port = self.server.server_port
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        case.addCleanup(self.desk.close)
        case.addCleanup(self.server.server_close)
        case.addCleanup(self.server.shutdown)
        self.cookie = ""

    def get(self, target: str, host: str | None = None, cookie: str | None = None, method: str = "GET",
            origin: str | None = None, body: bytes = b"") -> tuple[int, dict[str, str], str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        conn.putrequest(method, target, skip_host=True)
        conn.putheader("Host", host or f"localhost:{self.port}")
        if cookie if cookie is not None else self.cookie:
            conn.putheader("Cookie", cookie if cookie is not None else self.cookie)
        if origin is not None:
            conn.putheader("Origin", origin)
        if method == "POST":
            conn.putheader("Content-Type", "application/x-www-form-urlencoded")
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body or None)
        resp = conn.getresponse()
        text = resp.read().decode()
        conn.close()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, text

    def sign_in(self) -> None:
        status, headers, _ = self.get(f"/?token={install.desk_token()}", cookie="")
        assert status == 303, status
        self.cookie = headers["set-cookie"].split(";")[0]

    def form(self) -> str:
        return desk_server.form_token(install.desk_token())

    def post(self, action: str, origin: str | None = "", token: str | None = "",
             **fields: str) -> tuple[int, dict[str, str], str]:
        """POST a change as the desk's own page does: its Origin and form token unless told otherwise."""
        origin = f"http://localhost:{self.port}" if origin == "" else origin
        token = self.form() if token == "" else token
        body = urlencode({**({"form": token} if token is not None else {}), **fields}).encode()
        return self.get(f"/act/{action}", method="POST", origin=origin, body=body)

    def follow(self, action: str, **fields: str) -> str:
        """POST a change, check it redirects, and load the page it redirects to."""
        status, headers, body = self.post(action, **fields)
        assert status == 303, (status, body)
        status, _, body = self.get(headers["location"])
        assert status == 200, (status, body)
        return body


class SafetyTest(unittest.TestCase, Isolated, Served):
    """Every request passes Host and token checks; the server's Temporal is down here, so pages still render."""

    def setUp(self):
        self.base = self.isolate(self)
        self.repo, self.state, tmp = make_repo("true")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_ENGINE_STATE", None)
        install.SETTINGS.write_text(f'repos = ["{self.repo}"]\ntime_zone = "UTC"\nserver_port = 1\n')  # nothing listens
        self.settings = settings_mod.load()
        self.serve(self)

    def test_a_request_never_makes_the_token(self):
        status, _, body = self.get("/?token=anything")
        self.assertEqual(status, 403)
        self.assertIn("factory engine desk", body)
        self.assertFalse(install.DESK_TOKEN.exists())

    def test_other_apps_malformed_cookies_dont_hide_the_desks(self):
        self.sign_in()
        mine = self.cookie
        for others in ('x={"a":1, "b":2}', 'sess=a b', 'a="x', 'broken', '"=;;'):
            status, _, _ = self.get("/desk.css", cookie=f"{others}; {mine}; later=1")
            self.assertEqual(status, 200, others)
        self.assertEqual(self.get("/desk.css", cookie='x={"a":1}; other=2')[0], 403)

    def test_the_cookie_is_derived_from_the_token_not_the_token(self):
        token = install.desk_token()
        self.sign_in()
        self.assertNotIn(token, self.cookie)
        self.assertEqual(self.cookie, f"factory_desk_{self.port}={desk_server.cookie_value(token)}")
        self.assertEqual(self.get("/desk.css", cookie=f"factory_desk_{self.port}={token}")[0], 403)

    def test_status_tells_the_desk_from_another_program_on_its_port(self):
        self.assertEqual(desk_server.probe(self.port), "up")
        quiet = type("Quiet", (http.server.BaseHTTPRequestHandler,), {"log_message": lambda *a: None})
        other = http.server.HTTPServer(("127.0.0.1", 0), quiet)  # answers 501, without the desk's header
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        self.assertEqual(desk_server.probe(other.server_port), "port held by something else")
        free = other.server_port + 1 if other.server_port + 1 != self.port else other.server_port + 2
        self.assertIn(desk_server.probe(free), ("not running", "port held by something else"))

    def test_the_desk_command_prints_the_sign_in_link_and_makes_the_token(self):
        out = io.StringIO()
        settings = replace(self.settings, desk_port=self.port)
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.cmd_desk(settings, cli.parser().parse_args(["desk"])), 0)
        token = install.read_desk_token()
        self.assertEqual(out.getvalue().strip(), f"http://localhost:{self.port}/?token={token}")
        self.assertIn(f"— up;", cli.desk_line(settings))
        self.assertNotIn(token or "?", cli.desk_line(settings))

    def test_the_request_log_never_holds_the_token(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
                sock.sendall(b"GET /?token=s3cret extra words HTTP/1.1\r\n\r\n")  # a bad request line is logged
                answer = sock.recv(4096)
        self.assertIn(b" 400 ", answer)
        self.assertIn("token=", err.getvalue())
        self.assertNotIn("s3cret", err.getvalue())

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
            self.assertIn("factory engine desk", body)
            self.assertTrue(headers["content-type"].startswith("text/plain"))
        self.assertEqual(self.get("/", cookie=f"factory_desk_{self.port}=wrong")[0], 403)

    def test_the_token_is_traded_for_a_strict_cookie_and_a_clean_url(self):
        token = install.desk_token()
        self.assertEqual(os.stat(install.DESK_TOKEN).st_mode & 0o777, 0o600)
        status, headers, _ = self.get(f"/team?id=team%2Fdemo%2Fqa&token={token}")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/team?id=team%2Fdemo%2Fqa")
        cookie = headers["set-cookie"]
        for part in ("HttpOnly", "SameSite=Strict", "Path=/", f"factory_desk_{self.port}={desk_server.cookie_value(token)}"):
            self.assertIn(part, cookie)
        self.assertEqual(self.get("/?token=not-it")[0], 403)
        self.cookie = cookie.split(";")[0]
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-security-policy"].split(";")[0], "default-src 'self'")
        self.assertFalse([h for h in headers if h.startswith("access-control-")])
        self.assertIn('data-state="stopped"', body)  # the server is down: the page says so, in a word and a shape
        self.assertIn("<span>Engine stopped</span>", body)
        self.assertEqual(self.get("/", method="PUT")[0], 405)

    def test_a_change_needs_the_desks_exact_origin(self):
        self.sign_in()
        for origin in (None, "null", f"http://localhost:{self.port + 1}", f"http://127.0.0.1:{self.port}",
                       f"https://localhost:{self.port}", "http://evil.example", f"http://localhost:{self.port}/"):
            status, _, body = self.post("spend-off", origin=origin)
            self.assertEqual(status, 403, origin)
            self.assertIn("Origin", body)
        self.assertTrue(settings_mod.load().unattended)  # nothing changed
        self.assertEqual(ops.read_actions(self.settings), [])

    def test_a_change_needs_the_form_token(self):
        self.sign_in()
        for token in (None, " ", "0" * 64, desk_server.cookie_value(install.desk_token()), install.desk_token()):
            status, _, body = self.post("spend-off", token=token)  # None: no form token at all
            self.assertEqual(status, 403, token)
            self.assertIn("form", body)
        self.assertEqual(self.get("/act/spend-off", method="POST", origin=f"http://localhost:{self.port}",
                                  body=b"form=" + self.form().encode(), cookie="")[0], 403)  # and the cookie

    def test_the_spend_switch_asks_before_turning_on_and_reads_back_what_changed(self):
        self.sign_in()
        overview = self.follow("spend-off")
        self.assertFalse(settings_mod.load().unattended)
        self.assertIn('class="notice"', overview)
        self.assertIn('data-state="paused"><svg', overview.split('class="notice"')[1])  # a shape and a word
        self.assertIn("Unattended agent runs are off", overview)

        status, headers, _ = self.post("spend-on")  # not confirmed: to the confirm page, nothing changed
        self.assertEqual((status, headers["location"]), (303, "/confirm?action=spend-on"))
        self.assertFalse(settings_mod.load().unattended)
        status, _, confirm = self.get(headers["location"])
        self.assertEqual(status, 200)
        self.assertIn('action="/act/spend-on"', confirm)
        self.assertIn('name="confirmed" value="yes"', confirm)
        self.assertIn(f'name="form" value="{self.form()}"', confirm)
        self.assertIn("spends money", confirm)
        self.assertNotIn("<script", confirm)

        overview = self.follow("spend-on", confirmed="yes")
        self.assertTrue(settings_mod.load().unattended)
        self.assertIn("Unattended agent runs are on", overview)
        log = ops.read_actions(self.settings)
        self.assertEqual([(c.who, c.action, c.before, c.after) for c in log],
                         [("desk", "spend", True, False), ("desk", "spend", False, True)])

    def test_run_now_confirms_and_says_it_would_stop_at_once_while_spend_is_off(self):
        self.sign_in()
        install.set_setting("unattended", False)
        status, headers, _ = self.post("run-now", team="team/demo/qa")
        self.assertEqual(status, 303)
        status, _, confirm = self.get(headers["location"])
        self.assertEqual(status, 200)
        self.assertIn("demo · qa", confirm)
        self.assertIn("would stop at once", confirm)
        self.assertIn('data-state="paused"><svg', confirm)
        install.set_setting("unattended", True)
        self.assertNotIn("would stop at once", self.get(headers["location"])[2])
        self.assertEqual(self.get("/confirm?action=run-now&team=team/demo/nobody")[0], 404)

    def test_a_change_the_engine_cant_take_is_a_page_with_the_reason(self):
        self.sign_in()
        status, _, body = self.post("pause", team="team/demo/qa", note="holiday")
        self.assertEqual(status, 503)
        self.assertIn("not answering", body)
        self.assertIn('data-state="failed"><svg', body)
        self.assertIn("<span>Not done</span>", body)
        self.assertNotIn("Traceback", body)
        status, _, body = self.post("pause", team="team/demo/nobody")
        self.assertEqual(status, 409)
        self.assertIn("no such team", body)
        status, _, body = self.post("add-repo", path="  ")
        self.assertEqual(status, 409)
        self.assertIn("give the path", body)
        status, _, body = self.post("remove-repo", entry="/not/one", confirmed="yes")
        self.assertEqual(status, 409)
        self.assertEqual(self.post("format-disk")[0], 404)

    def source_checkout(self, script: str) -> Path:
        """A source checkout whose `home/bin/factory` is `script`, recorded as the one deploys come from, and a
        current build made from an older commit."""
        checkout = self.base / "factory"
        factory = checkout / "home" / "bin" / "factory"
        factory.parent.mkdir(parents=True)
        factory.write_text("#!/bin/sh\n" + script)
        factory.chmod(0o755)
        git("init", "-q", "-b", "main", str(checkout), cwd=self.base)
        for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
            git("config", key, value, cwd=checkout)
        git("add", ".", cwd=checkout)
        git("commit", "-qm", "init", cwd=checkout)
        install.SOURCE.write_text(str(checkout) + "\n")
        (install.BUILDS / "b1").mkdir(parents=True)
        (install.BUILDS / "b1" / install.COMMIT).write_text("0ld\n")
        install.CURRENT.write_text("b1")
        return checkout

    def deploy_page(self, location: str, until: str) -> str:
        for _ in range(100):
            status, _, body = self.get(location)
            self.assertEqual(status, 200)
            if until in body:
                return body
            time.sleep(0.1)
        self.fail(f"the deploy page never said {until}: {body}")

    def test_deploy_confirms_runs_the_checkouts_upgrade_and_shows_its_log_by_id(self):
        self.sign_in()
        self.source_checkout('echo "upgrade asked: $*"\necho "<b>built</b>"\n')
        engine = self.get("/engine")[2]
        self.assertIn('action="/confirm"', engine)
        self.assertIn('name="action" value="deploy"', engine)
        status, headers, _ = self.post("deploy")
        self.assertEqual((status, headers["location"]), (303, "/confirm?action=deploy"))
        confirm = self.get("/confirm?action=deploy")[2]
        self.assertIn("factory engine upgrade", confirm)
        self.assertIn("0ld", confirm)

        status, headers, _ = self.post("deploy", confirmed="yes")
        self.assertEqual(status, 303)
        location = headers["location"]
        self.assertRegex(location, r"^/deploy\?id=\d{8}T\d{6}Z$")
        page = self.deploy_page(location, "<span>Deployed</span>")
        self.assertIn('data-state="done"><svg', page)
        self.assertIn("upgrade asked: engine upgrade", page)
        self.assertIn("&lt;b&gt;built&lt;/b&gt;", page)  # the log is text
        self.assertNotIn(str(self.state), page)  # a deploy is found by its ID, the page never names its log's path
        (change,) = [c for c in ops.read_actions(self.settings) if c.action == "deploy"]
        self.assertEqual((change.who, change.before), ("desk", "0ld"))
        for target in ("/deploy?id=../../secret", "/deploy?id=20261118T000000Z", f"/deploy?path={self.state}"):
            self.assertEqual(self.get(target)[0], 404, target)

    def test_a_failed_upgrade_reads_failed_with_its_exit_status(self):
        self.sign_in()
        self.source_checkout('echo "no docker"\nexit 3\n')
        status, headers, _ = self.post("deploy", confirmed="yes")
        page = self.deploy_page(headers["location"], "Failed (exit 3)")
        self.assertIn('data-state="failed"><svg', page)

    def test_a_second_deploy_is_refused_while_one_runs(self):
        self.sign_in()
        self.source_checkout('sleep 3\n')
        with install.deploy_lock("upgrade"):
            status, _, body = self.post("deploy", confirmed="yes")
            self.assertEqual(status, 409)
            self.assertIn(f"upgrade (pid {os.getpid()})", body)
            self.assertNotIn('value="deploy"', self.get("/engine")[2])  # not offered meanwhile
            self.assertEqual(self.get("/confirm?action=deploy")[0], 409)
        # one the desk started, before it holds the lock (the fake upgrade never takes it)
        status, headers, _ = self.post("deploy", confirmed="yes")
        self.assertEqual(status, 303)
        status, _, body = self.post("deploy", confirmed="yes")
        self.assertEqual(status, 409)
        self.assertIn("is running", body)

    def test_two_deploys_at_once_start_one(self):
        self.sign_in()
        self.source_checkout('sleep 3\n')
        results: list[int] = []
        threads = [threading.Thread(target=lambda: results.append(self.post("deploy", confirmed="yes")[0]))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [303, 409, 409, 409])

    def test_a_deploy_that_died_without_its_exit_status_blocks_nothing(self):
        self.sign_in()
        self.source_checkout("true\n")
        folder = self.state / ops.DEPLOYS
        folder.mkdir(parents=True)
        # Killed (or the machine went down) an hour ago: no exit line, and its pid may belong to anything by now.
        (folder / "20261118T080000Z.log").write_text("factory desk: deploying\nbuilding…\n")
        (folder / "20261118T080000Z.json").write_text(f'{{"started": {time.time() - 3600}, "pid": {os.getpid()}}}')
        run = ops.read_deploy(self.settings, "20261118T080000Z")
        assert run is not None
        self.assertEqual(run.state, "unknown")
        with install.deploy_lock("upgrade"):  # the newest one, while the lock is held: running
            self.assertEqual(ops.read_deploy(self.settings, "20261118T080000Z").state, "running")  # type: ignore[union-attr]
        self.assertIn("<span>Ended without an exit status</span>", self.get("/deploy?id=20261118T080000Z")[2])
        self.assertEqual(self.post("deploy", confirmed="yes")[0], 303)

    def test_browsers_send_the_origin_the_desk_checks(self):
        # Under Referrer-Policy no-referrer a browser sends `Origin: null` even on a same-origin form POST, and every
        # change would be refused; same-origin keeps the real Origin on the desk's own forms (the confirm page's too).
        self.sign_in()
        for path in ("/", "/confirm?action=spend-on", "/engine"):
            self.assertEqual(self.get(path)[1]["referrer-policy"], "same-origin", path)
        self.assertEqual(self.post("spend-off", origin="null")[0], 403)
        self.assertEqual(self.post("spend-off")[0], 303)

    def test_nothing_to_deploy_when_the_build_is_the_checkouts_head(self):
        self.sign_in()
        checkout = self.source_checkout("true\n")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True).stdout
        (install.BUILDS / "b1" / install.COMMIT).write_text(head)
        self.assertIn("nothing to deploy", self.get("/engine")[2])
        status, _, body = self.post("deploy", confirmed="yes")
        self.assertEqual(status, 409)
        self.assertIn("already the source checkout", body)

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
        self.serve(self)  # engine.toml read at each request, as the desk does
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

    async def test_a_failed_secondary_read_shows_unknown_and_keeps_the_rest(self):
        await ops.reconcile(self.client, self.settings)
        real = ops.recent_runs

        async def team_runs_fail(client: Client, limit: int, workflow_id: str | None = None) -> list[ops.RunView]:
            if workflow_id is not None:  # one team's runs; the snapshot's own read still works
                raise RuntimeError("runs unreadable")
            return await real(client, limit)

        (install.BUILDS / "b1").mkdir(parents=True)  # a build, so the engine page has a worker line to show
        with mock.patch.object(repos, "run_gh", side_effect=repos.GhError("offline")), \
             mock.patch.object(ops, "pollers", side_effect=RuntimeError("pollers unreadable")), \
             mock.patch.object(ops, "recent_runs", team_runs_fail):
            projects = await self.page("/projects")
            team_page = await self.page("/team?id=team%2Fdemo%2Fqa")
            engine = await self.page("/engine")
        self.assertIn("runs unreadable", projects)  # the team's last run: unknown, with why
        self.assertIn('data-state="unknown"', projects)
        self.assertIn(schedules.NEW_NOTE, projects)  # the snapshot is still there
        self.assertIn("runs unreadable", team_page)
        self.assertIn('data-state="paused"', team_page)
        self.assertIn("pollers unreadable", engine)
        self.assertIn("<span>Reachable</span>", engine)

    async def scheduled(self) -> ops.ScheduleView:
        snap = await ops.snapshot(self.client, settings_mod.load())
        return next(s for s in snap.schedules if s.schedule_id == self.ref.schedule_id)

    async def test_pause_resume_and_run_now_from_the_desk(self):
        await ops.reconcile(self.client, self.settings)  # created paused
        page = await asyncio.to_thread(self.follow, "resume", team=self.ref.workflow_id, note="first run was good")
        s = await self.scheduled()
        self.assertEqual((s.paused, s.note), (False, "first run was good"))
        notice = page.split('class="notice"')[1].split("</p>")[0]
        self.assertIn('data-state="active"><svg', notice)
        self.assertIn("its schedule is active (note: first run was good)", notice)
        self.assertIn('action="/act/pause"', page)  # now offers the opposite

        page = await asyncio.to_thread(self.follow, "pause", team=self.ref.workflow_id, note="")
        s = await self.scheduled()
        self.assertEqual((s.paused, s.note), (True, "paused by hand"))
        self.assertIn("its schedule is paused (note: paused by hand)", page)

        status, headers, _ = await asyncio.to_thread(self.post, "run-now", team=self.ref.workflow_id)
        self.assertEqual(status, 303)
        self.assertTrue(headers["location"].startswith("/confirm?"))
        page = await asyncio.to_thread(self.follow, "run-now", team=self.ref.workflow_id, confirmed="yes")
        self.addAsyncCleanup(self.client.get_workflow_handle(self.ref.workflow_id).terminate, "test over")
        snap = await ops.snapshot(self.client, self.settings)
        self.assertEqual([r.workflow_id for r in snap.open_runs], [self.ref.workflow_id])
        self.assertIn("demo · qa is running now", page)  # no worker here: it stays open
        log = ops.read_actions(self.settings)
        self.assertEqual([(c.who, c.action) for c in log if c.who == "desk"],
                         [("desk", "resume"), ("desk", "pause"), ("desk", "run-now")])

    async def test_add_a_repo_needs_its_factory_toml_and_remove_lists_what_goes(self):
        await ops.reconcile(self.client, self.settings)
        other, _, tmp = make_repo("true")
        self.addCleanup(tmp.cleanup)
        os.environ["FACTORY_ENGINE_STATE"] = str(self.state)  # make_repo pointed it at its own
        (other / ".factory.toml").unlink()
        git("commit", "-qam", "opt out", cwd=other)
        git("push", "-q", "origin", "main", cwd=other)
        status, _, body = await asyncio.to_thread(self.post, "add-repo", path=str(other))
        self.assertEqual(status, 409)
        self.assertIn("not opted in (no .factory.toml on its default branch)", body)
        self.assertEqual(settings_mod.load().repos, (str(self.repo),))

        await ops.run_now(self.client, self.settings, self.ref)
        self.addAsyncCleanup(self.client.get_workflow_handle(self.ref.workflow_id).terminate, "test over")
        entry = str(self.repo)
        status, headers, _ = await asyncio.to_thread(self.post, "remove-repo", entry=entry)
        self.assertEqual(status, 303)
        confirm = await self.page(headers["location"])
        self.assertIn("<code>schedule/demo/qa</code>", confirm)
        self.assertIn("Runs still open", confirm)
        self.assertIn('data-state="running"><svg', confirm)
        self.assertIn("team/demo/qa", confirm)
        self.assertEqual(settings_mod.load().repos, (entry,))  # nothing changed yet

        page = await asyncio.to_thread(self.follow, "remove-repo", entry=entry, repo="demo", confirmed="yes")
        self.assertEqual(settings_mod.load().repos, ())
        self.assertEqual((await ops.snapshot(self.client, settings_mod.load())).schedules, ())
        self.assertIn("<span>Removed</span>", page)
        self.assertIn("1 of its runs are still open: they finish", page)

        page = await asyncio.to_thread(self.follow, "add-repo", path=entry)
        self.assertIn("<span>Added</span>", page)
        self.assertIn("demo is one of the engine&#x27;s repos", page)
        self.assertIn("paused until each team has run once by hand", page)
        s = await self.scheduled()
        self.assertEqual((s.paused, s.note), (True, schedules.NEW_NOTE))
        self.assertEqual([(c.who, c.action) for c in ops.read_actions(self.settings) if c.action.endswith("-repo")],
                         [("desk", "remove-repo"), ("desk", "add-repo")])

    async def test_a_repo_name_any_config_allows_is_found_by_lookup(self):
        (self.repo / ".factory.toml").write_text((self.repo / ".factory.toml").read_text()
                                                 .replace('name = "demo"', 'name = "odd name+1 & co"'))
        for args in (("commit", "-qam", "rename"), ("push", "-q", "origin", "main")):
            git(*args, cwd=self.repo)
        state = self.state / "teams" / "odd name+1 & co" / "qa"
        state.mkdir(parents=True)
        (state / "notebook.md").write_text("an odd repo's notes\n")
        team_id = "team/odd name+1 & co/qa"
        notebook = await self.page("/notebook?" + urlencode({"team": team_id}))
        self.assertIn("an odd repo&#x27;s notes", notebook)
        team_page = await self.page(pages.team_url(team_id))
        self.assertIn("odd name+1 &amp; co · qa", team_page)


class DeskRestartTest(unittest.TestCase, Served):
    """The desk as its service runs it (`python -m engine.desk`, a process group of its own, as a launchd job is),
    killed and started again while the upgrade it started runs, as the upgrade itself does to it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        config, share = base / "config", base / "share"
        config.mkdir()
        (config / "engine.toml").write_text(f'repos = []\ntime_zone = "UTC"\nserver_port = 1\ndesk_port = {self.port}\n')
        self.env = dict(os.environ, FACTORY_ENGINE_CONFIG_DIR=str(config), FACTORY_ENGINE_HOME=str(share),
                        FACTORY_ENGINE_CONFIG=str(config / "engine.toml"), FACTORY_ENGINE_STATE=str(base / "state"))
        for name, value in (("DESK_TOKEN", config / "desk.token"), ("SOURCE", share / "source")):
            patch = mock.patch.object(install, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        # The source checkout: its `factory engine upgrade` outlasts the desk's restart, then ends well.
        checkout = base / "factory"
        factory = checkout / "home" / "bin" / "factory"
        factory.parent.mkdir(parents=True)
        factory.write_text('#!/bin/sh\necho "upgrade started"\nsleep 3\necho "still here after the restart"\n')
        factory.chmod(0o755)
        git("init", "-q", "-b", "main", str(checkout), cwd=base)
        for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
            git("config", key, value, cwd=checkout)
        git("add", ".", cwd=checkout)
        git("commit", "-qm", "init", cwd=checkout)
        (share / "builds" / "b1").mkdir(parents=True)
        (share / "builds" / "b1" / "COMMIT").write_text("0ld\n")
        (share / "current").write_text("b1")
        (share / "source").write_text(f"{checkout}\n")
        self.cookie = ""

    def start_desk(self) -> subprocess.Popen[bytes]:
        desk = subprocess.Popen([sys.executable, "-m", "engine.desk"], env=self.env, start_new_session=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                cwd=Path(__file__).resolve().parents[1])
        self.addCleanup(lambda: (desk.poll() is None and os.killpg(desk.pid, signal.SIGKILL), desk.wait()))
        for _ in range(200):
            if desk_server.probe(self.port, timeout=0.5) == "up":
                return desk
            time.sleep(0.05)
        self.fail("the desk never answered")

    def test_the_upgrade_the_desk_started_outlives_its_restart_and_its_log_shows_after(self):
        self.assertIsNone(install.read_desk_token())
        first = self.start_desk()
        self.assertIsNotNone(install.read_desk_token())  # made at the desk's startup
        self.sign_in()
        status, headers, body = self.post("deploy", confirmed="yes")
        self.assertEqual(status, 303, body)
        location = headers["location"]
        self.assertIn("upgrade started", self.deploy_text(location, "upgrade started"))

        os.killpg(first.pid, signal.SIGTERM)  # what launchd does to the desk's job when the upgrade restarts it
        first.wait(10)
        self.assertNotEqual(desk_server.probe(self.port), "up")
        self.start_desk()
        page = self.deploy_text(location, "<span>Deployed</span>")  # the same cookie: the token did not change
        self.assertIn("still here after the restart", page)
        self.assertIn(f"{ops.EXIT_MARK}0", page)

    def deploy_text(self, location: str, until: str) -> str:
        for _ in range(150):
            status, _, body = self.get(location)
            if status == 200 and until in body:
                return body
            time.sleep(0.1)
        self.fail(f"the deploy page never said {until}")


if __name__ == "__main__":
    unittest.main()
