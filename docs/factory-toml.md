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
schedule = "0 6 * * 1"        # cron (minute hour day month weekday; numbers, comma lists, *); "off" = no schedule
ecosystems = ["pip", "npm", "docker"]
manifests = []                # empty = auto-detect requirements*.txt, pyproject.toml, package.json, compose, Dockerfile
risky = []                    # package-name globs that become tasks, never PRs (databases, workflow servers…)
ignore = []                   # package-name globs never touched
install = []                  # install the changed manifests in the worktree (pip install -r …, npm ci)
deprecation_check = []        # commands whose output shows deprecation warnings (tests with warnings on, a build)
max_prs = 3                   # per run; the upgrade capability is also the engine's team `upgrade`

[teams.qa]                    # a standing team the engine runs (docs/designs/engine.md); one table per team
prompt = "/factory:qa"        # required: what the agent session is asked to do
schedule = "0 2 * * *"        # cron, UTC; "off" (default) = events and `factory engine run-now` only
budget_minutes = 90           # one run's agent time, across retries
wake_on = ["ci_red"]          # events that wake it: ci_red, deploy_failed, incident, merged
```

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
| `factory task …` | the task system ([tasks.md](tasks.md)): `init`, `new`, `start`, `subtask`, `complete`, `close`, `set`, `show`, `check`, `check-pr`, `render`, `exists`; with `system = "commands"` only `new` / `exists`, running the repo's commands with every value shell-quoted |
| `factory run upgrade [--dry-run] [--keep]` | the whole upgrade unattended: fresh worktree off `origin/<default_branch>`, `[worktree] setup`, then the agent runs `/factory:upgrade`; one run per repo at a time; log under `~/.cache/software-factory/logs/<repo>/` |
| `factory schedule install\|remove\|show upgrade [--print]` | the repo's `schedule` as a launchd agent (macOS), a systemd user timer (Linux) or a crontab line (anything else) |

## Moving to another machine or runner

- **Another laptop:** clone software-factory, `install.sh`, then `factory schedule install upgrade --repo <path>`.
- **Another agent client:** change `[agent] command` (e.g. a different CLI agent that takes a prompt).
- **CI or a cloud box instead of a laptop:** run `factory run upgrade --repo .` from that environment's scheduler; it
  needs git push rights, `gh` auth and the agent's credentials there.
