"""The desk's pages as HTML strings, from what the server read (docs/designs/desk.md, "What it shows and does").

Pure: no I/O, so a test renders any state without a server. Every state is a word and a shape (`status.mark`), never
colour alone, and every string that isn't the desk's own (agent-written summaries and notes, PR titles, repo
config, file contents) goes through `e()`. No inline style or script: the desk serves CSP `default-src 'self'`, so
the look is one stylesheet, `/desk.css` (STYLE, built on status.CSS). A page shows its action forms only when given
the form token (`form`); the notice after a change is built from what the engine says now, never from the form.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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
TEXT_CHARS = 512 * 1024  # a deploy's log shows its last this many characters
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
    # build → when its worker last polled; None when that read failed (pollers_error says why)
    pollers: dict[str, datetime] | None = field(default_factory=dict)
    pollers_error: str = ""
    team_runs: dict[str, tuple[ops.RunView, ...]] = field(default_factory=dict)  # team ID → its latest finished runs
    team_runs_errors: dict[str, str] = field(default_factory=dict)  # team ID → why its runs could not be read


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
    if live.pollers is None:
        return "degraded", "Engine degraded", f"whether a worker is polling could not be read ({live.pollers_error})"
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


# ---- forms --------------------------------------------------------------------------------------------------------
# A change is a form POSTed to /act/<action> with the form token, then redirected (post-redirect-get). Spending money,
# deploying and removing a repo first open a confirm page (a GET of /confirm, server-rendered: no script needed),
# whose form posts with confirmed=yes.

ACTIONS = ("spend-on", "spend-off", "pause", "resume", "run-now", "add-repo", "remove-repo", "deploy")
CONFIRM = ("spend-on", "run-now", "remove-repo", "deploy")


def hidden(fields: dict[str, str]) -> str:
    return "".join(f'<input type="hidden" name="{e(k)}" value="{e(v)}">' for k, v in fields.items())


def post_form(action: str, fields: dict[str, str], label: str, form: str, inputs: str = "") -> str:
    """A button that makes the change (`inputs`: the form's own fields, HTML)."""
    return (f'<form method="post" action="/act/{e(action)}" class="act">{hidden({**fields, "form": form})}{inputs}'
            f'<button type="submit">{e(label)}</button></form>')


def confirm_url(action: str, fields: dict[str, str]) -> str:
    return "/confirm?" + urlencode({"action": action, **fields})


def confirm_button(action: str, fields: dict[str, str], label: str) -> str:
    """A button that opens the action's confirm page (nothing changes yet)."""
    return (f'<form method="get" action="/confirm" class="act">{hidden({"action": action, **fields})}'
            f'<button type="submit">{e(label)}</button></form>')


def actions(*forms: str) -> str:
    return f'<div class="actions">{"".join(forms)}</div>'


def notice(key: str, word: str, text: str) -> str:
    """What an action changed, read back from the engine after the redirect: a word and a shape, then the words."""
    return f'<p class="notice" role="status">{mark(key, word)} {e(text)}</p>'


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

def spend_card(settings: EngineSettings, form: str = "") -> str:
    if settings.unattended:
        state = mark("active", "On")
        say = "Teams may start agent sessions on every project."
        button = post_form("spend-off", {}, "Turn off", form)
    else:
        state = mark("paused", "Off")
        say = "No team starts a new agent session, on any project."
        button = confirm_button("spend-on", {}, "Turn on…")
    return (f'<section class="card"><h2>Unattended agent runs {state}</h2><p>{say}</p>'
            '<p class="note">The spend switch. Turning it off stops runs that have not started their session yet; a '
            'session already running finishes. (Or <code>factory engine spend on|off</code>.)</p>'
            f'{actions(button) if form else ""}</section>')


def needs_you(snap: ops.EngineSnapshot, form: str = "") -> str:
    items = []
    for repo in snap.repos:
        for team in repo.teams:
            need = team_state(team)
            if need is None:
                continue
            title, detail = need
            note = team.scheduled.note if team.scheduled else ""
            buttons = actions(confirm_button("run-now", {"team": team.ref.workflow_id}, "Run once now…"),
                              post_form("resume", {"team": team.ref.workflow_id}, "Resume its schedule", form)) if form else ""
            items.append(f'<li>{mark("paused")} <strong>{link(team_url(team.ref.workflow_id), f"{repo.name} · {team.ref.team}")}'
                         f'</strong> {e(title)}<div class="note">{e(detail)}</div>'
                         f'<div class="note">Its note: {e(note or "none")}</div>{buttons}</li>')
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


def project_card(repo: ops.RepoView, gh: RepoGitHub | None, pins: tuple[str, ...] | None, full: bool,
                 form: str = "") -> str:
    rows = [("Path", f"<code>{e(repo.entry)}</code>"), ("Factory pin", pins_html(pins))]
    if gh is not None:
        rows += [("Main branch CI", ci_mark(gh.ci)), ("Open factory PRs", prs_html(gh.prs))]
    rows.append(("Teams", team_counts(repo)))
    more = "" if full else f'<p>{link("/projects#" + anchor(repo.name), "Open its teams")}</p>'
    if full and form:
        more = actions(confirm_button("remove-repo", {"entry": repo.entry}, "Remove this repo…"))
    return f'<section class="card" id="{e(anchor(repo.name))}"><h2>{e(repo.name)}</h2>{facts(rows)}{more}</section>'


def overview(settings: EngineSettings, live: Live, github: dict[str, RepoGitHub],
             pins: dict[str, tuple[str, ...] | None], form: str = "", notice: str = "") -> str:
    body = notice + spend_card(settings, form)
    snap = live.snap
    if snap is None:
        return layout("Overview", "/", settings, live, body + unreachable(live))
    body += needs_you(snap, form)
    cards = "".join(project_card(r, github.get(r.root), pins.get(r.root), full=False) for r in snap.repos)
    body += f'<section><h2>Projects</h2><div class="grid">{cards or "<p class=note>No repos yet.</p>"}</div></section>'
    runs = (*snap.open_runs, *snap.recent_runs)
    body += (f'<section><h2>Recent runs</h2>{run_list(runs, settings, "No team has run yet.")}'
             f'<p>{link("/runs", "All recent runs")}</p></section>')
    return layout("Overview", "/", settings, live, body)


def unread(reason: str) -> str:
    """A read that failed: unknown, with why."""
    return f'{mark("unknown")} <span class="note">could not be read: {e(reason)}</span>'


def team_row(team: ops.TeamView, live: Live, settings: EngineSettings) -> str:
    runs = live.team_runs.get(team.ref.workflow_id, ())
    running = next((r for r in (live.snap.open_runs if live.snap else ()) if r.workflow_id == team.ref.workflow_id), None)
    last = running or (runs[0] if runs else None)
    error = live.team_runs_errors.get(team.ref.workflow_id)
    if last:
        last_html = f"{run_mark(last)} {when(last.started if last is running else last.closed, settings)}"
    else:
        last_html = unread(error) if error else "never"
    nxt = team.scheduled.next_run if team.scheduled and not team.scheduled.paused else None
    return (f'<tr><td data-label="Team"><strong>{link(team_url(team.ref.workflow_id), team.ref.team)}</strong>'
            f'<div class="note">{e(team.prompt)}</div></td>'
            f'<td data-label="Schedule">{e(cron_words(team.schedule))}</td>'
            f'<td data-label="Next run">{when(nxt, settings) if nxt else ("— (paused)" if team.scheduled else "—")}</td>'
            f'<td data-label="Last run">{last_html}</td>'
            f'<td data-label="Failures in a row">{team.failures}</td>'
            f'<td data-label="State">{schedule_mark(team)}</td></tr>')


def add_repo_form(form: str) -> str:
    field = ('<label class="field">Path of its checkout on this machine'
             '<input type="text" name="path" required placeholder="~/Dev/project" autocomplete="off"></label>')
    return (f'<section class="card"><h2>Add a repo</h2><p class="note">Its default branch must have a .factory.toml. '
            'Its teams\' schedules start paused until each has run once by hand.</p>'
            f'{post_form("add-repo", {}, "Add the repo", form, field)}</section>')


def projects(settings: EngineSettings, live: Live, github: dict[str, RepoGitHub],
             pins: dict[str, tuple[str, ...] | None], form: str = "", notice: str = "") -> str:
    snap = live.snap
    adding = add_repo_form(form) if form else ""
    if snap is None:
        rows = "".join(f"<li><code>{e(r)}</code></li>" for r in settings.repos)
        body = (notice + unreachable(live) + f'<section><h2>Repos in engine.toml</h2><ul class="plain">{rows}</ul>'
                f'</section>')
        return layout("Projects", "/projects", settings, live, body)
    body = notice + (f'<p class="note">Schedules are in the engine\'s time zone, {e(settings.time_zone)}; each repo '
                     'sets them in its own .factory.toml.</p>')
    for repo in snap.repos:
        card = project_card(repo, github.get(repo.root), pins.get(repo.root), full=True, form=form)
        if repo.teams:
            head = "".join(f"<th>{h}</th>" for h in ("Team", "Schedule", "Next run", "Last run", "Failures in a row",
                                                     "State"))
            rows = "".join(team_row(t, live, settings) for t in repo.teams)
            card = card.removesuffix("</section>") + (f'<div class="scroll"><table class="teams"><thead><tr>{head}'
                                                      f'</tr></thead><tbody>{rows}</tbody></table></div></section>')
        body += card
    if not snap.repos:
        body += '<p class="note">No repos yet: add one below, or <code>factory engine repo add PATH</code>.</p>'
    return layout("Projects", "/projects", settings, live, body + adding)


def team_actions(team: ops.TeamView, form: str) -> str:
    """Pause or resume its schedule (with an optional note) and run it now; a team on events only can only run."""
    ref, s = team.ref, team.scheduled
    out = ""
    if s is not None:
        verb = "resume" if s.paused else "pause"
        note = ('<label class="field">Note (optional)<input type="text" name="note" maxlength="300" '
                f'placeholder="{"resumed" if s.paused else "paused"} by hand" autocomplete="off"></label>')
        out += post_form(verb, {"team": ref.workflow_id}, "Resume its schedule" if s.paused else "Pause its schedule",
                         form, note)
    out += confirm_button("run-now", {"team": ref.workflow_id}, "Run now…")
    return (f'<section class="card"><h2>Actions</h2>{actions(out)}<p class="note">Run now wakes the team past its '
            "schedule's pause; it starts an agent session.</p></section>")


def team_page(settings: EngineSettings, live: Live, team: ops.TeamView | None, team_id: str, form: str = "",
              notice: str = "") -> str:
    if live.snap is None:
        return layout("Team", "", settings, live, unreachable(live))
    if team is None:
        return layout("Team", "", settings, live, f'<p>No team {e(team_id)} in the engine\'s repos.</p>')
    ref, s = team.ref, team.scheduled
    running = [r for r in live.snap.open_runs if r.workflow_id == ref.workflow_id]
    runs = live.team_runs.get(ref.workflow_id, ())
    error = live.team_runs_errors.get(ref.workflow_id)
    cron = cron_words(team.schedule)
    sched = e(cron) + (f' <span class="note">({e(settings.time_zone)}; cron <code>{e(team.schedule)}</code>)</span>'
                       if team.schedule not in ("", "off") else "")
    rows = [
        ("What it does", f"<code>{e(team.prompt)}</code>" if team.prompt else "—"),
        ("Schedule", sched),
        ("Also wakes on", e(", ".join(REASON_WORDS.get(w, w) for w in team.wake_on)) or "nothing else"),
        ("Budget", f"{team.budget_minutes} min of agent time per run"),
        ("Next run", when(s.next_run, settings) if s and not s.paused else ("— (paused)" if s else "—")),
        ("Last run", (f"{run_mark(runs[0])} {when(runs[0].closed, settings)}" if runs
                      else unread(error) if error else "never")),
        ("Failures in a row", f"{team.failures} <span class=\"note\">(the engine pauses it at {PAUSE_AFTER_FAILURES})</span>"),
        ("State", schedule_mark(team)),
        ("Links", " ".join([link("/notebook?" + urlencode({"team": ref.workflow_id}), "Notebook"),
                            *([link(ui_url(settings, "schedules", ref.schedule_id), "Schedule in Temporal",
                                    external=True)] if s else [])])),
    ]
    body = notice + f'<section class="card">{facts(rows)}</section>' + (team_actions(team, form) if form else "")
    if running:
        body += f'<section><h2>Running now</h2>{run_list(running, settings, "", team_link=False)}</section>'
    recent = f"<p>{unread(error)}</p>" if error else run_list(runs, settings, "This team has not run yet.",
                                                                 team_link=False)
    body += f'<section><h2>Recent runs</h2>{recent}</section>'
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
        if live.pollers is None:
            worker = f"worker: {unread(live.pollers_error)}"
        else:
            polled = live.pollers.get(b.build_id)
            worker = e(f"worker polling (last {ago(time.time() - polled.timestamp())})" if polled else "no worker polling")
        items.append(f"<li><code>{e(b.build_id)}</code> {state}<div class=\"note\">from commit "
                     f"<code>{e(short(b.commit))}</code> · {worker}</div></li>")
    return f'<ul class="plain">{"".join(items)}</ul>'


DEPLOY_WORDS = {"running": ("running", "Deploying"), "done": ("done", "Deployed"), "failed": ("failed", "Failed"),
                "unknown": ("unknown", "Ended without an exit status")}


def deploy_mark(run: ops.DeployRun) -> str:
    key, word = DEPLOY_WORDS[run.state]
    return mark(key, f"{word} (exit {run.exit_code})" if run.state == "failed" else word)


def deploy_url(deploy_id: str) -> str:
    return "/deploy?" + urlencode({"id": deploy_id})


def source_section(plan: ops.DeployPlan | None, deploys: list[ops.DeployRun], form: str) -> str:
    if plan is None:
        return ('<p class="note">No deploy has recorded its source checkout yet (the next '
                '<code>factory engine upgrade</code> does).</p>')
    rows = [
        ("Running", f"build <code>{e(plan.build or 'none')}</code> from commit <code>{e(short(plan.commit))}</code>"),
        ("Source checkout", f"<code>{e(plan.source)}</code> at <code>{e(short(plan.head))}</code> "
                            f"(branch {e(plan.branch or 'detached')})"),
    ]
    out = facts(rows)
    if plan.busy:
        out += f'<p>{mark("running", "Busy")} {e(plan.busy)} is running.</p>'
    elif plan.head is not None and plan.head == plan.commit:
        out += '<p class="note">The running build is the checkout\'s HEAD: nothing to deploy.</p>'
    elif plan.refusal:
        out += f'<p>{mark("unknown", "Cannot deploy")} {e(plan.refusal)}</p>'
    elif form:
        out += ('<p class="note">The checkout differs from the running build. Deploy runs '
                '<code>factory engine upgrade</code> in it, exactly as the CLI would; the desk never pulls or switches '
                'branches.</p>' + actions(confirm_button("deploy", {}, "Deploy the checkout's HEAD…")))
    if deploys:
        items = "".join(f'<li>{deploy_mark(d)} {link(deploy_url(d.deploy_id), d.deploy_id)}</li>' for d in deploys)
        out += f'<h3>Deploys from the desk</h3><ul class="plain">{items}</ul>'
    return out


def engine_page(settings: EngineSettings, live: Live, plan: ops.DeployPlan | None, deploys: list[ops.DeployRun],
                form: str = "") -> str:
    reachable = (mark("running", "Reachable") if live.reachable
                 else mark("stopped", "Not answering") + f' <span class="note">{e(live.error)}</span>')
    temporal = facts([
        ("Server", f"{reachable} <code>{e(settings.server_host)}:{settings.server_port}</code>"),
        ("Namespace", f"<code>{e(NAMESPACE)}</code>"),
        ("Dashboard", link(ui_url(settings), f"localhost:{settings.ui_port}", external=True)),
    ])
    src = source_section(plan, deploys, form)
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


def error_page(settings: EngineSettings, what: str, reason: str, back: str = "/") -> str:
    """An action that did not happen, and why (never a traceback)."""
    body = (f'<p class="alert">{mark("failed", "Not done")} {e(reason)}</p>'
            f'<p>{link(back, "Back")}</p>')
    return layout(what, "", settings, None, body)


# ---- what an action changed (read back after the redirect, never echoed from the form) ---------------------------

def spend_notice(settings: EngineSettings) -> str:
    if settings.unattended:
        return notice("active", "On", "Unattended agent runs are on: teams may start agent sessions.")
    return notice("paused", "Off", "Unattended agent runs are off: no team starts a new agent session.")


def team_notice(done: str, team: ops.TeamView, live: Live, settings: EngineSettings) -> str:
    name = f"{team.ref.repo_name} · {team.ref.team}"
    if done in ("pause", "resume"):
        s = team.scheduled
        if s is None:
            return notice("unknown", "Unknown", f"{name}: its schedule could not be read back.")
        if s.paused:
            return notice("paused", "Paused", f"{name}: its schedule is paused (note: {s.note or 'none'}).")
        return notice("active", "Active", f"{name}: its schedule is active (note: {s.note or 'none'}).")
    snap = live.snap
    running = next((r for r in snap.open_runs if r.workflow_id == team.ref.workflow_id), None) if snap else None
    spend = "" if settings.unattended else " The spend switch is off: the run stops before it starts a session."
    if running is not None:
        return notice("running", "Running", f"{name} is running now.{spend}")
    runs = live.team_runs.get(team.ref.workflow_id, ())
    if runs:
        key, word = runs[0].state
        return notice(key, word, f"{name}: its latest run has ended.{spend}")
    return notice("unknown", "Not seen yet", f"{name} was woken; reload to see its run.{spend}")


def repo_notice(done: str, args: dict[str, str], settings: EngineSettings, live: Live) -> str:
    snap = live.snap
    if done == "add-repo":
        repo = next((r for r in snap.repos if r.name == args.get("repo")), None) if snap else None
        if repo is None:
            return notice("unknown", "Unknown", "The repo could not be read back from the engine.")
        teams = len(repo.teams)
        return notice("done", "Added", f"{repo.name} is one of the engine's repos, with {teams} "
                                       f"team{'' if teams == 1 else 's'}; new schedules are paused until each team "
                                       "has run once by hand.")
    entry = args.get("entry", "")
    if any(install.same_repo(entry, r) for r in settings.repos):
        return notice("unknown", "Still there", f"{entry} is still one of the engine's repos.")
    name = args.get("repo", "")
    left = [r for r in snap.open_runs if r.repo_name == name] if snap else []
    runs = f" {len(left)} of its runs are still open: they finish, nothing new starts." if left else ""
    return notice("done", "Removed", f"{entry} is no longer one of the engine's repos; its schedules are deleted.{runs}")


# ---- confirm pages -----------------------------------------------------------------------------------------------

def confirm_page(settings: EngineSettings, title: str, body: str, action: str, fields: dict[str, str], label: str,
                 form: str, back: str) -> str:
    go = post_form(action, {**fields, "confirmed": "yes"}, label, form)
    return layout(title, "", settings, None, f'<section class="card confirm">{body}'
                                             f'{actions(go, link(back, "Cancel"))}</section>')


def confirm_spend_on(settings: EngineSettings, form: str) -> str:
    body = ("<p>Teams may then start agent sessions on every project, on their schedules and on events: this spends "
            "money.</p>")
    return confirm_page(settings, "Turn unattended agent runs on?", body, "spend-on", {}, "Turn on", form, "/")


def confirm_run_now(settings: EngineSettings, team: ops.TeamView, form: str) -> str:
    ref = team.ref
    body = (f"<p>Run <strong>{e(ref.repo_name)} · {e(ref.team)}</strong> now, past its schedule's pause. It starts "
            f"an agent session of up to {team.budget_minutes} min: this spends money.</p>")
    if not settings.unattended:
        body += (f'<p class="alert">{mark("paused", "Spend switch off")} The run would stop at once, before it starts '
                 "a session. Turn the spend switch on first to have it work.</p>")
    return confirm_page(settings, f"Run {ref.repo_name} · {ref.team} now?", body, "run-now",
                        {"team": ref.workflow_id}, "Run now", form, team_url(ref.workflow_id))


def confirm_remove_repo(settings: EngineSettings, live: Live, entry: str, form: str) -> str:
    snap = live.snap
    if snap is None:
        body = (f'<p class="alert">{mark("unknown")} The engine\'s state could not be read ({e(live.error)}), so '
                "the schedules this would delete and the runs still open can't be listed. Try again when it "
                "answers.</p>")
        return layout(f"Remove {entry}?", "", settings, live, body + f'<p>{link("/projects", "Back")}</p>')
    repo = next((r for r in snap.repos if r.entry == entry), None)
    name = repo.name if repo else Path(entry).name
    gone = [s for s in snap.schedules if s.repo_name == name]
    open_ = [r for r in snap.open_runs if r.repo_name == name]
    scheds = "".join(f"<li><code>{e(s.schedule_id)}</code></li>" for s in gone) or "<li>none</li>"
    runs = "".join(f"<li>{run_mark(r)} {e(r.workflow_id)}</li>" for r in open_) or "<li>none</li>"
    body = (f"<p>Remove <code>{e(entry)}</code> from the engine's repos. Its checkout and its history stay as they "
            f'are.</p><h3>Schedules that will be deleted</h3><ul class="plain">{scheds}</ul>'
            f'<h3>Runs still open</h3><p class="note">They finish; nothing new starts.</p><ul class="plain">{runs}</ul>')
    return confirm_page(settings, f"Remove {name}?", body, "remove-repo", {"entry": entry, "repo": name},
                        "Remove the repo", form, "/projects")


def confirm_deploy(settings: EngineSettings, plan: ops.DeployPlan, form: str) -> str:
    body = (f"<p>Deploy <code>{e(plan.source)}</code> at <code>{e(short(plan.head))}</code> "
            f"(branch {e(plan.branch or 'detached')}) over build <code>{e(plan.build or 'none')}</code> from "
            f"<code>{e(short(plan.commit))}</code>.</p><p>This runs <code>factory engine upgrade</code> in that "
            "checkout: a new build and its worker, new runs go to it, drained builds are retired. The desk restarts "
            "on the new build, so a page may not load for a few seconds. Open runs finish on the build they "
            "started on.</p>")
    return confirm_page(settings, "Deploy the source checkout?", body, "deploy", {}, "Deploy", form, "/engine")


def deploy_page(settings: EngineSettings, run: ops.DeployRun) -> str:
    meta = run.meta
    rows = [("State", deploy_mark(run)),
            ("Source", f"<code>{e(meta.get('source', '?'))}</code> at <code>{e(short(meta.get('head')))}</code> "
                       f"(branch {e(meta.get('branch') or 'detached')})"),
            ("Over", f"build <code>{e(meta.get('build') or 'none')}</code> from commit "
                     f"<code>{e(short(meta.get('commit')))}</code>")]
    note = ""
    if run.state == "running":
        note = ('<p class="note">Reload to follow it. The desk restarts on the new build during the deploy, so a '
                "reload may fail for a few seconds.</p>")
    elif run.state == "unknown":
        note = '<p class="note">The upgrade is gone without its exit status: it was stopped. Its log is below.</p>'
    text = run.log[-TEXT_CHARS:]
    cut = f'<p class="note">The last {TEXT_CHARS // 1024} K characters.</p>' if len(run.log) > TEXT_CHARS else ""
    body = (f'<section class="card">{facts(rows)}{note}</section>{cut}<pre class="text">{e(text)}</pre>'
            f'<p>{link("/engine", "Back to the engine")}</p>')
    return layout(f"Deploy {run.deploy_id}", "", settings, None, body)


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
h3{font-size:14px;margin:14px 0 4px}
.notice{border:1px solid var(--border);border-left:4px solid var(--ink);border-radius:12px;padding:10px 14px;
background:var(--card)}
.actions{display:flex;flex-wrap:wrap;align-items:flex-end;gap:10px 14px;margin-top:12px}
form.act{display:flex;flex-wrap:wrap;align-items:flex-end;gap:8px;margin:0}
.field{display:grid;gap:4px;font-size:13px;color:var(--muted)}
input[type=text]{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--border);border-radius:8px;
min-height:44px;padding:0 10px;min-width:min(320px,100%)}
button{font:inherit;font-weight:600;color:var(--bg);background:var(--fg);border:0;border-radius:8px;min-height:44px;
padding:0 16px;cursor:pointer}
.actions a{display:inline-flex;align-items:center;min-height:44px}
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
