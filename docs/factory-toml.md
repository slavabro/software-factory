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

[efficiency]                  # the efficiency scout (docs/tasks/F-32-efficiency-scout.md)
enabled = false               # true = the engine's team `efficiency`; `factory efficiency scan` works either way
schedule = "0 7 * * 1"        # cron in the engine's time zone; "off" = manual runs only
scope = "repo"                # "repo": this repo, its worktrees and `repos` · "all": every project on the machine
since = "7d"                  # the window a run looks back over (7d, 24h, 2w or a date)
sources = ["sessions", "runs", "runtime"]   # dev sessions, the factory's own runs, the app's runtime usage
repos = []                    # more repo paths whose sessions count as this repo's
projects_dir = "~/.claude/projects"         # where the agent client keeps session transcripts
runtime_usage = ""            # command printing the app's model usage as JSONL (schema in the F-32 spec);
                              # {since} = the window start, ISO 8601, shell-quoted; runs in the repo root
thresholds = {}               # e.g. { repeat_read_min = 4, min_usd = 1 }; every key and default: DEFAULT_THRESHOLDS
                              # in sfactory/config.py
disabled = []                 # detector names not run (e.g. ["permission-prompts"])
ignore = []                   # globs of session working dirs and file paths left out
poll_commands = ["gh pr checks", "gh run view", "gh run watch", "gh run list", "gh pr view", "docker ps"]
corrections = []              # regexes for a prompt that corrects the agent; empty = the built-in patterns
expensive_models = ["*opus*"] # model globs that count as top tier for runtime jobs
max_tasks = 5                 # tasks or PRs one run files

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

## Safety model for unattended runs

1. **Isolation** — every run is a fresh worktree off `origin/<default_branch>` with its own environments; the main
   checkout and other worktrees are never touched. Cleanup never deletes unpushed commits (the worktree is kept and
   named in the log).
2. **Allowlist** — the agent may only run the commands on `allowed_tools`; nothing that runs arbitrary code by name.
3. **Deny list** — `disallowed_tools` wins over the allowlist: merges, default-branch and force pushes, repo settings.
4. **Guard hook** — the factory plugin's PreToolUse hook blocks the same actions on the full command text during a
   run (`FACTORY_RUN` set), whatever the permission mode. Merging stays the repo's gate's job.
5. **The repo's own gate** — every PR goes through `[land]` (CI, review gate, the tower or the principal).
6. **Limits** — one run per repo and capability (a stale lock from a dead run is taken over), a timeout, `max_prs`.

## Commands

| Command | Does |
|---------|------|
| `factory config` | resolved config |
| `factory upgrade scan [--json]` | what can move, how far (patch / minor / major), risky or not, release-notes links |
| `<check> 2>&1 \| factory upgrade deprecations` | deprecation warnings raised from the repo's own code; exit 1 while any |
| `factory efficiency scan [--since 7d] [--all] [--json] [--top N]` | wasted tokens and repeated work in this repo's sessions, its worktrees, factory runs and runtime usage (`--all`: every project), priced; metrics, paths and command names only |
| `factory efficiency show SESSION [--json]` | one session's tool calls: paths, command names, result sizes, cost, flags E (error) P (approval) S (sleep) |
| `factory task …` | the task system ([tasks.md](tasks.md)): `init`, `new`, `start`, `subtask`, `complete`, `close`, `set`, `show`, `check`, `check-pr`, `render`, `exists`; with `system = "commands"` only `new` / `exists`, running the repo's commands with every value shell-quoted |
| `factory spec check` | every capability spec in `[specs] dir` is well formed, history-free and proven by tests that exist; exit 1 with each problem and its fix |
| `factory run efficiency [--dry-run] [--keep]` | the efficiency scout unattended, like `run upgrade`: the agent runs `/factory:efficiency`, files tasks or factory PRs |
| `factory run upgrade [--dry-run] [--keep]` | the whole upgrade unattended: fresh worktree off `origin/<default_branch>`, `[worktree] setup`, then the agent runs `/factory:upgrade`; one run per repo at a time; log under `~/.cache/software-factory/logs/<repo>/` |
| `factory engine install --repo PATH` | the engine runs this repo's teams: each team's `schedule` becomes a Temporal Schedule (in the engine's time zone, `engine.toml` `time_zone`, default the machine's), paused until its first run by hand |
| `factory engine run-now --repo PATH --team T` · `status` · `pause` · `resume` · `signal` | run a team now, see schedules and recent runs, pause or resume a team, wake teams on an event ([engine/README.md](../engine/README.md)) |

## Moving to another machine or runner

- **Another laptop:** clone software-factory, `install.sh`, then `factory engine install --repo <path>` on the one
  machine that should run the schedules (the home machine, F-06).
- **Another agent client:** change `[agent] command` (e.g. a different CLI agent that takes a prompt).
- **CI or a cloud box instead of a laptop:** run `factory run upgrade --repo .` from that environment's scheduler; it
  needs git push rights, `gh` auth and the agent's credentials there.
