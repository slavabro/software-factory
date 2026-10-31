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
new = ""                      # how to file a task; placeholders {title} {body_file} {priority}
                              # empty: `gh issue create --title {title} --body-file {body_file}`

[worktree]
setup = []                    # run in each fresh run worktree; $REPO = the main checkout, $WORKTREE = the run's
                              # give the worktree its OWN venv / node_modules: an upgrade installs into them

[agent]
command = ["claude", "-p", "{prompt}", "--allowedTools", "{allowed_tools}"]   # any CLI agent; {prompt} {allowed_tools}
allowed_tools = [...]         # replaces the default list (see sfactory/config.py DEFAULT_ALLOWED_TOOLS)
extra_allowed_tools = []      # added to it — usually what a repo wants

[upgrade]
enabled = true
schedule = "0 6 * * 1"        # cron (minute hour day month weekday; numbers, comma lists, *); "off" = no schedule
ecosystems = ["pip", "npm", "docker"]
manifests = []                # empty = auto-detect requirements*.txt, pyproject.toml, package.json, compose, Dockerfile
risky = []                    # package-name globs that become tasks, never PRs (databases, workflow servers…)
ignore = []                   # package-name globs never touched
install = []                  # install the changed manifests in the worktree (pip install -r …, npm ci)
deprecation_check = []        # commands whose output shows deprecation warnings (tests with warnings on, a build)
max_prs = 3                   # per run
```

## Commands

| Command | Does |
|---------|------|
| `factory config` | resolved config |
| `factory upgrade scan [--json]` | what can move, how far (patch / minor / major), risky or not, release-notes links |
| `<check> 2>&1 \| factory upgrade deprecations` | deprecation warnings raised from the repo's own code; exit 1 while any |
| `factory run upgrade [--dry-run] [--keep]` | the whole upgrade unattended: fresh worktree off `origin/<default_branch>`, `[worktree] setup`, then the agent runs `/factory:upgrade`; one run per repo at a time; log under `~/.cache/software-factory/logs/<repo>/` |
| `factory schedule install\|remove\|show upgrade [--print]` | the repo's `schedule` as a launchd agent (macOS), a systemd user timer (Linux) or a crontab line (anything else) |

## Moving to another machine or runner

- **Another laptop:** clone software-factory, `install.sh`, then `factory schedule install upgrade --repo <path>`.
- **Another agent client:** change `[agent] command` (e.g. a different CLI agent that takes a prompt).
- **CI or a cloud box instead of a laptop:** run `factory run upgrade --repo .` from that environment's scheduler; it
  needs git push rights, `gh` auth and the agent's credentials there.
