# software-factory

The reusable half of how slavabro's agents work: the tools, skills and checks that are the same in every repo and on
every laptop. Each project keeps only what is specific to it (its domain reviewer skills, its task workflow, its
`.skillcheck.toml`).

## What's here

| Path | What | Installed as |
|------|------|--------------|
| `plugins/factory/skillcheck/` | **skillcheck** — lints skills, legacy commands, subagents and CLAUDE.md files: frontmatter and description rules, size and progressive-disclosure budgets, dead paths and links, copies that will drift, repo-specific banned patterns. Python 3.11+, no dependencies | `skillcheck` on PATH; GitHub Action (`action.yml`); `pip install` (`pyproject.toml`) |
| `plugins/factory/hooks/` | SessionStart hook — runs skillcheck on the session's repo and `~/.claude/skills`; silent unless there are errors | part of the `factory` plugin |
| `plugins/factory/skills/skills-review/` | `/factory:skills-review` — the judgement half: verify every skill against the code, verdict report, apply approved fixes | part of the `factory` plugin |
| `home/skills/fleet/` | `/fleet` — Orca control tower: one session watches, relays and lands for every agent | `~/.claude/skills/fleet` |
| `home/bin/tower` | starts the tower session (Sonnet, Telegram channel) | `~/.local/bin/tower` |
| `docs/INVENTORY.md` | what moved here, what stays per project, what to build next | — |

Rule catalog with sources: [plugins/factory/skills/skills-review/references/rules.md](plugins/factory/skills/skills-review/references/rules.md).

## Install on a laptop

```sh
git clone git@github.com:slavabro/software-factory.git ~/Dev/software-factory
~/Dev/software-factory/install.sh
```

It symlinks, so `git pull` updates everything; new Claude Code sessions pick it up. Alternative for a machine
without a clone: `claude plugin marketplace add slavabro/software-factory` then
`claude plugin install factory@software-factory` (plugin only — no `/fleet`, no PATH tools).

## Use in a repo

- Local: `skillcheck --strict` from the repo root (a `make skills-check` target is the convention).
- CI, as a PR check (the repo must be allowed to use this private repo's actions — already set for slavabro's repos):

  ```yaml
  - uses: actions/checkout@v4
  - uses: slavabro/software-factory@main
  ```

- Config: `.skillcheck.toml` at the repo root — `[budgets]`, `[paths] allow/ignore`, `[scan] include` (extra docs such as a testing guide), `[[banned]]` patterns with the
  true replacement, `[severity]` overrides, `fail_on`. `skillcheck --rules` lists rule IDs.

## Develop

```sh
python3 -m unittest discover -s tests
PYTHONPATH=plugins/factory python3 -m skillcheck . --strict
claude plugin validate --strict . && claude plugin validate --strict plugins/factory
```
