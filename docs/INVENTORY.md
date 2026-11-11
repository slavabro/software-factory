# Inventory: what lives where

A survey of the agent environment on 2026-10-31. A thing belongs in the factory when it is the same across repos or
laptops. It stays in a project when it encodes that project's domain or its live infrastructure.

## Moved into the factory (v0.1)

| Thing | Was | Now |
|-------|-----|-----|
| Skill linter | — (new) | `plugins/factory/skillcheck/` + SessionStart hook + `action.yml` |
| Skills review workflow | one-off session in FamilyOffice | `/factory:skills-review` |
| Task system (front matter records, generated tables, CI checks) | FamilyOffice `scripts/tasks.py` | `factory task` + `docs/tasks.md` (F-03); FamilyOffice's `scripts/tasks.py` forwards to it |
| Dependency upgrades | by hand, occasionally | `/factory:upgrade` + `factory run/schedule upgrade` (F-01) |
| Fleet tower skill + `fleet.py` | `~/.claude/skills/fleet/` (untracked, backup in `~/.claude/backups/`) | `home/skills/fleet/`, symlinked back; Telegram rules split into a reference that loads only when Telegram is on |
| `tower` launcher | `~/.local/bin/tower` (untracked) | `home/bin/tower`, symlinked back |

## Stays where it is

| Thing | Where | Why |
|-------|-------|-----|
| Domain reviewer skills (architecture, DAL, Temporal, LLM, wealth, frontend, Tufte, testing) | FamilyOffice `.claude/skills/` | Until F-30 splits them: the general practice moves here, the domain facts (and `wealth-management` whole) stay |
| Task workflow skills (`/design-task` … `/complete-feature`, `/land`, `/review-code`) | FamilyOffice | Bound to its CI jobs and review gate until F-11 |
| LLM layer, agent jobs, support desk, process engine, notifications | FamilyOffice `common/`, `org/ops/`, `workflows/` | Product code today; the generic core moves to the shared runtime (F-41, F-42, F-24) |
| Prod deployer and smoke checks | FamilyOffice `scripts/` | The generic flow becomes the deploy stage (F-19); hosts and secrets stay |
| `.claude/fleet-brief.md` | FamilyOffice main checkout (untracked) | Per-repo technical notes the tower appends to briefs |
| Orca `orca-cli` / `orchestration` skills | `~/.agents/skills/` | Vendor stubs that load guides from the Orca binary; versioned with Orca |
| Anthropic synced skills | `~/.claude/skills/synced/` | Managed by claude.ai sync |
| `~/.claude/settings.json` hooks (Orca hook relay, `notifyutil`) and status line | user settings | Written by Orca and per-machine apps; copying them would fight their installers |
| Auto memory | `~/.claude/projects/*/memory/` | Per project and per user by design |
| Backups, `.env*`, the prod host | FamilyOffice / `~/FamilyOffice-prod` | Live infrastructure, not tooling |

## Next

What to build next, and why: [BACKLOG.md](BACKLOG.md) (direction: [VISION.md](VISION.md)).
