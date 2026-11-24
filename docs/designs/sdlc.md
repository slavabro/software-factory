# F-26 design: each project's SDLC, configured on the Desk

The Factory Desk is the factory's admin panel. On it, the principal sees everything the factory can do (its
capabilities) and decides, for each registered project, how that project's software lifecycle runs:
- which stages a change goes through, which agents work in each and on which model;
- which checks run and where (while an agent works, on the PR, after merge);
- where a person is in the loop;
- which processes run on their own, and how often (upgrades, support, QA sweeps).

The day-to-day work is not on the Desk (D-13). That means a task moving through its stages, a PR waiting for review,
or an agent asking a question. It happens in each developer's own tool (Claude Code, Cursor, Orca), and the Desk
never shows or answers it. software-factory is configured like any other project (VISION "The factory builds
itself", F-04). Mockup (interactive, sample data marked): https://claude.ai/artifact/Jsz6U7aePHpkPpskVQtuiw

VISION's "How work flows" table owns what each stage is for. This design covers how a project's lifecycle is
described, where that description lives, how the Desk edits it, and how it reaches the places where work happens.

Decided by the principal:
- **D-6** (2026-11-22): the SDLC is configured on the Desk.
- **D-7** (2026-11-22): a checkpoint set to "until trusted" asks once before it stops asking.
- **D-8** (2026-11-22): a miss sends a trusted checkpoint back to asking at once.
- **D-10** (2026-11-22): standing teams become recurring processes, configured on the Desk.
- **D-13** (2026-11-24): the Desk is configuration and the factory's health, never the day-to-day work.

## First principles

1. **Three kinds of fact, three owners.**
   - A **capability** is something the factory can do (an agent, a check, a guard, a recurring process, a service).
     The factory repo owns it, versioned with its code.
   - **Policy** is which capabilities a project uses and how. The project owns it, and the principal edits it on
     the Desk.
   - **Work** is tasks, PRs, runs and answers. The task system (F-03), GitHub and the engine own it, and people see
     it in their own tools.
2. **Policy must reach every place where work happens:** each developer's tool on any laptop, CI, and the engine.
   Only the repo reaches all of them. So the policy is a file in the repo, and the Desk is the editor that writes it
   (D-14). Git is its history.
3. **Policy says what, not how.** A developer running skills in Cursor today follows the same lifecycle policy that
   the orchestrator (F-12) will follow later. So the policy is declarative, not an executable workflow (section 7).
4. **Defaults come from the factory, at the version the project pins** (D-2). The project's file holds only what
   differs. A factory release that changes a default reaches a project in the pin-bump PR, where it is seen and
   reviewed. The Desk shows every value with its source: *factory default* or *project*.
5. **A person meets a checkpoint in their own tool**, as a question in the session, a PR to review or a task. The
   factory adds no inbox of its own, and the Desk only decides where those checkpoints are.

## What the principal does on the Desk

| View | Shows | Actions |
|------|-------|---------|
| **Capabilities** | every capability, grouped as stage agents, checks, guards, recurring processes and services. Each shows what it does, its status (*available* since a factory version, or *planned* with its F-ID), where it runs, its settings and defaults, and what it needs from a repo | open one; apply it to a project |
| **Coverage** | capabilities × registered projects. Each cell is a word and a shape: **On** ●, **Off** ○, **Needs setup** ◌ (names the missing repo fact), **Planned** ◇ (not built yet) | switch a cell on or off (into that project's draft) |
| **Project → Lifecycle** | the stages a change goes through, left to right. For each: on, off, or only when a rule holds; its agent (a factory capability or a repo skill) and model; extra agents (review lenses); the checkpoint after it and how it asks | edit a stage; add an agent or a stage; preview a sample change |
| **Project → Checks & CI** | each check and where it runs: in the session, on the PR (advisory or required), on main after merge, or in a scheduled run; the one GitHub check the merge waits for | move a check; make it required or advisory |
| **Project → Recurring** | each process that runs on a cadence or an event: the cadence in words and on a week strip, its events, budget, model, what it produces (PRs, tasks, a report), its last result as one word | add one from the catalog; change the cadence; pause or resume |
| **Project → In the loop** | every point where a person decides, across the lifecycle, Land, Deploy and the outputs of recurring processes. For each: the mode, how it reaches the person (session, PR, task), and its record (approvals as is in a row, the last miss). Floors show locked | change a mode; trust a checkpoint (D-7); ask again |
| **Project → Repo facts** | the repo's `.factory.toml`, resolved: commands, paths, worktree setup, the factory version it pins | none: they change with the code, by PR |
| **Apply** | the project's draft as a list of changes in words. Changes that loosen something are listed separately, each to confirm | apply (a PR, merged when its checks pass); discard |
| **History** | each commit that changed the policy, in words, with who applied it | revert (a PR, like any change) |

Semantics stated on the page:

- **Draft, then apply.** Edits collect in a per-project draft, which is the Desk's own state. Apply validates the
  draft, opens a PR that changes only the policy file (branch `factory/sdlc-<time>`), and merges it when the
  project's checks pass. The principal's apply is the approval that the floor for policy changes requires (section 5).
- **When a change takes effect.** It takes effect when the PR merges:
  - developers' tools read the policy on their next `factory sdlc` command;
  - CI reads it on its next run;
  - the engine reconciles its schedules once, right after the Desk's merge, and again on every later read of the
    default branch.
- **Loosening changes need a confirmation.** These are: removing a stage, check or checkpoint; moving a required
  check to advisory or off; setting a checkpoint to "never"; trusting a checkpoint; widening an agent's tools.
- **Floors are locked** (section 5). The Desk shows them, with their reason, and has no control that removes them.
- **Preview** takes a sample change (its size, whether it touches the UI, whether it touches ask paths) and shows
  which stages would run, with which agents and models, and where it would ask. It is computed from the policy, so
  nothing runs and nothing is spent.
- **Not on the Desk (D-13):** tasks in flight, open PRs, checkpoint questions and their answers. F-25's
  **Needs you** keeps only admin items:
  - a recurring process paused or failing;
  - a budget over its limit;
  - an apply that failed;
  - a capability that needs setup;
  - a checkpoint ready to be trusted (D-7);
  - a checkpoint sent back to asking (D-8).

## 1. Capabilities: the catalog

The factory declares each capability once, in `plugins/factory/capabilities.toml`, next to the code that
implements it. Each entry has:
- an ID, a kind and one line on what it does;
- its status (`available` since a version, or `planned` with its F-ID);
- where it runs (`session`, `ci`, `engine`);
- its settings, with their defaults;
- what it needs from a repo (for example `[deploy] commands`).

A skillcheck rule keeps the catalog and the code in step: every available entry points at a skill, check or process
that exists.

| Kind | Examples (status today) | Applied to a project as |
|------|-------------------------|-------------------------|
| **Stage agent** | product shaping (F-10), UX design (F-17), technical design, review lenses, build and land (F-11), QA (F-18), release (F-19), support (F-24); all planned as factory skills | the agent of a lifecycle stage, or an extra agent in one |
| **Check** | verify (the repo's `[verify]` commands; available), skillcheck (available), task check (available), plugin validate (available), docs drift (planned, F-23) | placed at one or more points: session, PR, main, scheduled |
| **Guard** | the push-to-main guard (available), the hook pack (planned, F-15) | on or off; runs in sessions |
| **Recurring process** | upgrade (available), skills review (available), QA sweep, CI health, security, code health, factory improver (planned, F-23), support triage (planned, F-24) | a cadence and/or events, with budget, model and output |
| **Service** | task system (available), shared runtime (available), decision queue (planned, F-13), notifications (planned, F-42) | on or off, with what it needs |

**A repo's own skills are capabilities of that project.** The catalog lists them under the project, read from the
repo, never copied. Examples are FamilyOffice's `/design-task` and its domain reviewers. A repo skill can be the
agent of a stage or an extra agent in one. Before the factory's own stage agents exist, that is how FamilyOffice's
lifecycle is configured.

**A planned capability shows, but can't be applied.** Its F-ID links to the BACKLOG, so the catalog is also the
honest list of what the factory can't do yet. A new reusable agent is a skill: it lands by PR with its trigger evals
(F-14), and appears in the catalog once merged (D-12).

## 2. The policy file

Two files sit at the repo root, with different writers:

| File | Says | Written by | Changes |
|------|------|-----------|---------|
| `.factory.toml` | how to do things in this repo: commands (verify, deploy, health, rollback), paths that make Land ask, the CI check name, worktree setup, tool lists | developers, by hand, with comments | with the code, by PR |
| `.factory.sdlc.toml` | what the factory does for this repo, and when: stages, agents, models, where checks run, checkpoints, recurring processes | the Desk (or `factory sdlc set`), machine-written | by the Desk's PR |

- **It holds only overrides.** A missing key is the factory default at the pinned version. `factory sdlc show
  --repo .` prints the resolved policy, with each value's source.
- **It follows the same rules as the rest of the config.** Unknown keys are an error, as in `.factory.toml`. The
  reference lives in `docs/factory-sdlc.md`, with the loader in `sfactory/sdlc.py`. The file is written atomically,
  and comments are not kept, as with `engine.toml`. `factory sdlc check` validates it in the factory's gate job
  (section 4).
- **Keys that move out of `.factory.toml`:** `[upgrade] enabled`, `schedule` and `max_prs`, and every `[teams.*]`
  table, which become `[recurring.*]` (D-10). The upgrade's repo facts (`manifests`, `install`,
  `deprecation_check`, `risky`, `ignore`) stay, because they change with the code.

```toml
# .factory.sdlc.toml (FamilyOffice, sample). Written by the Factory Desk; missing keys = factory defaults.
[lifecycle]
stages = ["shape", "ux", "design", "build", "review", "qa", "land", "deploy"]

[lifecycle.shape]
agent = "repo:roadmap-add"            # until factory:product (F-10) is available
model = "opus"
checkpoint = "until-trusted"          # approve the split

[lifecycle.ux]
when = "task.ui"

[lifecycle.design]
agent = "repo:design-task"
model = "opus"
lenses = ["repo:data-access-layer", "repo:temporal-workflows"]   # fresh context each
checkpoint = "when task.size >= L"

[lifecycle.build]
agent = "repo:implement-feature"
model = "sonnet"
budget_minutes = 120

[lifecycle.land]
checkpoint = "until-trusted"
trusted_since = "2026-12-01"          # set by "Trust" on the Desk (D-7)

[lifecycle.deploy]
checkpoint = "always"                 # production

[checks]
verify = ["pr-required"]
skillcheck = ["pr-required"]
tasks = ["session", "pr-required"]

[recurring.upgrade]
cadence = "0 6 * * 1"                 # Mondays 06:00, engine time zone
model = "sonnet"
budget_minutes = 90
max_prs = 3

[recurring.ci-fix]
on = ["ci_red"]
```

## 3. The lifecycle

Stage names come from VISION's table: Intake, Shape, UX design, Technical design, Build, Review, QA, Land, Deploy.
Support and Upkeep are recurring or event processes (section 6). Learn applies to every stage (F-16).

| Setting | Meaning |
|---------|---------|
| `when` | a rule under which the stage runs (`task.ui`, `task.size >= M`, `paths ~ "**/migrations/**"`); a stage left out of `stages` is off |
| `agent` | `factory:<capability>`, `repo:<skill>`, or `none` (the developer does the stage; its checkpoint still applies) |
| `model` | an F-41 tier alias (`opus`, `sonnet`, `haiku`) |
| `budget_minutes` | agent time for the stage |
| `tools` | a profile: `read`, `edit`, `edit+pr`; the lists behind each profile are repo facts (`[agent]`) |
| `lenses` | extra agents that review the stage's output, each with fresh context (VISION "Fresh eyes review") |
| `checkpoint` | how the person is asked after the stage (section 5) |
| `shows` | what the checkpoint shows (split, design, mockup, PR, deploy plan); defaults per stage |

**Adding a stage.** `[lifecycle.<name>]` with `after = "<stage>"` and an agent puts a stage of the principal's own,
for example a security review before Land in a project that handles money.

**Who follows the lifecycle.**
- **Today, by hand:** a developer runs the stages in their tool. A factory or repo skill starts with
  `factory sdlc stage <name> --repo . --json`, which returns the resolved agent, model, lenses, checkpoint and what
  to show. The skill runs the stage's agent and lenses as subagents on those models; the session's own model stays
  the developer's choice. At a checkpoint it asks the developer, in that session, with options and a
  recommendation.
- **Other tools:** anything that can run a shell command (Cursor, Codex in Orca) uses the same CLI. The repo's
  `AGENTS.md` points at it.
- **Later:** the orchestrator (F-12) runs the same policy in the engine.

## 4. Checks and CI

A check is placed at one or more points:

| Point | Runs | Effect |
|-------|------|--------|
| `session` | as a hook while an agent works (the plugin's hooks read the policy) | blocks the action, says why |
| `pr` | in the factory's CI job on every PR | reported, doesn't block |
| `pr-required` | in the same job | the job fails, so the merge waits |
| `main` | in the factory's CI job after each merge to the default branch | a failure is the `ci_red` event |
| `scheduled` | inside a recurring process (for example docs drift in code health) | becomes PRs or tasks |

**One factory job, one required check.** The factory ships a reusable GitHub workflow at the version the project
pins. The repo calls it from a thin `.github/workflows/factory.yml`. The workflow:
- reads the policy;
- runs the checks placed at `pr`, `pr-required` and `main`, using `action.yml`'s `setup` to get the factory's
  commands;
- reports one check, `factory / gate`.

Registering a project makes `factory / gate` a required check on the default branch, once, with the principal's
confirmation. After that, where each check runs is policy, and no branch-protection change is needed. The repo's
own CI (for example `make ci`) stays its own. The tower's gate waits for both the repo's `[land] check` (a repo
fact, F-02) and `factory / gate`.

GitHub Actions is the only CI the factory supports. A key for another runner is added when a second runner is built,
not before.

## 5. In the loop: checkpoints, trust, floors

| `checkpoint` | Behaviour |
|--------------|-----------|
| `always` | always asks |
| `until-trusted` | asks until the principal trusts it (D-7), then acts alone until a miss (D-8) |
| `when <rule>` | asks only when the rule holds (size, paths, labels, cost) |
| `never` | doesn't ask; the outcome is still recorded (confirmed on apply) |

**Where it reaches the person** (never the Desk):

| Checkpoint at | Reaches the person as |
|---------------|-----------------------|
| a stage run in a session | a question in that session, with options and a recommendation |
| Land | the PR: the gate says ASK, which shows in GitHub and in the tower |
| Deploy | the deploy's own confirmation in the session that runs it |
| an unattended run (recurring, event, later F-12) | its outputs: PRs, which reach Land's checkpoint; tasks in the repo's task system; a question it can't answer from the code becomes a task (the decision queue, F-13, later) |

**The record comes from where the work happens, so nothing new stores it.**
- A session checkpoint writes its outcome into the task's record, under `checkpoints:` in the F-03 front matter:
  `approved`, `changed` or `rejected`. The outcome lands with the task's PR.
- Merges, reverts and changes pushed after review come from GitHub.
- Rollbacks come from the deploy log.

`factory sdlc record --repo .` computes each checkpoint's streak of approvals as is and its last miss, and the Desk
caches it.

- **Trusting (D-7).** When a streak reaches `trust_after` (default 10, a policy key), the Desk shows the proposal
  under Needs you and on In the loop. For example: "Let Land merge alone in FamilyOffice? The last 10 were approved
  as is." **Trust** adds `trusted_since = <date>` to the draft, applied like any change.
- **A miss (D-8).** A checkpoint acts alone only while no miss has happened since `trusted_since`. A miss is a
  change, rejection, revert, rollback, or correction (F-16's signal). The check is computed when the checkpoint is
  reached, so a miss makes it ask at once with no write. The Desk shows the reason and the new streak. Trusting it
  again sets a new date.

**Floors** are factory-owned points that ask whatever the policy says, so that a project can never loosen the gate
that judges it (D-11). The Desk shows them locked:
- a PR that changes `.factory.toml`, `.factory.sdlc.toml` or the CI workflows (a Desk apply meets this floor,
  because the apply is the principal's approval);
- a PR that touches the repo's `[land] ask_paths` (FamilyOffice: migrations, infra and the deployer;
  software-factory: hooks, guards and the gate itself, D-3);
- a project's first production deploy.

The tower's gate (`fleet.py gate`) enforces the Land floors and reads Land's checkpoint in place of
`TOWER_AUTOMERGE`. Its FamilyOffice-specific constants (`ASK_PATHS`, `DEPLOY_DIR` and the `CI OK` check name) move
to FamilyOffice's `.factory.toml` (F-02).

## 6. Recurring and event processes

A recurring process is a recurring capability applied to a project with:
- `cadence` (cron, in the engine's time zone, shown in words);
- `on`, the events that start it (`ci_red`, `deploy_failed`, `incident`, `merged`);
- `budget_minutes` and `model`;
- `output`: `prs` (through Land), `tasks`, or `report`;
- the capability's own settings (for example `max_prs`).

It runs on the engine as today's team workflow does (D-10). The engine reads the policy from the default branch, as
it reads `.factory.toml` today, and reconciles its Temporal Schedules. New schedules start paused, as now. The Desk
shows the next run and the last result as one word; F-25's run pages have the detail.

| Process | Default | Status |
|---------|---------|--------|
| Upgrade | Mondays 06:00, PRs | available (F-01) |
| Skills review | Mondays 07:00, PRs | available |
| CI fix | on `ci_red`, PRs | planned (F-23 DevOps) |
| QA sweep | nightly, tasks | planned (F-23) |
| Security | weekly, PRs and tasks | planned (F-23) |
| Code health and docs drift | monthly, PRs | planned (F-23) |
| Support | on `incident` and `deploy_failed`, PRs and tasks | planned (F-24) |
| Factory improver | weekly, PRs to the factory | planned (F-23, F-16) |

## 7. Is the process engine the right representation?

No for the policy, yes for running it. The policy is followed by people in their tools and by CI, and neither runs
Temporal. A policy made of overrides on factory defaults reads, diffs and reviews in words. Retries, the
continuation after a checkpoint, and how parallel lenses run are execution details, so they belong to the executor.

When F-12 runs the lifecycle, it turns the resolved policy into a run of the runtime's process engine
(process-engine.md), with factory step types. Recurring processes already run as team workflows. One owner each: the
policy says what happens, the engine decides how it runs.

## 8. Specs and acceptance scenarios (an option: D-9)

If adopted, "Living specs" is a capability like any other: an OpenSpec-compatible format built into the task
system, not the OpenSpec tool. Applying it to a project changes the following:

| Where | What |
|-------|------|
| Shape | the stage also writes the task's spec delta (`docs/tasks/active/<id>/spec.md`: `## ADDED / MODIFIED / REMOVED Requirements`, each `#### Scenario:` WHEN / THEN); these are the acceptance criteria |
| Technical design | names the code and test that prove each scenario |
| Checks | "one test per scenario", placed at `pr-required` |
| QA (F-18) | runs every scenario end to end |
| Land | `factory task complete` merges the delta into `docs/specs/<capability>/spec.md` |
| Recurring | code health reports drift between living specs and their tests |

## 9. The factory as a project (F-04)

software-factory gets the same two files as any product, and is registered on the Desk like one.

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

| Policy | software-factory |
|--------|------------------|
| Lifecycle | Shape, Technical design (asks when the task is large), Build, Review, Land (D-3's ask paths are a floor); UX design only `when task.ui` (Desk pages, mockups); no QA stage; Deploy without a checkpoint, because a merge is the release (D-4) |
| Checks | verify, skillcheck and task check at `pr-required`; the guard in sessions |
| Recurring | upgrade weekly; skills review weekly; code health monthly once available; no support (no users beyond the principal) |

**The factory must stay fixable by hand.** Its Land and Deploy never depend only on the build being deployed:
`factory engine upgrade` and the tower's `land` still run from a terminal when the engine or the Desk is down.

## How it is built

- **The catalog** (`plugins/factory/capabilities.toml`): its loader, and a skillcheck rule that keeps it in step
  with the skills and checks.
- **The policy** (`sfactory/sdlc.py`, standard library): schema, loader, resolution (factory defaults at the pinned
  version, then the project's values, each with its source), validation and one atomic writer. Its CLI is
  `factory sdlc show|stage|check|set|record`. The reference is `docs/factory-sdlc.md`.
- **The engine** reads `[recurring.*]` in place of `[teams.*]` and `[upgrade] schedule`. Each recurring process is a
  team workflow run, unchanged.
- **CI:** the reusable workflow `factory / gate`, and the thin caller in each project.
- **The Desk** (`engine/engine/desk/`): `/capabilities`, `/coverage`, `/projects/<repo>/sdlc` (lifecycle, checks,
  recurring, in the loop, repo facts), `/projects/<repo>/apply`, `/projects/<repo>/history`. Like the rest of the
  Desk, these are server-rendered forms with post-redirect-get and no script.
  - Drafts live in `~/.local/state/software-factory/desk/drafts/<repo>.toml`.
  - Apply goes through `ops.apply_policy(repo)`, which validates, branches, commits, pushes, opens the PR, merges it
    when `factory / gate` passes, and reconciles. Each step is written to `state/actions.jsonl`, and a step that
    fails is said in words under Needs you.
- **Status words** (`sfactory/status.py`), each a word and a shape, never colour alone:
  - coverage: On ●, Off ○, Needs setup ◌, Planned ◇;
  - checkpoints: Always asks ?, Earning *n* of *m* ◐, Acts alone ●, Asks when… ◑, Never asks ○.

## Build order

0. **Foundations:** the catalog and its skillcheck rule; `sfactory/sdlc.py` with the CLI and reference; the new
   status words.
1. **Recurring processes move to the policy (D-10):** the engine reconciles from `[recurring.*]`;
   `factory sdlc import` writes a repo's `[teams.*]` and `[upgrade] schedule` into its policy file, and the repo's
   PR removes them from `.factory.toml`.
2. **CI:** `factory / gate` and its callers in FamilyOffice and software-factory. The tower's gate waits for it and
   reads `[land]` and Land's checkpoint (F-02).
3. **Desk, read-only:** Capabilities, Coverage, the project's SDLC pages and History. F-25's Projects drops the open
   factory PRs (day-to-day work, D-13), and Needs you keeps only admin items.
4. **Desk, editing:** drafts, preview, apply, revert, trust.
5. **Hand mode reads the policy:** the factory's skills and FamilyOffice's pipeline skills call `factory sdlc stage`
   and record checkpoint outcomes in the task record (FamilyOffice's PR for its skills).
6. **The factory as a project (F-04):** its `.factory.toml`, its policy, the aggregate `CI OK` job and
   `factory / gate`.

## Tests

- **Policy:** an unknown key is refused; resolution gives each value's source; a project file with only overrides
  resolves to the factory defaults at its pin; the writer is atomic and its output loads back unchanged.
- **Catalog:** every available capability points at code that exists; a planned one can't be applied; a missing
  repo fact makes the project's cell Needs setup and names the fact.
- **Checks:** a `pr-required` failure fails `factory / gate`; a `pr` failure is reported and passes; `main`
  failures raise `ci_red`.
- **Trust:** a streak reaching `trust_after` proposes once; trusting sets `trusted_since`; a miss after that date
  makes the checkpoint ask with no write; floors ask even when trusted or set to `never`.
- **Recurring:** reconcile turns `[recurring.*]` into schedules (new ones paused) and removes the schedules of
  deleted entries; an unchanged schedule is left alone.
- **Desk:** apply lists every loosening change; floors have no remove control; each state is a word and a shape;
  no page shows a task, a PR waiting or a checkpoint question (D-13).
- **VISION:** the default lifecycle's stages match the "How work flows" table.

## Out of scope

- Running the lifecycle in the engine (F-12) and the decision queue (F-13).
- Writing new skills on the Desk: they are code with evals, landed by PR (D-12).
- Editing repo facts on the Desk: they change with the code.
- Budgets and cost per stage: F-33 and F-31 (`budget_minutes` is the key this design uses).
- CI runners other than GitHub Actions.
- Policy edits from other laptops' Desks (F-06): the Desk runs on the home machine, and its PRs are the only way
  the policy changes.
