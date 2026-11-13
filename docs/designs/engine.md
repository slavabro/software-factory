# F-50 design: the factory engine on Temporal

The factory's own always-on program. It runs every piece of factory work that isn't a person typing: standing teams
(QA, DevOps, upgrades, …), later the feature pipeline (F-12). Each piece is a Temporal workflow, so it survives
restarts, retries what failed, waits without holding anything, and shows every run in one UI. Context and build
order: [foundation.md](foundation.md). Practices follow Temporal's documentation (sources at the end) and the house
rules products already use (FamilyOffice's `temporal-workflows` checklist). Reviewed against those docs on
2026-11-13; the review's findings are folded in.

## 1. One program of its own

The factory's Temporal is separate from every product's: its own server, database, UI, namespace, workers and
history. The factory changes a product only through pull requests; it never talks to a product's Temporal.

| Piece | Runs as | Where |
|-------|---------|-------|
| Temporal server 1.32 (≥ 1.29.1 for Worker Versioning) | Docker Compose project `software-factory` | home machine (F-06 setting); host port 7243 → container 7233 |
| Postgres 16 | same project, named volume `software-factory-temporal-db` | history survives restarts |
| Temporal UI 2.57 | same project | host port 8243 → container 8080 |
| Setup (one-shot) | same project, `temporalio/admin-tools` | creates namespace `factory` (retention 30 days) and the search attributes `FactoryRepo`, `FactoryTeam` (Keyword) |
| Worker | host process, launchd (macOS) / systemd user service (Linux) | needs git, `gh`, agent CLIs, Orca on the host |

Not `temporal server start-dev`: it keeps no history, and a product's dev script may stop every `start-dev` on the
machine (FamilyOffice's `start_all.sh` does). Only host ports change, never the container's; ports and versions are
settings in `~/.config/software-factory`. `factory engine install | status | stop | upgrade` manages the set, and
`status` names any port already taken. The standard-library `factory` CLI never imports the engine: for engine
commands it runs the engine's own virtualenv (`<venv>/bin/python -m engine.cli …`), which uses `factory_runtime`.

## 2. Code layout and house rules

```
engine/                        its own pyproject + lockfile: factory-runtime (../runtime), temporalio, pydantic
├── engine/models.py           every workflow / activity / message input and output (Pydantic, one per call)
├── engine/workflows/team.py   TeamWorkflow
├── engine/workflows/tick.py   ScheduleTick
├── engine/activities/         repo.py, agent.py, deliver.py, notebook.py, budget.py, wake.py, schedule.py
├── engine/schedules.py        desired schedules from each repo's .factory.toml → create / update / delete
├── engine/cli.py              signal, run-now, pause, resume, status (what `factory engine …` calls)
├── engine/worker.py           the one worker: task queue `factory`, Worker Versioning
└── tests/                     time-skipping tests, activity tests, replay tests
```

- Workflow code calls activities **by string name**, always with `result_type=` (without it a Pydantic result comes
  back as a dict), and imports only `engine.models` (passed through the sandbox).
- No I/O, clock, randomness or settings in workflow code: `workflow.now()`, config arrives in the input.
- Every activity call has `start_to_close_timeout` (per attempt), `schedule_to_close_timeout` where a total budget
  applies, an explicit bounded `RetryPolicy`, a heartbeat timeout when it runs long, and a `summary`.
- Input errors raise non-retryable `ApplicationError` (`DATA_SHAPE_ERRORS`); transient errors stay retryable.
- Clients, workers and tests use `factory_runtime` (one data converter, one sandbox runner, `make_worker`).

## 3. The team workflow

### `TeamWorkflow`: one per (repo, team), alive only while there is work

Workflow ID `team/<repo>/<team>` (e.g. `team/slavabro/FamilyOffice/qa`): Temporal's entity pattern, one workflow
per key, which is what makes "one run at a time per team per repo" hold.

- **Woken by a signal,** `wake(Wake{reason, event_id, details})`, sent with **signal-with-start**: conflict policy
  `USE_EXISTING` (a running workflow gets the signal), reuse policy `ALLOW_DUPLICATE` (a finished one starts a new
  run). Reasons: `schedule`, `ci_red`, `deploy_failed`, `incident`, `merged`, `decision`, `manual`. Search attributes
  `FactoryRepo` and `FactoryTeam` are set at start.
- **Dedupe:** signals arrive at least once, so each carries an `event_id`; the workflow drops ids it has seen (the
  set is carried across continue-as-new, last 200). Wakes that arrive during a run merge into one pending wake.
- **Lifetime:** take the pending wake → run the steps below (inline, no child workflow) → if more wakes arrived,
  **continue-as-new** carrying them and the seen ids; otherwise await `all_handlers_finished` and **complete**. A
  signal that lands while the completion is being recorded is not lost: the server refuses the completion, and the
  next workflow task sees the signal. Nothing stays open while a team is idle.
- **Why no long-lived entity:** a Pinned workflow keeps its worker version across continue-as-new by default, so a
  workflow that lives for months would never move to new code and the old worker could never be retired. A workflow
  that lives for one run drains on its own.
- **Versioning:** Pinned (section 6).

### The steps of one run

| Step | Activity | Timeouts / retries | Safe to repeat because |
|------|----------|--------------------|------------------------|
| budget | `check_budget` | 30 s, 3 attempts | read only; answers `go`, `wait_until <time>` or `stop` (F-41 plan limits, spend switch) |
| wait | durable `workflow.sleep` until the window reopens | — | a timer; no worker held |
| prepare | `prepare_worktree` | 5 min, 3 attempts | path and branch come from the run (below); an existing worktree is reused, and a stale agent left in it is killed first |
| work | `run_agent_session` | start-to-close = team budget (default 90 min), schedule-to-close = budget + 30 min, heartbeat every 30 s, heartbeat timeout 3 min, 2 attempts, cancellation type `WAIT_CANCELLATION_COMPLETED`; non-retryable: `AgentRefused`, `NothingToDo` | heartbeat details carry the session id and log offset; one agent per worktree (lock + pidfile) |
| deliver | `open_or_update_pr` | 5 min, 5 attempts, backoff | finds the open PR for the run's branch before creating one (closed and merged PRs are ignored); a task is filed only after an `exists` check |
| record | `append_notebook` | 1 min, 5 attempts | keyed by `activity_idempotency_key()`; a repeat write is skipped |
| clean | `remove_worktree` | 5 min, 3 attempts | runs in `finally` (after a cancel too); a missing worktree is success |

**Names.** Branch `factory/<team>/<YYYYMMDD>-<run id first 8>`, worktree under
`~/.local/state/software-factory/worktrees/<repo>/<team>/` with the same suffix: unique even if a workflow is
terminated and started again.

**The agent session is a supervised process,** not model calls inside workflow code. The activity:
- starts the team's agent CLI through the existing runner (`sfactory/runner.py`) in **its own process group**, with a
  pidfile and a lock in the worktree, so a worker that died can't leave a second agent writing to the same branch;
- keeps the machine awake while it runs (`caffeinate -i -w <pid>` on macOS, `systemd-inhibit` on Linux): Temporal's
  timeouts are server wall-clock timers, so a laptop that sleeps mid-run would otherwise come back to timeouts that
  already fired;
- heartbeats every 30 s with progress, which is also how a cancel reaches it, and kills the process group on cancel,
  finishing only when it is gone (`WAIT_CANCELLATION_COMPLETED`), so `clean` never removes a worktree under a live
  agent;
- a timeout that spans a sleep anyway (lid closed on battery) is recorded as **interrupted**, retried, and not
  counted as a failure.

**Large output stays out of history:** logs, diffs and the notebook live on disk and in the repo; payloads carry
paths and short summaries.

**Decisions don't hold the team.** When a run needs the principal (an ASK merge, a risky change), it delivers what
it has (PR or task), files the question with options and a recommendation (the tower now, the decision queue
F-13 later), and ends. The answer comes back as a wake with reason `decision` and the choice in `details`.

### `ScheduleTick`: the bridge from a Schedule to the team

A Temporal Schedule can only start a workflow, and it appends the time to that workflow's ID, so a schedule cannot
start `team/<repo>/<team>` itself. Each schedule starts `ScheduleTick(repo, team)`, which runs one activity,
`wake_team` (30 s, 5 attempts): signal-with-start of the team with reason `schedule` and `event_id` = the tick's own
workflow ID (unique per scheduled time, so a retried tick is deduped).

## 4. Schedules and pausing

- **Declared in each repo's `.factory.toml`** (`[teams.qa] schedule = "0 2 * * *"`, UTC). `engine.schedules`
  reconciles one Temporal Schedule per (repo, team), ID `schedule/<repo>/<team>`: created, updated through
  `handle.update()`, or deleted to match the config, whenever `factory engine upgrade` runs or the config changes.
- **Overlap policy `SKIP`** on ticks (a tick takes a second; the team workflow already serialises the runs).
- **Catchup window = one interval:** a laptop that slept through 02:00 runs once when it wakes, not a backlog.
- **Jitter 5 minutes,** so teams don't start on the same second.
- **One owner for "paused": the team's Schedule.** Its pause carries a note with the reason. New schedules start
  paused until the team has run once by hand with a good result; three failed runs in a row pause it (the run's last
  step does that) and tell the tower. `wake_team` and `factory engine signal` check the schedule's pause before
  waking a team, and `factory engine run-now` is the only way past it.

## 5. Events

`factory engine signal <reason> --repo R [--team T] --event-id ID [--details JSON]` signal-with-starts the matching
team workflows. Callers: the tower (main's CI red, a failed deploy, a merged PR it watched), product CI hooks later,
and the support desk (F-24). Without `--team`, the repo's `[events]` table in `.factory.toml` names which teams
wake for which reason.

## 6. Deploying new engine code: Worker Versioning

Every workflow type (`TeamWorkflow`, `ScheduleTick`) is **Pinned**: a run finishes on the code it started with,
so changing workflow code never breaks a run in flight and needs no patching.

- **Build ID** = a hash of `engine/`, `runtime/` and the engine's lockfile. A merge that changes none of them (docs,
  skills) deploys nothing.
- **Upgrade** (`factory engine upgrade`, run by the self-update when the Build ID changes): start a worker on the
  new Build ID next to the old one, set it **Current** in Worker Deployment `factory-engine`, then stop the old
  worker once the server reports nothing pinned to it is still open. Runs are bounded by their budget, so the old
  worker drains within about two hours. This is the rainbow deployment Worker Versioning requires (rolling
  replacement isn't supported).
- **First install** sets the first version Current; without that, no workflow tasks reach a versioned worker.
- **Replay tests** in CI run recorded histories of each workflow type against the new code before it can merge.

## 7. Observability and failure

- The Temporal UI (port 8243) shows every run, filterable by `FactoryRepo` and `FactoryTeam`.
- `factory engine status` reads the schedules (paused or not, and why), the open and recent runs through the
  visibility API (`list_workflows` on the search attributes), each run's result from its memo, and failures through
  `get_run_failure`. It never queries a closed run, which would need a worker able to replay its version.
- The engine reports its own health: server reachable, worker polling, schedules matching config, ports free.

## 8. First team: upgrades (F-01 moves here)

The upgrade capability becomes the first team: team `upgrade`, its agent session running `/factory:upgrade`. Its
launchd schedule (`factory schedule`) is removed in the same change, so there is one way to schedule factory work.

## 9. Tests

- **Time-skipping** (runtime's `make_worker`, `time_skipping_env`): dedupe and merging of wakes; one run at a time;
  continue-as-new when wakes arrived mid-run and completion when none did; a signal racing completion is still
  processed; budget wait on a durable timer; cancel reaching the agent activity and `clean` running after it.
- **Activities** (`ActivityEnvironment`): worktree reuse and stale-agent kill, lock; PR lookup ignores closed PRs;
  notebook idempotency; heartbeat details on resume; interrupted vs failed classification.
- **Replay:** recorded histories of each workflow type against the current code.
- **End to end** against the real Docker server in CI: install, wake, run a fake team, see the PR step called,
  upgrade to a new Build ID and see the old worker drain.

## Sources

- Entity workflows, signal-with-start: https://docs.temporal.io/design-patterns/entity-workflow,
  https://docs.temporal.io/design-patterns/signal-with-start
- Workflow ID reuse and conflict policies: https://docs.temporal.io/workflow-execution/workflowid-runid
- Schedules (overlap, catchup, jitter, pause, UTC, workflow IDs): https://docs.temporal.io/schedule
- Long-running activities, heartbeats, cancellation, timeouts:
  https://docs.temporal.io/encyclopedia/detecting-activity-failures
- Messages (signals, updates, validators, `all_handlers_finished`): https://docs.temporal.io/develop/python/message-passing
- Worker Versioning (Pinned, rainbow deployments, server ≥ 1.29.1; Pinned stays pinned across continue-as-new):
  https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning,
  https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning/upgrade-on-continue-as-new
