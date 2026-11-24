# F-26 design: the SDLC command center

The Factory Desk is where the principal designs how each project is developed. The factory offers **building
blocks**: agents, the tools they may use, checks, commands, human steps, logic, and events. The principal composes
them into **processes**. A process starts on an event, on a schedule or on a click, and processes start each other
through events. The principal places human steps and **gates** where they want to decide, and picks each agent's
model and autonomy.

The day-to-day work is not on the Desk (D-13): a task in flight, a PR waiting, an agent's question. It happens in
each developer's own tool (Claude Code, Cursor, Orca). software-factory is a project like any other (F-04).
Mockup (interactive, sample data marked): https://claude.ai/artifact/Jsz6U7aePHpkPpskVQtuiw

VISION's "How work flows" table owns what each stage is for. This design covers:
- the blocks and the card that explains each one;
- processes, their triggers and the events between them;
- gates and autonomy;
- where processes run and where they are stored;
- the Desk's views.

Decided by the principal:
- **D-6** (2026-11-22): the SDLC is designed on the Desk.
- **D-7** (2026-11-22): a checkpoint set to "until trusted" asks once before it stops asking.
- **D-8** (2026-11-22): a miss sends a trusted checkpoint back to asking at once.
- **D-10** (2026-11-22): standing teams become processes with schedule or event triggers.
- **D-13** (2026-11-24): the Desk is configuration and the factory's health, never the day-to-day work.

## What similar systems do, and what this design borrows

Researched 2026-11-24 (sources in the PR):
- agentic SDLC tools: GitHub Agentic Workflows, the Copilot coding agent, Factory Droids, Devin, Cursor
  Automations and Bugbot, Codex, Jules, Kiro, Claude Code, GitLab Duo flows, Azure SRE Agent;
- workflow builders and portals: n8n, Zapier, GitHub Actions, Azure Pipelines, Backstage, Port, Cortex, Inngest,
  Trigger.dev, Temporal, Dify, OpenAI Agent Builder, LangGraph.

| Pattern (where it comes from) | In this design |
|-------------------------------|----------------|
| An automation is trigger + conditions + agent + outputs + limits (Cursor, Devin, gh-aw) | a process: triggers, `when` rules, steps, safe outputs, budget and concurrency (sections 2, 3) |
| A searchable block catalog; a node's settings in a side panel (n8n, Agent Builder, Dify) | Library with a card per block; Designer with an inspector (section 1, Desk views) |
| An agent is prompt + model + effort + tools, with tools plugged into it (Claude Code subagents, Factory droids, n8n) | the `agent` block, with `tool` blocks attached (section 1) |
| Triggers: forge events, schedules, manual with typed inputs, other workflows' events (GitHub Actions, Inngest) | `event`, `schedule` and `manual` triggers; one event bus; `wait-for-event` (section 3) |
| One event starts every process that subscribes to it; a run can wait for an event (Inngest, Trigger.dev) | processes are loosely coupled through named events, with a chain limit against loops (section 3) |
| The agent proposes and a separate writer applies typed, capped outputs (gh-aw safe outputs) | an agent's `outputs` list with `max`; the writer holds the permissions (section 2) |
| An autonomy ladder with a cap set above the agent (Factory droids, Azure SRE run modes) | `autonomy` per step, capped per project (section 4) |
| Gates attached to resources, inherited by every pipeline that touches them (Azure checks, GitHub environments) | gates on "merge to the default branch" and "production deploy" (section 4) |
| Each approval outcome is its own edge, including timeout (Dify, LangGraph) | a human step has `approved`, `changes`, `rejected` and `timeout` exits (section 2) |
| Draft, then publish an immutable version; production runs the published one (n8n, Zapier, Agent Builder) | draft on the Desk; publish opens a PR to the repo (section 6) |
| Config read from the base branch, so a PR can't loosen its own rules (Bugbot, gh-aw) | processes and gates are read from the default branch (section 6) |
| A capability × service heat map (Cortex Bird's Eye, Soundcheck) | Coverage (Desk views) |
| Check severities off, warning, error (CodeRabbit) | a check's `mode` (section 1) |

## The model in one table

| Concept | What it is | Owned by |
|---------|-----------|----------|
| **Block** | one kind of thing a step can do: an agent, a tool, a check, a command, a human step, logic, an event | the factory (built in, or a factory skill) or the project (its own skills, commands and checks) |
| **Process** | triggers plus steps, grouped into stages, with exits between steps | the project; designed on the Desk, published to the repo |
| **Trigger** | what starts a process: an event, a schedule, a click | the process |
| **Event** | something that happened, with a name and fields: from GitHub, from a process, from the clock | the event catalog (factory) plus each project's own events |
| **Gate** | a rule on a resource (merging, deploying, factory config) that every process touching it must pass | the project, with floors from the factory |
| **Executor** | where a run executes: a developer's session, the engine, or CI | each process says where it can run |

## 1. Blocks and their cards

| Kind | Does | Main settings |
|------|------|---------------|
| `agent` | an AI agent session working on the run's worktree | `skill` (factory or repo), `instructions` (with inputs such as `{{task.design}}`), `model`, `effort`, `budget_minutes`, `autonomy`, `tools` (attached tool blocks), `fresh_context`, `outputs` |
| `tool` | something an agent may use, attached to an agent block, never a step of its own: GitHub (read, comment, open PR), the repo's shell commands, the browser (QA), an MCP server, Temporal's CLI | its permission level: read, write, or act on the world |
| `check` | a machine gate: tests, a linter, skillcheck, the task check, the review block | `run`, `mode` (`off`, `warn`, `error`), `on_fail` (`back_to: <step>`, `retries`) |
| `command` | a repo command (deploy, health, rollback, notify) | `run` (a `.factory.toml` command), `on_fail` |
| `human` | a person decides: approve a split, a design, a PR, a deploy | `asks` (`always`, `until-trusted`, `when <rule>`, `never`), `shows`, `options`, `timeout`, and its exits |
| `logic` | `when` on any step; `parallel` (all must finish); `for_each` (over tasks or packages); `call` (run another process and wait) | its rule or its branches |
| `wait-for-event` | the run waits until a matching event arrives or a timeout passes (for example a deploy's health, a PR merged) | `event`, `match`, `timeout` |
| `emit` | publishes a named event other processes may start on | `event`, the fields it carries |

**Every block has a card.** The Library shows it, and the Designer shows it when a step is selected:

| Section | Holds |
|---------|-------|
| What it does | one paragraph in plain words, and when to use it |
| Source and status | `factory:<name>` at a factory version, or `repo:<name>` from the project. Status is *available* or *planned* (with its F-ID), so the library is also the honest list of what isn't built |
| Inputs | typed fields it reads (`task`, `diff`, `design`, `packages`) and where they come from |
| Outputs | what it may produce: a typed list (`pr`, `comment`, `task`, `report`, `files`), each with its cap |
| Events | what it emits (`design.ready`, `upgrade.risky_found`) |
| Settings | each setting with its default and allowed values. Agents: model, effort, budget, autonomy, tools |
| Runs in | the executors it supports (session, engine, CI) |
| Needs | the repo facts it reads (for example `[deploy] commands`), named when missing |
| Track record | runs, outcomes, average minutes and cost (F-33), checkpoint approvals, trigger-eval pass rate (F-14) |
| Used by | the processes and projects that use it |

**Where block definitions live.**
- **Factory blocks** are declared once in `plugins/factory/blocks/<name>.toml`, next to the skill or code that
  implements them. A skillcheck rule keeps each available block in step with its code.
- **A project's own blocks** are its skills, commands and checks. The Library reads them from the repo
  (`.claude/skills/`, `.factory.toml`), never copies them, and shows their cards with what the repo provides.
- **A new reusable agent is a skill.** It lands by PR with its trigger evals (F-14) and appears in the Library once
  merged (D-12). On the Desk, any agent step can be given its own instructions, model and tools without a new skill.

## 2. Processes

A process is:
- one or more triggers;
- `inputs` (typed; a click asks for them);
- steps, in stages;
- exits between the steps;
- `limits` (how many runs at once, a total budget);
- `runs_in` (the executors it may use).

**Stages are lanes.** A stage groups steps under one of VISION's stage names, or a name the principal gives one.
The Designer draws stages left to right, with their steps inside them.

**Exits.** Every step has a `next`. A failing check has `back_to`. A human step has one exit per outcome:
`approved`, `changes` (back to a named step, with the person's note as input), `rejected` (ends the run, or goes to a
named step) and `timeout`. The Designer draws each exit as its own labelled edge.

**Safe outputs.** An agent never holds write permissions itself. It returns the outputs on its `outputs` list (a PR,
a comment, a task, a label), and the run's writer applies them within their caps. The cap is `max` per run; for
example, Upgrade has `pr: {max: 3}`. Anything else the agent asks for is refused and logged. This is the same split
as today's unattended runs: allowlist, deny list, and a guard hook.

Sample processes (the factory's templates):

| Process | Triggers | Steps (by stage) |
|---------|----------|------------------|
| Feature delivery | `task.ready`; click on a task | Shape (product agent; human: approve split) → Design (architect agent; lenses in parallel; human when L) → Build (implementer; check verify, back to build ×2) → Review (lenses; check review block) → Land (lander agent, output `pr`; gate: merge) → emits `feature.landed` |
| Bug fix | event `issue.labeled(bug)`; click | Reproduce (agent) → Fix (agent; check verify) → Land |
| Upgrade | schedule Mondays 06:00; click | Scan (agent, `for_each` package) → Upgrade (agent, outputs `pr ≤ 3`, `task`) → emits `upgrade.risky_found` per risky package |
| Main CI red | event `ci.failed(branch=main)` | Diagnose (agent, model haiku) → Fix (agent; check verify) → Land |
| Release | event `pr.merged`; click | Deploy staging (command) → wait-for-event `health.ok` (10 min) → human: production → Deploy (command) → emits `deploy.done` or `deploy.failed` |
| Incident | event `deploy.failed`, `incident.opened` | Runbook (command) → Diagnose (agent, read-only) → Fix (agent, output `pr`) or human: escalate |
| Nightly QA | schedule every day 02:00 | QA sweep (agent with browser tool, outputs `task`) |

## 3. Triggers and events

| Trigger | Starts a run | Example |
|---------|--------------|---------|
| `event` | when a matching event arrives, optionally filtered (`ci.failed` where `branch == "main"`) | Main CI red |
| `schedule` | on a cadence in the engine's time zone, shown in words ("Mondays 06:00") | Upgrade |
| `manual` | on a click on the Desk, `factory process run`, or `/factory:run` in a session; asks for the process's `inputs` | Feature delivery on a chosen task; a one-off audit |

**The event catalog.** Every event has a name, a source and its fields. The factory defines these; a project may add
its own through `emit`:

| Source | Events |
|--------|--------|
| GitHub | `pr.opened`, `pr.merged`, `pr.review_requested`, `ci.failed`, `ci.passed`, `issue.opened`, `issue.labeled`, `comment.command` (`/factory <process>`) |
| Task system (F-03) | `task.created`, `task.ready`, `task.done` |
| Factory processes | `feature.landed`, `design.ready`, `upgrade.risky_found`, `deploy.done`, `deploy.failed`, `health.ok`, `process.failed` |
| Operations | `incident.opened` (the support desk, F-24) |

**How events travel.**
- **One bus, in the engine.** An event starts every process that subscribes to it (fan-out), and wakes every run
  waiting for it.
- **GitHub and task events are read, not pushed.** The home machine takes no inbound connections, so the engine
  reads GitHub and each repo's task records on the default branch every two minutes. A run in a developer's session
  needs no channel to the engine: what it does that matters (a PR opened, a task done) shows up in GitHub and the
  task records.
- **An event names its cause** (the run or GitHub item that produced it). A chain of events starting processes stops
  at depth 3, as GitHub limits `workflow_run`, so two processes can't start each other forever. The Desk's Events
  view warns about a cycle when the chain is drawn.

## 4. Gates, autonomy and trust

**Gates sit on resources, not in processes.** A project defines a gate once on each resource it has, and every step
that touches the resource passes it, whichever process the step is in:

| Resource | Default gate |
|----------|--------------|
| Merge to the default branch | `factory / gate` and the repo's `[land] check` pass; then the merge checkpoint (`asks: until-trusted`) |
| Production deploy | the deploy checkpoint (`asks: always`); the project's first production deploy is a floor |
| Factory configuration | a PR changing `.factory.toml`, `.factory/` or CI workflows always asks (a floor; a Desk publish is the principal's approval) |
| Ask paths | a PR touching `[land] ask_paths` always asks (a floor; software-factory: hooks, guards and the gate, D-3) |

**Autonomy** is set per agent step: `read`, then `propose` (outputs only), then `edit` (the worktree), then `pr`,
then `merge`. A project-wide cap limits every step, so a process can't give an agent more than the project allows.
Autonomy and gates are separate: an agent with `pr` autonomy still has its PR stopped at the merge gate.

**Checkpoints and trust.** These apply to human steps and to the checkpoint in each gate.
- `asks` takes one of four values: `always`, `until-trusted`, `when <rule>`, or `never`.
- **The record comes from where the work happens.** A session checkpoint writes `approved`, `changes` or `rejected`
  into the task's record. Merges, reverts and changes after review come from GitHub. Rollbacks come from the deploy
  log.
- **Trusting (D-7).** After `trust_after` approvals as is in a row (default 10), the Desk proposes trusting the
  checkpoint. Saying yes publishes `trusted_since` for it.
- **A miss (D-8).** A checkpoint acts alone only while no miss has happened since `trusted_since`. This is computed
  when the checkpoint is reached, so a miss sends it back to asking at once, with no write.

**Floors** are the gates marked as floors above. The Desk shows them locked and has no control that removes them
(D-11). The tower's gate (`fleet.py gate`) enforces the merge gate. Its FamilyOffice constants (`ASK_PATHS`,
`DEPLOY_DIR`, `CI OK`) move to FamilyOffice's `.factory.toml` (F-02).

## 5. Where a process runs

| Executor | Runs | Human steps reach the person as | Started by |
|----------|------|-------------------------------|------------|
| **Session** | in a developer's Claude Code, Cursor or Orca terminal. `/factory:run <process> [task]` (or `factory process run --here` from any tool) walks the steps: agents become subagents on the step's model and tools, and checks and commands run locally | a question in that session, with options and a recommendation | the developer |
| **Engine** | unattended on the home machine, in a fresh worktree, as today's team runs | the outputs: a PR at the merge gate, or a task in the repo for a question it can't answer from the code (the decision queue later, F-13) | events, schedules, clicks |
| **CI** | in the factory's reusable GitHub workflow, at the version the project pins; only `check` and `command` steps | a failing required check (`mode: error`) fails `factory / gate` | GitHub events (`pr.opened`, `pr.merged`) |

A process says which executors it may use. Feature delivery runs in a session today and in the engine once the
orchestrator (F-12) exists. "PR checks" is a CI process whose check steps decide what `factory / gate` requires: that
is where the CI goes.

**Long waits don't hold a build.** A process in the engine that reaches a human step or a long `wait-for-event` saves
its position and ends. The answer or the event starts a continuation from the next step, on the current build and
the same process version. That way no Pinned workflow stays open for days (engine.md). F-12 designs the details.

## 6. Drafts, publishing and storage

- **Draft on the Desk.** Each edit is saved to the draft of its process. Drafts are the Desk's own state, in
  `~/.local/state/software-factory/desk/drafts/`.
- **Check before publishing.** Validate catches unknown blocks, missing repo facts, exits that lead nowhere, event
  cycles, and steps above the autonomy cap. Preview shows, for a sample input, the path the run would take, the
  agents and models, where it would ask, and which events it would emit; nothing runs. A **staged run** runs the
  agents for real on a sample task, applying none of their outputs, and shows what they would have done (gh-aw's
  staged mode). It spends budget, so it confirms first.
- **Publish opens a PR to the project.** The PR changes only `.factory/`, and the Desk merges it when the checks
  pass. The publish page lists every change that loosens something, each to confirm: a removed human step, check or
  gate; `asks` moved toward `never`; higher autonomy or a wider tool; a check moved from `error` to `warn` or off.
  The principal's confirmation meets the factory-configuration floor.
- **The repo is the store** (D-14), read from the default branch, so a PR can't loosen the rules that judge it:
  - `.factory/processes/<id>.json`: one file per process. JSON, pretty-printed and machine-written, because JSON is
    YAML: the runtime's process engine loads it unchanged, and the standard-library `factory` CLI reads it with
    `json`. Git history is its versions; a run records the commit it ran.
  - `.factory/gates.json`: the gates, the autonomy cap, `trust_after`, and each checkpoint's `trusted_since`.
  - `.factory.toml` keeps the repo facts (commands, paths, worktree setup, tool lists), written by hand.
    `[teams.*]` and `[upgrade] schedule` leave it (D-10).
- **Templates.** A project's process starts as a copy of a factory template at the pinned version and records which
  one. When the template changes, the Desk shows the difference and offers each change to the draft. Nothing changes
  a project's processes unless the principal publishes it.

## 7. The format

A process file is the runtime's process format (process-engine.md) with factory step types: `agent`, `check`,
`command`, `human`, `wait-for-event`, `emit` (`FACTORY_STEP_TYPES` in `engine/`). `parallel`, `for_each` and
`call` map to the engine's own `parallel`, `for_each` and `workflow` steps. Stages, triggers, inputs, limits,
`runs_in` and exits for human steps are fields the factory's process workflow classes read, declared on
`ProcessWorkflowBase` as process-engine.md section 3 expects. The session executor walks the same file with the
standard library. One format and one validator serve the Desk, the engine, the session and CI.

```json
{
  "name": "feature-delivery", "template": "feature-delivery@3", "runs_in": ["session", "engine"],
  "triggers": [{"event": "task.ready"}, {"manual": {"inputs": {"task": "task"}}}],
  "limits": {"concurrent": 3, "budget_minutes": 480},
  "stages": [{"name": "Shape", "steps": ["shape", "approve-split"]},
             {"name": "Technical design", "steps": ["design", "lenses", "approve-design"]},
             {"name": "Build", "steps": ["build", "verify"]},
             {"name": "Land", "steps": ["land"]}],
  "steps": [
    {"id": "shape", "type": "agent", "skill": "repo:roadmap-add", "model": "opus", "autonomy": "propose",
     "outputs": {"task": {"max": 8}}},
    {"id": "approve-split", "type": "human", "shows": "split", "asks": "until-trusted",
     "exits": {"approved": "design", "changes": "shape", "rejected": "end", "timeout": "end"}},
    {"id": "design", "type": "agent", "skill": "repo:design-task", "model": "opus", "budget_minutes": 60},
    {"id": "lenses", "type": "parallel", "steps": [
      {"id": "lens-data", "type": "agent", "skill": "repo:data-access-layer", "model": "sonnet", "fresh_context": true},
      {"id": "lens-workflows", "type": "agent", "skill": "repo:temporal-workflows", "model": "sonnet", "fresh_context": true}]},
    {"id": "approve-design", "type": "human", "shows": "design", "asks": "when task.size >= L",
     "exits": {"approved": "build", "changes": "design", "rejected": "end", "timeout": "end"}},
    {"id": "build", "type": "agent", "skill": "repo:implement-feature", "model": "sonnet", "autonomy": "edit",
     "tools": ["repo-commands"]},
    {"id": "verify", "type": "check", "run": "verify", "mode": "error", "on_fail": {"back_to": "build", "retries": 2}},
    {"id": "land", "type": "agent", "skill": "repo:land", "model": "sonnet", "autonomy": "pr", "outputs": {"pr": {"max": 1}}},
    {"id": "landed", "type": "emit", "event": "feature.landed"}
  ]
}
```

## The Desk's views

| View | Shows | Actions |
|------|-------|---------|
| **Library** | every block by kind, searchable, each with its card (section 1). Factory blocks, then each project's own blocks | open a card; add the block to a process; see where it is used |
| **Coverage** | blocks and template processes × projects. Each cell is a word and a shape: **On** ●, **Off** ○, **Needs setup** ◌ (names the missing repo fact), **Planned** ◇ | add a template process to a project (a draft) |
| **Processes** (per project) | each process: its triggers in words, where it runs, its published version, its draft if any | create from a template or blank; open; run now (click trigger, asks for inputs); pause or resume its triggers |
| **Designer** (one process) | triggers on the left, stages left to right with their steps, human steps with their labelled exits, `emit` and `wait-for-event` steps linked to the Events view | add a step from the palette; move, remove; set triggers, inputs, limits; validate, preview, staged run; publish |
| **Inspector** (one step) | the block's card, and the step's settings with their source: *template*, *project*, or *repo fact* | edit; reset to the template value |
| **Events** (per project) | every event: who emits it, which processes start on it, which steps wait for it. Drawn as a map of processes joined by events, with cycles marked | add a project event; open a process |
| **Schedules** | a week view of every scheduled trigger across projects, in the engine's time zone, with next runs | open a process; pause a trigger |
| **Gates** (per project) | each resource's gate, the autonomy cap, every checkpoint's mode and record (approvals as is in a row, the last miss), floors locked | change a mode; trust a checkpoint (D-7); ask again |
| **Versions** | per process: each published version (a commit), what changed in words, who published it | compare; revert (a publish of the old version) |

F-25's **Needs you** adds admin items only:
- a draft that failed to publish;
- a process failing or paused;
- a block that needs setup;
- a checkpoint ready to trust;
- a checkpoint sent back to asking.

It never shows tasks, PRs or questions (D-13). F-25's run pages keep the run history of engine processes.

All views are server-rendered forms with post-redirect-get, like the rest of the Desk. The Designer works without
script: add, move and remove are forms. Drawing exits and the event map are server-rendered SVG. Each state is a word
and a shape, never colour alone (`sfactory/status.py`).

## 8. Specs and acceptance scenarios (an option: D-9)

If adopted, living specs come as blocks and a template, built into the task system in an OpenSpec-compatible format
(not the OpenSpec tool):
- an agent step in Shape writes the task's spec delta (`## ADDED / MODIFIED / REMOVED Requirements`, each
  `#### Scenario:` WHEN / THEN);
- a check, "one test per scenario";
- a QA agent step runs every scenario (F-18);
- `factory task complete` merges the delta into `docs/specs/<capability>/spec.md`;
- a scheduled process reports drift between specs and tests.

The template is "Feature delivery with specs". Nothing else depends on it.

## 9. The factory as a project (F-04)

software-factory gets a `.factory.toml`, a `.factory/` folder and the same views.

```toml
# software-factory's .factory.toml (repo facts)
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

| Process | software-factory |
|---------|------------------|
| Feature delivery | in a session; UX only `when task.ui`; a design checkpoint when the task is large; no QA |
| Release | on `pr.merged`: the deploy commands, then `factory engine status` as a check; no production checkpoint, because a merge is the release (D-4) |
| Upgrade, Skills review | weekly schedules |
| PR checks | in CI: verify, skillcheck, task check at `error` |
| Incident | none: no users beyond the principal |

**The factory must stay fixable by hand.** `factory engine upgrade` and the tower's `land` still run from a terminal
when the engine or the Desk is down.

## Build order

0. **Foundations:**
   - block declarations and their skillcheck rule;
   - the process file validator (stdlib, shared by the CLI and the Desk);
   - the factory step types and process workflow classes on the engine;
   - the event catalog;
   - `.factory/gates.json`;
   - the new status words.
1. **Teams become processes (D-10):** `factory process import` writes `[teams.*]` and `[upgrade]` schedules as
   processes; the engine reconciles schedule triggers from `.factory/processes/`; the repo's PR removes the old
   keys.
2. **Events:** the engine's GitHub and task-record reader, the bus, event triggers and `wait-for-event`, and the
   chain limit; the Main CI red template.
3. **CI and gates:** the reusable workflow and `factory / gate`; the tower's gate reads `.factory/gates.json`
   (F-02).
4. **Desk, read-only:** Library with cards, Coverage, Processes, Designer, Events, Schedules, Gates, Versions. F-25's
   Projects drops the open factory PRs (D-13).
5. **Desk, designing:** drafts, the step forms, validate, preview, staged run, publish by PR, revert, trust.
6. **The session executor:** `/factory:run` and `factory process run --here`. FamilyOffice's pipeline becomes its
   Feature delivery process (its PR).
7. **The factory as a project (F-04).**

## Tests

- **Format:** every template validates; the runtime loads a published file unchanged; a step above the autonomy cap,
  an unknown block, a missing repo fact or an exit to nowhere is refused at publish.
- **Events:** an event starts every subscribed process; a chain stops at depth 3; a cycle is reported; a waiting run
  resumes on a matching event and takes its timeout exit otherwise.
- **Safe outputs:** an output not on the step's list is refused and logged; caps hold.
- **Gates:** a merge step in any process passes the merge gate; floors ask even when trusted or set to `never`; trust
  is computed from the record, and a miss asks at once.
- **Executors:** the session walk and the engine run take the same path for the same input (golden comparison);
  human steps end an engine run and the answer continues it.
- **Desk:** every block has a full card; publish lists every loosening change; floors have no remove control; no page
  shows a task, a PR waiting or a question (D-13).

## Out of scope

- The orchestrator's continuation mechanics (F-12) and the decision queue (F-13).
- Writing skills on the Desk (D-12); editing repo facts on the Desk.
- Budgets and cost reporting: F-33 and F-31 (`budget_minutes` and `limits` are the keys this design uses).
- CI runners other than GitHub Actions; events from sources beyond GitHub, the task system and the factory's own
  processes (Slack, Sentry, Linear) until one is needed.
- Desks on other laptops (F-06).
