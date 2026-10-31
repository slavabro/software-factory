# Backlog

Direction: [VISION.md](VISION.md). Inventory of what exists and where: [INVENTORY.md](INVENTORY.md).
Until the factory has its own task system (F-03), this file is the record: one row per task, newest decisions in
the Status column. Each task becomes a spec under `docs/tasks/` when work starts.

## Now

| ID | Task | Why | Status |
|----|------|-----|--------|
| F-01 | **Dependency scout agent** — see spec below | Upgrades, security fixes and new capabilities are found by reading release notes, not by luck | open — spec ready, decisions below |
| F-02 | **Factory PR ownership** — who reviews and merges factory PRs (the tower with the same gate, or the principal) and whether Dependabot can bump the private action pin in consumer repos | Factory changes need the same landing discipline as product code | open — separate worktree, principal's call |
| F-03 | **Task system as a factory product** — package FamilyOffice's `scripts/tasks.py` (front matter as the record, generated tables, IDs unique across worktrees, CI checks) for any repo, and use it here | Every stage of the pipeline reads and writes tasks | open |

## Next — the pipeline

| ID | Task | Why |
|----|------|-----|
| F-10 | **Intake + shaping skill** — idea in plain words → repo task → shaping questions → split into small vertical tasks with dependencies | The principal's only required input becomes the idea |
| F-11 | **Generic design → build → review → land pipeline** — today's FamilyOffice workflow skills with repo specifics moved into a repo config (`factory.toml`: test tiers, gate rules, task system, domain reviewers) | Reuse in every repo; one place to improve |
| F-12 | **Orchestrator** — runs a shaped task graph through the pipeline with agents in worktrees (Orca), parallel where deps allow; feeds the tower's decision queue | Removes the principal from step-driving |
| F-13 | **Decision queue** — one list of what needs the principal, with options and a recommendation, on terminal and phone (extends the tower's needs-you queue) | Decisions out, nothing else |
| F-14 | **Skill trigger evals** — `claude plugin eval` cases for every factory and repo skill: fires when it should, stays quiet otherwise | Skills are the factory's machinery; test them like code |
| F-15 | **Hook pack** — PreToolUse guards that block instead of advise: push to `main`, raw `pytest` where a tier script exists, `git stash` in shared worktrees, edits to generated tables | Rules are code |
| F-16 | **Learning loop** — when the principal corrects an agent, propose the rule that would have prevented it (banned pattern, skill line, hook) as a PR | Mistakes happen once |

## Later

| ID | Task | Why |
|----|------|-----|
| F-20 | Worktree bootstrap — one command for venv / node_modules links and `.env` from the example | Every new agent pays this today |
| F-21 | New-repo template — CLAUDE.md skeleton, `.skillcheck.toml`, PR + Skills workflows, `make ci` conventions | A new repo starts inside the factory |
| F-22 | Context-cost report — `claude plugin details` + skillcheck sizes → which skills cost most per session | Keep the always-on cost visible |
| F-23 | Other upkeep scouts — CI health (flaky tests, slow jobs), docs drift beyond skills, dead code | Same pattern as F-01 |

---

## F-01 spec — Dependency scout agent

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

**Decisions for the principal before building.**
1. **Where it runs:** a scheduled cloud routine (`/schedule`, runs without the laptop) vs a local job that spawns an
   agent through the tower (sees Orca, uses the laptop's auth).
2. **Cadence:** weekly digest + immediate run on a security advisory, or monthly.
3. **Autonomy at start:** report-only for the first runs, then safe PRs, then (earned) auto-merge of patch-level
   bumps through the gate — or safe PRs from day one.
4. **Scope:** FamilyOffice only first, or every repo in `~/Dev` that has a factory config.

**Done when:** a run on FamilyOffice produces a digest, at least one grouped upgrade PR that passes CI, tasks for the
risky items and inbox ideas for the opportunities — with every claim linked to a release note.
