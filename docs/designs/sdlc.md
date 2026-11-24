# F-26 design: the SDLC command center

On the Factory Desk, the principal designs how each project is developed. They build **processes** from a small
set of **blocks** (agents, checks, commands, human steps, events). Each process starts on an **event**, a
**schedule** or a **click**. **Gates** on merging and deploying decide where the principal must say yes.

The day-to-day work is not on the Desk (D-13). A task in flight, a PR waiting and an agent's question all stay in
each developer's own tool (Claude Code, Cursor, Orca). software-factory is a project like any other (F-04).
Mockup (interactive, sample data marked): https://claude.ai/artifact/Jsz6U7aePHpkPpskVQtuiw

Decided by the principal:
- **D-6** (2026-11-22): the SDLC is designed on the Desk.
- **D-7** (2026-11-22): a checkpoint set to "until trusted" asks once before it stops asking.
- **D-8** (2026-11-22): a miss sends it back to asking at once.
- **D-10** (2026-11-22): standing teams become processes with schedule or event triggers.
- **D-11** (2026-11-24): floors can't be removed on the Desk.
- **D-12** (2026-11-24): a new reusable agent is a skill landed by PR.
- **D-13** (2026-11-24): the Desk is configuration, never day-to-day work.
- **D-14** (2026-11-24): processes and gates live in the repo under `.factory/`, written by the Desk's PR.

**Generic and configurable** (the principal, 2026-11-24). Every vocabulary is data in the factory's catalog
(models, autonomy ladder, ask modes, executors, outputs, tools, resources, events, default gates, floors), every
block is a file, and a project's folder, skills folders, extra models and extra events are `.factory.toml`
`[sdlc]` settings. Nothing names a project. Reference: [factory-sdlc.md](../factory-sdlc.md).

## Borrowed from similar systems

Researched 2026-11-24 across two groups:
- agentic SDLC tools: GitHub Agentic Workflows, Cursor Automations, Devin, Kiro, Factory, Codex, Jules, GitLab
  Duo, Claude Code;
- workflow builders and portals: n8n, Zapier, GitHub Actions, Azure Pipelines, Port, Cortex, Inngest, Dify.

Most of them model an automation the same way: a trigger, steps, the outputs it may produce, and limits. This design
keeps five of their ideas:

1. **Agents propose, the run applies.** An agent returns typed outputs (a PR, a comment, a task), each with a cap.
   It never holds write access itself. (GitHub Agentic Workflows)
2. **Processes start each other through named events.** One event starts every process that listens for it.
   (Inngest, GitHub Actions)
3. **Each human decision is its own exit**: approved, changes, rejected. (Dify, LangGraph)
4. **Gates sit on merging and deploying, not inside processes.** Every process that merges or deploys passes the
   same gate. (Azure Pipelines checks, GitHub environments)
5. **Edit a draft, publish a version.** The settings are read from the default branch, so a PR can't loosen its own
   rules. (n8n, Zapier, Cursor Bugbot)

## 1. Blocks

There are five kinds of block. Each has a **card** that says what it does and what it needs.

| Kind | Does | Settings |
|------|------|----------|
| **Agent** | an AI agent working on the repo: a factory skill (`factory:upgrade`) or the project's own (`repo:design-task`) | model, budget, tools, autonomy (`read`, `edit`, `pr`), instructions, the outputs it may produce |
| **Check** | a machine gate: verify, skillcheck, the task check | blocking or warning; on failure, go back to a named step (at most *n* times) |
| **Command** | a repo command from `.factory.toml`: deploy, health, rollback | which command |
| **Human** | a person decides: approve a split, a design, a deploy | asks: `always`, `until trusted`, `when large`, `never`; where each answer leads |
| **Event** | emits an event other processes start on | the event's name |

Steps in a stage may run **in parallel** (for example, two review lenses), and any step may have a **when** rule (for
example `task.ui`, which runs the step only for tasks that touch the UI).

A **card** shows:
- what the block does, in plain words;
- its source and status: *available* at a factory version, or *planned* with its F-ID;
- its inputs and outputs, and the events it emits;
- its defaults;
- what it needs from the repo, named when missing;
- its record (runs, outcomes, minutes);
- where it is used.

Factory blocks are files in `plugins/factory/sdlc/blocks/`, and the factory's tests keep them in step with the code
(every available agent's skill exists, every `run_from` is a setting). A project's own blocks are files in its
`.factory/blocks/` that point at its skills and commands, which stay where they are and are never copied. A new
reusable agent is a skill landed by PR with its evals (D-12). On the Desk, any agent step can be given its own
instructions and model.

## 2. Processes and triggers

A process has three parts:
- **triggers**: `event` (`pr.merged`, `ci.failed`, `task.ready`, or one another process emits), `schedule`
  ("Mondays 06:00") or `click` (the Desk's Run now, or `/factory:run` in a session);
- **stages**, each holding steps;
- **where it runs**: in a developer's session (human steps ask there), in the engine (unattended; human steps
  reach people as a PR or a task), or in CI (checks only).

The factory ships templates. A project starts from one and changes what it needs.

| Template | Starts on | Steps |
|----------|-----------|-------|
| Feature delivery | `task.ready`, click | Shape (agent; human: approve split) → Design (agent; lenses in parallel; human when large) → Build (agent; check verify) → Land (agent opens the PR) |
| Bug fix | `issue.labeled(bug)`, click | Reproduce and fix (agent; check verify) → Land |
| PR checks | `pr.opened` (CI) | verify, skillcheck, task check; their result is the `factory / gate` check a merge waits for |
| Upgrade | Mondays 06:00, click | Upgrade agent (PRs ≤ 3, tasks for risky moves) |
| Main CI red | `ci.failed` on main | Diagnose (agent) → Fix (agent; check verify) → Land |
| Release | `pr.merged`, click | Deploy staging → health → human: production → deploy; emits `deploy.failed` on failure |
| Incident | `deploy.failed`, `incident.opened` | Runbook → diagnose (agent, read-only) → human: approve the fix |

**Events** come from GitHub and the task records, which the engine reads every two minutes (the home machine takes
no inbound connections), or from an Event step. A chain of processes starting each other stops at depth 3, so two
processes can't loop forever.

## 3. Gates and trust

Each project has two gates, and every process passes them:

| Gate | Default |
|------|---------|
| **Merge** to the default branch | the checks pass, then the merge checkpoint asks *until trusted* |
| **Production deploy** | asks *always* |

- **Trust (D-7, D-8).** After 10 approvals as is in a row, the Desk offers to trust a checkpoint. A miss (a change,
  revert or rollback) makes it ask again at once. The record comes from where the work happens: task records,
  GitHub, and the deploy log.
- **Floors** always ask, and the Desk can't remove them (D-11):
  - a PR changing `.factory.toml`, `.factory/` or CI;
  - a PR touching the repo's ask paths (software-factory: hooks, guards and the gate, D-3), once F-02 adds
    `[land] ask_paths`;
  - a project's first production deploy.
- **Autonomy cap.** A project-wide maximum autonomy that no agent step can exceed.

The tower's gate enforces the merge gate. Its FamilyOffice-specific constants move to FamilyOffice's
`.factory.toml` (F-02).

## 4. The Desk

| View | Shows | Actions |
|------|-------|---------|
| **Projects** | each project's processes (what starts them, where they run, draft or published) and its two gates with their record | run now, pause; add a process from a template; change a gate, trust |
| **Designer** | one process: triggers on the left, stages left to right, steps as cards; human steps show where each answer leads | add a step from the library, edit or remove it; preview a sample task's path; publish |
| **Library** | every block, each with its card | add it to a process |
| **Triggers** | the week's schedules across projects, and which events start which processes | open a process; pause a trigger |

Every state is a word and a shape, never colour alone. The pages are server-rendered forms, like the rest of the
Desk. F-25's **Needs you** adds only admin items: a failed publish, a process failing, a checkpoint ready to trust.

## 5. Storing and publishing

- **Edits go to a draft** (the Desk's own state).
- **Publishing opens a PR** that changes only `.factory/` in the project's repo, after the principal confirms each
  change that loosens something. The Desk merges it when the checks pass.
- **The repo is the store** (D-14), read from the default branch:
  - `.factory/processes/<name>.json`: one per process, in the runtime's process format with factory step types.
    JSON is valid YAML, so the runtime loads it as is, and the standard-library CLI reads it.
  - `.factory/gates.json`: the gates, the autonomy cap, and when each checkpoint was trusted.

  Every developer's session, CI and the engine read the same files, and git keeps the versions.
- **`.factory.toml` keeps the repo facts** (commands, paths, worktree setup). `[teams.*]` and `[upgrade] schedule`
  leave it, because they become processes (D-10).
- **Long waits.** An engine run that reaches a human step saves its place and ends, and the answer continues it.
  That way no Pinned workflow stays open for days (engine.md; the details are F-12's).

## 6. software-factory as a project (F-04)

software-factory gets a `.factory.toml`:
- verify commands: the unit tests, skillcheck, plugin validation, and the runtime and engine suites in their own
  virtualenvs;
- `[land] check = "CI OK"`, with an aggregate job added to `ci.yml`;
- `ask_paths` covering the hooks, guards, gate and CI (D-3);
- `[deploy] commands = ["factory self-update", "factory engine upgrade"]` (D-4: a merge is the release).

Its processes:
- Feature delivery, in a session;
- PR checks;
- Release on `pr.merged`, with no production checkpoint;
- Upgrade and Skills review, weekly.

It has no Incident process, because it has no users beyond the principal. `factory engine upgrade` and the tower's
`land` keep working from a terminal when the engine or the Desk is down.

## 7. Specs (an option: D-9)

If adopted, the template "Feature delivery with specs" adds:
- a spec-writing step in Shape, with WHEN / THEN scenarios as acceptance criteria;
- a check, "one test per scenario";
- a QA step that runs each scenario.

The spec format is OpenSpec-compatible, built into the task system.

## Build order

1. **Foundations:** the catalog, block files and cards, templates, the process and gates formats with their
   validator, `[sdlc]`, and `factory sdlc` (built). Then the factory step types on the engine.
2. **Teams become processes:** the upgrade team becomes the Upgrade process, and the old `.factory.toml` keys go.
3. **Events and CI:** the engine's GitHub reader, event triggers, PR checks and `factory / gate`; the tower reads
   the gates (F-02).
4. **Desk:** Projects, Designer, Library and Triggers, read-only first, then editing and publishing.
5. **The session runner:** `/factory:run`. FamilyOffice's pipeline becomes its Feature delivery process.
6. **software-factory as a project.**

## Tests

- **Format:** every template validates, and the runtime loads a published file as is. A step above the autonomy
  cap, an unknown block or a missing repo fact is refused at publish.
- **Events:** an event starts every process listening for it, and a chain stops at depth 3.
- **Outputs:** an output not on the step's list is refused.
- **Gates:** every process's merge passes the merge gate. Floors ask even when trusted. A miss asks at once.
- **Desk:** every block has a full card. Publish lists every loosening change. No page shows tasks, PRs or questions.

## Out of scope

- The orchestrator (F-12) and the decision queue (F-13).
- Writing skills on the Desk (D-12).
- Cost reports (F-33).
- Event sources beyond GitHub, the task system and the factory.
- Desks on other laptops (F-06).
