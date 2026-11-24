"""The costs page: one self-contained HTML file (inline CSS and SVG, no script, nothing fetched).

Built and read like a product's own LLM-usage page: a framed strip of four figures with the provenance line under
it, the budgets as meters (a thin track, a flat fill, a tick at the warn and danger lines, the unclamped figure and
the state as a word and a shape), daily spend as bars from zero with the budget pace as a dashed rule, then the
department × project grid, what a landed task costs each department, ranked tables that fold the tail into "Other"
and state their finding in the title, the costliest sessions, the product's own runtime spend ("Run") and the
cache. Red-green safe: no state is shown by colour alone (CLAUDE.md rule 6). Only metrics and names reach the
page, never transcript text.
"""

from __future__ import annotations

import datetime as dt
import html
import math
from collections import Counter
from dataclasses import dataclass, field

from .cost import (
    APPROACHING, LANDED, NEAR, OVER, WITHIN, LandedRow, Meter, Period, Settings, Totals, agent_words, cost_per_landed_task,
    days_in_month, fmt_tokens, fmt_usd, group, local_midnight, month_start, total,
)
from .usage import UsageRecord

ROW_LIMIT = 6  # more rows fold into one "Other" row, so the breakdown tables stay short
GRID_PROJECTS = 5  # project columns in the department × project grid before "Other"
TOP_SESSIONS = 12


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def cost_dp(values) -> int:
    return 2 if max((abs(v) for v in values), default=0) >= 1 else 4


def fold(rows: list[tuple[str, Totals]], limit: int = ROW_LIMIT) -> list[dict]:
    """Largest `limit - 1` rows, then one "Other (n)" row; each with its share of the total."""
    all_cost = sum(t.cost for _, t in rows)
    out = [{"name": n, "t": t, "other": 0} for n, t in rows]
    if len(out) > limit:
        tail = out[limit - 1:]
        merged = Totals()
        for r in tail:
            merged.cost += r["t"].cost
            merged.calls += r["t"].calls
            merged.tokens += r["t"].tokens
        out = out[: limit - 1] + [{"name": "Other", "t": merged, "other": len(tail)}]
    for r in out:
        r["pct"] = r["t"].cost / all_cost * 100 if all_cost else 0
    return out


def share_title(rows: list[dict], fallback: str) -> str:
    named = [r for r in rows if not r["other"] and r["t"].cost > 0]
    if not named:
        return fallback
    if len([r for r in rows if r["t"].cost > 0]) == 1:
        return f"All spend came from {named[0]['name']}"
    return f"{named[0]['name']} drove {round(named[0]['pct'])}% of spend"


@dataclass
class Model:
    """Everything the page shows, computed once."""

    period: Period
    now: dt.datetime
    spend: Totals
    prior_cost: float | None
    prior_first: dt.datetime | None  # the earliest turn in the prior period: later than its start = partial data
    month_cost: float
    today_cost: float
    projected: float | None
    budget_pace: float | None  # USD/day an all-spend monthly budget allows
    daily: list[tuple[dt.date, float, int, int]]  # day, cost, turns, tokens
    meters: list[Meter]
    dept_names: dict[str, str]
    direct: str
    grid: dict  # {"projects": [...], "rows": [(dept, {project: cost}, total)], "totals": {project: cost}}
    landed: list[LandedRow] | None  # None: not asked
    landed_tasks: int  # distinct tasks that landed and had spend in the period
    breakdowns: list[tuple[str, str, list[dict]]]  # noun, fallback title, rows
    sessions: list[dict]
    runtime: dict | None  # the product's own spend: {"total", "departments", "projects", "configured"}
    cache_saved: float
    cache_premium: float
    unpriced: list[str]
    plan_monthly: float | None
    notes: list[str] = field(default_factory=list)

    def dept(self, dept_id: str) -> str:
        return self.dept_names.get(dept_id, dept_id)

    @classmethod
    def build(cls, rows: list[UsageRecord], runtime: list[UsageRecord], found: list[Meter], settings: Settings,
              p: Period, now: dt.datetime, status: dict[tuple[str, str], str] | None = None,
              landed: bool = True) -> "Model":
        cur = [r for r in rows if p.since <= r.ts < p.until]
        prior = [r for r in rows if p.prior_since <= r.ts < p.since]
        month = [r for r in rows if r.ts >= month_start(now)]
        today = [r for r in rows if r.ts >= local_midnight(now)]
        month_cost = sum(r.cost_usd for r in month)
        days = days_in_month(now)
        pace_budget = next((m for m in found if not m.scope and not m.runtime and m.unit == "usd"
                            and m.window == "this month"), None)
        daily: dict[dt.date, list] = {}
        day = p.since.astimezone(now.tzinfo).date()
        while day <= now.date():
            daily[day] = [0.0, 0, 0]
            day += dt.timedelta(days=1)
        for r in cur:
            d = daily.setdefault(r.ts.astimezone(now.tzinfo).date(), [0.0, 0, 0])
            d[0] += r.cost_usd
            d[1] += 1
            d[2] += r.tokens
        saved = premium = 0.0
        for r in cur:
            price = settings.prices(r.project).price(r.model)
            if price:
                saved += r.cache_read_tokens * (price.input - price.cache_read) / 1e6
                premium += ((r.cache_write_tokens - r.cache_write_1h) * (price.cache_write_5m - price.input)
                            + r.cache_write_1h * (price.cache_write_1h - price.input)) / 1e6
        org = settings.org
        names = {d.id: d.name or d.id for d in org.departments}
        # Department × project: departments in the org's order, projects by spend.
        by_project = [name for name, _ in group(cur, "project")]
        shown = by_project if len(by_project) <= GRID_PROJECTS + 1 else by_project[:GRID_PROJECTS]
        other = [x for x in by_project if x not in shown]
        cols = shown + (["Other"] if other else [])
        cells: dict[str, dict[str, float]] = {}
        for r in cur:
            col = r.project if r.project in shown else "Other"
            cells.setdefault(r.department, {}).setdefault(col, 0.0)
            cells[r.department][col] += r.cost_usd
        order = [d.id for d in org.departments] + sorted(set(cells) - {d.id for d in org.departments})
        grid_rows = [(dept, cells[dept], sum(cells[dept].values())) for dept in order if dept in cells]
        col_totals = {c: sum(row[1].get(c, 0.0) for row in grid_rows) for c in cols}
        sessions = []
        for sid, t in group(cur, "session")[:TOP_SESSIONS]:
            mine = [r for r in cur if r.session_id == sid]
            main = [r for r in mine if not r.is_subagent] or mine
            sub_cost = sum(r.cost_usd for r in mine if r.is_subagent)
            top = lambda f, pool=main: Counter(f(r) for r in pool).most_common(1)[0][0]  # noqa: E731
            sessions.append({
                "id": sid, "t": t, "project": top(lambda r: r.project, mine), "branch": top(lambda r: r.branch),
                "agent": top(lambda r: r.agent), "task": top(lambda r: r.task),
                "department": max({r.department for r in mine},
                                  key=lambda d: sum(r.cost_usd for r in mine if r.department == d)),
                "sub_pct": sub_cost / t.cost * 100 if t.cost else 0,
            })
        breakdowns = [
            ("Agent", "Spend by agent", fold(group(cur, lambda r: agent_words(r.agent)))),
            ("Process", "Spend by process", fold(group(cur, lambda r: r.process or "—"))),
            ("Task", "Spend by task", fold(group(cur, "task"))),
            ("Model", "Spend by model", fold(group(cur, "model"))),
        ]
        rt = [r for r in runtime if p.since <= r.ts < p.until]
        configured = sorted(pr for pr, c in settings.projects.items() if c.runtime_usage)
        runtime_view = None
        if configured or rt:
            runtime_view = {
                "total": total(rt), "configured": configured,
                "departments": fold(group(rt, lambda r: r.department or "—"), limit=8),
                "projects": fold(group(rt, "project"), limit=8),
            }
        return cls(
            period=p, now=now, spend=total(cur), prior_cost=sum(r.cost_usd for r in prior) if prior else None,
            prior_first=min((r.ts for r in prior), default=None), month_cost=month_cost,
            today_cost=sum(r.cost_usd for r in today),
            projected=month_cost / now.day * days if month else None,
            budget_pace=pace_budget.limit / days if pace_budget else None,
            daily=[(d, v[0], v[1], v[2]) for d, v in sorted(daily.items())], meters=found, dept_names=names,
            direct=org.direct, grid={"projects": cols, "rows": grid_rows, "totals": col_totals},
            landed=cost_per_landed_task(cur, status or {}) if landed else None,
            landed_tasks=len({(r.project, r.task) for r in cur
                              if (status or {}).get((r.project, r.task)) == LANDED}),
            breakdowns=breakdowns, sessions=sessions, runtime=runtime_view, cache_saved=saved, cache_premium=premium,
            unpriced=sorted({r.model for r in cur if not r.priced}), plan_monthly=settings.plan_monthly_usd or None,
            notes=list(settings.notes),
        )


# ── Marks ────────────────────────────────────────────────────────────────────────────────────────────────────────

_ICONS = {
    WITHIN: '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    APPROACHING: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/>'
                 '<path d="M12 9v4"/><path d="M12 17h.01"/>',
    OVER: '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>',
}
_ICONS[NEAR] = _ICONS[APPROACHING]
_TONE = {WITHIN: "neutral", APPROACHING: "attention", NEAR: "negative", OVER: "negative"}


def status_mark(state: str, word: str) -> str:
    return (f'<span class="mark {_TONE[state]}" data-state="{state}"><svg viewBox="0 0 24 24" aria-hidden="true">'
            f"{_ICONS[state]}</svg><span>{esc(word)}</span></span>")


def meter(m: Meter) -> str:
    value = max(0.0, min(m.pct, 100.0))
    fill = "danger" if value >= m.danger else "warn" if value >= m.warn else "ok"
    ticks = "".join(f'<span class="tick" style="left:{t:g}%"></span>' for t in (m.warn, m.danger) if 0 < t < 100)
    return (f'<div class="meter" role="progressbar" aria-label="{esc(m.label)}: {m.pct_text} of limit" '
            f'aria-valuenow="{round(value)}" aria-valuemin="0" aria-valuemax="100">'
            f'<div class="fill {fill}" style="width:{value:.1f}%"></div>{ticks}</div>')


def mini_bar(pct: float) -> str:
    return f'<span class="minibar" aria-hidden="true"><span style="width:{max(0.0, min(pct, 100.0)):.1f}%"></span></span>'


def sparkline(values: list[float]) -> str:
    if len(values) < 2:
        return ""
    w, h, top = 120, 28, max(values) or 1
    pts = " ".join(f"{i * w / (len(values) - 1):.1f},{h - v / top * (h - 2) - 1:.1f}" for i, v in enumerate(values))
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" role="img" aria-label="Daily spend over {len(values)} days">'
            f'<polyline points="{pts}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>')


def _nice(top: float) -> float:
    """The axis top: the next 1, 2, 2.5 or 5 × a power of ten at or above `top`."""
    if top <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(top))
    for step in (1, 2, 2.5, 5, 10):
        if top <= step * mag:
            return step * mag
    return 10 * mag


def daily_chart(m: Model) -> str:
    days = m.daily
    if not days or not any(c for _, c, _, _ in days):
        return '<div class="empty">No spend recorded in this period</div>'
    W, H, left, bottom, top_pad = 1000, 220, 64, 24, 10
    top = _nice(max(max(c for _, c, _, _ in days), m.budget_pace or 0))
    dp = cost_dp([top])
    plot_w, plot_h = W - left, H - bottom - top_pad
    slot = plot_w / len(days)
    bar = min(28.0, slot * 0.7)
    y = lambda v: top_pad + plot_h - v / top * plot_h  # noqa: E731
    parts = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Daily spend, USD">']
    for tick in (0, top / 2, top):
        parts.append(f'<line class="grid" x1="{left}" x2="{W}" y1="{y(tick):.1f}" y2="{y(tick):.1f}"/>'
                     f'<text class="axis" x="{left - 8}" y="{y(tick) + 4:.1f}" text-anchor="end">{fmt_usd(tick, dp)}</text>')
    every = max(1, round(len(days) / 8))
    for i, (day, c, turns, tokens) in enumerate(days):
        x = left + i * slot + (slot - bar) / 2
        label = day.strftime("%-d %b")
        parts.append(f'<rect class="bar" x="{x:.1f}" y="{y(c):.1f}" width="{bar:.1f}" height="{y(0) - y(c):.1f}">'
                     f"<title>{label}: {fmt_usd(c)} · {turns:,} turns · {fmt_tokens(tokens)} tokens</title></rect>")
        if i % every == 0:
            parts.append(f'<text class="axis" x="{x + bar / 2:.1f}" y="{H - 6}" text-anchor="middle">{label}</text>')
    if m.budget_pace:
        py = y(m.budget_pace)
        parts.append(f'<line class="pace" x1="{left}" x2="{W}" y1="{py:.1f}" y2="{py:.1f}"/>'
                     f'<text class="axis" x="{W - 4}" y="{py - 6:.1f}" text-anchor="end">budget pace '
                     f"{fmt_usd(m.budget_pace)}/day</text>")
    parts.append("</svg>")
    return "".join(parts)


def spend_trend_title(m: Model) -> str:
    if not any(c for _, c, _, _ in m.daily):
        return "Daily spend"
    tot = sum(c for _, c, _, _ in m.daily)
    peak = max(m.daily, key=lambda d: d[1])
    dp = cost_dp([c for _, c, _, _ in m.daily])
    return (f"{fmt_usd(tot, dp)} over {len(m.daily)} days, peaking at {fmt_usd(peak[1], dp)} on "
            f"{peak[0].strftime('%-d %b')}")


# ── Sections ─────────────────────────────────────────────────────────────────────────────────────────────────────


def stats(m: Model) -> str:
    t = m.spend
    vs = ""
    if m.prior_cost is not None:
        change = t.cost - m.prior_cost
        arrow = "▲" if change > 0 else "▼" if change < 0 else "="
        pct = f" ({change / m.prior_cost * 100:+.0f}%)" if m.prior_cost else ""
        partial = ""
        if m.prior_first and m.prior_first - m.period.prior_since > dt.timedelta(days=1):
            partial = f" (records start {m.prior_first.astimezone(m.now.tzinfo).strftime('%-d %b')})"
        vs = f"{arrow} {fmt_usd(abs(change))}{pct} vs {esc(m.period.prior)}{partial} · "
    pace = ""
    month_budget = next((x for x in m.meters if not x.scope and not x.runtime and x.unit == "usd"
                         and x.window == "this month"), None)
    if m.projected is not None and month_budget:
        pace = f" · {m.projected / month_budget.limit * 100:.0f}% of the {fmt_usd(month_budget.limit, 0)} budget"
    per_turn = t.cost / t.calls if t.calls else None
    hit = t.cache_hit
    spark = sparkline([c for _, c, _, _ in m.daily]) if len(m.daily) >= 2 else ""
    direct = sum(row[2] for row in m.grid["rows"] if row[0] == m.direct)
    direct_text = f" · {direct / t.cost * 100:.0f}% {esc(m.dept(m.direct))}" if t.cost else ""
    return f"""
<div class="strip">
  <div class="stat"><span class="label">Projected this month</span>
    <div class="headline">{fmt_usd(m.projected) if m.projected is not None else "—"}</div>
    <p class="caption">{fmt_usd(m.month_cost)} month to date{pace}</p></div>
  <div class="stat"><span class="label">Spend, {esc(m.period.last)}</span>
    <div class="row"><span class="value">{fmt_usd(t.cost)}</span>{spark}</div>
    <p class="caption">{vs}{t.calls:,} turns · {fmt_tokens(t.tokens)} tokens</p></div>
  <div class="stat"><span class="label">Daily rate</span>
    <div class="value">{fmt_usd(t.cost / m.period.days)}/day</div>
    <p class="caption">{fmt_usd(m.today_cost)} today so far · {f"budget pace {fmt_usd(m.budget_pace)}/day" if m.budget_pace else "no monthly budget set"}</p></div>
  <div class="stat"><span class="label">Cost per turn</span>
    <div class="value">{fmt_usd(per_turn, 4) if per_turn is not None else "—"}</div>
    <p class="caption">{len(t.sessions):,} sessions{direct_text}{f" · cache hit {hit * 100:.0f}%" if hit else ""}</p></div>
</div>"""


def budgets_section(m: Model) -> str:
    if not m.meters:
        body = ('<p class="muted">No budgets set. Add <code>[[budgets]]</code> to '
                '<code>~/.config/software-factory/factory.toml</code> or a repo\'s <code>.factory.toml</code>.</p>')
        ticks = "75% and 90% by default"
    else:
        rows = []
        # A glob budget ("every task") shows the values that need attention; the healthy rest fold into one line.
        shown = [x for x in m.meters if not x.each or x.state != WITHIN]
        folded = Counter((", ".join(x.scope), x.window) for x in m.meters if x.each and x.state == WITHIN)
        for x in shown:
            kind = ("hard limit: stops factory runs" if x.hard and not x.runtime and x.window != "latest run"
                    else "hard limit: stops the run" if x.hard and x.window == "latest run"
                    else "hard limit: the product enforces it" if x.hard else "alert only")
            rows.append(f"""
  <div class="budget">
    <div class="budget-head"><span class="budget-name">{esc(x.label)}</span>
      <span class="caption">{esc(x.window)} · {kind}</span></div>
    <div class="budget-row">{meter(x)}
      <span class="figure">{esc(x.figure(x.used))}<span class="muted"> of {esc(x.figure(x.limit))}</span></span>
      <span class="pct">{x.pct_text}</span></div>
    {status_mark(x.state, x.word)}
  </div>""")
        for (keys, window), n in sorted(folded.items()):
            rows.append(f'<p class="caption">{status_mark(WITHIN, "Within budget")} {n} more '
                        f'{esc(keys)} value{"s" if n != 1 else ""}, {esc(window)}</p>')
        body = "".join(rows)
        ticks = " · ".join(f"{w:g}% and {d:g}%" for w, d in sorted({(x.warn, x.danger) for x in m.meters}))
    return f"""
<section class="card">
  <h2>Spending budgets</h2>
  <p class="caption">Ticks mark {ticks} · the figure is not clamped, so over the limit reads as over · department
  budgets steer (alert only); project, factory-run and runtime budgets are hard caps unless set otherwise</p>
  {body}
</section>"""


def table(noun: str, rows: list[dict], dp: int, extra: str = "", calls: str = "Turns") -> str:
    max_cost = max((r["t"].cost for r in rows), default=0)
    body = []
    for r in rows:
        name = f'{esc(r["name"])} ({r["other"]})' if r["other"] else esc(r["name"])
        bar = r["t"].cost / max_cost * 100 if max_cost else 0
        body.append(f"""<tr><td class="{"muted" if r["other"] else ""} name">{name}</td>
<td class="num muted">{r["t"].calls:,}</td><td class="num muted">{fmt_tokens(r["t"].tokens)}</td>
<td class="num">{fmt_usd(r["t"].cost, dp)}</td>
<td class="share">{mini_bar(bar)}<span class="caption num">{r["pct"]:.0f}%</span></td></tr>""")
    foot = ""
    if len(rows) > 1:
        cost = sum(r["t"].cost for r in rows)
        n = sum(r["t"].calls for r in rows)
        tok = sum(r["t"].tokens for r in rows)
        foot = (f'<tfoot><tr><td>Total</td><td class="num">{n:,}</td><td class="num">{fmt_tokens(tok)}'
                f'</td><td class="num">{fmt_usd(cost, dp)}</td><td></td></tr></tfoot>')
    return f"""<div class="scroll"><table>
<thead><tr><th>{esc(noun)}</th><th class="num">{calls}</th><th class="num">Tokens</th><th class="num">Cost ($)</th>
<th class="share-h">Share</th></tr></thead><tbody>{"".join(body)}</tbody>{foot}</table></div>{extra}"""


def grid_section(m: Model) -> str:
    g = m.grid
    if not g["rows"]:
        return ""
    total_cost = sum(row[2] for row in g["rows"])
    dp = cost_dp([total_cost])
    top = max(g["rows"], key=lambda row: row[2])
    top_project = max(g["totals"].items(), key=lambda kv: kv[1]) if g["totals"] else ("—", 0)
    title = (f"{esc(m.dept(top[0]))} took {top[2] / total_cost * 100:.0f}% of spend; "
             f"{esc(top_project[0])} {top_project[1] / total_cost * 100:.0f}%") if total_cost else "Department × project"
    head = "".join(f'<th class="num">{esc(c)}</th>' for c in g["projects"])
    body = []
    for dept, cells, row_total in g["rows"]:
        tds = "".join(f'<td class="num{" muted" if not cells.get(c) else ""}">'
                      f'{fmt_usd(cells[c], dp) if cells.get(c) else "—"}</td>' for c in g["projects"])
        share = row_total / total_cost * 100 if total_cost else 0
        inferred = ' <span class="caption">inferred</span>' if dept == m.direct else ""
        body.append(f'<tr><td class="name">{esc(m.dept(dept))}{inferred}</td>{tds}'
                    f'<td class="num">{fmt_usd(row_total, dp)}</td>'
                    f'<td class="share">{mini_bar(share)}<span class="caption num">{share:.0f}%</span></td></tr>')
    foot = "".join(f'<td class="num">{fmt_usd(g["totals"][c], dp)}</td>' for c in g["projects"])
    return f"""
<section class="card">
  <h2>{title}</h2>
  <p class="caption">Departments are functions, projects are repos: each cell is one department's spend in one
  project · {esc(m.dept(m.direct))} is the principal's own sessions outside any pipeline skill (the only inferred row)</p>
  <div class="scroll"><table>
  <thead><tr><th>Department</th>{head}<th class="num">Total</th><th class="share-h">Share</th></tr></thead>
  <tbody>{"".join(body)}</tbody>
  <tfoot><tr><td>Total</td>{foot}<td class="num">{fmt_usd(total_cost, dp)}</td><td></td></tr></tfoot>
  </table></div>
</section>"""


def landed_section(m: Model) -> str:
    if m.landed is None or not m.landed:
        return ""
    rows = [r for r in m.landed if r.landed or r.open or r.unchecked]
    landed_all = sum(r.landed_cost for r in m.landed)
    dp = cost_dp([r.landed_cost for r in m.landed] + [r.open_cost for r in m.landed])
    any_unchecked = any(r.unchecked for r in m.landed)
    n = m.landed_tasks
    title = (f"{n} task{'s' if n != 1 else ''} landed, at {fmt_usd(landed_all / n)} each across every department"
             if n else "No task has landed in this period")
    body = []
    for r in rows:
        unchecked = (f'<td class="num">{r.unchecked}</td><td class="num muted">{fmt_usd(r.unchecked_cost, dp)}</td>'
                     if any_unchecked else "")
        body.append(f"""<tr><td class="name">{esc(m.dept(r.department))}</td>
<td class="num">{r.landed}</td><td class="num">{fmt_usd(r.landed_cost, dp)}</td>
<td class="num">{fmt_usd(r.per_landed) if r.per_landed is not None else "—"}</td>
<td class="num">{r.open}</td><td class="num muted">{fmt_usd(r.open_cost, dp)}</td>{unchecked}
<td class="num muted">{fmt_usd(r.no_task_cost, dp)}</td></tr>""")
    unchecked_head = '<th class="num">Not checked</th><th class="num">Their spend</th>' if any_unchecked else ""
    return f"""
<section class="card">
  <h2>{title}</h2>
  <p class="caption">A task has landed when a branch it ran on merged as a pull request (asked of GitHub with
  <code>gh</code>) · a task is counted in every department that spent on it · {fmt_usd(landed_all)} went to landed
  tasks{" · not checked: the repo has no main checkout here or gh could not answer" if any_unchecked else ""}</p>
  <div class="scroll"><table>
  <thead><tr><th>Department</th><th class="num">Landed tasks</th><th class="num">Their spend</th>
  <th class="num">Per landed task</th><th class="num">Not landed</th><th class="num">Their spend</th>{unchecked_head}
  <th class="num">No task</th></tr></thead>
  <tbody>{"".join(body)}</tbody></table></div>
</section>"""


def breakdowns(m: Model) -> str:
    cells = []
    for noun, fallback, rows in m.breakdowns:
        if not rows:
            cells.append(f'<section><h3 class="label">{fallback}</h3><p class="caption">No usage in this period</p></section>')
            continue
        dp = cost_dp([r["t"].cost for r in rows])
        extra = ""
        if noun == "Model" and m.unpriced:
            extra = (f'<p class="note">{status_mark(APPROACHING, "No price")} {esc(", ".join(m.unpriced))}: counted at '
                     "$0 — add them under <code>[cost.prices]</code></p>")
        cells.append(f'<section><h3 class="label">{esc(share_title(rows, fallback))}</h3>{table(noun, rows, dp, extra)}</section>')
    return f"""
<section class="card">
  <h2>Where the spend went</h2>
  <p class="caption">Four more cuts of the same total · each sorted by cost · share is of the total · a process is the
  skill, subagent type or factory capability a turn ran in (— = none)</p>
  <div class="grid2">{"".join(cells)}</div>
</section>"""


def sessions_section(m: Model) -> str:
    if not m.sessions:
        return ""
    dp = cost_dp([s["t"].cost for s in m.sessions])
    rows = []
    for s in m.sessions:
        t = s["t"]
        when = t.first.astimezone(m.now.tzinfo).strftime("%-d %b %H:%M") if t.first else "—"
        rows.append(f"""<tr><td class="mono">{esc(s["id"][:8])}</td><td>{esc(s["project"])}</td>
<td class="name">{esc(s["branch"] or "—")}</td><td>{esc(m.dept(s["department"]))}</td>
<td>{esc(agent_words(s["agent"]))}</td><td>{esc(s["task"])}</td>
<td class="num muted">{esc(when)}</td><td class="num muted">{t.calls:,}</td><td class="num muted">{fmt_tokens(t.tokens)}</td>
<td class="num muted">{s["sub_pct"]:.0f}%</td><td class="num">{fmt_usd(t.cost, dp)}</td></tr>""")
    share = sum(s["t"].cost for s in m.sessions) / m.spend.cost * 100 if m.spend.cost else 0
    return f"""
<section class="card">
  <h2>The {len(m.sessions)} costliest sessions took {share:.0f}% of spend</h2>
  <p class="caption">A session with its subagents · department: where most of its spend went · ids are Claude Code
  session ids (the transcript's file name)</p>
  <div class="scroll"><table>
  <thead><tr><th>Session</th><th>Project</th><th>Branch</th><th>Department</th><th>Agent</th><th>Task</th>
  <th class="num">Started</th><th class="num">Turns</th><th class="num">Tokens</th><th class="num">Subagents</th>
  <th class="num">Cost ($)</th></tr></thead>
  <tbody>{"".join(rows)}</tbody></table></div>
</section>"""


def runtime_section(m: Model) -> str:
    rt = m.runtime
    if rt is None:
        return ""
    t = rt["total"]
    if not t.calls:
        body = '<p class="muted">No runtime model calls in this period.</p>'
        title = "Run: the products' own model spend"
    else:
        dp_d = cost_dp([r["t"].cost for r in rt["departments"]])
        dp_p = cost_dp([r["t"].cost for r in rt["projects"]])
        body = (f'<div class="grid2"><section><h3 class="label">{esc(share_title(rt["departments"], "By the app’s department"))}</h3>'
                f'{table("Department", rt["departments"], dp_d, calls="Calls")}</section>'
                f'<section><h3 class="label">{esc(share_title(rt["projects"], "By project"))}</h3>'
                f'{table("Project", rt["projects"], dp_p, calls="Calls")}</section></div>')
        title = f"Run: the products spent {fmt_usd(t.cost)} on their own model calls"
    return f"""
<section class="card">
  <h2>{title}</h2>
  <p class="caption">Runtime usage the product reports itself (<code>[efficiency] runtime_usage</code>), grouped by
  the product's own departments · separate from agent spend above, never added to it · from
  {esc(", ".join(rt["configured"]) or "—")}</p>
  {body}
</section>"""


def cache_section(m: Model) -> str:
    t = m.spend
    prompt = t.input + t.cache_write + t.cache_read
    if not prompt:
        return ""
    hit = t.cache_read / prompt * 100
    return f"""
<section class="card">
  <h2>The cache served {hit:.0f}% of every prompt</h2>
  <p class="caption">Share of prompt tokens read from the cache instead of sent fresh · savings at list rates</p>
  <div class="strip divided">
    <div class="stat"><span class="label">Cache hit ratio</span><div class="value">{hit:.0f}%</div>
      <p class="caption">{fmt_tokens(t.cache_read)} read · {fmt_tokens(t.input)} sent fresh</p></div>
    <div class="stat"><span class="label">Written to cache</span><div class="value">{fmt_tokens(t.cache_write)}</div>
      <p class="caption">{fmt_usd(m.cache_premium)} paid above the input price for writing</p></div>
    <div class="stat"><span class="label">Saved by reading</span><div class="value">{fmt_usd(m.cache_saved)}</div>
      <p class="caption">what the cached tokens would have cost sent fresh, less their read price</p></div>
    <div class="stat"><span class="label">Net</span><div class="value">{fmt_usd(m.cache_saved - m.cache_premium)}</div>
      <p class="caption">saved, after the write premium</p></div>
  </div>
</section>"""


CSS = """
:root{--bg:hsl(0 0% 100%);--card:hsl(0 0% 99%);--fg:hsl(0 0% 11%);--muted:hsl(0 0% 36%);--border:hsl(0 0% 90%);
--track:hsl(0 0% 93%);--ok:hsl(0 0% 11% / .55);--warn:hsl(36 92% 40%);--danger:hsl(345 70% 46%);--ink:hsl(213 53% 45%);
--serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:hsl(0 0% 9%);--card:hsl(0 0% 12%);--fg:hsl(0 0% 98%);
--muted:hsl(0 0% 68%);--border:hsl(0 0% 20%);--track:hsl(0 0% 18%);--ok:hsl(0 0% 98% / .55);--warn:hsl(38 92% 55%);
--danger:hsl(345 72% 63%);--ink:hsl(213 60% 62%)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 var(--sans)}
main{max-width:1120px;margin:0 auto;padding:24px 16px 48px}
header{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:20px}
h1{font:400 28px/1.2 var(--serif);margin:0}
h2{font:400 17px/1.35 var(--serif);margin:0}
h3.label{margin:0 0 8px}
.label{font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.caption{font-size:12px;color:var(--muted);margin:2px 0 0}
.muted{color:var(--muted)}
.provenance{font-size:12px;color:var(--muted);opacity:.85;margin:8px 0 24px}
.headline{font:400 36px/1 var(--serif);font-variant-numeric:tabular-nums;margin:6px 0 4px}
.value{font:400 24px/1 var(--serif);font-variant-numeric:tabular-nums;margin:6px 0 4px}
.row{display:flex;align-items:flex-end;gap:12px}
.spark{width:120px;height:28px;color:var(--ink)}
.strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;background:var(--border);
border:1px solid var(--border);border-radius:12px;overflow:hidden}
.strip .stat{background:var(--card);padding:16px 20px}
.strip.divided{border:0;border-radius:0;margin-top:12px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;margin:0 0 16px}
.card>.caption{margin-bottom:16px}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:32px 40px}
.budget{margin:0 0 16px}
.budget-head{display:flex;flex-wrap:wrap;justify-content:space-between;gap:4px 12px;align-items:baseline}
.budget-name{font-weight:500}
.budget-row{display:flex;align-items:center;gap:12px;margin:6px 0 4px}
.meter{position:relative;flex:1;height:6px;background:var(--track)}
.meter .fill{height:100%}.fill.ok{background:var(--ok)}.fill.warn{background:var(--warn)}.fill.danger{background:var(--danger)}
.meter .tick{position:absolute;top:-4px;bottom:-4px;width:1px;background:var(--fg);opacity:.7}
.figure{font-size:13px;font-variant-numeric:tabular-nums;white-space:nowrap}
.pct{width:48px;text-align:right;font-weight:500;font-variant-numeric:tabular-nums}
.mark{display:inline-flex;align-items:center;gap:4px;font-size:12px}
.mark svg{width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.mark.neutral{color:var(--muted)}.mark.attention{color:var(--warn)}.mark.negative{color:var(--danger);font-weight:600}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);text-align:left;
padding:0 12px 6px;border-bottom:1px solid var(--border);white-space:nowrap;vertical-align:bottom}
td{padding:6px 12px;border-bottom:1px solid color-mix(in srgb,var(--border) 55%,transparent);vertical-align:baseline}
th:first-child,td:first-child{padding-left:0}th:last-child,td:last-child{padding-right:0}
tbody tr:last-child td{border-bottom:0}
tfoot td{border-top:1px solid var(--border);font-weight:500}
th.num{text-align:right}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.name{max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.share{width:120px}.share-h{width:120px}
.share{white-space:nowrap}.share .caption{display:inline-block;width:36px;text-align:right}
.minibar{display:inline-block;width:72px;height:6px;background:var(--track);vertical-align:middle}
.minibar span{display:block;height:100%;background:var(--ink)}
.chart{width:100%;height:auto;display:block}
.chart .bar{fill:var(--ink)}.chart .grid{stroke:var(--border);stroke-width:1}
.chart .axis{fill:var(--muted);font-size:12px;font-family:var(--sans)}
.chart .pace{stroke:var(--muted);stroke-width:1;stroke-dasharray:5 4}
.empty{height:160px;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:12px}
.note{font-size:12px;margin:10px 0 0}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
@media (max-width:900px){.strip{grid-template-columns:repeat(2,minmax(0,1fr))}.grid2{grid-template-columns:1fr}}
@media (max-width:560px){.strip{grid-template-columns:1fr}.card{padding:16px}.share,.share-h{display:none}
.budget-row{flex-wrap:wrap}.budget-row .meter{flex-basis:100%}.caption,.provenance{overflow-wrap:anywhere}}
"""


def render(m: Model) -> str:
    asof = m.now.strftime("%-d %b %Y %H:%M")
    plan = (f" · subscription {fmt_usd(m.plan_monthly, 0)}/month: a flat fee, not comparable with the period and "
            "not added" if m.plan_monthly else "")
    notes = "".join(f'<p class="note muted">note: {esc(n)}</p>' for n in m.notes)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent costs</title><style>{CSS}</style></head>
<body><main>
<header><h1>Agent costs</h1>
<span class="caption">{esc(m.period.last)} · {esc(m.period.since.strftime("%-d %b"))} to {esc(asof)}</span></header>
{stats(m)}
<p class="provenance">USD at API list rates · from Claude Code session transcripts on this machine · interactive
sessions, subagents and factory runs{plan} · another period: <code>factory cost page --since 7d</code></p>
{budgets_section(m)}
<section class="card"><h2>{esc(spend_trend_title(m))}</h2>
<p class="caption">Daily agent spend, USD{" · dashed rule is the daily pace the monthly budget allows" if m.budget_pace else ""}</p>
{daily_chart(m)}</section>
{grid_section(m)}
{landed_section(m)}
{breakdowns(m)}
{sessions_section(m)}
{runtime_section(m)}
{cache_section(m)}
{notes}
</main></body></html>
"""
