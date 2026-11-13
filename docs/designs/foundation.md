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

The factory's own always-on program: its own Temporal (server, database, UI, workers), separate from every
product's; they share only code. Each standing team is a workflow per (repo, team), woken by schedules and events and
alive only while it has work; new code deploys with Worker Versioning. Full design: [engine.md](engine.md).

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
