"""The Factory Desk's server (docs/designs/desk.md, "The desk itself" and "Safety").

A standard-library `ThreadingHTTPServer` on 127.0.0.1 that renders each page on the server from fresh reads. The
Temporal client lives on one asyncio loop in a background thread, and only Temporal calls run there; request threads
hand it coroutines with `run_coroutine_threadsafe`. Pages are GETs; a change is a form POSTed to `/act/<action>` and
made through `engine.ops` (as the CLI makes it, logged with who = desk), then redirected to a page that reads the
result back from the engine (post-redirect-get). Changes that spend money or deploy (and removing a repo) open a
server-rendered confirm page first (`/confirm`), so no script is needed.

Every request, reads included, passes these checks before anything is read:

- **Host** is `localhost:<port>` or `127.0.0.1:<port>` (defeats DNS rebinding).
- **Token**: the first visit carries `?token=` (the link `factory engine desk` prints); it is traded for an
  HttpOnly, SameSite=Strict cookie and a redirect to the URL without it. The cookie holds a value derived from the
  token, never the token: cookies are not scoped by port, so every other app on localhost receives it.
- **A POST** also needs an `Origin` equal to `http://<its Host>` (a missing one is refused: SameSite treats every
  localhost port as the same site) and the form token (`form_token`, derived from the token, compared in constant
  time).
- Responses carry CSP `default-src 'self'` and no CORS headers; logs and notebooks are found from team and run IDs
  looked up among the configured teams, never from a path the client gives.

Every response carries `X-Factory-Desk`, so `factory engine status` can tell the desk from another program on its
port (`probe`).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.client
import re
import sys
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal, TypeVar, cast
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
ACTION_TIMEOUT = 300.0  # seconds a change may take (adding a repo fetches it and every other repo)
BODY_LIMIT = 16 * 1024  # bytes a form may have
NOTE_CHARS = 300  # a pause or resume note
REQUEST_TIMEOUT = 30  # seconds a client may take to send its request
TEXT_LIMIT = 512 * 1024  # a log or notebook shows its last this many bytes
COOKIE_MAX_AGE = 30 * 24 * 3600
MARKER = "X-Factory-Desk"
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    MARKER: "1",
}
RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SECRET = re.compile(r"(token=)[^&\s'\"]*")
REFUSED = ("This is the Factory Desk. Open it with the link `factory engine desk` prints (`factory engine desk --open` "
           "opens it); the link signs this browser in.\n")


def base_url(port: int) -> str:
    return f"http://localhost:{port}/"


def signed_url(port: int, token: str) -> str:
    """The link that signs a browser in: what `factory engine desk` prints."""
    return base_url(port) + "?" + urlencode({"token": token})


def cookie_value(token: str) -> str:
    """What the cookie holds: derived from the token, so an app on another localhost port that receives the cookie
    still can't make the sign-in link."""
    return hmac.new(token.encode(), b"desk-cookie", hashlib.sha256).hexdigest()


def form_token(token: str) -> str:
    """What every form carries: derived from the token and the cookie's value, so only a page the desk served (to a
    browser holding the cookie) has it, and a new token voids old pages' forms."""
    return hmac.new(token.encode(), b"desk-form|" + cookie_value(token).encode(), hashlib.sha256).hexdigest()


def cookie_named(header: str | None, name: str) -> str | None:
    """The cookie `name` from a Cookie header, parsed by hand: other localhost apps' cookies (JSON values, spaces,
    stray quotes) must not hide the desk's, as they would from http.cookies."""
    for part in (header or "").split(";"):
        key, sep, value = part.partition("=")
        if sep and key.strip() == name:
            return value.strip()
    return None


DeskState = Literal["up", "not running", "port held by something else"]


def probe(port: int, timeout: float = 2.0) -> DeskState:
    """Whether the desk answers on `port`: an unauthenticated request it refuses, recognised by its header."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/", headers={"Host": f"localhost:{port}"})
        resp = conn.getresponse()
        resp.read()
        return "up" if resp.getheader(MARKER) == "1" else "port held by something else"
    except ConnectionRefusedError:
        return "not running"
    except (OSError, http.client.HTTPException):
        return "port held by something else"
    finally:
        conn.close()


@dataclass(frozen=True)
class Request:
    method: str
    target: str  # path and query, as sent
    host: str | None
    cookie: str | None
    port: int  # the port the desk listens on
    origin: str | None = None
    body: bytes = b""  # a POST's form


class EngineDown(Exception):
    """The engine's Temporal server can't be reached, so a change could not be made."""


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


def plausible_team_id(team_id: str) -> bool:
    """Worth looking up: the lookup among the configured teams (`ops.find_team`) is the real check."""
    return team_id.startswith("team/") and len(team_id) <= 500


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
        if not self._thread.is_alive():
            self.loop.close()

    # ---- reads on the Temporal loop -----------------------------------------------------------------------------

    def _on_loop(self, coro: Coroutine[Any, Any, T], timeout: float = READ_TIMEOUT, what: str = "reads") -> T:
        future: Future[T] = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except FutureTimeout:
            future.cancel()
            raise TimeoutError(f"the engine's {what} took longer than {timeout:.0f} s") from None

    def on_engine(self, settings: EngineSettings, op: Callable[[Client], Coroutine[Any, Any, T]]) -> T:
        """An action on the Temporal loop; EngineDown when the server can't be reached."""
        async def run() -> T:
            try:
                client = await self._client(settings)
            except Exception as e:  # noqa: BLE001 — down or refusing: the error page says so
                raise EngineDown(f"the engine's Temporal server is not answering ({_short(e)})") from None
            return await op(client)
        return self._on_loop(run(), ACTION_TIMEOUT, "change")

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
        except Exception as e:  # noqa: BLE001 — the page says why there are no teams or runs
            return pages.Live(None, _short(e))
        # The rest is secondary: each read that fails shows as unknown with its reason, the snapshot stays.
        try:
            pollers: dict[str, Any] | None = await ops.pollers(client)
            pollers_error = ""
        except Exception as e:  # noqa: BLE001
            pollers, pollers_error = None, _short(e)
        runs, errors = {}, {}
        for wid, n in team_runs(snap).items():
            try:
                runs[wid] = tuple(await ops.recent_runs(client, n, wid))
            except Exception as e:  # noqa: BLE001
                errors[wid] = _short(e)
        return pages.Live(snap, pollers=pollers, pollers_error=pollers_error, team_runs=runs, team_runs_errors=errors)

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

    def handle(self, req: Request) -> Response:
        method, target, host, cookie, port = req.method, req.target, req.host, req.cookie, req.port
        if host not in (f"localhost:{port}", f"127.0.0.1:{port}"):
            return refused(403, "Factory Desk: this host name is not served here; use the link `factory engine "
                                "desk` prints.\n")
        token = install.read_desk_token()  # only read: a request never makes the token
        if token is None:
            return refused(403, "Factory Desk: there is no desk token yet; `factory engine desk` makes one.\n")
        parts = urlsplit(target)
        query = parse_qs(parts.query, keep_blank_values=True)
        name = f"factory_desk_{port}"
        if "token" in query:
            if not hmac.compare_digest(query["token"][-1].encode(), token.encode()):
                return refused(403, "Factory Desk: that link's token is not this desk's (desk.token has changed). "
                                    + REFUSED)
            rest = {k: v for k, v in query.items() if k != "token"}
            clean = parts.path + ("?" + urlencode(rest, doseq=True) if rest else "")
            return Response(303, "", headers={
                "Location": clean,
                "Set-Cookie": f"{name}={cookie_value(token)}; HttpOnly; SameSite=Strict; Path=/; "
                              f"Max-Age={COOKIE_MAX_AGE}",
            })
        sent = cookie_named(cookie, name)
        if sent is None or not hmac.compare_digest(sent.encode(), cookie_value(token).encode()):
            return refused(403, REFUSED)
        if method not in ("GET", "POST"):
            return Response(405, "", "text/plain; charset=utf-8", {"Allow": "GET, POST"})
        form: dict[str, str] = {}
        if method == "POST":
            # SameSite treats every localhost port as one site, so a change must also show it comes from the desk's
            # own page: its exact origin (scheme, host and port) and the form token only the desk's pages carry.
            if req.origin is None or req.origin != f"http://{host}":
                return refused(403, f"Factory Desk: a change is taken only from the desk's own pages (Origin "
                                    f"http://{host}); this request's Origin is {req.origin or 'missing'}.\n")
            form = {k: v[-1] for k, v in parse_qs(req.body.decode("utf-8", "replace"), keep_blank_values=True).items()}
            if not hmac.compare_digest(form.get("form", "").encode(), form_token(token).encode()):
                return refused(403, "Factory Desk: this form's token is missing or out of date; reload the page and "
                                    "try again.\n")
        try:
            settings = self.load_settings()
        except Exception as e:  # noqa: BLE001 — a broken engine.toml: say so, don't crash
            return refused(500, f"Factory Desk: engine.toml could not be read: {_short(e)}\n")
        if method == "POST":
            return self.post(parts.path, form, settings)
        args = {k: v[-1] for k, v in query.items()}
        return self.page(parts.path, args, settings, form_token(token))

    def find_team(self, settings: EngineSettings, team_id: str) -> ops.TeamView | None:
        return ops.find_team(settings, team_id) if plausible_team_id(team_id) else None

    def page(self, path: str, args: dict[str, str], settings: EngineSettings, form: str) -> Response:
        done = args.get("done", "")
        if path == "/desk.css":
            return Response(200, pages.STYLE, "text/css; charset=utf-8")
        if path == "/":
            live = self.live(settings, pages.RECENT)
            notice = pages.spend_notice(settings) if done == "spend" else ""
            return Response(200, pages.overview(settings, live, *self.repo_reads(live), form, notice))
        if path == "/projects":
            live = self.live(settings, team_runs=lambda snap: {t.ref.workflow_id: 1 for r in snap.repos for t in r.teams})
            notice = pages.repo_notice(done, args, settings, live) if done in ("add-repo", "remove-repo") else ""
            return Response(200, pages.projects(settings, live, *self.repo_reads(live), form, notice))
        if path == "/runs":
            return Response(200, pages.runs_page(settings, self.live(settings, pages.RECENT_RUNS)))
        if path == "/engine":
            live = self.live(settings)
            return Response(200, pages.engine_page(settings, live, ops.deploy_plan(settings),
                                                   ops.recent_deploys(settings), form))
        if path == "/deploy":
            run = ops.read_deploy(settings, args.get("id", ""))
            if run is None:
                return Response(404, pages.message_page(settings, "Not found", "No such deploy."))
            return Response(200, pages.deploy_page(settings, run))
        if path == "/team":
            team_id = args.get("id", "")
            team = self.find_team(settings, team_id)
            live = self.live(settings, team_runs=lambda _: {team_id: pages.TEAM_RUNS} if team else {})
            if team is not None and live.snap is not None:  # the snapshot's view carries its schedule's state
                team = next((t for r in live.snap.repos for t in r.teams if t.ref.workflow_id == team_id), team)
            notice = ""
            if team is not None and live.snap is not None and done in ("pause", "resume", "run-now"):
                notice = pages.team_notice(done, team, live, settings)
            return Response(200 if team else 404, pages.team_page(settings, live, team, team_id, form, notice))
        if path == "/confirm":
            return self.confirm(args, settings, form)
        if path in ("/log", "/notebook"):
            return self.text(path, args, settings)
        return Response(404, pages.message_page(settings, "Not found", "The desk has no such page."))

    # ---- changes ------------------------------------------------------------------------------------------------

    def confirm(self, args: dict[str, str], settings: EngineSettings, form: str) -> Response:
        """The confirm page of a change that spends money, deploys or deletes schedules (nothing changes yet)."""
        action = args.get("action", "")
        if action == "spend-on":
            return Response(200, pages.confirm_spend_on(settings, form))
        if action == "run-now":
            team = self.find_team(settings, args.get("team", ""))
            if team is None:
                return Response(404, pages.message_page(settings, "Not found", "No such team in the engine's repos."))
            return Response(200, pages.confirm_run_now(settings, team, form))
        if action == "remove-repo":
            entry = args.get("entry", "")
            if entry not in settings.repos:
                return Response(404, pages.message_page(settings, "Not found", "That is not one of the engine's repos."))
            return Response(200, pages.confirm_remove_repo(settings, self.live(settings), entry, form))
        if action == "deploy":
            plan = ops.deploy_plan(settings)
            reason = ("no deploy has recorded the source checkout yet" if plan is None else plan.refusal)
            if plan is None or reason:
                return Response(409, pages.error_page(settings, "Deploy", reason, "/engine"))
            return Response(200, pages.confirm_deploy(settings, plan, form))
        return Response(404, pages.message_page(settings, "Not found", "The desk has no such change."))

    def post(self, path: str, form: dict[str, str], settings: EngineSettings) -> Response:
        """A change, then a redirect to the page that reads it back (post-redirect-get). A refusal or an engine that
        can't be reached is a page with the reason, never a traceback."""
        action = path.removeprefix("/act/") if path.startswith("/act/") else ""
        if action not in pages.ACTIONS:
            return Response(404, pages.message_page(settings, "Not found", "The desk has no such change."))
        fields = {k: v for k, v in form.items() if k not in ("form", "confirmed")}
        if action in pages.CONFIRM and form.get("confirmed") != "yes":
            return Response(303, "", headers={"Location": pages.confirm_url(action, fields)})
        try:
            location = self.act(action, fields, settings)
        except ops.OpError as e:
            return Response(409, pages.error_page(settings, "Not done", str(e)))
        except EngineDown as e:
            return Response(503, pages.error_page(settings, "Not done", str(e)))
        except TimeoutError as e:
            return Response(504, pages.error_page(settings, "Not done", f"{_short(e)}; it may still finish — reload "
                                                                        "the page to see"))
        except Exception as e:  # noqa: BLE001 — a bug or a broken file: the reason, never a traceback
            return Response(500, pages.error_page(settings, "Not done", _short(e)))
        return Response(303, "", headers={"Location": location})

    def act(self, action: str, fields: dict[str, str], settings: EngineSettings) -> str:
        """Make the change through engine.ops (the CLI's own operations, logged as the desk's); where to go next."""
        if action in ("spend-on", "spend-off"):
            ops.set_unattended(settings, action == "spend-on", who="desk")
            return "/?done=spend"
        if action in ("pause", "resume", "run-now"):
            team = self.find_team(settings, fields.get("team", ""))
            if team is None:
                raise ops.OpError("no such team in the engine's repos")
            ref, note = team.ref, fields.get("note", "").strip()[:NOTE_CHARS] or None
            if action == "pause":
                self.on_engine(settings, lambda c: ops.pause(c, settings, ref, note, who="desk"))
            elif action == "resume":
                self.on_engine(settings, lambda c: ops.resume(c, settings, ref, note, who="desk"))
            else:
                self.on_engine(settings, lambda c: ops.run_now(c, settings, ref, who="desk"))
            return pages.team_url(ref.workflow_id) + "&" + urlencode({"done": action})
        if action == "add-repo":
            path = fields.get("path", "").strip()
            if not path:
                raise ops.OpError("give the path of the repo's checkout")
            added = self.on_engine(settings, lambda c: ops.add_repo(c, settings, path, who="desk"))
            return "/projects?" + urlencode({"done": "add-repo", "repo": added.name}) + "#" + pages.anchor(added.name)
        if action == "remove-repo":
            entry = fields.get("entry", "")
            if entry not in settings.repos:
                raise ops.OpError(f"{entry} is not one of the engine's repos")
            removed = self.on_engine(settings, lambda c: ops.remove_repo(c, settings, entry, who="desk"))
            return "/projects?" + urlencode({"done": "remove-repo", "entry": removed.entry, "repo": removed.name})
        if action == "deploy":
            return pages.deploy_url(ops.start_deploy(settings, who="desk").deploy_id)
        raise ops.OpError(f"no such change: {action}")

    def text(self, path: str, args: dict[str, str], settings: EngineSettings) -> Response:
        """A run's log or a team's notebook, found from the team's ID (and the run's), never from a client path."""
        team_id, run_id = args.get("team", ""), args.get("run", "")
        team = self.find_team(settings, team_id)
        if team is None or (path == "/log" and RUN_ID.fullmatch(run_id) is None):
            return Response(404, pages.message_page(settings, "Not found", "No such team or run."))
        back = pages.team_url(team_id)
        if path == "/log":
            file, home, title = run_log(settings, team.ref, run_id), team_logs(settings, team.ref), f"Log of run {run_id[:8]}"
            within = settings.state / "logs"
            missing = "This run wrote no log: a run that stops before its session starts writes none."
        else:
            file, home, title = team_dir(settings, team.ref) / "notebook.md", team_dir(settings, team.ref), "Notebook"
            within = settings.state / "teams"
            missing = "This team's notebook is empty: each finished run adds an entry."
        heading = f"{team.ref.repo_name} · {team.ref.team} · {title}"
        # The team's own directory must be under the state's (a name from a repo's config could hold `..`), and the
        # file under the team's (a symlink must not lead out).
        inside = home.resolve().is_relative_to(within.resolve())
        if not inside or file is None or not file.is_file() or not file.resolve().is_relative_to(home.resolve()):
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
    timeout = REQUEST_TIMEOUT  # a client that never finishes its request doesn't hold a thread for ever

    def _serve(self) -> None:
        port = cast(ThreadingHTTPServer, self.server).server_port
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= BODY_LIMIT:
            resp = refused(413, "Factory Desk: that request's body is not a form the desk takes.\n")
            self.close_connection = True
        else:
            body = self.rfile.read(length) if length else b""
            req = Request(self.command, self.path, self.headers.get("Host"), self.headers.get("Cookie"), port,
                          self.headers.get("Origin"), body)
            try:
                resp = self.desk.handle(req)
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

    def log_message(self, format: str, *args: Any) -> None:
        """Errors (log_error) only; anything that could quote the request line has its token blanked."""
        sys.stderr.write("factory desk: " + SECRET.sub(r"\1…", format % args) + "\n")


def make_server(desk: Desk, port: int) -> ThreadingHTTPServer:
    handler = type("DeskHandler", (Handler,), {"desk": desk})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server
