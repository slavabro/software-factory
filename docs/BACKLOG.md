# Backlog

Direction: [VISION.md](VISION.md). Inventory of what exists and where: [INVENTORY.md](INVENTORY.md).
Until the factory has its own task system (F-03), this file is the record: one row per task, newest decisions in
the Status column. Each task becomes a spec under `docs/tasks/` when work starts.

The factory is the company, each repo a product it makes (VISION → "What the factory provides"). Phases run in
order, foundation before what stands on it (CLAUDE.md rule 1: no stopgaps, no technical debt); tasks
inside a phase can run in parallel. "From FamilyOffice" means the working version exists there and moves here as a generic piece, leaving the project-specific part behind.

## Decisions

| ID | Decision | Recommendation |
|----|----------|----------------|
| D-1 | **Portability rule scope.** CLAUDE.md rule 2 (standard library only) — does it cover only the factory's tools and hooks, or also the shared runtime (F-42), which needs Temporal, LiteLLM and Pydantic? | **Decided 2026-11-11, updated 2026-11-13:** the rule covers the tools, skills, hooks and the CI action; `runtime/` and `engine/` are the two folders with their own dependencies (Temporal, LiteLLM, Pydantic), each tested in its own CI job |
| D-2 | **Where the shared runtime lives** — this repo or its own repo | **Decided 2026-11-13** (replaces 2026-11-11's own repo): this repo, in `runtime/`, as the installable package `factory-runtime`; products pin it by the factory's version tag (`git+…/software-factory@vX.Y#subdirectory=runtime`), so tools and runtime always match, and the upgrade capability (F-01) moves the pin |
| D-3 | **Who merges factory PRs** (F-02) | **Decided 2026-11-11:** the tower merges with the same gate; ASK for changes to hooks, guards and the gate itself |
| D-4 | **Release model for the factory** (F-05) | **Decided 2026-11-11:** a separate install copy on each laptop that follows `main` (updates itself about hourly), so a change is live only after it merges |
| D-5 | **Where the factory's engine code lives** (F-50). Its workflows need the Temporal SDK, and D-1 keeps this repo standard-library only | **Decided 2026-11-12:** an `engine/` package in this repo with its own dependencies, built on `runtime/` (F-42); rule 2 keeps covering the tools, skills, hooks and the CI action, which must run on a bare laptop |

## Phase 0 — the factory runs on itself

| ID | Task | Why | Status |
|----|------|-----|--------|
| F-01 | **Upgrade capability** (dependency scout) — see spec below | Upgrades, security fixes and new capabilities are found by reading release notes, not by luck | v0.1 built: `factory upgrade …`, `/factory:upgrade`, `factory run/schedule`; first scheduled run on FamilyOffice next |
| F-02 | **Factory PR ownership** — who reviews and merges factory PRs and whether Dependabot can bump the private action pin in consumer repos | Factory changes need the same landing discipline as product code | decided (D-3) — to build; the gate must read the repo's "CI passed" check name from `.factory.toml` (today it waits for FamilyOffice's `CI OK`, so factory PRs stay WAIT) |
| F-03 | **Task system as a factory product** — package FamilyOffice's `scripts/tasks.py` (front matter as the record, generated tables, IDs unique across worktrees, CI checks) for any repo | Every stage of the pipeline reads and writes tasks | built: `factory task` ([tasks.md](tasks.md)); FamilyOffice runs on it; this backlog moves onto it with F-04 |
| F-04 | **Dogfood** — this repo gets its own `.factory.toml`, a scheduled upgrade run, its tasks in F-03's system, and every change through the gate the factory gives products | The factory is held to its own standard; what hurts here gets fixed for every product | open |
| F-05 | **Release channel** — the laptop runs an install copy that follows `main` (`factory self-update`); development happens in a separate clone and worktrees | Today `install.sh` links the working checkout, so unmerged edits are live in every session | decided (D-4) — to build |
| F-06 | **Several laptops** — `factory doctor` checks a laptop's install, tools and logins; the home for schedules and unattended runs is one setting (`~/.config/software-factory`, default: the laptop where they are installed today), so two laptops never run the same job and moving to another laptop or a server is a config change; the tower on each laptop sees that laptop's agents | Same factory everywhere, no double runs |

## Phase 1 — foundation

The base everything later stands on, built the way it is meant to end up. Design and build order:
[designs/foundation.md](designs/foundation.md).

| ID | Task | Why |
|----|------|-----|
| F-40 | **House stack** — the factory's defaults, written once: Temporal for anything that runs in the background, waits for a person or retries; AI work through F-41 (LiteLLM underneath); one default database and chart library; opt-outs stated in `.factory.toml`; the new-repo template (F-21) starts from it | Agents get good at one stack; reviewers and the runtime work everywhere |
| F-42 | **Shared runtime package** (`runtime/` in this repo, D-2; products move to new versions through F-01's upgrade PRs) — the building blocks a product installs: YAML process engine on Temporal, support desk core, notifications (Telegram and others), the decision queue's data, agent jobs; FamilyOffice becomes its first consumer (depends on F-40; the Temporal core and process engine come first, extracted from FamilyOffice `common/temporal/` and `workflows/`). **R1 built:** `runtime/factory_runtime/temporal` (settings, client, converter, sandbox, activity helpers, run history, signal-with-start) and `factory_runtime.testing`; FamilyOffice binding next | Build each block once; every product gets the fixes |
| F-41 | **AI access, built the FamilyOffice way** — every piece of AI work is a typed job that runs on the backend config names: `worker` (the principal's own agent session — Claude Code or Codex on their subscription — claims the job and submits the answer), `api` (LiteLLM, pay per call, model tiers with failover, prompt caching) or `off`. Default "subscription first, then pay per call": a job no session answers in time, or one routed while a plan usage window is near its limit, follows its fallback. Budgets and an on/off switch for unattended spending, cost per product and job, every answer checked against the job's output model so behaviour holds on any backend. The principal's session can also be the operator, through the product's CLI. Switching is configuration, never code (from FamilyOffice `common/llm/`, `common/agent_jobs/`, `docs/architecture/agent-operated-office.md`) | No product rebuilds model access; one place for keys, limits and cost; the subscription does the work first |
| F-50 | **Factory engine on Temporal** — the factory's own Temporal server and worker on the home machine (F-06), its own namespace; every factory process is a workflow: Temporal Schedules for timed runs, signals for events (main's CI red, a failed deploy, an incident, a merged PR; the tower sends them), pause-and-wait for the principal's OK, one run per team per repo, history and errors in Temporal's UI; F-01's upgrade run moves onto it from launchd. Code location: D-5 (depends on F-40, F-42) | The base every standing team, the pipeline and the orchestrator run on, built once and right |

## Phase 2 — move what is already generic

| ID | Task | Why |
|----|------|-----|
| F-11 | **Engineering pipeline** — FamilyOffice's `/design-task` → `/review-design` → `/compact-design` → `/implement-feature` → `/sync-docs` → `/complete-feature` → `/land`, plus `/review-code`, `/bug-report`, `/debug-fix`, `/challenge`, `git-workflow`; project specifics (test tiers, gate rules, domain reviewers) move into `.factory.toml` | Reuse in every repo; one place to improve |
| F-30 | **Practice guides** — split each FamilyOffice reviewer into a generic factory skill and the project's notes: data access (schema, migrations, sessions, idempotent writes), Temporal workflows, LLM integration, frontend, testing patterns, data presentation (Tufte) | Good practice is the same in every product; only the domain facts differ |
| F-31 | **Model and cost policy** — model per stage in `.factory.toml` (Opus for shaping and design, Sonnet for building and review, Haiku for triage and log reading); tokens and cost logged for every run and stage; a weekly cost report, including what each skill costs every session (absorbs the old F-22) | Choose models by evidence; keep spend visible |

## Phase 3 — the departments

| ID | Task | Why |
|----|------|-----|
| F-10 | **Product** — idea in plain words → recorded verbatim → shaping questions → problem, users, success measure → small vertical tasks with dependencies; keeps the repo's roadmap current (generalises `/roadmap-add`) | The principal's only required input becomes the idea |
| F-17 | **UX design** — flows, screens and a mockup per user-facing task, reviewed against the repo's design rules and the data-presentation guide | Products get designed, not only engineered |
| F-18 | **QA** — runs the app (the `run` pattern) and checks each acceptance criterion end to end before landing | Tests prove code; QA proves the feature |
| F-19 | **Release and deploy** — a `[deploy]` stage in `.factory.toml` (environments, deploy, health, rollback commands); staging → health → production → watch → roll back; production stays ASK until the record is good (from FamilyOffice: `deployer.py`, `smoke_prod.sh`) | One way to ship every product |
| F-24 | **Support** — the support desk as a department: incident log, runbook first, a support agent that investigates with read-only tools, actions only from an allowlist, fix PRs through the normal gate, escalation only with a diagnosis and a recommendation; the product keeps its runbooks and allowed actions (from FamilyOffice task 0.38) | Every live product gets watched and mended the same way |
| F-23 | **Standing teams** — system-wide improvers that are not tied to a feature, each a skill plus a Temporal workflow on F-50, woken by a schedule or an event: **QA** (untested code, flaky or slow tests, bugs fixed without a test), **DevOps** (slow or flaky CI, deploy health, images, CI minutes), **upgrades** (F-01), **security** (secrets, permissions, vulnerable packages), **code health** (dead code, duplication, oversized files, docs drift), **factory improver** (where agents fail or get corrected, what costs the most tokens; feeds F-16). Each picks the top few items per run, keeps a notebook of what it tried and what was rejected, stays inside its budget and the plan limits (F-41), and outputs small PRs through the normal gate or tasks (depends on F-50, F-02) | Quality improves all the time, not only when a feature touches the code |

## Phase 4 — the factory runs the work

| ID | Task | Why |
|----|------|-----|
| F-12 | **Orchestrator** — on the engine (F-50), next to the standing teams: runs a shaped task graph through the departments as Temporal workflows: each stage an activity, agents in worktrees (Orca), parallel where dependencies allow, surviving restarts, pausing for decisions | Removes the principal from step-driving |
| F-13 | **Decision queue** — one list of what needs the principal, with options and a recommendation, on terminal and phone; the orchestrator's pause point (extends the tower's needs-you queue) | Decisions out, nothing else |
| F-16 | **Learning loop** — when the principal corrects an agent, propose the rule that would have prevented it (banned pattern, skill line, hook) as a PR | Mistakes happen once |

## Alongside any phase

| ID | Task | Why |
|----|------|-----|
| F-14 | **Skill trigger evals** — `claude plugin eval` cases for every factory and repo skill: fires when it should, stays quiet otherwise | Skills are the factory's machinery; test them like code |
| F-15 | **Hook pack** — PreToolUse guards that block instead of advise: push to `main`, raw `pytest` where a tier script exists, `git stash` in shared worktrees, edits to generated tables | Rules are code |
| F-20 | **Worktree bootstrap** — one command for venv / node_modules links and `.env` from the example | Every new agent pays this today |
| F-21 | **New project** — `factory new <name>` creates the repo from the template: CLAUDE.md skeleton, VISION/ROADMAP, `.factory.toml`, `.skillcheck.toml`, `docs/tasks/`, `make ci`, CI and Skills workflows, the house stack (Temporal, LiteLLM, AI access) as opt-in pieces; registers the project with the factory so schedules and the tower know it | A new repo starts inside the factory on day one |

---

## F-01 spec — Upgrade capability (dependency scout)

**Goal.** On a schedule, for each repo the factory serves: find every dependency that can move forward, read what
changed between our version and the latest, and turn it into work — safe upgrades as PRs, risky ones as tasks,
new capabilities as ideas — so the principal only sees results and decisions.

**What it covers** (each repo declares its manifests in repo config; FamilyOffice today):
- Python: `requirements.txt` (pinned `==`), `pyproject.toml`
- Frontend: `frontend/dashboard/package.json` + lockfile
- Containers: image tags in `infra/docker-compose*.yml` (Postgres, Temporal server / admin-tools / UI) and Dockerfiles
- GitHub Actions: already bumped by Dependabot — the scout only reads their release notes for opportunities
- Tooling pins: ruff, ast-grep (Makefile / CI), Node and Python versions, the software-factory pin itself

**How a run works.**
1. **Inventory** current versions from the manifests; latest versions from the registries (PyPI JSON API, `npm view`,
   Docker Hub / GHCR tags, GitHub releases). No credentials beyond read access.
2. **Read the notes** for every version between ours and the latest: GitHub releases, CHANGELOG files, PyPI/npm
   project pages. Summarise per package: security fixes (with advisory IDs), breaking changes, deprecations, new
   features, performance.
3. **Map to the code.** Grep the repo for the APIs each breaking change or deprecation touches; read the repo's
   VISION/ROADMAP/BACKLOG to spot new features that serve something planned or replace code we wrote ourselves.
4. **Classify each package:**
   - *safe* — patch/minor, no breaking change touching our usage → grouped upgrade PR, tests at the right tiers
   - *risky* — breaking change in code we use, major version, data/infra component (Postgres, Temporal) → a task in
     the repo's task system with the migration notes, never an auto PR
   - *opportunity* — a new capability worth using → an idea in the repo's inbox, with the release-note link and the
     place in our code it would change
   - *security* — any of the above with an advisory → top of the report and the principal's queue, whatever its size
5. **Report** one short digest (what moved, what was proposed, what needs a decision) to the decision queue; full
   detail in the PR/task bodies. Red-green safe, plain words.

**Rules.**
- Never merges; upgrade PRs land through the repo's normal gate (`make ci`, review gate, CI, tower).
- Respects repo policy: FamilyOffice upgrades Python/npm deps deliberately, so every upgrade is a reviewed PR,
  never a silent bump. Data components (Postgres, Temporal) are always *risky*.
- One upgrade PR per risk group per run, not one per package; skips packages with an open PR or task already.
- Read-only on registries and release notes; no tokens or private data leave the machine.

**Decided (2026-10-31, by the principal).**
1. **Generic**: a factory capability for every repo that has a `.factory.toml`, with generic skills — nothing
   repo-specific in the factory ([factory-toml.md](factory-toml.md)).
2. **Upgrades from day one** and runs the whole flow: notes → code changes → verify → no deprecations → land.
3. **Proves the code still works**: the repo's `[verify]` gate, plus `[upgrade] deprecation_check` piped through
   `factory upgrade deprecations` until no warning comes from our code.
4. **Configurable schedule** (`[upgrade] schedule`, cron); runs locally (`factory schedule install upgrade` → launchd
   / systemd / cron), and moves to another machine or runner by changing config, not code (`[agent] command`).

**Done when:** a run on FamilyOffice produces a digest, at least one grouped upgrade PR that passes CI, tasks for the
risky items and inbox ideas for the opportunities — with every claim linked to a release note.
