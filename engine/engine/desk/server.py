"""The Factory Desk's server (docs/designs/desk.md, "The desk itself" and "Safety").

A standard-library `ThreadingHTTPServer` on 127.0.0.1 that renders each page on the server from fresh reads. The
Temporal client lives on one asyncio loop in a background thread; request threads hand it coroutines with
`run_coroutine_threadsafe`. Only GET, and only these pages; the actions come with build order step 2.

Every request, reads included, passes three checks before anything is read:

- **Host** is `localhost:<port>` or `127.0.0.1:<port>` (defeats DNS rebinding).
- **Token**: the first visit carries `?token=` (the URL `factory engine status` prints); it is traded for an
  HttpOnly, SameSite=Strict cookie and a redirect to the URL without it. Every other request needs that cookie.
- Responses carry CSP `default-src 'self'` and no CORS headers; logs and notebooks are served by team and run ID,
  never by a path the client gives.
"""

from __future__ import annotations

import asyncio
import hmac
import re
import sys
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import timedelta
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TypeVar, cast
from urllib.parse import parse_qs, urlencode, urlsplit

from temporalio.client import Client

from factory_runtime.temporal import get_client
from engine import ops
from engine import settings as settings_mod
from engine.activities.repo import run_log, team_logs
from engine.activities.teams import team_dir
from engine.desk import pages, repos
from engine.factory import install
from engine.settings import EngineSettings

T = TypeVar("T")

CONNECT_TIMEOUT = 5.0  # seconds to reach the engine's Temporal server
READ_TIMEOUT = 60.0  # seconds a page's reads may take
TEXT_LIMIT = 512 * 1024  # a log or notebook shows its last this many bytes
TOKEN_MAX_AGE = 30 * 24 * 3600
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
TEAM_ID = re.compile(r"team/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+")
RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
REFUSED = ("This is the Factory Desk. Open it with the URL that `factory engine status` prints on its `desk:` line; "
           "the link signs this browser in.\n")


def url(settings: EngineSettings, token: str | None = None) -> str:
    """The desk's URL with its token: what `factory engine status` prints."""
    return f"http://localhost:{settings.desk_port}/?" + urlencode({"token": token or install.desk_token()})


@dataclass
class Response:
    status: int
    body: str
    content_type: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


def refused(status: int, text: str) -> Response:
    return Response(status, text, "text/plain; charset=utf-8")


def _short(error: BaseException) -> str:
    text = str(error).strip() or type(error).__name__
    return text.splitlines()[0][:300]


def valid_team_id(team_id: str) -> bool:
    return len(team_id) <= 300 and TEAM_ID.fullmatch(team_id) is not None and \
        not any(part in (".", "..") for part in team_id.split("/"))


class Desk:
    """The pages and their reads. `handle` is the whole request path: checks first, then the page."""

    def __init__(self, load_settings: Callable[[], EngineSettings] = settings_mod.load,
                 github: repos.GitHub | None = None) -> None:
        self.load_settings = load_settings
        self.github = github or repos.GitHub()
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="desk-temporal", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)

    # ---- reads on the Temporal loop -----------------------------------------------------------------------------

    def _on_loop(self, coro: Coroutine[Any, Any, T]) -> T:
        future: Future[T] = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(READ_TIMEOUT)
        except FutureTimeout:
            future.cancel()
            raise TimeoutError(f"the engine's reads took longer than {READ_TIMEOUT:.0f} s") from None

    async def _client(self, settings: EngineSettings) -> Client:
        client = await asyncio.wait_for(get_client(settings.temporal), CONNECT_TIMEOUT)
        await client.service_client.check_health(timeout=timedelta(seconds=CONNECT_TIMEOUT))
        return client

    async def _read(self, settings: EngineSettings, recent: int,
                    team_runs: Callable[[ops.EngineSnapshot], dict[str, int]]) -> pages.Live:
        try:
            client = await self._client(settings)
        except Exception as e:  # noqa: BLE001 — down, refusing, or not there yet: the page says so
            return pages.Live(None, _short(e), reachable=False)
        try:
            snap = await ops.snapshot(client, settings, recent)
            pollers = await ops.pollers(client)
            runs = {wid: tuple(await ops.recent_runs(client, n, wid)) for wid, n in team_runs(snap).items()}
        except Exception as e:  # noqa: BLE001 — one failed read must not fail the page
            return pages.Live(None, _short(e))
        return pages.Live(snap, pollers=pollers, team_runs=runs)

    def live(self, settings: EngineSettings, recent: int = 0,
             team_runs: Callable[[ops.EngineSnapshot], dict[str, int]] = lambda _: {}) -> pages.Live:
        try:
            return self._on_loop(self._read(settings, recent, team_runs))
        except TimeoutError as e:
            return pages.Live(None, _short(e))

    def repo_reads(self, live: pages.Live) -> tuple[dict[str, repos.RepoGitHub], dict[str, tuple[str, ...] | None]]:
        roots = [Path(r.root) for r in live.snap.repos if not r.problem] if live.snap else []
        return self.github.repos(roots), {str(r): repos.factory_pins(r) for r in roots}

    # ---- the request path ---------------------------------------------------------------------------------------

    def handle(self, method: str, target: str, host: str | None, cookie: str | None, port: int) -> Response:
        if host not in (f"localhost:{port}", f"127.0.0.1:{port}"):
            return refused(403, "Factory Desk: this host name is not served here; use the URL `factory engine "
                                "status` prints.\n")
        parts = urlsplit(target)
        query = parse_qs(parts.query, keep_blank_values=True)
        token = install.desk_token()
        name = f"factory_desk_{port}"
        if "token" in query:
            if not hmac.compare_digest(query["token"][-1].encode(), token.encode()):
                return refused(403, "Factory Desk: that link's token is not this desk's (desk.token has changed). "
                                    + REFUSED)
            rest = {k: v for k, v in query.items() if k != "token"}
            clean = parts.path + ("?" + urlencode(rest, doseq=True) if rest else "")
            return Response(303, "", headers={
                "Location": clean,
                "Set-Cookie": f"{name}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={TOKEN_MAX_AGE}",
            })
        if not self._signed_in(cookie, name, token):
            return refused(403, REFUSED)
        if method != "GET":
            return Response(405, "", "text/plain; charset=utf-8", {"Allow": "GET"})
        try:
            settings = self.load_settings()
        except Exception as e:  # noqa: BLE001 — a broken engine.toml: say so, don't crash
            return refused(500, f"Factory Desk: engine.toml could not be read: {_short(e)}\n")
        args = {k: v[-1] for k, v in query.items()}
        return self.page(parts.path, args, settings)

    @staticmethod
    def _signed_in(header: str | None, name: str, token: str) -> bool:
        if not header:
            return False
        try:
            jar = SimpleCookie(header)
        except CookieError:
            return False
        morsel = jar.get(name)
        return morsel is not None and hmac.compare_digest(morsel.value.encode(), token.encode())

    def page(self, path: str, args: dict[str, str], settings: EngineSettings) -> Response:
        if path == "/desk.css":
            return Response(200, pages.STYLE, "text/css; charset=utf-8")
        if path == "/":
            live = self.live(settings, pages.RECENT)
            return Response(200, pages.overview(settings, live, *self.repo_reads(live)))
        if path == "/projects":
            live = self.live(settings, team_runs=lambda snap: {t.ref.workflow_id: 1 for r in snap.repos for t in r.teams})
            return Response(200, pages.projects(settings, live, *self.repo_reads(live)))
        if path == "/runs":
            return Response(200, pages.runs_page(settings, self.live(settings, pages.RECENT_RUNS)))
        if path == "/engine":
            live = self.live(settings)
            return Response(200, pages.engine_page(settings, live, install.source_head(), install.deploy_running()))
        if path == "/team":
            team_id = args.get("id", "")
            team = ops.find_team(settings, team_id) if valid_team_id(team_id) else None
            live = self.live(settings, team_runs=lambda _: {team_id: pages.TEAM_RUNS} if team else {})
            if team is not None and live.snap is not None:  # the snapshot's view carries its schedule's state
                team = next((t for r in live.snap.repos for t in r.teams if t.ref.workflow_id == team_id), team)
            return Response(200 if team else 404, pages.team_page(settings, live, team, team_id))
        if path in ("/log", "/notebook"):
            return self.text(path, args, settings)
        return Response(404, pages.message_page(settings, "Not found", "The desk has no such page."))

    def text(self, path: str, args: dict[str, str], settings: EngineSettings) -> Response:
        """A run's log or a team's notebook, found from the team's ID (and the run's), never from a client path."""
        team_id, run_id = args.get("team", ""), args.get("run", "")
        team = ops.find_team(settings, team_id) if valid_team_id(team_id) else None
        if team is None or (path == "/log" and RUN_ID.fullmatch(run_id) is None):
            return Response(404, pages.message_page(settings, "Not found", "No such team or run."))
        back = pages.team_url(team_id)
        if path == "/log":
            file, home, title = run_log(settings, team.ref, run_id), team_logs(settings, team.ref), f"Log of run {run_id[:8]}"
            missing = "This run wrote no log: a run that stops before its session starts writes none."
        else:
            file, home, title = team_dir(settings, team.ref) / "notebook.md", team_dir(settings, team.ref), "Notebook"
            missing = "This team's notebook is empty: each finished run adds an entry."
        heading = f"{team.ref.repo_name} · {team.ref.team} · {title}"
        if file is None or not file.is_file() or not file.resolve().is_relative_to(home.resolve()):
            return Response(200, pages.text_page(settings, heading, back, "", missing))
        size = file.stat().st_size
        with open(file, "rb") as f:
            f.seek(max(0, size - TEXT_LIMIT))
            text = f.read().decode(errors="replace")
        note = f"The last {TEXT_LIMIT // 1024} KB of {size // 1024} KB." if size > TEXT_LIMIT else ""
        return Response(200, pages.text_page(settings, heading, back, text, note))


class Handler(BaseHTTPRequestHandler):
    desk: Desk
    server_version = "FactoryDesk"
    sys_version = ""

    def _serve(self) -> None:
        port = cast(ThreadingHTTPServer, self.server).server_port
        try:
            resp = self.desk.handle(self.command, self.path, self.headers.get("Host"), self.headers.get("Cookie"), port)
        except Exception as e:  # noqa: BLE001 — a bug in a page: a short error, never a dead server
            resp = refused(500, f"Factory Desk: this page failed: {_short(e)}\n")
        body = resp.body.encode()
        self.send_response(resp.status)
        for key, value in {**HEADERS, **resp.headers, "Content-Type": resp.content_type,
                           "Content-Length": str(len(body))}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """Only failures, and never the query (a first visit carries the token)."""
        if str(code).isdigit() and int(code) >= 500:
            sys.stderr.write(f"factory desk: {self.command} {urlsplit(self.path).path} {code}\n")


def make_server(desk: Desk, port: int) -> ThreadingHTTPServer:
    handler = type("DeskHandler", (Handler,), {"desk": desk})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server
