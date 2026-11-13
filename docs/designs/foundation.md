# Phase 1 design: the foundation

Covers F-42 (shared runtime, Temporal core first) and F-50 (the factory engine on Temporal), in the order they are
built. F-40 (house stack) is the set of defaults this design puts in place; F-41 (AI access) runs alongside and
plugs in where marked. Rule 1 applies throughout: each piece is built the way it is meant to end up, and when a
piece moves, its old copy is removed in the same change, so there are never two ways to do one thing.

## 1. The shared runtime package (F-42)

**Where and how.** `runtime/` in this repo (D-2), installable package `factory-runtime`, import name `factory_runtime`, Python 3.11+,
dependencies `temporalio` and `pydantic` (more only when a slice needs them), tested in its own CI job. It ships
with the factory's version tag.
A product depends on
`factory-runtime @ https://github.com/slavabro/software-factory/archive/<commit>.tar.gz#subdirectory=runtime`, the same
commit as its action pin (one version for the whole factory, so tools and runtime always match), and F-01's upgrade
run moves that pin forward like any other dependency.

**No product config inside.** The package never imports a product's settings. Every entry point takes what it
needs as arguments (`TemporalSettings(url, namespace, task_queue, compress_min_bytes, warn_bytes)`), and the
product's own `common/temporal` keeps only the lines that bind its settings to these calls. That replaces
FamilyOffice's pattern where the sandbox passes `common.config` through. The product passes its extra passthrough
modules into the runner instead.

### Slice R1: Temporal core (first)

From FamilyOffice `common/temporal/`, about 450 lines:

| Moves | Becomes |
|-------|---------|
| `client.py` | `factory_runtime.temporal.client`: process-wide client from `TemporalSettings` |
| `data_converter.py` | `factory_runtime.temporal.converter`: Pydantic JSON plus compression of large payloads |
| `sandbox.py` | `factory_runtime.temporal.sandbox`: `build_workflow_runner(passthrough=…, import_policy=…)`, with the strict test policy |
| `activity_helpers.py`, `run_history.py`, `process_runs.py` | `factory_runtime.temporal.*`, same jobs |
| (new) | `factory_runtime.testing`: a time-skipping test environment built with the same runner and converter, so tests and production can't differ |

**Done when:**
- FamilyOffice's `common/temporal/` is a thin binding to the package, and nothing else in FamilyOffice changes.
- FamilyOffice's Temporal test tier passes unchanged.
- The package has its own CI with tests and a type check.

### Slice R2: the process engine

From FamilyOffice `workflows/interpreters/` (YAML processes: linear, parallel, conditional, hybrid, `for_each`,
decision, pause and approval steps) and `workflows/schedulers/` (schedules declared in YAML). Their ties to
FamilyOffice are config, decision models, YAML helpers and the LLM on/off control. Each becomes an argument or a
small interface the product implements.

**Done when:** FamilyOffice's processes and schedules run on the package, and its Temporal tier and process tests
pass.

### Later slices (with their tasks)

These use FamilyOffice's database layer, so each needs a storage interface first, and each moves with the task
that owns it:
- agent jobs (F-41);
- decisions (F-13);
- support and conversations (F-24);
- triggers (the trigger engine moves; FamilyOffice's domain providers stay).

## 2. The factory engine (F-50)

**Separate programs.** The factory and each product are separate programs, each with its own Temporal: its own
server, database, UI, workers and history. They share only code (`runtime/`, installed as a package), never a running
server. Nothing the factory does can stop, slow or fill a product's Temporal, and the reverse.

**Where it runs.** On the home machine (the F-06 setting):
- **The server** is its own Docker Compose project, `software-factory`: a Temporal server, its own Postgres (a named
  volume, so history survives restarts) and the Temporal UI, on its own ports (7243 for the server, 8243 for the UI;
  settings). It is a Docker project, not `temporal server start-dev`, because a product's dev scripts may stop every
  `start-dev` process on the machine (FamilyOffice's `start_all.sh` does), and a dev server keeps no history.
- **The worker** runs on the host, not in a container, because its activities drive the laptop's own tools: git
  worktrees, agent sessions, `gh`. It is kept up by launchd (or a systemd user service on Linux), the same way
  `factory schedule` installs things today.

**Code.** `engine/` in this repo (D-5): its own `pyproject.toml` depending on `runtime/` and `temporalio`,
and its own CI job. `factory engine install` creates its virtualenv under `~/.local/share/software-factory/`, then
installs the server and worker services. `factory engine status` reports server, worker, schedules and recent runs.
The standard-library CLI only starts and asks; it never imports the engine.

**The first workflow: `TeamRun(team, repo)`**

| Step (activity) | Does |
|-----------------|------|
| prepare | a fresh worktree of the repo |
| work | the team's agent session, through the existing runner (`factory run <capability>`); the AI access policy (F-41) picks subscription or API |
| deliver | opens a PR through the normal gate, or files a task |
| record | appends to the team's notebook: what it tried, what was rejected |

The engine works like this:
- **Timing:** schedules are Temporal Schedules, made from `.factory.toml` (`[teams.<name>] schedule = "…"`).
- **Events:** `factory engine signal <event> --repo <r>` (CI red on main, deploy failed, incident, PR merged)
  starts or wakes a team. The tower and CI call it.
- **One run at a time:** each team has one run per repo, because the workflow ID is `team:<repo>:<team>` and a
  duplicate is refused while one is running.
- **Waiting for the principal:** a step that needs their OK pauses on an update until they answer. Later this is
  the decision queue (F-13).

**Migration.** The upgrade run (F-01) is the first team. It moves from its launchd schedule to a Temporal Schedule,
and `factory schedule` (the launchd path) is removed in the same change.

## Build order

1. R1, the Temporal core: `runtime/` with its CI job and a release tag; FamilyOffice binds to it.
2. F-50, the engine: server, worker, `TeamRun`, and the upgrade run moved onto it.
3. R2, the process engine.
4. F-23, the standing teams, QA and DevOps first.

F-41 (AI access) runs in parallel from step 2.

## To settle while building

- **Access.** Settled 2026-11-13: the factory repo is public, so CI, laptops and Docker builds install the runtime
  with no token.
- **Ports for the engine's server.** Pick free defaults that don't clash with a product's dev server (FamilyOffice
  uses 7233 and 8233), stored in the engine's config.
