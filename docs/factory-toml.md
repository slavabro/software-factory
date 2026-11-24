# `.factory.toml` reference

A repo opts into the factory with a `.factory.toml` at its root. Every key is optional; the defaults suit a plain
repo with `make ci`. `factory config` prints the resolved result.

```toml
[repo]
name = "my-repo"              # default: the directory name (used for run dirs, logs, scheduler labels)
default_branch = "main"

[verify]
commands = ["make ci"]        # proves the code still works; every upgrade PR must pass them

[land]
with = "skill:land"           # "skill:<name>" invokes that repo skill; anything else is a shell command

[tasks]
system = "factory"            # the factory's task system (docs/tasks.md); "commands" = the two commands below
legacy_duplicate_ids = []     # IDs that shipped twice before IDs were checked
id_pattern = '...'            # regex for a task reference in prose; default per system: "factory" = its IDs
                              # (1.60, 0.41b), "commands" = an issue number (#12). `factory spec check` refuses it
# system = "commands": placeholders are shell-quoted by `factory task new|exists`
new = "gh issue create --title {title} --body-file {body_file}"   # {title} {body_file} {priority}
exists = "gh issue list --state open --search {query} --json number --jq 'length > 0' | grep -q true"  # exit 0 = exists

[worktree]
setup = []                    # run in each fresh run worktree; $REPO = the main checkout, $WORKTREE = the run's
                              # give the worktree its OWN venv / node_modules: an upgrade installs into them
path = []                     # worktree-relative dirs put first on PATH for the agent (e.g. ".venv/bin")

[agent]
command = ["claude", "-p", "{prompt}", "--allowedTools", "{allowed_tools}", "--disallowedTools", "{disallowed_tools}"]
allowed_tools = [...]         # replaces the default list (sfactory/config.py DEFAULT_ALLOWED_TOOLS) — narrow on purpose
extra_allowed_tools = []      # added to it — usually what a repo wants (its own scripts, make targets)
disallowed_tools = [...]      # replaces the default deny list (merge, push to the default branch, force push, …)
extra_disallowed_tools = []   # added to it
timeout_minutes = 180         # the agent's whole process group is killed after this

[report]
with = ""                     # command run with {file} = the run's digest (e.g. post it somewhere); empty = path in the log

[upgrade]
enabled = true
schedule = "0 6 * * 1"        # cron (minute hour day month weekday) in the engine's time zone; "off" = no schedule
ecosystems = ["pip", "npm", "docker"]
manifests = []                # empty = auto-detect requirements*.txt, pyproject.toml, package.json, compose, Dockerfile
risky = []                    # package-name globs that become tasks, never PRs (databases, workflow servers…)
ignore = []                   # package-name globs never touched
install = []                  # install the changed manifests in the worktree (pip install -r …, npm ci)
deprecation_check = []        # commands whose output shows deprecation warnings (tests with warnings on, a build)
max_prs = 3                   # per run; the upgrade capability is also the engine's team `upgrade`

[specs]                       # capability specs: what each capability promises today, proven by tests
dir = "specs"                 # repo-relative folder of <capability>.md files; keep it out of every workflow's paths-ignore
resolver = "pytest"           # how a cited test is found without running it: pytest node IDs, by AST

[teams.qa]                    # a standing team the engine runs (docs/designs/engine.md); one table per team
prompt = "/factory:qa"        # required: what the agent session is asked to do
schedule = "0 2 * * *"        # cron in the engine's time zone; "off" (default) = events and `factory engine run-now` only
budget_minutes = 90           # one run's agent time, across retries
wake_on = ["ci_red"]          # events that wake it: ci_red, deploy_failed, incident, merged
```

## Capability specs (`[specs]`)

One file per capability, `<dir>/<capability>.md` (`CLAUDE.md` and `README.md` there are the folder's own notes):

```markdown
---
capability: evaluations                      # equals the file name
owners: [common/evaluations, common/policies]  # directories whose CLAUDE.md links this file
---
# Asset evaluations

One or two lines on what the capability is.

## judgement-is-never-pruned
Disabling a criterion SHALL remove only the ratings a rule gave; a rating the principal gave SHALL be kept.

Proven by:
- tests/database/test_rule_pass.py::test_disabled_criterion_prunes_rule_ratings_only
- tests/database/test_rule_pass.py::TestPrune::test_keeps_judgement[analyst]
```

`factory spec check` (run it from the repo's CI, e.g. `make ci`) fails, naming the file, the requirement slug and
the fix, when:

- the front matter is missing or has other keys, `capability` differs from the file name, or an owner directory
  is missing or its CLAUDE.md has no link to the spec;
- a `## <slug>` heading is not a lowercase slug or repeats one, a requirement has no SHALL or MUST, or it does
  not end with a `Proven by:` list of at least one test;
- a cited test does not resolve. Nothing is run or imported: the `pytest` resolver parses the file and finds
  `path::function`, `path::Class::method` (nested classes and base classes in the same file too); a `[param]`
  suffix is accepted as written;
- the prose holds history: a task reference (`[tasks] id_pattern`) or a date. Why and when live in the finished
  task specs and in git;
- a `paths-ignore` glob in `.github/workflows/*.yml` (or `.yaml`) matches a spec (GitHub Actions globs: `**` crosses `/`, `*`
  does not), so a PR that only edits a spec would skip that workflow. `docs/**` is the usual trap; hence `specs/`
  at the root.

## Org, costs and budgets: `[org]`, `[cost]`, `[[budgets]]`

Agent spend comes from Claude Code's session transcripts on the machine (every repo, interactive sessions, subagents,
factory runs), priced at API list rates (`factory cost`, spec: [tasks/F-33-costs-budgets.md](tasks/F-33-costs-budgets.md)).
It is cut two ways: by **department**, a function in the factory's org, and by **project**, the repo, which cuts
across the departments. The same sections live in two places:

| File | Holds |
|------|-------|
| `~/.config/software-factory/factory.toml` (`FACTORY_USER_CONFIG`; only these sections) | the org (rename, describe or extend a department, add one, map any skill), overall budgets |
| a repo's `.factory.toml` | `[org.skills]` / `[org.agents]` / `[org.capabilities]` for the repo's own names only, its budgets (they count only its spend), `[cost]` keys that win for its turns, `[efficiency] runtime_usage` |

### The org

The default org ships with the factory (`plugins/factory/sfactory/org.toml`), in the shape of a product's org
registry: each department has an `id`, `name`, `description`, `reports_to` and owns `skills` (as invoked:
`factory:upgrade`, `fleet`), `agents` (subagent types) and `capabilities` (`factory run` / engine teams).

| id | Department | Owns by default |
|----|------------|-----------------|
| `cos` | Chief of Staff | the tower, fleet, orchestrator, decision queue (`fleet` declares itself) |
| `product` | Product | intake and shaping |
| `design` | Design | UX and technical design (subagent type `Plan`) |
| `engineering` | Engineering | building |
| `quality` | Quality | review and the reviewer lenses (`code-review`, `security-review`, `review`) |
| `release` | Release | landing, the gate, CI follow-up |
| `upkeep` | Upkeep | capabilities `upgrade`, `efficiency`; `factory:upgrade` and `factory:skills-review` declare themselves |
| `direct` | Direct | the principal's own sessions outside any pipeline skill: the only inferred bucket |

A factory skill declares its own department in its SKILL.md front matter:

```yaml
metadata:
  department: upkeep
```

```toml
# user file: the org itself
[org]
name = "Software factory"
direct = "direct"                 # the inferred bucket's id

[[org.departments]]               # an existing id is renamed / described / extended; a new id is added
id = "design"
name = "UX and design"
skills = ["mockup"]
agents = ["ux-reviewer"]          # subagent types
capabilities = []

[org.skills]                      # any skill, for every project
review-pr = "quality"

# a repo's .factory.toml: only its own names, for its own turns
[org.skills]
design-task = "design"
implement-feature = "engineering"
review-code = "quality"
land = "release"
```

**How a turn is attributed** (raw signals are stored per turn and resolved at query time, so a changed org re-buckets
all history): a factory run takes its capability's department; a subagent whose type is mapped takes that
department; otherwise the **stage**, the last mapped skill invoked in the session before the turn (a `Skill` call or
a typed `/command`; `/clear` resets it; a subagent with no stage of its own inherits its parent's at the moment it
was spawned); otherwise Direct. **Agent** is `interactive`, `subagent:<type>` or `run:<capability>`; **process** is
the skill, subagent type or capability that decided the department; **task** is the first match of
`[cost] task_pattern` in the branch, else the worktree's name (`no task` otherwise). `factory cost org` prints the
org as resolved.

### `[cost]`

```toml
[cost]
task_pattern = '...'          # default: "PROJ-12" / "f-33" (letters-dash-digits) or "1.60a" (dotted)
plan_monthly_usd = 200        # a subscription's flat fee: shown beside the spend, never added to it (user file)
stale_hours = 6               # the SessionStart check refreshes the usage store in the background when older (user file)
web_search_usd = 0.01         # per server-side web search

[cost.prices."claude-opus-5-5"]   # USD per million tokens; a key left out keeps the default (sfactory/usage.py)
input = 4.0                   # also output, cache_write_5m, cache_write_1h, cache_read; a new model needs all five
fast_multiplier = 2.0         # applied when a turn ran in fast mode; US-only inference adds 10 %
```

### `[[budgets]]`

The vocabulary of a product's spending rules, plus tokens and a per-run cap:

```toml
[[budgets]]
name = "FamilyOffice, per day"    # optional; the label otherwise says the scope
project = "FamilyOffice"          # scope: department, project, agent, task, capability; project may pair with one
daily_spend_usd = 300             # limits, at least one: daily_spend_usd, monthly_spend_usd, monthly_call_count,
                                  # daily_tokens, monthly_tokens, run_spend_usd (each single run of a capability)
warn = 75                         # % of the limit: "Approaching" (default 75)
danger = 90                       # "Near limit" (default 90); from 100 %: "Over limit"
hard = true                       # see the defaults below

[[budgets]]
capability = "upgrade"            # each upgrade run: stopped while it runs once it passes $15
run_spend_usd = 15

[[budgets]]
department = "engineering"        # a steering target: alert only unless hard = true
monthly_spend_usd = 400

[[budgets]]
task = "*"                        # a glob scope is one budget per match: each task
monthly_spend_usd = 250

[[budgets]]
runtime = true                    # the product's own model calls ([efficiency] runtime_usage), not agent sessions
project = "FamilyOffice"
monthly_spend_usd = 50
```

**Hard by default** where a budget guards money: a project's own spend, a capability's runs, and runtime spend.
Department, agent, task and all-spend budgets are **alert only** by default (steering targets). Over a hard limit,
`factory run <capability>` and the engine's `check_budget` refuse a run the budget covers (all spend, the run's
project, its department, its agent `run:<capability>` or its capability) and say which budget and where it is set; a
`run_spend_usd` cap stops a `factory run` while it runs (its spend is checked every minute); a hard runtime budget is
shown and reported, and the product enforces it. The plugin's SessionStart hook (`factory cost hook`) prints one line
per budget at its danger line or over, and nothing otherwise; it reads the state the last scan saved, never scans.

Calendar day and month, local time. Tokens = input + output + cache writes + cache reads; calls = assistant turns
(model calls at runtime).

### `[efficiency] runtime_usage`

A command (run in the repo's main checkout) that prints the product's own model usage as JSONL, one object per call
in the shared usage format (`sfactory/usage.py` `USAGE_FIELDS`: `ts`, `model` required; `project`, `department`,
`agent`, `process`, `task`, `run_id`, the four token counts, `cost_usd` or null to price from the tokens). `{since}`
is replaced by the window start (ISO 8601, shell-quoted). The costs page groups it by the product's own departments
in its **Run** section; it is never added to agent spend. A failing command is reported by its exit code only.

## Safety model for unattended runs

1. **Isolation** — every run is a fresh worktree off `origin/<default_branch>` with its own environments; the main
   checkout and other worktrees are never touched. Cleanup never deletes unpushed commits (the worktree is kept and
   named in the log).
2. **Allowlist** — the agent may only run the commands on `allowed_tools`; nothing that runs arbitrary code by name.
3. **Deny list** — `disallowed_tools` wins over the allowlist: merges, default-branch and force pushes, repo settings.
4. **Guard hook** — the factory plugin's PreToolUse hook blocks the same actions on the full command text during a
   run (`FACTORY_RUN` set), whatever the permission mode. Merging stays the repo's gate's job.
5. **The repo's own gate** — every PR goes through `[land]` (CI, review gate, the tower or the principal).
6. **Limits** — one run per repo and capability (a stale lock from a dead run is taken over), a timeout, `max_prs`,
   and the hard `[[budgets]]` that cover the run.

## Commands

| Command | Does |
|---------|------|
| `factory config` | resolved config |
| `factory upgrade scan [--json]` | what can move, how far (patch / minor / major), risky or not, release-notes links |
| `<check> 2>&1 \| factory upgrade deprecations` | deprecation warnings raised from the repo's own code; exit 1 while any |
| `factory task …` | the task system ([tasks.md](tasks.md)): `init`, `new`, `start`, `subtask`, `complete`, `close`, `set`, `show`, `check`, `check-pr`, `render`, `exists`; with `system = "commands"` only `new` / `exists`, running the repo's commands with every value shell-quoted |
| `factory spec check` | every capability spec in `[specs] dir` is well formed, history-free and proven by tests that exist; exit 1 with each problem and its fix |
| `factory run upgrade [--dry-run] [--keep]` | the whole upgrade unattended: fresh worktree off `origin/<default_branch>`, `[worktree] setup`, then the agent runs `/factory:upgrade`; one run per repo at a time; log under `~/.cache/software-factory/logs/<repo>/` |
| `factory cost [--since 7d\|30d\|month\|today\|DATE] [--by department\|project\|agent\|process\|task\|session\|model] [--json]` | agent spend across every repo on the machine, ranked, with share; USD at API list rates |
| `factory cost budgets [--json]` | every budget's state (word and shape: ✓ Within budget, ! Approaching, ! Near limit, ✕ Over limit); exit 1 while any is over |
| `factory cost page [--since …] [--out PATH] [--open] [--no-landed]` | the costs page, one self-contained HTML file (default `~/.cache/software-factory/usage/costs.html`) |
| `factory cost org [--json]` | the org as resolved: each department and the skills, subagent types and capabilities it owns |
| `factory cost check --capability C` | exit 1 with the reason when a hard budget stops this repo's C runs |
| `factory engine install --repo PATH` | the engine runs this repo's teams: each team's `schedule` becomes a Temporal Schedule (in the engine's time zone, `engine.toml` `time_zone`, default the machine's), paused until its first run by hand |
| `factory engine run-now --repo PATH --team T` · `status` · `pause` · `resume` · `signal` | run a team now, see schedules and recent runs, pause or resume a team, wake teams on an event ([engine/README.md](../engine/README.md)) |

## Moving to another machine or runner

- **Another laptop:** clone software-factory, `install.sh`, then `factory engine install --repo <path>` on the one
  machine that should run the schedules (the home machine, F-06).
- **Another agent client:** change `[agent] command` (e.g. a different CLI agent that takes a prompt).
- **CI or a cloud box instead of a laptop:** run `factory run upgrade --repo .` from that environment's scheduler; it
  needs git push rights, `gh` auth and the agent's credentials there.
