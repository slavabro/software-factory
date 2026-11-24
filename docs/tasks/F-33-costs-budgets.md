# F-33 — Costs page and budgets for agent spend

**Goal.** The principal sees what agent work costs and what it buys: by department (a function of the factory's org)
and project (a repo), by agent, task, process and model; what a task that landed cost each department; and, beside
it, what the products themselves spend on models at runtime. Budgets cap it: a factory run over a hard limit does not
start, a run over its per-run cap is stopped, and a session opens with one line when a budget is at its danger line or
over. The page is built and read like a product's own LLM-usage page (FamilyOffice is the first example): the same
section order, figures, meter, wording and budget states.

**How it relates.** F-31 (model and cost policy) needs tokens and cost for every run and stage; this task builds that
foundation, `sfactory/usage.py` (usage records in one format, the one price table, the store), and puts the org
attribution, the page and the budgets on top. Model per stage and the weekly report stay in F-31. The efficiency
scout (F-32) reads the same records and prices, and its runtime export uses the same row format. The org itself is
F-35's first piece: the shared org model starts here as the cost dimension.

## The org (F-35's model, used here for attribution)

Departments are functions, a project cuts across them. The default org (`sfactory/org.toml`, generic) follows a
product's org registry (id, name, description, reports_to, and the processes and agents a department owns):

| Department | Owns |
|------------|------|
| Chief of Staff | the tower, fleet, orchestrator, decision queue |
| Product | intake and shaping |
| Design | UX and technical design |
| Engineering | building |
| Quality | review and the reviewer lenses |
| Release | landing, the gate, CI follow-up |
| Upkeep | upgrade, efficiency, skills review |
| Direct | the principal's own sessions outside any pipeline skill: the only inferred bucket |

Who declares what: a factory skill in its SKILL.md (`metadata: department: upkeep`; skillcheck and
`claude plugin validate --strict` accept the key); the user file renames, extends or adds departments and maps any
skill; a repo's `.factory.toml` maps only its own skill names (its `/design-task` → design). No project's skill names
appear in factory code.

**Attribution of a turn**, resolved at query time from raw signals, so a changed org re-buckets all history:

1. a factory run (its worktree is under `runs/` or the engine's worktrees) takes its capability's department;
2. a subagent whose type is mapped takes that department;
3. otherwise the **stage**: the last mapped skill invoked in the session before the turn (a `Skill` tool call or a
   typed `/command`; `/clear` resets it); a subagent with no stage of its own inherits its parent's at spawn;
4. otherwise Direct.

## What it reads

Claude Code writes one JSONL transcript per session under `~/.claude/projects/<encoded-cwd>/<session>.jsonl`, and
one per subagent under `<session>/subagents/agent-<id>.jsonl` beside `agent-<id>.meta.json` (`agentType`). Assistant
records carry `message.id`, `message.model`, `message.usage` (input, output, cache writes split by TTL, cache reads,
`speed`, `inference_geo`, web searches), tool_use blocks, `timestamp`, `sessionId`, `cwd`, `gitBranch`. A message
streamed in several blocks is written once per block: turns merge by message id. Skill invocations are kept as events
(a `Skill` call's skill name, a typed command's name); nothing else from a message is read.

Only metrics, names and ids leave a transcript: never a prompt, a reply, a tool input or a tool result. The store
keeps a hash of each file's first bytes (to notice a rewrite), never the bytes.

## Data model

One record format for agent sessions and product runtime (`usage.USAGE_FIELDS`, `UsageRecord.as_row()`):

```
ts, project, department, agent, process, task, run_id, model,
input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost_usd
```

plus, for sessions, the raw signals it was worked out from (skill/stage, subagent type, capability, branch, cwd,
tools, speed, geo). It extends the efficiency scout's runtime schema v1 with project, department and process.

- **Store**: SQLite at `~/.cache/software-factory/usage.db` (stdlib `sqlite3`, WAL): files (size, mtime, offset
  read, fingerprint), turns, skill events, cwd → project, merged PRs. An unchanged transcript is not opened; a grown
  one is read from its offset. A schema change rebuilds the store from the transcripts.
- **Prices**: defaults from Anthropic's pricing page (checked 2026-11-17; cache TTLs, fast mode, US-only, long Haiku
  prompts, web search), `[cost.prices]` overrides; an unknown model is priced 0 and named.
- **Runtime**: a repo's `[efficiency] runtime_usage` command prints rows in the same format; the page groups them by
  the product's own departments.

## Config

`[org]`, `[cost]`, `[[budgets]]` in the user file (`~/.config/software-factory/factory.toml`) and in a repo's
`.factory.toml`; reference in [../factory-toml.md](../factory-toml.md). Budgets use a product's spending-rule
vocabulary — `daily_spend_usd`, `monthly_spend_usd`, `monthly_call_count` — plus `daily_tokens`, `monthly_tokens`
and `run_spend_usd` (each run of a capability); scope by department, project, project + department, agent, task or
capability, or `runtime = true` for the product's own calls. Hard by default on a project's spend, a capability's
runs and runtime spend; alert only by default on department, agent, task and all-spend budgets (steering targets).

## Budget states (as the product page)

| Used | State | Mark |
|------|-------|------|
| < warn | Within budget | ✓ check |
| ≥ warn (75 %) | Approaching | ! triangle |
| ≥ danger (90 %) | Near limit | ! triangle, heavier |
| ≥ 100 % | Over limit | ✕ cross |

Every state is a word and a shape; the meter adds ticks at warn and danger (position, readable in greyscale) and
prints the unclamped figure beside it; under the limit the percent is never rounded up to 100.

## Commands

`factory cost [--since] [--by department|project|agent|process|task|session|model] [--json]`, `factory cost budgets`,
`factory cost page [--out] [--open] [--no-landed]`, `factory cost org`, `factory cost check --capability C`,
`factory cost hook` (SessionStart), `factory cost refresh`.

## Enforcement

- `factory run <capability>` and the engine's `check_budget` refuse a run a hard budget over its limit covers, with
  the budget, the figure and where it is set.
- A hard `run_spend_usd` cap stops a `factory run` while it runs: the runner checks the spend of the sessions in the
  run's worktree every minute and stops the agent's process group past the cap (engine team runs: next step).
- The SessionStart hook reads `usage/state.json` (written by every evaluation), prints one line per budget at danger
  or over, and starts a detached refresh when the state is older than `stale_hours`. Under 1 s, never fails a session.

## Page

```
Agent costs                                                        last 30 days · as of …
[ Projected this month | Spend, last 30 days + sparkline | Daily rate | Cost per turn · Direct share · cache ]
provenance: USD at API list rates · transcripts · subscription fee shown, not added
Spending budgets        meters: label · window · hard/alert · ▮▮▮▯|▯| $x of $y · 82 % · ! Approaching
Daily spend             bars from zero, budget pace dashed
Department × project    rows: departments (org order), columns: projects by spend, totals, share
Cost per landed task    per department: landed tasks, their spend, per task; not landed; not checked; no task
Where the spend went    agent · process · task · model (ranked, "Other", total)
Costliest sessions      session · project · branch · department · agent · task · turns · tokens · subagents · cost
Run                     the products' runtime spend by the app's own departments and by project (when configured)
Cache                   hit ratio, written, saved, net
```

A task has landed when any branch it ran on merged as a PR (`gh pr list --state merged`, cached for six hours per
repo); without a main checkout or `gh`, its tasks are "not checked".

## Done when

- `factory cost`, `budgets`, `page`, `org` run over the laptop's real transcripts; a re-run with nothing new takes
  about a second.
- Turns are attributed by the org (stage, subagent type, capability, Direct), and changing the org re-buckets history
  without a re-scan.
- Budgets in the new vocabulary from the user file and a repo's file are evaluated together; a hard budget over its
  limit stops `factory run` and an engine team run with the reason; a per-run cap stops a running `factory run`.
- Tests with synthetic transcripts cover the store, stages, the org, prices, budgets and their hard defaults, the
  gate, the per-run cap, landed tasks, runtime rows, the hook's silence and the page (no transcript text, every state
  a word and a shape).
