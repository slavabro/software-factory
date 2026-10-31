# Upgrade playbook

Commands per ecosystem for `/factory:upgrade`. The repo's `.factory.toml` wins wherever it says otherwise.

## Contents

- [Bumping](#bumping)
- [Reading release notes](#reading-release-notes)
- [Verifying](#verifying)
- [Deprecations](#deprecations)
- [Tasks and PRs](#tasks-and-prs)

## Bumping

| Ecosystem | Manifest change | Install |
|-----------|-----------------|---------|
| pip, `requirements*.txt` | edit `name==old` → `name==new` (keep extras and markers) | `[upgrade] install`, else `python3 -m pip install -r <file>` in the worktree's own venv |
| pip, `pyproject.toml` | edit the specifier in `[project] dependencies` the same way | as above, or the repo's lock tool if it has one |
| npm | `npm install <name>@<version>` from the manifest's directory (updates the lockfile) | `npm ci` |
| docker | edit the tag in every listed `image:` / `FROM` line, same suffix (`-alpine`, `-slim`) | rebuild if the repo has a build check |

**Fully pinned files** (every transitive dependency pinned, as `pip freeze` writes them): never bump a package
whose `required_by` names another candidate on its own — the parent pins its exact version and `pip install -r`
fails. Bump the parent(s), install with the resolver, `pip check`, then copy the resolved versions of everything
that moved back into the file (`python -m pip freeze`, filtered to the names already in it).

A worktree must never share a virtualenv or `node_modules` with another checkout when it upgrades — installing
there would change every other worktree. The repo's `[worktree] setup` creates private ones.

## Reading release notes

- GitHub: `gh api repos/<owner>/<repo>/releases --paginate --jq '.[] | {tag_name, body}'`, then keep the tags
  between current and latest. Tags may be `v1.2.3`, `1.2.3` or `<pkg>@1.2.3` (monorepos).
- CHANGELOG / HISTORY / NEWS files: fetch the raw file and read the sections for the versions in range.
- PyPI / npm pages when the project has neither.
- Note the **advisories** (GHSA / CVE IDs), **breaking** items, **deprecated** items with their replacements, and
  **new** features. Quote the line that justifies each decision in the PR body.

## Verifying

- `[verify] commands` is the repo's own gate (lint, tests, build). It must pass on the branch before landing.
- When a test fails, read whether the failure is ours (fix the code) or the dependency's regression (pin to the
  last good version in range and say why in the PR, or drop the package to a task).
- Frontend: a dependency change also needs the repo's build to pass, not only its tests.

## Deprecations

- `[upgrade] deprecation_check` commands produce output with warnings visible (e.g. pytest with warnings shown,
  a build log). Pipe it: `<command> 2>&1 | factory upgrade deprecations` — it exits 1 while any warning comes from
  our code, and lists `file:line category: message`. For a tool run in a subfolder (`cd web && npm run build`), add
  `--base web` so its relative paths resolve.
- Fix by calling the replacement the warning or the release notes name. Never silence a warning with a filter.
- Dependency-level deprecations (`npm warn deprecated <pkg>`) become an upgrade or replacement candidate for the
  next run, or a task.

## Tasks and PRs

- Task: `factory task new --title "<title>" --body-file <file> [--priority N]` (it runs the repo's `[tasks] new`
  with every value shell-quoted). Duplicate check first: `factory task exists "<title start>"`.
  Title: `Upgrade <name> <from> → <to>` (risky) or `Adopt <feature> from <name> <version>` (opportunity).
- PR titles: `deps(<ecosystem>): <n> safe upgrades`, `deps: <name> <from> → <to>`, `security: <name> <to> (<advisory>)`.
- One branch per PR (`$FACTORY_BRANCH-<group>`), cut from the default branch, so each PR can land alone.
