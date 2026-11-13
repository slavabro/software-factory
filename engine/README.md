# factory-engine

The factory's own always-on program: standing teams as Temporal workflows on the factory's own Temporal server.
Design: [docs/designs/engine.md](../docs/designs/engine.md). Uses `runtime/` (factory-runtime) and the factory's
standard-library runner (`plugins/factory/sfactory`), so it runs from the factory checkout:

```sh
factory engine install --repo ~/Dev/FamilyOffice     # Docker project, this build's worker, schedules
factory engine status                                # schedules, running teams, recent runs
factory engine run-now --repo ~/Dev/FamilyOffice --team upgrade
factory engine upgrade                               # deploy a new build; retire drained ones
```

Each build is a frozen copy of the committed code under `~/.local/share/software-factory/engine/builds/<build>` with
its own virtualenv and worker service (`sfactory/engine.py`). For development, a virtualenv with
`pip install -r engine/requirements.lock -e runtime -e engine` runs the tests.

| Module | Holds |
|--------|-------|
| `engine/models.py` | every workflow, activity and message input and output |
| `engine/workflows/team.py` | `TeamWorkflow`: one per (repo, team), woken by `wake`, alive only while it has work |
| `engine/workflows/tick.py` | `ScheduleTick`: a Schedule's bridge to a team |
| `engine/activities/` | resolve, prepare, agent session, deliver, clean, budget, outcome, wake |
| `engine/process.py` | process groups, pidfile with start time, the worktree lock, keep-awake, sleep detection |
| `engine/steps.py` | prepare in a child process, so a cancel can stop the repo's setup |
| `engine/infra/` | the Docker Compose project: Temporal server, Postgres, UI, one-shot setup |
| `engine/schedules.py` | Temporal Schedules reconciled from each repo's `.factory.toml` |
| `engine/worker.py` | the worker (task queue `factory`, Worker Versioning, Build ID from the code) |
| `engine/cli.py` | `signal`, `run-now`, `pause`, `resume`, `reconcile`, `status`, `build-id`, `set-current`, `drained` |
| `engine/settings.py` | `~/.config/software-factory/engine.toml` |

Tests (`cd engine && python -m unittest discover -s tests -t .`): workflows on the time-skipping test server,
activities against a real git repo and real processes, schedules and versioning on a real local server (downloaded
by the SDK on first use). Type check: `cd engine && python -m mypy engine`.
