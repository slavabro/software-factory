# factory-engine

The factory's own always-on program: standing teams as Temporal workflows on the factory's own Temporal server.
Design: [docs/designs/engine.md](../docs/designs/engine.md). Uses `runtime/` (factory-runtime) and the factory's
standard-library runner (`plugins/factory/sfactory`), so it runs from the factory checkout:

```sh
python3 -m venv ~/.local/share/software-factory/engine-venv
~/.local/share/software-factory/engine-venv/bin/pip install -r engine/requirements.lock -e runtime -e engine
```

| Module | Holds |
|--------|-------|
| `engine/models.py` | every workflow, activity and message input and output |
| `engine/workflows/team.py` | `TeamWorkflow`: one per (repo, team), woken by `wake`, alive only while it has work |
| `engine/workflows/tick.py` | `ScheduleTick`: a Schedule's bridge to a team |
| `engine/activities/` | resolve, prepare, agent session, deliver, clean, budget, outcome, wake |
| `engine/schedules.py` | Temporal Schedules reconciled from each repo's `.factory.toml` |
| `engine/worker.py` | the worker (task queue `factory`, Worker Versioning, Build ID from the code) |
| `engine/cli.py` | `signal`, `run-now`, `pause`, `resume`, `reconcile`, `status`, `build-id`, `set-current`, `drained` |
| `engine/settings.py` | `~/.config/software-factory/engine.toml` |

Tests (`cd engine && python -m unittest discover -s tests -t .`): workflows on the time-skipping test server,
activities against a real git repo and real processes, schedules and versioning on a real local server (downloaded
by the SDK on first use). Type check: `cd engine && python -m mypy engine`.
