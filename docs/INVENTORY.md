# Inventory: what lives where

A survey of the agent environment on 2026-10-31. A thing belongs in the factory when it is the same across repos or
laptops. It stays in a project when it encodes that project's domain or its live infrastructure.

## Moved into the factory (v0.1)

| Thing | Was | Now |
|-------|-----|-----|
| Skill linter | — (new) | `plugins/factory/skillcheck/` + SessionStart hook + `action.yml` |
| Skills review workflow | one-off session in FamilyOffice | `/factory:skills-review` |
| Fleet tower skill + `fleet.py` | `~/.claude/skills/fleet/` (untracked, backup in `~/.claude/backups/`) | `home/skills/fleet/`, symlinked back; Telegram rules split into a reference that loads only when Telegram is on |
| `tower` launcher | `~/.local/bin/tower` (untracked) | `home/bin/tower`, symlinked back |

## Stays where it is

| Thing | Where | Why |
|-------|-------|-----|
| Domain reviewer skills (architecture, DAL, Temporal, LLM, wealth, frontend, Tufte, testing) | FamilyOffice `.claude/skills/` | They encode FamilyOffice's rules; they point at its CLAUDE.md files |
| Task workflow skills (`/design-task` … `/complete-feature`, `/land`, `/review-code`) | FamilyOffice | Bound to `scripts/tasks.py`, its CI jobs and review gate. Generic shape is a candidate below |
| `.claude/fleet-brief.md` | FamilyOffice main checkout (untracked) | Per-repo technical notes the tower appends to briefs |
| Orca `orca-cli` / `orchestration` skills | `~/.agents/skills/` | Vendor stubs that load guides from the Orca binary; versioned with Orca |
| Anthropic synced skills | `~/.claude/skills/synced/` | Managed by claude.ai sync |
| `~/.claude/settings.json` hooks (Orca hook relay, `notifyutil`) and status line | user settings | Written by Orca and per-machine apps; copying them would fight their installers |
| Auto memory | `~/.claude/projects/*/memory/` | Per project and per user by design |
| Prod deployer, backups, `.env*` | FamilyOffice / `~/FamilyOffice-prod` | Live infrastructure, not tooling |

## Next candidates (ordered by payoff)

1. **Skill evals.** `claude plugin eval` cases under `plugins/factory/evals/`: does `/factory:skills-review` trigger on
   "audit my skills", and stay quiet on "review this PR"? Then the same harness for each project's workflow skills.
2. **Generic landing + review gate.** `/land` and `/review-code --gate` minus FamilyOffice paths: lenses, consolidation
   and the PR-body block are generic; the per-path skill table and docs-only globs become repo config.
3. **Task system.** `scripts/tasks.py` (front matter as the record, generated tables, IDs unique across worktrees) is
   project-agnostic; package it with its CI checks.
4. **Hook pack.** PreToolUse guards as a plugin: refuse `git push` to `main`, raw `pytest` where a tier script exists,
   `git stash` in shared worktrees — enforced instead of advised (CLAUDE.md is advisory).
5. **Worktree bootstrap.** One command that gives a fresh Orca worktree its venv / node_modules links and `.env` from
   the example (today a memory note).
6. **New-repo template.** CLAUDE.md skeleton (≤ 200 lines, critical rules, landing), `.skillcheck.toml`, the PR
   workflow, `make ci` / `make skills-check` conventions.
7. **Context-cost report.** `claude plugin details` + skillcheck sizes → which skills cost the most per session.
