# F-33 — Costs page and budgets for agent spend

**Goal.** The principal sees what agent work costs: by department, agent, task, repo, session and model, from
every Claude Code session on the laptop (interactive, subagents, factory runs). Budgets in config cap it: a factory
run over its hard limit does not start, and a session opens with one line when a budget is at its danger line or
over. The page is built and read like the product's own LLM-usage page (FamilyOffice is the first example): the
same section order, figures, meter, wording and budget states.

**How it relates.** F-31 (model and cost policy) needs tokens and cost for every run and stage; this task builds
that foundation, `sfactory/usage.py` (usage records and the one price table), and puts the page, departments and
budgets on top. Model per stage and the weekly report stay in F-31. The efficiency scout (F-32) reads the same
records and prices, so both report the same dollar.

## What it reads

Claude Code writes one JSONL transcript per session under `~/.claude/projects/<encoded-cwd>/<session>.jsonl`, and
one per subagent under `<session>/subagents/agent-<id>.jsonl` with an `agent-<id>.meta.json` beside it
(`agentType`). Every assistant record carries `message.id`, `message.model`, `message.usage` (`input_tokens`,
`output_tokens`, `cache_creation_input_tokens` split by TTL in `cache_creation.ephemeral_5m/1h_input_tokens`,
`cache_read_input_tokens`, `speed`, `inference_geo`, `server_tool_use.web_search_requests`), its `tool_use` blocks,
`timestamp`, `sessionId`, `cwd`, `gitBranch` and `isSidechain`. A message streamed in several content blocks is
written once per block with the same usage, so **turns are deduplicated by message id** (last record wins: it has
the final output count).

Only metrics leave a transcript: never a prompt, a reply, a tool input or a tool result. The page and reports show
numbers, session ids, repo, branch, task and model names only.

## Data model

`sfactory/usage.py` (the reader; F-04's efficiency scout imports it, so its interface is documented in the module
and kept stable):

```
UsageRecord(session_id, ts, cwd, repo, branch, model, input, output, cache_write, cache_read, cost_usd,
            is_subagent, agent_type, tool_names,
            message_id, cache_write_1h, capability, priced, speed, geo, web_searches, worktree)
records(since=None, until=None, projects=None, cache=None, prices=None) -> Iterator[UsageRecord]
```

- **repo**: a factory run's worktree (`~/.cache/software-factory/runs/<repo>/<capability>-<stamp>`, or the
  engine's `~/.local/state/software-factory/worktrees/<repo>/<team>/…`) gives repo and capability from the path;
  any other cwd resolves through git (the main checkout of a worktree, so every worktree counts for its repo); a
  cwd that no longer exists resolves from its nearest existing ancestor, else that ancestor's name.
- **cost_usd**: tokens × the model's price per million tokens (input, output, 5-minute and 1-hour cache writes,
  cache reads), × the fast-mode price when `speed = "fast"`, × 1.1 for US-only inference, plus web searches.
  Prices: defaults in code from Anthropic's pricing page (checked 2026-11-17), overridden by `[cost.prices]`. A
  model with no price is priced 0 and flagged (`priced = False`); the page and the CLI name it.
- **Incremental cache**: `~/.cache/software-factory/usage/files/<hash>.json` per transcript, keyed by path, size
  and mtime, holding the parsed turns and the byte offset read so far. An appended transcript is read from that
  offset; an unchanged one is not opened. Costs are computed at read time, so a price change needs no re-scan.

`sfactory/cost.py` (attribution, budgets, CLI) on top of it:

- **department** — a group the principal names, matched by rules in config (repo, capability, branch glob, cwd
  glob, agent); first match wins, otherwise `unassigned`.
- **agent** — `interactive` (a session the principal drives), `subagent:<type>` (Explore, general-purpose, …), or
  `run:<capability>` (a factory run: upgrade, an engine team; its subagents count for the run).
- **task** — the first match of `[cost] task_pattern` in the branch, then the worktree directory name; otherwise
  `no task`.

## Config

Two places, the same sections:

| File | Holds | Applies to |
|------|-------|------------|
| `~/.config/software-factory/factory.toml` (`FACTORY_USER_CONFIG`) | `[cost]`, `[departments.*]`, `[[budgets]]` | every repo |
| a repo's `.factory.toml` | the same sections | that repo's records only; its departments are tried first, its budgets are scoped to it |

```toml
[cost]
task_pattern = '...'          # regex; group 1 (or the whole match) is the task ID
plan_monthly_usd = 0          # the subscription's flat fee, shown beside the spend, never added to it
stale_hours = 6               # the SessionStart line refreshes the cache in the background when older

[cost.prices."claude-opus-5-5"]   # USD per million tokens; any key left out keeps the default
input = 4.0
output = 20.0
cache_write_5m = 5.0
cache_write_1h = 8.0
cache_read = 0.2
fast_multiplier = 2.0

[departments.upkeep]          # every key is a list of globs; all keys given must match (any glob within one)
capabilities = ["*"]
[departments.factory-dev]
repos = ["software-factory"]

[[budgets]]
department = "upkeep"         # scope: one of department / agent / task / repo; none = all spend
monthly_usd = 300
daily_usd = 25
monthly_tokens = 0            # 0 = no token limit; tokens = input + output + cache writes + cache reads
warn = 75                     # percent of the limit: "Approaching"
danger = 90                   # "Near limit"; 100 % and above is "Over limit"
hard = true                   # over the limit, `factory run` and the engine refuse to start runs in its scope

[[budgets]]
task = "*"                    # a glob scope is one budget per match: every task, each capped at $40 a month
monthly_usd = 40
```

## Budget states (as the product page)

| Used | State | Mark |
|------|-------|------|
| < warn | Within budget | ✓ check |
| ≥ warn (75 %) | Approaching | ! triangle |
| ≥ danger (90 %) | Near limit | ! triangle, heavier |
| ≥ 100 % | Over limit | ✕ cross |

Every state is a word and a shape; the meter adds ticks at warn and danger (position, readable in greyscale) and
prints the unclamped figure beside it.

## Commands

| Command | Does |
|---------|------|
| `factory cost [--since 7d\|30d\|month\|today\|DATE] [--by department\|agent\|task\|repo\|session\|model] [--json]` | a ranked table with share of spend |
| `factory cost budgets [--json]` | every budget's state for today and this month; exit 1 when any is over |
| `factory cost page [--since …] [--out PATH] [--open]` | the static page (default `~/.cache/software-factory/usage/costs.html`) |
| `factory cost check --capability C` | exit 1 with the reason when a hard budget covering this repo's C runs is over |
| `factory cost hook` | SessionStart: silent unless a budget is at danger or over; reads the saved state, never scans |

## Enforcement

- `factory run <capability>` checks before it takes the lock; over a hard limit it prints which budget, how far
  over and where to change it, and exits 1. `--dry-run` shows the budget line.
- The engine's `check_budget` activity asks the same question for every team run (verdict `stop` with the reason).
- The plugin's SessionStart hook runs `factory cost hook`: it reads `usage/state.json` (written by every scan),
  prints one line per budget at danger or over, and when the state is older than `stale_hours` starts a detached
  refresh for the next session. Under 1 s, never fails a session.

## Page wireframe

```
Agent costs                                            period: last 30 days · as of 17 Nov 2026 14:05
┌ Projected this month ┬ Spend, last 30 days ─ sparkline ┬ Daily rate ──────┬ Cost per turn ─────────┐
│ $1,234               │ $987  ▲ $120 vs the 30 days before│ $41/day          │ $0.071                 │
│ $612 month to date   │ 14,000 turns · 2.1B tokens        │ budget pace …    │ cache hit 94 %, $… saved│
└──────────────────────┴───────────────────────────────────┴──────────────────┴────────────────────────┘
USD at API list rates · from Claude Code transcripts · subscription: $200/month flat, not added
Budgets       meter rows: name · ▮▮▮▮▮▯|▯|  $x of $y · 82 % · ! Approaching
"$987 over 30 days, peaking at $88 on 12 Nov"   daily bars from zero, budget pace as a dashed rule
Where the spend went   four ranked tables: department · agent · task · repo (share bars, Other row, total)
Top sessions          session · repo · branch · agent · turns · tokens · cost
Model mix             model · turns · tokens · cost · share (unpriced models named)
Cache                 hit ratio, tokens read vs written, what the cache saved at list rates
```

One self-contained HTML file: inline CSS and SVG, no script needed to read it, no external fetch, light and dark
themes from the system setting.

## Done when

- `factory cost`, `factory cost budgets`, `factory cost page` run over the laptop's real transcripts; a re-run
  with nothing new takes seconds.
- Budgets from the user file and a repo's file are evaluated together; a hard budget over its limit stops
  `factory run` and an engine team run with the reason; the SessionStart line appears only at danger or over.
- Tests with synthetic transcripts cover dedupe, subagents, run paths, pricing (cache TTLs, fast, US-only,
  unknown model), attribution rules, task parsing, budget states, the gate, the hook's silence and the page (no
  transcript text in it, every state a word).
- `docs/factory-toml.md` documents `[cost]`, `[departments]`, `[[budgets]]` and the commands.
