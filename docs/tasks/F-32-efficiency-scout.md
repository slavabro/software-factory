# F-32 — Efficiency scout

**Goal.** Find where agents spend tokens on work that was repeated or not needed, and turn each pattern into a fix,
so cost falls without the principal reading transcripts. Agents here means the sessions the principal types in, the
factory's own unattended runs, and the model calls a product makes at run time. It is the measuring half of the
factory improver team (F-23) and feeds the learning loop (F-16).

**Split.** As with upgrades (F-01): a deterministic scan in Python (`factory efficiency scan`,
`plugins/factory/sfactory/efficiency.py`) finds and prices patterns; the skill `/factory:efficiency` groups them,
checks the worst sessions and files fixes.

## Sources

| Source | Where | How a session is placed |
|---|---|---|
| Dev sessions | `[efficiency] projects_dir` (Claude Code: `~/.claude/projects/<encoded cwd>/<session>.jsonl`, subagents in `<session>/subagents/agent-*.jsonl` + `.meta.json`) | `git rev-parse --git-common-dir` of the session's cwd → the main checkout, so every worktree counts for its repo; a removed worktree gets the repo of the worktrees still next to it; a subagent gets its parent's place |
| Factory runs | the same transcripts, when the cwd is under `factory run`'s `~/.cache/software-factory/runs/<repo>/<capability>-<stamp>` or the engine's `$FACTORY_ENGINE_STATE/worktrees/<repo>/<team>/…` | origin `factory-run`, the repo and capability from the path |
| App runtime | `[efficiency] runtime_usage`: a command in the product repo printing JSONL (schema below) | the repo whose config names it |

`[efficiency] scope = "repo"` scans the repo, its worktrees and `[efficiency] repos`; `"all"` (or `--all`) scans every
project. Spend counts each model request once (by message id across transcripts: a resumed or forked session copies
earlier turns into its own file) and only inside the window, as F-33's `usage.py` does. The detectors read whole
sessions active in the window, since a pattern does not stop at its edge.

## Project and department

Every finding carries its **project** (the repo, worktrees folded in) and the **department** that owns the work. The
departments are functions: Chief of Staff, Product, Design, Engineering, Quality, Release, Upkeep, and Direct (the
principal's own work); the project is a separate dimension. A session's department, first that applies:

1. the last skill or command invoked in it that declares a department;
2. for a subagent, its declared type, else its parent's department;
3. for a factory run, its capability's skill (the run's prompt, e.g. `/factory:upgrade`);
4. otherwise **Direct**.

A declaration today is `department:` in the front matter of a skill, command or subagent file, top level or under
`metadata:` (the factory's skills declare `metadata: department: Upkeep`). **TODO(F-33, PR #26):** #26 settles the
one declaration mechanism (`org/org_structure.yaml`, agents declaring `department:`) and the `[org]` config;
`efficiency.Org` and `department_of` then read that, so the costs page and the scout never disagree on an owner.
Cross-session findings take the owner of most of their sessions (`base-context`) or of the biggest one
(`duplicate-effort`); runtime findings take the department the app's own export names (`unassigned` when it names
none). The digest totals spend, findings and $ at stake per department, so fixes go to the department that owns them.

## Cost model

Per request: input, 5-minute and 1-hour cache writes, cache reads and output, at API list prices per model
(`PRICES`, mirroring F-33's table; **TODO(F-33):** read usage.py's table once #26 lands, and build `Turn` from its
records). A tool call costs its share of the request that made it plus its result carried by every later request
until the next compaction: `tokens × (cache write price + cache read price × later requests)`. *At stake* is what the
pattern cost: an upper bound on the saving; findings overlap (a big read inside a long session).

Reconciled with F-33's reader on 2026-11-24, 30 days, all projects: $3,568 here against $3,564 from
`usage.records` (0.1%). The first run's $4,957 came from family prices (Opus 5.5 priced as $5 / $25 / $0.50 cache
read instead of $4 / $20 / $0.20) and from counting whole sessions instead of the window; streamed blocks were
already merged by message id.

## Detectors

Thresholds are `[efficiency] thresholds.<name>` (defaults in `sfactory/config.py` `DEFAULT_THRESHOLDS`); any detector
can be turned off with `[efficiency] disabled`. A finding below `min_usd` (0.25) is dropped, except the two counted ones.

| Detector | Fires when (default) | At stake |
|---|---|---|
| repeat-read | a file read `repeat_read_min` (3)+ times in a session, same range, no edit or compaction between | the redundant reads |
| big-read | a whole-file Read returns more than `big_read_tokens` (8000) | the read |
| oversized-result | any other tool result over `oversized_tokens` (6000) | the result, carried |
| repeat-call | `repeat_call_min` (3)+ identical calls (same tool, same input) | the repeats |
| retry-unchanged | a failed call repeated unchanged within `retry_window` (3) calls and failing again | the repeats |
| polling | `poll_min` (5)+ sleeps, `BashOutput`/`TaskOutput` or `poll_commands` (`gh pr checks`, `gh run view` …) | all but the first |
| cache-rebuild | a cache write over `cache_rebuild_tokens` (20000) mid-session after a pause over `cache_gap_seconds` (300), a model switch or a context change | rewrite minus read price |
| long-context | requests carrying more than `long_context_tokens` (150000) | the excess, at cache-read price |
| subagent-overspawn | `max_subagents` (6)+ subagents from one session | the base context each one loads |
| subagent-reread | a subagent reads a file its parent had read before spawning it | those reads |
| base-context | a repo's median first-request context over `base_context_tokens` (30000) | the excess × every request; lists the skills used |
| duplicate-effort | sessions in different worktrees of one repo editing `duplicate_shared_files` (3)+ of the same files (≥ `duplicate_overlap` 0.6 of the smaller set; files most sessions touch don't count) or on the same branch | the cluster's cost minus its biggest session |
| permission-prompts | `permission_prompts` (5)+ calls waiting for approval (AskUserQuestion excluded) | counted; lists the commands to allowlist |
| user-correction | a typed prompt matching a correction pattern (`no`, `stop`, `repeat` "I said", `wrong`, `revert`, `why did…`) or an interrupt | the work since the prompt before it; learning-loop candidates |
| runtime-repeat-prompt | `runtime_repeat_min` (3)+ calls with one `prompt_hash` | the repeats: a cache candidate |
| runtime-expensive-model | an agent/task on `expensive_models` (`*opus*`), 5+ calls, median output ≤ `runtime_trivial_output_tokens` (500) | 60% of its cost (one tier down) |
| runtime-retry | calls with outcome error, retry or expired, or attempt > 1 | their cost |
| runtime-unused | `output_used = false` | their cost |

Skills and CLAUDE.md files loaded every session are measured as `base-context`; their sizes come from `skillcheck`
and the judgement from `/factory:skills-review`.

## Runtime usage schema (v2)

One JSON object per line on stdout; `{since}` in the command is replaced by the window start (ISO 8601, shell-quoted).
Required: `ts`, `model`. v2 adds `department` and `process`; a v1 line (without them, without `v`) is still read. A
line with a newer `v` than the reader knows is reported, not guessed.

| Field | Type | Meaning |
|---|---|---|
| v | int | schema version, 2 (optional; absent = 1) |
| ts | ISO 8601 | when the call was made |
| kind | `llm_call` \| `agent_job` | default `llm_call` |
| agent | string | who called (agent, department or service) |
| task | string | what for (job type, caller, workflow type) |
| department | string | the app's own department that owns the agent, from the app's org declaration (v2); kept as given |
| process | string | the process or workflow run the call belonged to, if any (v2) |
| run_id | string | groups the calls of one job or run (optional) |
| model | string | the model that answered |
| input_tokens, output_tokens, cache_write_tokens, cache_read_tokens | int | default 0 |
| cost_usd | float \| null | null = priced from the tokens |
| duration_ms | int \| null | |
| prompt_hash | string | hash of the normalised request (system + messages + tools), never the text |
| outcome | `ok` \| `error` \| `retry` \| `expired` \| `cancelled` | default `ok` |
| attempt | int | 1 for the first try |
| output_used | bool \| null | whether anything consumed the result; null = unknown |

## Output

`factory efficiency scan [--since 7d] [--all] [--json] [--top N]`: the window's totals, the top findings (at most
three per detector; `--json` has all) with project and department, spend and $ at stake per department, totals per
detector, corrections, permission prompts and cost per project.
`factory efficiency show SESSION [--json]`: one session's calls with paths, command names, sizes, cost and letter flags
(E error, P approval, S sleep). `factory run efficiency` and the engine's team `efficiency` (`[efficiency] enabled`,
`schedule`) run the skill unattended; the digest goes through `[report] with`.

**Privacy.** Transcripts hold private data. A session is reduced while it is read to metrics, ids, timestamps, file
paths, tool names, skill names and command names (`command_head`: the program and subcommand, never arguments).
Prompt text, file contents and tool output never leave the reader; a test checks every output for a planted secret.
A failing `runtime_usage` command is reported by exit code only (its stderr may hold data).

## Done when

- [x] `factory efficiency scan` over 30 days of real transcripts on the principal's laptop prices every detector
  above, in seconds, and the top findings are true when checked against `factory efficiency show`.
- [x] `/factory:efficiency` turns findings into tasks (deduplicated with `factory task exists`) or factory PRs.
- [x] `[efficiency]` documented in [factory-toml.md](../factory-toml.md); tests for every detector, the config and
  privacy.
- [ ] The product repo emits runtime usage (its own task) and a scheduled run files its first fixes.
- [x] Every finding carries project and department; the digest groups by department; totals match F-33's reader.
- [ ] `Turn`, `price` and the department declaration come from F-33 (`usage.py`, the org declaration) once #26 lands.
