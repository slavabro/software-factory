"""The desk's pages as HTML strings, from what the server read (docs/designs/desk.md, "What it shows and does").

Pure: no I/O, so a test renders any state without a server. Every state is a word and a shape (`status.mark`), never
colour alone, and every string that isn't the desk's own (agent-written summaries and notes, PR titles, repo
config, file contents) goes through `e()`. No inline style or script: the desk serves CSP `default-src 'self'`, so
the look is one stylesheet, `/desk.css` (STYLE, built on status.CSS).
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from engine import ops, schedules
from engine.activities.teams import PAUSE_AFTER_FAILURES
from engine.desk.repos import CI, PR, Read, RepoGitHub
from engine.factory import install, status
from engine.settings import NAMESPACE, EngineSettings

RECENT = 10  # finished runs on the overview (each costs a result read)
RECENT_RUNS = 20  # on the runs page
TEAM_RUNS = 10  # on a team's page
SUMMARY_CHARS = 400  # in a list; a team's own page shows the whole summary
NAV = (("/", "Overview"), ("/projects", "Projects"), ("/runs", "Runs"), ("/engine", "Engine"))
REASON_WORDS = {
    "schedule": "its schedule", "manual": "hand", "ci_red": "red CI", "deploy_failed": "a failed deploy",
    "incident": "an incident", "merged": "a merge", "decision": "a decision",
}
DAYS = ("Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays")


@dataclass(frozen=True)
class Live:
    """What the engine's Temporal said for one page, or why it could not be read."""

    snap: ops.EngineSnapshot | None
    error: str = ""  # why there is no snapshot
    reachable: bool = True  # False: the server did not answer at all
    pollers: dict[str, datetime] = field(default_factory=dict)  # build → when its worker last polled
    team_runs: dict[str, tuple[ops.RunView, ...]] = field(default_factory=dict)  # team ID → its latest finished runs


def e(value: object) -> str:
    """Escape for HTML text and quoted attributes."""
    return html.escape(str(value), quote=True)


def mark(key: str, word: str | None = None) -> str:
    """A state as icon and word (status.mark, the one owner of states' shapes)."""
    return str(status.mark(key, word))


# ---- small pieces -------------------------------------------------------------------------------------------------

def zone(settings: EngineSettings) -> ZoneInfo:
    try:
        return ZoneInfo(settings.time_zone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def when(dt: datetime | None, settings: EngineSettings) -> str:
    """A moment in the engine's time zone, e.g. `Mon 16 Nov 06:00`; `—` for none."""
    if dt is None:
        return "—"
    local = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(zone(settings))
    return f'<time datetime="{e(local.isoformat())}">{e(local.strftime("%a %d %b %H:%M"))}</time>'


def ago(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    return f"{hours} h ago" if hours < 48 else f"{hours // 24} days ago"


def cron_words(cron: str) -> str:
    """A team's cron in words where it is a plain daily or weekly time (`Mondays 06:00`), else the cron itself."""
    if cron in ("", "off"):
        return "no schedule: events and runs by hand only"
    parts = cron.split()
    if len(parts) != 5 or not (parts[0].isdigit() and parts[1].isdigit()) or parts[2:4] != ["*", "*"]:
        return cron
    at, dow = f"{int(parts[1]):02d}:{int(parts[0]):02d}", parts[4]
    if dow == "*":
        return f"Daily {at}"
    if dow == "1-5":
        return f"Weekdays {at}"
    if re.fullmatch(r"[0-7](,[0-7])*", dow):
        return f"{', '.join(DAYS[int(d) % 7] for d in dow.split(','))} {at}"
    return cron


def ui_url(settings: EngineSettings, *path: str) -> str:
    """A page of Temporal's own UI for the engine's namespace (each path part encoded: IDs hold slashes)."""
    base = f"http://localhost:{settings.ui_port}/namespaces/{NAMESPACE}"
    return "/".join([base, *(quote(p, safe="") for p in path)])


def history_url(settings: EngineSettings, run: ops.RunView) -> str:
    return ui_url(settings, "workflows", run.workflow_id, run.run_id, "history")


def team_url(workflow_id: str) -> str:
    return "/team?" + urlencode({"id": workflow_id})


def link(href: str, text: str, external: bool = False) -> str:
    return f'<a href="{e(href)}">{e(text)}{" ↗" if external else ""}</a>'


def short(commit: str | None) -> str:
    return commit[:12] if commit else "unknown"


def facts(rows: Iterable[tuple[str, str]]) -> str:
    """A definition list; the values are HTML already escaped by the caller."""
    return '<dl class="facts">' + "".join(f"<dt>{e(k)}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>"


def reasons(run: ops.RunView) -> str:
    if run.reasons:
        return "woken by " + ", ".join(e(REASON_WORDS.get(r, r)) for r in run.reasons)
    return "running" if run.status == "running" else "who woke it is not recorded"


# ---- engine health ------------------------------------------------------------------------------------------------

def health(live: Live) -> tuple[str, str, str]:
    """(status key, word, why) for the whole engine: running, degraded or stopped, with the reason in words."""
    snap = live.snap
    if not live.reachable:
        return "stopped", "Engine stopped", f"its Temporal server is not answering ({live.error})"
    if snap is None:
        return "degraded", "Engine degraded", f"the engine's state could not be read ({live.error})"
    build = snap.current_build
    if build is None:
        return "degraded", "Engine degraded", "no build is deployed on this machine (factory engine install)"
    view = next((b for b in snap.builds if b.build_id == build), None)
    if view is None or view.server is None or not view.server.current:
        return "degraded", "Engine degraded", f"build {build} is not the server's current build"
    if build not in live.pollers:
        return "degraded", "Engine degraded", f"no worker of build {build} is polling (factory engine start)"
    return "running", "Engine running", ""


def run_mark(run: ops.RunView) -> str:
    key, word = run.state
    return mark(key, word)


def ci_mark(read: Read[CI]) -> str:
    value = read.value
    words = {"passing": ("done", "Passing"), "failing": ("failed", "Failing"), "running": ("running", "Running"),
             "none": ("unknown", "No runs")}
    if read.error:
        last = ""
        if value is not None:
            last = f" — last: {words[value.state][1].lower()}, {ago(read.age())}"
        return f'{mark("unknown")} <span class="note">{e(read.error)}{e(last)}</span>'
    if value is None:
        return mark("unknown")
    key, word = words[value.state]
    out = mark(key, word)
    if value.failing:
        out += f' <span class="note">{e(", ".join(value.failing))}</span>'
    if value.url:
        out += " " + link(value.url, "run", external=True)
    return out


def prs_html(read: Read[tuple[PR, ...]]) -> str:
    value = read.value
    if read.error:
        last = f" — last: {len(value)} open, {ago(read.age())}" if value is not None else ""
        return f'{mark("unknown")} <span class="note">{e(read.error)}{e(last)}</span>'
    if value is None:
        return mark("unknown")
    if not value:
        return "none open"
    items = "".join(f"<li>{link(p.url, f'#{p.number}', external=True)} {e(p.title)}"
                    f'{" (draft)" if p.draft else ""} <span class="note">{e(p.branch)}</span></li>' for p in value)
    return f'{len(value)} open<ul class="plain">{items}</ul>'


def pins_html(pins: tuple[str, ...] | None) -> str:
    if pins is None:
        return mark("unknown") + ' <span class="note">its default branch could not be read</span>'
    if not pins:
        return "no pin"
    out = ", ".join(f"<code>{e(p[:12])}</code>" for p in pins)
    return out + (' <span class="note">(its pins differ)</span>' if len(pins) > 1 else "")


def schedule_mark(team: ops.TeamView) -> str:
    s = team.scheduled
    if s is None:
        return mark("unknown", "No schedule") if team.schedule not in ("", "off") else "events only"
    if not s.paused:
        return mark("active")
    return f'{mark("paused")} <span class="note">{e(s.note or "no note")}</span>'


def team_state(team: ops.TeamView) -> tuple[str, str] | None:
    """Why the team needs the principal: (title, detail), or None."""
    s = team.scheduled
    if s is None or not s.paused:
        return None
    if s.note == schedules.NEW_NOTE:
        return ("is paused until its first run by hand",
                "A new team's schedule stays off until it has run once by hand. Run it once now, look at the run, "
                "then resume its schedule.")
    if team.failures >= PAUSE_AFTER_FAILURES:
        return (f"was paused after {team.failures} failed runs in a row",
                "The engine paused its schedule. Look at the last runs, fix the cause, then resume it.")
    return None


# ---- page frame ---------------------------------------------------------------------------------------------------

def layout(title: str, here: str, settings: EngineSettings, live: Live | None, body: str) -> str:
    current = ' aria-current="page"'
    nav = "".join(f'<a href="{href}"{current if href == here else ""}>{e(name)}</a>' for href, name in NAV)
    top = ""
    if live is not None:
        key, word, why = health(live)
        build = live.snap.current_build if live.snap else install.current_build()
        top = (f'<div class="health">{mark(key, word)}'
               f'<span>build {e(build or "none")}</span>{link(ui_url(settings), "Temporal UI", external=True)}</div>')
        if why:
            body = f'<p class="alert">{mark(key, word)} {e(why)}</p>' + body
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{e(title)} · Factory Desk</title><link rel="stylesheet" href="/desk.css"></head><body>'
        f'<header class="top"><div class="wrap"><a class="brand" href="/">Factory Desk</a><nav>{nav}</nav>{top}'
        f'</div></header><main class="wrap"><h1>{e(title)}</h1>{body}</main></body></html>'
    )


def unreachable(live: Live) -> str:
    """What a page shows instead of teams and runs when there is no snapshot (the header says why)."""
    if not live.reachable:
        return ('<p class="card">Teams and runs show once the server answers again; '
                '<code>factory engine start</code> starts it.</p>')
    return '<p class="card">Teams and runs show once the engine\'s state can be read again.</p>'


def run_item(run: ops.RunView, settings: EngineSettings, team_link: bool = True) -> str:
    moment = run.started if run.status == "running" else run.closed
    name = f"{run.repo_name} · {run.team}"
    title = link(team_url(run.workflow_id), name) if team_link else e(name)
    text = run.summary or run.failure or ""
    if team_link and len(text) > SUMMARY_CHARS:
        text = text[:SUMMARY_CHARS] + "…"
    log = "/log?" + urlencode({"team": run.workflow_id, "run": run.run_id})
    links = [link(history_url(settings, run), "History", external=True), link(log, "Log"),
             link("/notebook?" + urlencode({"team": run.workflow_id}), "Notebook")]
    return (f'<li><div class="row">{run_mark(run)} <strong>{title}</strong> <span class="note">{when(moment, settings)}'
            f' · {reasons(run)}</span></div>' + (f'<div class="summary">{e(text)}</div>' if text else "")
            + f'<div class="links">{" ".join(links)}</div></li>')


def run_list(runs: Iterable[ops.RunView], settings: EngineSettings, empty: str, team_link: bool = True) -> str:
    items = "".join(run_item(r, settings, team_link) for r in runs)
    return f'<ul class="runs">{items}</ul>' if items else f'<p class="note">{e(empty)}</p>'


# ---- pages --------------------------------------------------------------------------------------------------------

def spend_card(settings: EngineSettings) -> str:
    if settings.unattended:
        state = mark("active", "On")
        say = "Teams may start agent sessions on every project."
    else:
        state = mark("paused", "Off")
        say = "No team starts a new agent session, on any project."
    return (f'<section class="card"><h2>Unattended agent runs {state}</h2><p>{say}</p>'
            '<p class="note">The spend switch. Turning it off stops runs that have not started their session yet; a '
            'session already running finishes. Change it with <code>factory engine spend on|off</code>.</p></section>')


def needs_you(snap: ops.EngineSnapshot) -> str:
    items = []
    for repo in snap.repos:
        for team in repo.teams:
            need = team_state(team)
            if need is None:
                continue
            title, detail = need
            note = team.scheduled.note if team.scheduled else ""
            items.append(f'<li>{mark("paused")} <strong>{link(team_url(team.ref.workflow_id), f"{repo.name} · {team.ref.team}")}'
                         f'</strong> {e(title)}<div class="note">{e(detail)}</div>'
                         f'<div class="note">Its note: {e(note or "none")}</div></li>')
    body = f'<ul class="plain needs">{"".join(items)}</ul>' if items else '<p class="note">Nothing needs you right now.</p>'
    return f'<section><h2>Needs you</h2>{body}</section>'


def team_counts(repo: ops.RepoView) -> str:
    if repo.problem:
        return f'{mark("unknown")} <span class="note">{e(repo.problem)}</span>'
    if not repo.teams:
        return "none"
    paused = sum(1 for t in repo.teams if t.scheduled is not None and t.scheduled.paused)
    active = sum(1 for t in repo.teams if t.scheduled is not None and not t.scheduled.paused)
    other = len(repo.teams) - paused - active
    parts = [f"{active} active"] if active else []
    parts += [f"{paused} paused"] if paused else []
    parts += [f"{other} on events only"] if other else []
    return e(", ".join(parts))


def anchor(name: str) -> str:
    return "repo-" + re.sub(r"[^A-Za-z0-9_-]", "-", name)


def project_card(repo: ops.RepoView, gh: RepoGitHub | None, pins: tuple[str, ...] | None, full: bool) -> str:
    rows = [("Path", f"<code>{e(repo.entry)}</code>"), ("Factory pin", pins_html(pins))]
    if gh is not None:
        rows += [("Main branch CI", ci_mark(gh.ci)), ("Open factory PRs", prs_html(gh.prs))]
    rows.append(("Teams", team_counts(repo)))
    more = "" if full else f'<p>{link("/projects#" + anchor(repo.name), "Open its teams")}</p>'
    return f'<section class="card" id="{e(anchor(repo.name))}"><h2>{e(repo.name)}</h2>{facts(rows)}{more}</section>'


def overview(settings: EngineSettings, live: Live, github: dict[str, RepoGitHub],
             pins: dict[str, tuple[str, ...] | None]) -> str:
    body = spend_card(settings)
    snap = live.snap
    if snap is None:
        return layout("Overview", "/", settings, live, body + unreachable(live))
    body += needs_you(snap)
    cards = "".join(project_card(r, github.get(r.root), pins.get(r.root), full=False) for r in snap.repos)
    body += f'<section><h2>Projects</h2><div class="grid">{cards or "<p class=note>No repos yet.</p>"}</div></section>'
    runs = (*snap.open_runs, *snap.recent_runs)
    body += (f'<section><h2>Recent runs</h2>{run_list(runs, settings, "No team has run yet.")}'
             f'<p>{link("/runs", "All recent runs")}</p></section>')
    return layout("Overview", "/", settings, live, body)


def team_row(team: ops.TeamView, live: Live, settings: EngineSettings) -> str:
    runs = live.team_runs.get(team.ref.workflow_id, ())
    running = next((r for r in (live.snap.open_runs if live.snap else ()) if r.workflow_id == team.ref.workflow_id), None)
    last = running or (runs[0] if runs else None)
    last_html = f"{run_mark(last)} {when(last.started if last is running else last.closed, settings)}" if last else "never"
    nxt = team.scheduled.next_run if team.scheduled and not team.scheduled.paused else None
    return (f'<tr><td data-label="Team"><strong>{link(team_url(team.ref.workflow_id), team.ref.team)}</strong>'
            f'<div class="note">{e(team.prompt)}</div></td>'
            f'<td data-label="Schedule">{e(cron_words(team.schedule))}</td>'
            f'<td data-label="Next run">{when(nxt, settings) if nxt else ("— (paused)" if team.scheduled else "—")}</td>'
            f'<td data-label="Last run">{last_html}</td>'
            f'<td data-label="Failures in a row">{team.failures}</td>'
            f'<td data-label="State">{schedule_mark(team)}</td></tr>')


def projects(settings: EngineSettings, live: Live, github: dict[str, RepoGitHub],
             pins: dict[str, tuple[str, ...] | None]) -> str:
    snap = live.snap
    if snap is None:
        rows = "".join(f"<li><code>{e(r)}</code></li>" for r in settings.repos)
        body = unreachable(live) + f'<section><h2>Repos in engine.toml</h2><ul class="plain">{rows}</ul></section>'
        return layout("Projects", "/projects", settings, live, body)
    body = (f'<p class="note">Schedules are in the engine\'s time zone, {e(settings.time_zone)}; each repo sets them '
            'in its own .factory.toml.</p>')
    for repo in snap.repos:
        card = project_card(repo, github.get(repo.root), pins.get(repo.root), full=True)
        if repo.teams:
            head = "".join(f"<th>{h}</th>" for h in ("Team", "Schedule", "Next run", "Last run", "Failures in a row",
                                                     "State"))
            rows = "".join(team_row(t, live, settings) for t in repo.teams)
            card = card.removesuffix("</section>") + (f'<div class="scroll"><table class="teams"><thead><tr>{head}'
                                                      f'</tr></thead><tbody>{rows}</tbody></table></div></section>')
        body += card
    if not snap.repos:
        body += '<p class="note">No repos yet: <code>factory engine repo add PATH</code>.</p>'
    return layout("Projects", "/projects", settings, live, body)


def team_page(settings: EngineSettings, live: Live, team: ops.TeamView | None, team_id: str) -> str:
    if live.snap is None:
        return layout("Team", "", settings, live, unreachable(live))
    if team is None:
        return layout("Team", "", settings, live, f'<p>No team {e(team_id)} in the engine\'s repos.</p>')
    ref, s = team.ref, team.scheduled
    running = [r for r in live.snap.open_runs if r.workflow_id == ref.workflow_id]
    runs = live.team_runs.get(ref.workflow_id, ())
    cron = cron_words(team.schedule)
    sched = e(cron) + (f' <span class="note">({e(settings.time_zone)}; cron <code>{e(team.schedule)}</code>)</span>'
                       if team.schedule not in ("", "off") else "")
    rows = [
        ("What it does", f"<code>{e(team.prompt)}</code>" if team.prompt else "—"),
        ("Schedule", sched),
        ("Also wakes on", e(", ".join(REASON_WORDS.get(w, w) for w in team.wake_on)) or "nothing else"),
        ("Budget", f"{team.budget_minutes} min of agent time per run"),
        ("Next run", when(s.next_run, settings) if s and not s.paused else ("— (paused)" if s else "—")),
        ("Last run", (f"{run_mark(runs[0])} {when(runs[0].closed, settings)}" if runs else "never")),
        ("Failures in a row", f"{team.failures} <span class=\"note\">(the engine pauses it at {PAUSE_AFTER_FAILURES})</span>"),
        ("State", schedule_mark(team)),
        ("Links", " ".join([link("/notebook?" + urlencode({"team": ref.workflow_id}), "Notebook"),
                            *([link(ui_url(settings, "schedules", ref.schedule_id), "Schedule in Temporal",
                                    external=True)] if s else [])])),
    ]
    body = f'<section class="card">{facts(rows)}</section>'
    if running:
        body += f'<section><h2>Running now</h2>{run_list(running, settings, "", team_link=False)}</section>'
    body += (f'<section><h2>Recent runs</h2>'
             f'{run_list(runs, settings, "This team has not run yet.", team_link=False)}</section>')
    return layout(f"{ref.repo_name} · {ref.team}", "", settings, live, body)


def runs_page(settings: EngineSettings, live: Live) -> str:
    snap = live.snap
    if snap is None:
        return layout("Runs", "/runs", settings, live, unreachable(live))
    body = (f'<section><h2>Running now</h2>{run_list(snap.open_runs, settings, "No team is running.")}</section>'
            f'<section><h2>Finished</h2><p class="note">The latest {RECENT_RUNS}, newest first; older runs are in '
            f'{link(ui_url(settings, "workflows"), "Temporal UI", external=True)}.</p>'
            f'{run_list(snap.recent_runs, settings, "No team has run yet.")}</section>')
    return layout("Runs", "/runs", settings, live, body)


def build_rows(live: Live) -> str:
    snap = live.snap
    if snap is None or not snap.builds:
        return '<p class="note">No build is installed.</p>'
    items = []
    for b in snap.builds:
        server = b.server
        if b.current:
            state = mark("current") + ' <span class="note">new runs go here</span>'
        elif server is None:
            state = mark("unknown") + ' <span class="note">the server could not say</span>'
        elif server.drainage == "drained" or (server.never_current and not server.current):
            state = mark("drained") + ' <span class="note">nothing open on it; retired at the next upgrade</span>'
        else:
            state = mark("draining") + ' <span class="note">its open runs finish on it, then it is retired</span>'
        polled = live.pollers.get(b.build_id)
        worker = f"worker polling (last {ago(time.time() - polled.timestamp())})" if polled else "no worker polling"
        items.append(f"<li><code>{e(b.build_id)}</code> {state}<div class=\"note\">from commit "
                     f"<code>{e(short(b.commit))}</code> · {e(worker)}</div></li>")
    return f'<ul class="plain">{"".join(items)}</ul>'


def engine_page(settings: EngineSettings, live: Live, source: tuple[str, str | None, str | None] | None,
                deploying: str | None) -> str:
    reachable = (mark("running", "Reachable") if live.reachable
                 else mark("stopped", "Not answering") + f' <span class="note">{e(live.error)}</span>')
    temporal = facts([
        ("Server", f"{reachable} <code>{e(settings.server_host)}:{settings.server_port}</code>"),
        ("Namespace", f"<code>{e(NAMESPACE)}</code>"),
        ("Dashboard", link(ui_url(settings), f"localhost:{settings.ui_port}", external=True)),
    ])
    snap = live.snap
    current = snap.current_build if snap else install.current_build()
    running_commit = next((b.commit for b in snap.builds if b.build_id == current), None) if snap else None
    if source is None:
        src = '<p class="note">No deploy has recorded its source checkout yet (the next factory engine upgrade does).</p>'
    else:
        path, head, branch = source
        same = head is not None and running_commit is not None and head == running_commit
        verdict = ("the checkout is at the running build's commit" if same else
                   "the checkout differs from the running build: <code>factory engine upgrade</code> deploys it")
        src = facts([
            ("Running", f"build <code>{e(current or 'none')}</code> from commit <code>{e(short(running_commit))}</code>"),
            ("Source checkout", f"<code>{e(path)}</code> at <code>{e(short(head))}</code> "
                                f"(branch {e(branch or 'detached')})"),
            ("", f'<span class="note">{verdict}</span>'),
        ])
    if deploying:
        src += f'<p>{mark("running", "Deploying")} {e(deploying)}</p>'
    config = facts([
        ("Time zone", e(settings.time_zone)),
        ("Spend switch", mark("active", "On") if settings.unattended else mark("paused", "Off")),
        ("Repos", "".join(f"<div><code>{e(r)}</code></div>" for r in settings.repos) or "none"),
        ("Desk", f"127.0.0.1:{settings.desk_port}"),
        ("Kept in", f"<code>{e(install.home_form(install.SETTINGS))}</code>"),
    ])
    body = (f'<section class="card"><h2>Temporal</h2>{temporal}</section>'
            f'<section class="card"><h2>Builds</h2>{build_rows(live)}</section>'
            f'<section class="card"><h2>Source</h2>{src}</section>'
            f'<section class="card"><h2>Settings</h2>{config}</section>')
    return layout("Engine", "/engine", settings, live, body)


def text_page(settings: EngineSettings, title: str, back: str, text: str, note: str = "") -> str:
    """A log or a notebook: the file's text, escaped, as it is."""
    body = (f'<p>{link(back, "Back to the team")}</p>' + (f'<p class="note">{e(note)}</p>' if note else "")
            + f'<pre class="text">{e(text)}</pre>')
    return layout(title, "", settings, None, body)


def message_page(settings: EngineSettings, title: str, text: str) -> str:
    return layout(title, "", settings, None, f"<p>{e(text)}</p>")


STYLE: str = status.CSS + """
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 var(--sans)}
a{color:var(--ink)}
code{font:13px ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}
.wrap{max-width:1100px;margin:0 auto;padding:12px 16px}
header.top{border-bottom:1px solid var(--border);background:var(--card)}
header.top .wrap{display:flex;flex-wrap:wrap;align-items:center;gap:8px 20px}
.brand{font-family:var(--serif);font-size:20px;font-weight:600;color:var(--fg);text-decoration:none}
nav{display:flex;flex-wrap:wrap;gap:4px}
nav a{display:inline-flex;align-items:center;min-height:44px;padding:0 12px;border-radius:8px;color:var(--fg);
text-decoration:none}
nav a[aria-current=page]{background:var(--fg);color:var(--bg);font-weight:600}
.health{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px;margin-left:auto;font-size:14px;color:var(--muted)}
h1{font-family:var(--serif);font-size:26px;font-weight:600;margin:20px 0 8px}
h2{font-size:17px;margin:0 0 8px;display:flex;flex-wrap:wrap;align-items:center;gap:8px}
section{margin:22px 0}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.alert{border:1px solid var(--border);border-radius:12px;padding:10px 14px;background:var(--card)}
.note{color:var(--muted);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.grid .card{margin:0}
dl.facts{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:0}
dl.facts dt{color:var(--muted);font-size:13px;padding-top:2px}
dl.facts dd{margin:0;min-width:0;overflow-wrap:anywhere}
ul.plain{list-style:none;padding:0;margin:4px 0}
ul.plain li{padding:4px 0}
ul.needs li{padding:10px 0;border-bottom:1px solid var(--border)}
ul.runs{list-style:none;padding:0;margin:0}
ul.runs li{display:grid;gap:3px;padding:10px 0;border-bottom:1px solid var(--border)}
.row{display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px}
.links{display:flex;flex-wrap:wrap;gap:14px;font-size:13px}
.summary{white-space:pre-wrap;overflow-wrap:anywhere}
.scroll{overflow-x:auto;margin-top:12px}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;font-size:12px;font-weight:600;color:var(--muted);border-bottom:1px solid var(--border);padding:6px 8px}
td{border-bottom:1px solid var(--border);padding:8px;vertical-align:top}
pre.text{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;
background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px}
@media (max-width:640px){
.health{margin-left:0}
dl.facts{grid-template-columns:1fr;gap:0}
dl.facts dd{margin-bottom:8px}
table.teams thead{display:none}
table.teams tr{display:block;padding:8px 0;border-bottom:1px solid var(--border)}
table.teams td{display:block;border:0;padding:2px 0}
table.teams td::before{content:attr(data-label) ": ";color:var(--muted);font-size:12px}
}
"""
