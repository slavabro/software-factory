# F-26 design: the SDLC designer

The principal is the architect of how work flows. On the Factory Desk they design each project's processes: the
per-task pipeline (VISION's Intake → Shape → … → Deploy) and every workflow that runs on a schedule or a trigger.
They place agents where they are needed, pick each agent's model, put human checkpoints where they want to decide,
and see where each task is right now. The factory ships the starting processes and the agents. Each project,
software-factory included (VISION "The factory builds itself", F-04), runs its own copy of them. Mockup
(interactive, sample data marked): https://claude.ai/artifact/Jsz6U7aePHpkPpskVQtuiw

VISION's "How work flows" table owns what each stage is for. This design covers how processes are described,
designed, stored, shown and run.

Decided by the principal on 2026-11-22:
- **D-6:** the SDLC is designed and changed live on the Desk. Facts that change with the code stay in the repo's
  `.factory.toml`.
- **D-7:** a human checkpoint may be set to "until trusted". The factory then asks once before it stops asking.
- **D-8:** a miss sends a trusted checkpoint back to asking at once.
- **D-10:** standing teams become processes with a schedule or event trigger, designed on the Desk.

## What the principal does on the Desk

| View | Shows | Actions |
|------|-------|---------|
| **Processes** (all projects) | per project: its processes, each with its trigger (on a new task, a schedule in words, an event), the version running, live runs, how many items wait on the principal | open a process; create one from a template or blank; pause or resume a process |
| **Designer** (one process) | the process as stages, left to right. Each stage holds its steps in order: agents (role, skill, model, budget), checks (commands, CI), commands (deploy, notify), human checkpoints (what is shown, when it asks), and parallel groups (for example the review lenses). Live tasks sit on the step they are in | add, move or remove a step; set any step's settings in the inspector; add or remove a stage; change the trigger; test the process on a sample task (dry run); publish |
| **Inspector** (one step) | every setting with its value and its source: *template*, *project*, or *repo* (the `.factory.toml` fact it uses). For a checkpoint: its record of outcomes | edit; reset to the template value; "Ask me again" on a trusted checkpoint |
| **Versions** | each published version of a process, with what changed, who published it, when, and which runs still use it | compare two versions; publish a draft; roll back (publishes an old version as the newest) |
| **Library** | the agents (factory departments and the repo's own skills, with default model and tools), step kinds, and the template processes | create a process from a template; see which projects differ from a template and how |
| **Needs you** (existing area) | adds checkpoints waiting on the principal, "Stop asking at *checkpoint*?" proposals (D-7), and checkpoints sent back to asking (D-8) | answer; acknowledge |

Semantics stated on the page:

- **Draft, then publish.** Edits go to a draft. Publishing makes a new immutable version. Runs that already started
  finish on the version they started with, and new runs use the newest one. A draft can be dry-run on a sample task
  first: the run shows each step's inputs, which agent and model would run, and where it would stop for the
  principal. Nothing runs and nothing is spent.
- **Loosening confirms on publish.** The publish page lists each change that removes a human checkpoint, a check or
  a required stage, sets a checkpoint to "never", or widens an agent's tools, and the principal confirms each one.
- **Floors cannot be removed on the Desk** (section 4). The designer shows them locked, with the reason.
- desk.md listed editing team schedules and prompts as out of scope. D-6 and D-10 reverse that, and desk.md is
  updated in the same change.

## 1. The model: processes, triggers, steps

**A process** is a named, versioned list of stages, each stage a list of steps, plus one or more triggers. It
belongs to one project.

| Trigger | Starts a run | Example |
|---------|--------------|---------|
| `task` | when a task enters the factory (Intake) | the feature pipeline |
| `schedule` | on a cron in the engine's time zone | nightly QA, Monday upgrades |
| `event` | on `ci_red`, `deploy_failed`, `incident`, `merged`, `pr_opened`, `decision` | fix main's CI when it goes red |
| `manual` | from the Desk or `factory process run` | a one-off audit |

**Step kinds.** These are the building blocks; each has a fixed set of settings:

| Kind | Does | Settings |
|------|------|----------|
| `agent` | an agent session in the run's worktree | `agent` (from the library), `instructions` (what this step asks of it, with inputs such as `{{task.design}}`), `model` (an F-41 tier alias), `budget_minutes`, `tools` (a profile: `read`, `edit`, `edit+pr`), `fresh_context` (reviewers never see the author's reasoning) |
| `check` | a machine gate; the run continues only when it passes, otherwise it goes back to a named step, at most `retries` times | `run` (the repo's `[verify]` commands, the CI check, the task system's check, the review block), `on_fail` (`back_to: <step>`, `retries`) |
| `human` | a checkpoint: the principal sees the output with options and a recommendation, and the run waits | `shows` (design, mockup, PR, deploy plan), `asks` (`always`, `until-trusted`, `when <rule>`, `never`), `options`, `timeout` and what a timeout does |
| `command` | a repo command | `run` (the repo's `[deploy]` commands, a notification), `on_fail` |
| `parallel` | steps at once, all of which must finish | its steps (for example one reviewer per lens) |
| `process` | runs another process and waits for it | `process` |

Any step may carry `when` (for example `task.ui`, `task.size >= M`, `paths ~ "**/migrations/**"`), which skips the
step when the rule is false.

**Stages are lanes, not machinery.** A stage groups steps under one of VISION's stage names (or a name the principal
gives one). The view uses stages, and the stage log records them (section 5). The run itself only follows the steps.

**Agents** come from the library:
- the factory's departments (product agent, UX designer, architect, implementer, reviewers, QA, lander, release
  agent, support agent, the standing teams), each a factory skill with a default model and tool profile;
- the repo's own skills: its domain reviewers, or a skill that stands in for a department, such as FamilyOffice's
  `/design-task`. These are read from the repo, never copied.

The principal adds an agent to a process by placing a step and writing its `instructions`. A new reusable agent
(a new skill) is code: it lands by PR with its trigger evals (F-14), and appears in the library once merged (D-12).

## 2. Where processes live: templates, project processes, repo facts

| Layer | Lives in | Changed by | Holds |
|-------|----------|-----------|-------|
| **Templates** | `plugins/factory/processes/*.yaml` in software-factory | PR to the factory | the starting processes ("Feature delivery", "Upgrade", "Nightly QA", "Main CI red", "Incident") and the agent library |
| **Project processes** | the factory's process store on the home machine (below) | the Desk, live (D-6) | each project's processes, created from a template or blank, and every published version |
| **Repo facts** | the repo's `.factory.toml` | PR to the repo | commands (verify, deploy, health, rollback), the CI check that means "passed", paths that make Land ask, worktree setup, agent tool lists, the repo's skills that may stand in for a department |

- **Created from a template, then owned by the project.** A project process records the template version it came
  from. When the template changes, the Desk shows the difference under "Template updated" and offers to apply each
  change to the project's draft. Nothing changes a project's process without the principal publishing it. So no
  layer silently overrides another, and the value shown is always the value that runs.
- **Repo facts are referenced, not copied.** A `check` step that runs `verify` reads `[verify] commands` when it
  runs. The inspector shows the commands and the file they come from, and they change by PR with the code.
- **The store:** `~/.config/software-factory/processes/<repo>/<process>/v<N>.yaml` (immutable, one per published
  version) plus `draft.yaml`. One writer (in `engine/ops.py`, atomic and locked, as for `engine.toml`) appends each
  publish to `state/actions.jsonl`. A run records `<process>@v<N>`. A project without a store entry runs nothing
  until a process is created for it, which is what "add a repo" offers to do from the templates.

## 3. The format: the runtime's process engine, with factory step kinds

The brief asked whether the YAML process engine (process-engine.md) should represent the SDLC. Now that the
principal composes processes from steps, triggers and schedules, it should. That is what the engine exists for, and
reusing it keeps one process language for the factory and for products:

- **One format, one validator.** `factory_runtime.process` already loads, validates (on load and on run start),
  resolves placeholders and conditions, and turns schedules into Temporal Schedules. The factory adds its step
  kinds the way products add theirs (process-engine.md section 3: step types plug in): `agent`, `check`, `human`,
  `command`, as `FACTORY_STEP_TYPES` in `engine/`. `parallel` and `process` map to the engine's own parallel run
  and `workflow` step.
- **Stages** are a list of step-id groups in the file's metadata. The interpreters ignore them, and the view and
  the stage log use them.
- **The factory's process workflow classes** are declared in `engine/` on `ProcessWorkflowBase`, as
  process-engine.md section 3 expects ("they come when the factory engine needs processes (F-12)"). They are Pinned
  like the team workflow. The Desk validates a draft with the same validator, so a draft that would fail to start
  is refused at publish, not at run time.

```yaml
# a project process, version 7 (sample)
name: feature-delivery
template: feature-delivery@3
interpreter: linear
triggers: [{task: {}}]
stages:
  - {name: Shape, steps: [shape, approve-split]}
  - {name: Technical design, steps: [design, lenses, approve-design]}
  - {name: Build, steps: [build, verify]}
  - {name: Land, steps: [land, merge-ok]}
steps:
  - {id: shape, type: agent, agent: product, model: opus, instructions: "Shape {{task.id}}: problem, users, success measure; split it."}
  - {id: approve-split, type: human, shows: split, asks: until-trusted}
  - {id: design, type: agent, agent: repo:design-task, model: opus, budget_minutes: 60}
  - {id: lenses, type: parallel, steps: [
      {id: lens-data, type: agent, agent: repo:data-access-layer, model: sonnet, fresh_context: true},
      {id: lens-workflows, type: agent, agent: repo:temporal-workflows, model: sonnet, fresh_context: true}]}
  - {id: approve-design, type: human, shows: design, asks: "when task.size >= L"}
  - {id: build, type: agent, agent: implementer, model: sonnet, tools: edit}
  - {id: verify, type: check, run: verify, on_fail: {back_to: build, retries: 2}}
  - {id: land, type: agent, agent: lander, model: sonnet, tools: edit+pr}
  - {id: merge-ok, type: human, shows: pr, asks: until-trusted}   # floors still ask (section 4)
```

**Long waits don't hold a build.** A task can wait days at a human checkpoint. engine.md does not allow a Pinned
workflow to stay open that long, because it would hold its build. So a `human` step records the question (the
decision queue, F-13; Needs you until then), saves the run's position and results, and ends the run. The answer
starts a continuation from the next step: on the current build, with the same process version. F-12 designs this
in detail. This design only requires it.

## 4. Human checkpoints, trust and floors

`asks` on a `human` step:

| Value | Behaviour |
|-------|-----------|
| `always` | always waits for the principal |
| `until-trusted` | waits until the factory has earned trust (below), then proceeds alone |
| `when <rule>` | waits only when the rule holds (size, paths, labels, cost) |
| `never` | the checkpoint is recorded but never waits (confirmed on publish) |

**The record** is kept per (project, process, checkpoint). Its outcomes: `approved` (as is), `changed` (approved
after a change, or sent back), `rejected`; and afterwards `reverted` (a PR it let through was reverted), `rolled-back`
(a deploy) and `corrected` (the principal corrected output that went through unasked; F-16's signal).

- **Earning trust (D-7).** After `trust_after` approvals as is in a row (default 10, a project setting), the
  factory files one question: "Stop asking at *merge-ok* in FamilyOffice? The last 10 were approved as is." A yes
  records `trusted`, and "not yet" waits for the next streak.
- **Losing it (D-8).** Any miss at a trusted checkpoint records `untrusted` at once, with the reason. Needs you
  shows it, and the streak starts again. "Ask me again" on the Desk does the same, with the reason "by hand".
- **The level is computed from the record**, never stored, so it cannot drift.

**Floors** are factory-owned rules that make a run wait for the principal whatever the process says. They exist so
the process cannot loosen the gate through itself, and the Desk cannot remove them (D-11):
- a PR that changes `.factory.toml` or CI workflows;
- a PR touching the repo's `[land] ask_paths` (FamilyOffice: migrations, infra, deployer; software-factory: hooks,
  guards and the gate itself, D-3);
- the first production deploy of a project;
- a process change that the factory itself proposes (from Learn or a template update) waits for the principal's
  publish.

The tower's gate (`fleet.py gate`) enforces the Land floors and reads `merge-ok`'s trust level in place of
`TOWER_AUTOMERGE`. Its FamilyOffice-specific constants (`ASK_PATHS`, `DEPLOY_DIR`, the `CI OK` check name) move to
FamilyOffice's `.factory.toml` (F-02).

## 5. Where work is: the stage log

One append-only log per project, `~/.local/state/software-factory/sdlc/<repo>.jsonl`, written by one writer (a
locked append):
`{"at", "task", "process": "feature-delivery@7", "stage", "step", "event", "by", "details"}`. Events are `entered`,
`asked`, `answered` (with the outcome), `passed`, `skipped`, `failed`, `miss`, `trusted` and `untrusted`. They are
the checkpoint record of section 4 too.

- **One owner each.** The task record (F-03) owns what a task is and whether it is open or done. The stage log owns
  where an open task is and every checkpoint outcome. The engine owns whether a run is going now.
- **Hand mode, until F-12 runs the processes.** The steps are still the project's skills, run by the principal or
  agents in Orca. Each skill reads its step settings (model, instructions, whether its checkpoint asks) with
  `factory process step --repo . feature-delivery design --json`, and records its events with `factory stage …`.
  The designer is the owner from day one, and F-12 only takes over the running of it.
- A failed write never fails the work. It says so on stderr, and the view shows that task's position as unknown.

## 6. Specs and acceptance scenarios (an option, not decided: D-9)

If adopted, as an OpenSpec-compatible living-spec format built into the task system (not the OpenSpec tool), specs
are a few steps the principal can place like any other:

| Where | Step |
|-------|------|
| Shape | an `agent` step writes the task's spec delta `docs/tasks/active/<id>/spec.md` (`## ADDED / MODIFIED / REMOVED Requirements`, each `#### Scenario:` WHEN / THEN); these are the acceptance criteria |
| Technical design | the design names the code and the test that proves each scenario |
| Build | a `check` step "one test per scenario" |
| QA (F-18) | an `agent` step runs every scenario end to end; a `check` step "every scenario checked" |
| Land | `factory task complete` merges the delta into `docs/specs/<capability>/spec.md` |
| Upkeep | a scheduled process reports drift between living specs and their tests |

The template "Feature delivery with specs" would carry these steps. Nothing else in this design depends on specs.

## 7. The factory as a project (F-04)

software-factory gets the same three things as any product: a `.factory.toml` (its repo facts), processes in the
store (created from the templates), and a stage log.

```toml
# software-factory's .factory.toml (repo facts; build order step 5)
[repo]
name = "software-factory"

[verify]
commands = [
  "python3 -m unittest discover -s tests",
  "PYTHONPATH=plugins/factory python3 -m skillcheck . --strict",
  "claude plugin validate --strict . && claude plugin validate --strict plugins/factory",
  ".venv-runtime/bin/python -m unittest discover -s runtime/tests -t runtime",
  "cd engine && ../.venv-engine/bin/python -m mypy engine && ../.venv-engine/bin/python -m unittest discover -s tests -t .",
]

[worktree]
setup = ["python3 -m venv .venv-runtime && .venv-runtime/bin/pip install -q ./runtime",
         "python3 -m venv .venv-engine && .venv-engine/bin/pip install -q -r engine/requirements.lock -e runtime -e engine"]

[land]
check = "CI OK"               # an aggregate job added to ci.yml, as products have
ask_paths = ["plugins/factory/hooks/**", "plugins/factory/sfactory/guard.py", "home/skills/fleet/scripts/fleet.py",
             "action.yml", ".github/workflows/**"]   # D-3: hooks, guards, the gate itself

[deploy]                      # D-4: merged is released; the install copy follows main
commands = ["factory self-update", "factory engine upgrade"]
```

Its processes differ from a product's only in their steps:

| Process | software-factory |
|---------|------------------|
| Feature delivery | UX steps only `when task.ui` (Desk pages, mockups); no production checkpoint, because a merge is the release (D-4); Deploy is `command: deploy`, then a `check` running `factory engine status`, with rollback to the previous build on failure |
| Upgrade | the template, on its own schedule |
| Skills review | a scheduled process running `/factory:skills-review` |
| Incident | none: it has no users beyond the principal, and its health is on the Desk |

**The factory must be fixable by hand.** Its Land and Deploy never depend only on the build being deployed:
`factory engine upgrade` and the tower's `land` stay runnable from a terminal when the engine or the Desk is down.
Its tasks show by stage-log ID until F-04 moves this BACKLOG onto the task system.

## How it is built

- **Templates and the library** (`plugins/factory/processes/`): the template processes in the runtime format, and
  `agents.toml` (each department: skill, default model, tool profile).
- **Factory step kinds** (`engine/engine/steps/`): `agent` (the existing supervised session, `run_agent_session`),
  `check`, `human`, `command`, registered on the engine's process workflow classes. Teams move onto them: a
  `TeamWorkflow` run becomes a process run with a one-step process, and `ScheduleTick` starts the process
  (D-10).
- **The store and its writer** (`engine/ops.py`): `processes(repo)`, `draft_set`, `publish` (validate, check
  floors, list what loosens, write `v<N+1>`, reconcile the schedule and event triggers), `rollback`,
  `dry_run(process, sample_task)`. The CLI is `factory process list|show|step|run|publish`.
- **Repo facts** in `config.py`: `[land] check / ask_paths`, `[deploy]`, the agent tool profiles. `[teams.*]` and
  `[upgrade] schedule` are removed once the store holds every repo's teams (`factory process import` writes them as
  processes, then the repo's PR deletes the keys).
- **The stage log** (`sfactory/stagelog.py`, standard library) and the trust level computed from it.
- **The Desk:** `/processes`, `/processes/<repo>/<process>` (designer and inspector, server-rendered forms, one step
  per form), `/versions`, `/library`. The designer works without script: add, move and remove are forms that
  post to `ops` and redirect. Drag and drop, if ever added, is extra on top, under the CSP's `script-src 'self'`.
- **Status words** (`sfactory/status.py`): `asks` (Asks you, ?), `trusted` (Acts alone, ●), `earning` (Earning *n*
  of *m*, ◐), `waiting` (Waiting on you, ‖), `blocked` (Blocked, ■), `draft` (Draft, ✎). Each is a word and a
  shape, never colour alone.

## Build order

0. **Foundations:** the factory step kinds and process workflow classes on the engine; the templates and library;
   the store, its writer and validator; the stage log; the repo-fact keys; the new status words.
1. **Teams become processes (D-10):** `factory process import`; the upgrade team runs as the "Upgrade" process;
   `[teams.*]` and `[upgrade] schedule` are removed from config and from FamilyOffice's `.factory.toml`.
2. **The gate on it (F-02):** `fleet.py gate` reads the Land checkpoint's trust and the floors and writes the stage
   log; FamilyOffice's constants move to its `.factory.toml`.
3. **Desk, read-only:** processes, designer, inspector, versions, library, Needs you additions.
4. **Desk, designing:** drafts, step forms, dry run, publish with the loosening list, rollback, trust answers.
5. **The factory as a project (F-04):** the aggregate `CI OK` job, its `.factory.toml`, its processes from the
   templates.
6. **Hand mode reads the designer:** FamilyOffice's pipeline skills read their step settings and write the stage
   log (its PR). F-12 then runs "Feature delivery" itself, on the same processes.

## Tests

- **Format:** every template validates; a draft with an unknown agent, a missing repo fact or a floor removed is
  refused at publish; a published version never changes; a run records its version.
- **Steps:** `check` goes back to its named step at most `retries` times; `human` ends the run and the answer
  continues it from the next step with the same version; `when` skips a step and records `skipped`.
- **Trust:** a streak reaching `trust_after` proposes once; "not yet" waits for the next streak; each miss
  untrusts at once; a floor waits even when its checkpoint is trusted or set to `never`.
- **Store:** atomic publish under the lock; triggers reconciled to the published version; the action log has every
  publish.
- **Desk:** each state is a word and a shape; publish lists every loosening change; floors have no remove control;
  a template update is offered and never applied by itself.
- **VISION:** the "Feature delivery" template's stages match the "How work flows" table.

## Out of scope

- F-12's continuation mechanics for human checkpoints (required in section 3, designed in F-12).
- Writing new skills on the Desk: they are code with evals, landed by PR (D-12).
- Editing repo facts on the Desk: they change with the code (D-6).
- Budgets and cost per step: F-33 and F-31 (the `budget_minutes` key is the one this design uses).
- The decision queue (F-13); until it exists, checkpoints and trust questions show under Needs you.
- Stage events from other laptops (F-06).
