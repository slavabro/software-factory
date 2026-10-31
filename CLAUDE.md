# CLAUDE.md

The software factory: the system that turns the principal's ideas into shipped code, in every repo and on every
laptop. Direction: docs/VISION.md; work: docs/BACKLOG.md. Projects consume it; nothing
here may name a project's domain, paths or people.

## Rules

1. **Portable.** Python 3.11+ standard library only; POSIX `sh` for scripts; no absolute home paths (use `~` or
   resolve from the script). Everything must run on a fresh laptop after `install.sh` and in GitHub Actions.
2. **Generic.** A project-specific rule belongs in that project's `.skillcheck.toml` or skills, not in a check here.
   Every check is configurable (severity override, budgets) and has a test in `tests/`.
3. **One owner per fact.** Skills here point at docs and code; they don't restate them. Rule sources live in
   `plugins/factory/skills/skills-review/references/rules.md`; `checks.RULES` is the code-side list — keep them in step.
4. **Silent when healthy.** Hooks print nothing unless something is wrong, never fail a session, and stay under 1 s.
5. **Red-green safe.** No red-vs-green as the only signal in any output (the user is red-green colour-deficient).
6. **No AI attribution** in commits, PRs or files.
7. **Land through a PR**; CI (`.github/workflows/ci.yml`) must be green.

## Check before you push

```sh
python3 -m unittest discover -s tests
PYTHONPATH=plugins/factory python3 -m skillcheck . --strict
claude plugin validate --strict . && claude plugin validate --strict plugins/factory
```

## Adding

- **A check:** a `RULES` entry + method in `plugins/factory/skillcheck/checks.py`, a test in `tests/test_skillcheck.py`,
  a row in the rules reference with its source.
- **A skill for every repo:** `plugins/factory/skills/<name>/SKILL.md` (invoked as `/factory:<name>`).
- **A user skill that must keep a bare `/name` or fixed paths:** `home/skills/<name>/` (install.sh links it).
- **A CLI:** `home/bin/<name>` (POSIX sh, resolves the repo from its own real path).
