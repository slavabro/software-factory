# The task system (`factory task`)

Every task is one Markdown spec; its front matter is the record. `docs/BACKLOG.md` and `docs/COMPLETED.md` hold
tables generated from those records, so two branches never edit the same table line. Code:
`plugins/factory/sfactory/tasks.py`; front matter reader: `sfactory/frontmatter.py` (flat YAML, no library).

## Layout

```
docs/
├── BACKLOG.md            prose + generated blocks: <!-- tasks:begin SECTION --> … <!-- tasks:end -->
├── COMPLETED.md          the `completed` block
└── tasks/
    ├── backlog/          open specs: {id}-{slug}.md, or a folder with design.md
    ├── active/           {id}-{slug}/design.md (+ design-{sub}.md per sub-task, REVIEW.md, mockup.html)
    ├── completed/        done; a parent folder moves whole
    └── closed/           not implemented, with a reason
```

`factory task init` creates this in a repo that has none (it never overwrites a file).

## The record

```yaml
---
id: "1.60"          # {priority}.{n}, sub-tasks add a letter (1.60a); never reused
title: Basis
status: open        # open | in_progress | done | closed
version: v0.2       # the BACKLOG block it sits in (inbox, icebox, or any block you add)
order: 20           # its place in that table
priority: P1        # size, deps, started, completed, reason: as the status needs them
---
```

`factory task check` says what is missing. A block name in BACKLOG.md is a valid `version`; adding a section is
adding a block.

## Lifecycle

| Command | Does |
|---------|------|
| `new --priority N --title T [--bug] [--version V] [--body-file F]` | next free ID across every branch and worktree; a `--bug` outside the inbox goes to the top of its table |
| `start ID` | In Progress; a backlog spec moves to `active/{name}/design.md` |
| `subtask ID a --title T --name SLUG` | `active/{name}/design-{slug}.md` as `{ID}a` |
| `complete ID` | done today; a parent folder with its unfinished sub-tasks moves to `completed/` |
| `close ID --reason R` | closed; moves to `closed/` |
| `set ID key=value …` | triage: version, order, size, deps, priority, reason, title |
| `show [SECTION]`, `next-id --priority N`, `exists QUERY` | read only |

## CI

- **On a PR:** `factory task check` (every spec valid, IDs unique) and `factory task check-pr --base <base>` (the
  branch did not edit a generated block or lose a task).
- **After a merge to the default branch:** `factory task render`, committed by a bot in a docs-only commit.

A repo runs these with the pinned action (`uses: slavabro/software-factory@<sha>` with `run: setup`, which puts
`factory` on PATH), so CI and laptops use one version. FamilyOffice's `task-specs.yml` and `task-tables.yml` are
the worked example.

## Config (`.factory.toml`)

`[tasks] system = "factory"` (the default) uses this system; `system = "commands"` uses the repo's own `new` /
`exists` commands instead (e.g. GitHub issues). `legacy_duplicate_ids` lists IDs that shipped twice before IDs
were checked.
