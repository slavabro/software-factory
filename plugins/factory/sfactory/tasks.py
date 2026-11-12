"""The factory's task system: each spec's front matter is the record; the BACKLOG / COMPLETED tables are views.

Every task row in docs/BACKLOG.md and docs/COMPLETED.md is generated from the front matter of one spec file
under docs/tasks/. A branch changes only its own task's files, so two branches never edit the same table line.
On the default branch the tables are regenerated after every merge (`factory task render`, run by CI); branches
leave them alone (`check-pr` enforces that). Layout and lifecycle: docs/tasks.md in software-factory.

    factory task init                         create docs/tasks/ and the two views in a repo that has none
    factory task show [SECTION]               print the current tables (writes nothing)
    factory task render [--check]             rewrite the generated blocks (the bot on the default branch)
    factory task check                        validate every spec's front matter
    factory task check-pr --base REF          fail if this branch edited a generated block or lost a task
    factory task next-id --priority N         next free ID, seen across every branch and worktree
    factory task new --priority N --title T [--bug] [--version V] [--body-file F]
    factory task exists QUERY                 exit 0 if an open spec or an open PR mentions QUERY
    factory task start ID                     to In Progress; a backlog spec moves to active/
    factory task subtask PARENT SUFFIX --title T --name SLUG
    factory task complete ID                  done today; a finished parent folder moves to completed/
    factory task close ID --reason R          closed; a backlog spec moves to closed/
    factory task set ID key=value ...         triage: version, order, size, deps, priority, reason, title

Standard library only (front matter: sfactory.frontmatter). `[tasks] legacy_duplicate_ids` in .factory.toml
lists IDs that shipped twice before IDs were checked.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from . import config as config_mod
from . import frontmatter

# ---- repo invariants (not tunables) ------------------------------------------
DOCS = Path("docs")
TASKS = DOCS / "tasks"
BACKLOG_DIR, ACTIVE_DIR, COMPLETED_DIR, CLOSED_DIR = (
    TASKS / "backlog",
    TASKS / "active",
    TASKS / "completed",
    TASKS / "closed",
)
BACKLOG_MD = DOCS / "BACKLOG.md"
COMPLETED_MD = DOCS / "COMPLETED.md"
VIEW_FILES = (BACKLOG_MD, COMPLETED_MD)

# A generated block: <!-- tasks:begin SECTION --> ... <!-- tasks:end -->
BLOCK_RE = re.compile(
    r"(<!-- tasks:begin (?P<section>[\w.-]+) -->\n)(?P<body>.*?)(<!-- tasks:end -->)", re.S
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ORDER_STEP = 10
PARKED_MARK = "📐 "  # an open task whose design already sits in active/
EMPTY_TABLE = "*None.*\n"


class Status(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CLOSED = "closed"


class Section(StrEnum):
    """Sections with their own columns. Version tables (`v0.2`, `playground`, …) are open: any block
    name in BACKLOG.md is a valid `version`, so a new version needs no code change."""

    IN_PROGRESS = "in-progress"  # from status
    CLOSED = "closed"  # from status
    COMPLETED = "completed"  # from status
    INBOX = "inbox"  # a version value
    ICEBOX = "icebox"  # a version value


STATUS_SECTIONS = (Section.IN_PROGRESS, Section.CLOSED, Section.COMPLETED)

# Which folders may hold a task in each status. A task folder moves whole, so a subtask finished or
# dropped before its parent rides along: done or closed subtasks in active/, completed/ and closed/.
FOLDER_STATUSES = {
    BACKLOG_DIR: {Status.OPEN},
    ACTIVE_DIR: {Status.OPEN, Status.IN_PROGRESS, Status.DONE, Status.CLOSED},
    COMPLETED_DIR: {Status.DONE, Status.CLOSED},
    CLOSED_DIR: {Status.CLOSED, Status.DONE},
}

FIELDS = (
    "id",
    "title",
    "status",
    "version",
    "order",
    "priority",
    "size",
    "deps",
    "started",
    "completed",
    "reason",
)
SETTABLE = ("title", "version", "order", "priority", "size", "deps", "reason")


class TaskError(Exception):
    """A spec or a command the index cannot accept; the message says what to fix."""


# ---- model ----------------------------------------------------------------------


@dataclass
class Task:
    path: Path  # relative to the repo root
    id: str
    title: str
    status: str
    version: str | None = None
    order: int | None = None
    priority: str | None = None
    size: str | None = None
    deps: str | None = None
    started: str | None = None
    completed: str | None = None
    reason: str | None = None
    body: str = field(default="", repr=False)

    @property
    def section(self) -> str | None:
        if self.status == Status.IN_PROGRESS:
            return Section.IN_PROGRESS
        if self.status == Status.DONE:
            return Section.COMPLETED
        if self.status == Status.CLOSED:
            return Section.CLOSED
        return self.version

    @property
    def parked(self) -> bool:
        return self.status == Status.OPEN and is_under(self.path, ACTIVE_DIR)

    def front_matter(self) -> dict:
        return {k: getattr(self, k) for k in FIELDS if getattr(self, k) is not None}


def is_under(path: Path, folder: Path) -> bool:
    return path.parts[: len(folder.parts)] == folder.parts


def natural_key(task_id: str) -> tuple:
    return tuple(
        (0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.findall(r"\d+|\D+", task_id)
    )


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")


# ---- front matter ----------------------------------------------------------------


def split_front_matter(text: str) -> tuple[dict | None, str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        raise TaskError("front matter opened with --- but never closed")
    try:
        data = frontmatter.load(text[4:end])
    except frontmatter.FrontMatterError as e:
        raise TaskError(str(e)) from None
    return data, text[end + len("\n---\n") :]


def dump_front_matter(data: dict) -> str:
    return "---\n" + "".join(f"{k}: {frontmatter.scalar(data[k])}\n" for k in FIELDS if k in data) + "---\n"


def _text_field(data: dict, key: str, where: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, (bool, float)):
        raise TaskError(f'{where}: `{key}` must be text (quote it: {key}: "{value}")')
    return str(value)


def parse_task(path: Path, text: str) -> Task | None:
    where = str(path)
    data, body = split_front_matter(text)
    if data is None:
        return None
    unknown = set(data) - set(FIELDS)
    if unknown:
        raise TaskError(f"{where}: unknown front matter keys {sorted(unknown)}")
    order = data.get("order")
    if order is not None and (not isinstance(order, int) or isinstance(order, bool)):
        raise TaskError(f"{where}: `order` must be an integer")
    values = {k: _text_field(data, k, where) for k in FIELDS if k != "order"}
    for key in ("id", "title", "status"):
        if not values[key]:
            raise TaskError(f"{where}: missing `{key}`")
    return Task(path=path, order=order, body=body, **values)


def write_task(task: Task, root: Path) -> None:
    (root / task.path).write_text(dump_front_matter(task.front_matter()) + task.body)


# ---- loading + validation ----------------------------------------------------------


def spec_files(root: Path) -> list[Path]:
    return sorted(p.relative_to(root) for p in (root / TASKS).rglob("*.md"))


def load_tasks(root: Path) -> list[Task]:
    tasks = []
    for rel in spec_files(root):
        task = parse_task(rel, (root / rel).read_text())
        if task is not None:
            tasks.append(task)
    return tasks


def must_have_front_matter(rel: Path) -> bool:
    """Every spec in backlog/ and closed/ is a task: top-level files and a folder's design.md."""
    for folder in (BACKLOG_DIR, CLOSED_DIR):
        if is_under(rel, folder):
            depth = len(rel.parts) - len(folder.parts)
            return depth == 1 or (depth == 2 and rel.name == "design.md")
    return False


def legacy_duplicate_ids(root: Path) -> frozenset[str]:
    return frozenset(config_mod.load(root).tasks_legacy_duplicate_ids)


def validate(tasks: list[Task], sections: set[str], root: Path) -> list[str]:
    errors = []
    legacy = legacy_duplicate_ids(root)
    seen: dict[str, Path] = {}
    for t in tasks:
        where = str(t.path)
        if t.status not in {s.value for s in Status}:
            errors.append(f"{where}: status `{t.status}` is not one of {', '.join(Status)}")
            continue
        for key in ("started", "completed"):
            value = getattr(t, key)
            if value is not None and not DATE_RE.match(value):
                errors.append(f"{where}: `{key}` must be YYYY-MM-DD, got `{value}`")
        if t.status == Status.OPEN:
            if not t.version:
                errors.append(f"{where}: an open task needs `version` (a BACKLOG section)")
            elif t.version in STATUS_SECTIONS:
                errors.append(f"{where}: version `{t.version}` is set by status, not by hand")
            elif t.version not in sections:
                errors.append(
                    f"{where}: version `{t.version}` has no tasks:begin block in BACKLOG.md"
                )
            if t.order is None:
                errors.append(f"{where}: an open task needs `order` (its place in the table)")
            if t.version == Section.ICEBOX and not t.reason:
                errors.append(f"{where}: an iced task needs `reason` (the Why iced column)")
            if t.version == Section.INBOX and not t.priority:
                errors.append(f"{where}: an Inbox task needs `priority`")
        if t.status == Status.IN_PROGRESS and not t.started:
            errors.append(f"{where}: an in-progress task needs `started`")
        if t.status == Status.DONE and not t.completed:
            errors.append(f"{where}: a done task needs `completed`")
        if t.status == Status.CLOSED:
            if not t.reason:
                errors.append(f"{where}: a closed task needs `reason`")
            if t.order is None:
                errors.append(f"{where}: a closed task needs `order`")
        for folder, allowed in FOLDER_STATUSES.items():
            if is_under(t.path, folder) and t.status not in allowed:
                errors.append(f"{where}: status `{t.status}` cannot live in {folder}/")
        if t.id in seen and t.id not in legacy:
            errors.append(
                f"{where}: ID {t.id} is already used by {seen[t.id]} — take `factory task next-id`"
            )
        seen.setdefault(t.id, t.path)
    have = {t.path for t in tasks}
    for rel in spec_files(root):
        if must_have_front_matter(rel) and rel not in have:
            errors.append(f"{rel}: spec has no front matter (every backlog/closed spec is a task)")
    return errors


# ---- rendering ---------------------------------------------------------------------


def _cell(text: str | None) -> str:
    return (text or "—").replace("|", "\\|")


def _link(task: Task, view: Path) -> str:
    rel = Path(*[".."] * (len(view.parent.parts) - len(DOCS.parts))) / task.path.relative_to(DOCS)
    return f"[spec]({rel.as_posix()})"


def _title(task: Task) -> str:
    return _cell((PARKED_MARK if task.parked else "") + task.title)


def section_rows(section: str, tasks: list[Task]) -> tuple[list[str], list[Task]]:
    """The column headers and ordered tasks of one generated table."""
    mine = [t for t in tasks if t.section == section]
    if section == Section.IN_PROGRESS:
        return ["Started"], sorted(mine, key=lambda t: (t.started, natural_key(t.id)))
    if section == Section.COMPLETED:
        by_id = sorted(mine, key=lambda t: natural_key(t.id))
        return ["Completed"], sorted(by_id, key=lambda t: t.completed, reverse=True)
    ordered = sorted(mine, key=lambda t: (t.order, natural_key(t.id)))
    if section == Section.CLOSED:
        return ["Reason"], ordered
    if section == Section.INBOX:
        return ["Priority"], ordered
    if section == Section.ICEBOX:
        return ["Why iced"], ordered
    return ["Size", "Deps"], ordered


def _cells(section: str, task: Task) -> list[str]:
    if section == Section.IN_PROGRESS:
        return [task.started]
    if section == Section.COMPLETED:
        return [task.completed]
    if section in (Section.CLOSED, Section.ICEBOX):
        return [_cell(task.reason)]
    if section == Section.INBOX:
        return [_cell(task.priority)]
    return [_cell(task.size), _cell(task.deps)]


def render_table(section: str, tasks: list[Task], view: Path) -> str:
    extra, rows = section_rows(section, tasks)
    if not rows:
        return EMPTY_TABLE
    header = ["ID", "Task", *extra, "Spec"]
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("-" * (len(h) + 2) for h in header) + "|",
    ]
    for t in rows:
        lines.append(
            "| " + " | ".join([t.id, _title(t), *_cells(section, t), _link(t, view)]) + " |"
        )
    return "\n".join(lines) + "\n"


def blocks(text: str) -> dict[str, str]:
    return {m["section"]: m["body"] for m in BLOCK_RE.finditer(text)}


def render_view(text: str, tasks: list[Task], view: Path) -> str:
    return BLOCK_RE.sub(
        lambda m: m.group(1) + render_table(m["section"], tasks, view) + m.group(4), text
    )


def all_sections(root: Path) -> set[str]:
    found: set[str] = set()
    for view in VIEW_FILES:
        found |= set(blocks((root / view).read_text()))
    return found


def render_all(root: Path, write: bool = True) -> list[Path]:
    """Regenerate every block; returns the views whose text changed."""
    tasks = load_tasks(root)
    errors = validate(tasks, all_sections(root), root)
    if errors:
        raise TaskError("\n".join(errors))
    changed = []
    for view in VIEW_FILES:
        old = (root / view).read_text()
        new = render_view(old, tasks, view)
        if new != old:
            changed.append(view)
            if write:
                (root / view).write_text(new)
    return changed


# ---- git ---------------------------------------------------------------------------


def git(*args: str, root: Path, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise TaskError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def move(src: Path, dst: Path, root: Path) -> None:
    """git mv when tracked (history follows), else a plain rename."""
    (root / dst).parent.mkdir(parents=True, exist_ok=True)
    tracked = git("ls-files", "--error-unmatch", str(src), root=root, check=False)
    if tracked.strip():
        git("mv", str(src), str(dst), root=root)
    else:
        (root / src).rename(root / dst)


def ids_in_paths(paths: list[str]) -> set[str]:
    """Task IDs named by spec paths: every spec file and folder starts with `<id>-`."""
    found = set()
    for p in paths:
        parts = Path(p).parts
        if len(parts) >= 4 and parts[:2] == TASKS.parts:
            name = parts[3]
            found.add(name.split("-", 1)[0].removesuffix(".md"))
    return found


def ids_elsewhere(root: Path) -> set[str]:
    """IDs on every local and remote branch and in every other worktree's files."""
    found: set[str] = set()
    git(
        "fetch", "--quiet", "--prune", "origin", root=root, check=False
    )  # best effort: offline is fine
    refs = git(
        "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes", root=root, check=False
    )
    for ref in refs.split():
        listing = git("ls-tree", "-r", "--name-only", ref, "--", str(TASKS), root=root, check=False)
        found |= ids_in_paths(listing.splitlines())
    for line in git("worktree", "list", "--porcelain", root=root, check=False).splitlines():
        if line.startswith("worktree "):
            tree = Path(line.removeprefix("worktree "))
            if (tree / TASKS).is_dir():
                found |= ids_in_paths([str(p.relative_to(tree)) for p in (tree / TASKS).rglob("*")])
                for t in load_tasks_quiet(tree):
                    found.add(t.id)
    return found


def load_tasks_quiet(root: Path) -> list[Task]:
    try:
        return load_tasks(root)
    except (TaskError, OSError):
        return []


# ---- commands ----------------------------------------------------------------------


def find_task(tasks: list[Task], task_id: str) -> Task:
    matches = [t for t in tasks if t.id == task_id and t.status != Status.DONE] or [
        t for t in tasks if t.id == task_id
    ]
    if not matches:
        raise TaskError(f"no task with ID {task_id} (front matter `id:` under {TASKS}/)")
    if len(matches) > 1:
        raise TaskError(f"ID {task_id} is ambiguous: {', '.join(str(t.path) for t in matches)}")
    return matches[0]


def next_order(tasks: list[Task], section: str) -> int:
    return max((t.order or 0 for t in tasks if t.section == section), default=0) + ORDER_STEP


def next_id(priority: int, root: Path, include_git: bool = True) -> str:
    taken = {t.id for t in load_tasks(root)} | ids_in_paths([str(p) for p in spec_files(root)])
    if include_git:
        taken |= ids_elsewhere(root)
    prefix = f"{priority}."
    numbers = [int(m[1]) for i in taken if (m := re.match(rf"^{re.escape(prefix)}(\d+)", i))]
    return f"{prefix}{max(numbers, default=0) + 1}"


def cmd_new(
    priority: int, title: str, bug: bool, version: str, root: Path, include_git: bool
) -> Task:
    if priority not in (0, 1, 2, 3):
        raise TaskError(f"priority must be 0, 1, 2 or 3, got {priority}")
    tasks = load_tasks(root)
    if version not in all_sections(root) or version in STATUS_SECTIONS:
        raise TaskError(f"version `{version}` is not an open BACKLOG section")
    task_id = next_id(priority, root, include_git)
    shown = f"[BUG] {title}" if bug else title
    path = BACKLOG_DIR / f"{task_id}-{slugify(title)}.md"
    if (
        bug and version != Section.INBOX
    ):  # a bug that makes a number wrong goes to the top of its version
        order = min((t.order or 0 for t in tasks if t.section == version), default=0) - ORDER_STEP
    else:
        order = next_order(tasks, version)
    task = Task(
        path=path,
        id=task_id,
        title=shown,
        status=Status.OPEN,
        version=version,
        order=order,
        priority=f"P{priority}",
        size="S" if bug and version != Section.INBOX else None,
        body=f"\n# {task_id}: {shown}\n",
    )
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    write_task(task, root)
    return task


def cmd_start(task_id: str, today: str, root: Path) -> Task:
    task = find_task(load_tasks(root), task_id)
    if task.status not in (Status.OPEN, Status.IN_PROGRESS):
        raise TaskError(f"{task_id} is {task.status}; only an open task can start")
    if is_under(task.path, BACKLOG_DIR):
        folder = task.path.parent.name if task.path.name == "design.md" else task.path.stem
        dst = ACTIVE_DIR / folder / "design.md"
        if (root / dst).exists():
            raise TaskError(f"{dst} already exists")
        if task.path.name == "design.md":
            move(task.path.parent, ACTIVE_DIR / folder, root)
        else:
            move(task.path, dst, root)
        task.path = dst
    task.status = Status.IN_PROGRESS
    task.started = task.started or today
    write_task(task, root)
    return task


def cmd_subtask(parent_id: str, suffix: str, title: str, name: str, today: str, root: Path) -> Task:
    tasks = load_tasks(root)
    parent = find_task(tasks, parent_id)
    if not is_under(parent.path, ACTIVE_DIR) or parent.path.name != "design.md":
        raise TaskError(
            f"{parent_id} is not an active design folder; run `factory task start {parent_id}`"
        )
    if not re.fullmatch(r"[a-z]", suffix):
        raise TaskError("suffix is one lowercase letter (a, b, c, ...)")
    task_id = parent_id + suffix
    if any(t.id == task_id for t in tasks):
        raise TaskError(f"ID {task_id} already exists")
    path = parent.path.parent / f"design-{slugify(name)}.md"
    if (root / path).exists():
        raise TaskError(f"{path} already exists")
    task = Task(
        path=path,
        id=task_id,
        title=title,
        status=Status.IN_PROGRESS,
        version=parent.version,
        started=today,
        body=f"\n# {task_id}: {title}\n",
    )
    write_task(task, root)
    return task


def _folder_and_siblings(
    task: Task, tasks: list[Task], target: Path
) -> tuple[Path | None, list[Task]]:
    """Where `task` moves when it leaves for `target`, and the unfinished tasks that go with it.

    A design.md speaks for its whole folder; a top-level backlog spec moves alone; a subtask stays put.
    Raises before anything is written when the destination is taken.
    """
    if task.path.name == "design.md" and not is_under(task.path, target):
        folder = task.path.parent
        siblings = [
            t
            for t in tasks
            if t is not task
            and t.path.parent == folder
            and t.status in (Status.OPEN, Status.IN_PROGRESS)
        ]
        dst = target / folder.name
    elif is_under(task.path, BACKLOG_DIR):
        siblings, dst = [], target / task.path.name
    else:
        return None, []
    return dst, siblings


def _apply_move(task: Task, moved: list[Task], dst: Path | None, root: Path) -> None:
    if dst is None:
        return
    if task.path.name == "design.md":
        move(task.path.parent, dst, root)
        for t in moved:
            t.path = dst / t.path.name
    else:
        move(task.path, dst, root)
        task.path = dst


def cmd_complete(task_id: str, today: str, root: Path) -> list[Task]:
    """Mark a task done. A parent design.md takes its unfinished subtasks with it to completed/."""
    tasks = load_tasks(root)
    task = find_task(tasks, task_id)
    if task.status in (Status.DONE, Status.CLOSED):
        raise TaskError(f"{task_id} is already {task.status}")
    dst, siblings = _folder_and_siblings(task, tasks, COMPLETED_DIR)
    if dst is not None and (root / dst).exists():
        raise TaskError(f"{dst} already exists")
    done = [task, *siblings]
    for t in done:
        t.status = Status.DONE
        t.completed = today
        t.order = None
        write_task(t, root)
    _apply_move(task, done, dst, root)
    return done


def cmd_close(task_id: str, reason: str, root: Path) -> list[Task]:
    """Close without implementing. A parent design.md closes its unfinished subtasks and moves to closed/."""
    tasks = load_tasks(root)
    task = find_task(tasks, task_id)
    if task.status in (Status.DONE, Status.CLOSED):
        raise TaskError(f"{task_id} is already {task.status}")
    dst, siblings = _folder_and_siblings(task, tasks, CLOSED_DIR)
    if dst is not None and (root / dst).exists():
        raise TaskError(f"{dst} already exists")
    closed = [task, *siblings]
    order = next_order(tasks, Section.CLOSED)
    for t in closed:
        t.status = Status.CLOSED
        t.reason = reason
        t.order = order
        order += ORDER_STEP
        write_task(t, root)
    _apply_move(task, closed, dst, root)
    return closed


def cmd_set(task_id: str, assignments: list[str], root: Path) -> Task:
    tasks = load_tasks(root)
    task = find_task(tasks, task_id)
    pairs = {}
    for item in assignments:
        key, sep, value = item.partition("=")
        if not sep or key not in SETTABLE:
            raise TaskError(f"`{item}`: use key=value with key in {', '.join(SETTABLE)}")
        pairs[key] = value
    if "version" in pairs and pairs["version"] != task.version and "order" not in pairs:
        pairs["order"] = str(next_order(tasks, pairs["version"]))
    for key, value in pairs.items():
        if key == "order":
            if not value.lstrip("-").isdigit():
                raise TaskError("order must be an integer")
            task.order = int(value)
        else:
            setattr(task, key, value or None)
    write_task(task, root)
    return task


VIEW_SKELETONS = {
    BACKLOG_MD: """# Backlog

Task rows are generated from each spec's front matter (`factory task`); edit the prose, never the tables.

## In Progress

<!-- tasks:begin in-progress -->
<!-- tasks:end -->

## Inbox

<!-- tasks:begin inbox -->
<!-- tasks:end -->

## Icebox

<!-- tasks:begin icebox -->
<!-- tasks:end -->

## Closed

<!-- tasks:begin closed -->
<!-- tasks:end -->
""",
    COMPLETED_MD: """# Completed

<!-- tasks:begin completed -->
<!-- tasks:end -->
""",
}


def cmd_init(root: Path) -> list[Path]:
    """The task folders and the two views, where missing; an existing file is never touched."""
    created = []
    for folder in (BACKLOG_DIR, ACTIVE_DIR, COMPLETED_DIR, CLOSED_DIR):
        if not (root / folder).is_dir():
            (root / folder).mkdir(parents=True)
            (root / folder / ".gitkeep").touch()
            created.append(folder)
    for view, text in VIEW_SKELETONS.items():
        if not (root / view).exists():
            (root / view).write_text(text)
            created.append(view)
    return created


def exists(query: str, root: Path) -> bool:
    """An open task (backlog/ or active/ spec text) or an open PR mentions `query`, case-insensitively."""
    needle = query.lower()
    for folder in (BACKLOG_DIR, ACTIVE_DIR):
        for path in sorted((root / folder).rglob("*.md")) if (root / folder).is_dir() else []:
            if needle in path.read_text(errors="replace").lower():
                return True
    found = subprocess.run(
        ["gh", "pr", "list", "--state", "open", "--search", query, "--json", "number", "--jq", "length > 0"],
        cwd=root, capture_output=True, text=True,
    )
    return found.returncode == 0 and found.stdout.strip() == "true"


def changed_blocks(base_ref: str, root: Path) -> list[str]:
    """Generated blocks that differ from `base_ref`: only the bot on main may change them."""
    changed = []
    for view in VIEW_FILES:
        before = blocks(git("show", f"{base_ref}:{view.as_posix()}", root=root))
        after = blocks((root / view).read_text())
        changed += [
            f"{view}: {s}" for s in sorted(set(before) & set(after)) if before[s] != after[s]
        ]
    return changed


def tasks_at(ref: str, root: Path) -> list[Task]:
    """The tasks recorded at a git ref (one `git cat-file --batch` for every spec)."""
    names = [
        n
        for n in git("ls-tree", "-r", "--name-only", ref, "--", str(TASKS), root=root).splitlines()
        if n.endswith(".md")
    ]
    if not names:
        return []
    batch = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=root,
        input="".join(f"{ref}:{n}\n" for n in names).encode(),
        capture_output=True,
        check=True,
    ).stdout
    found, pos = [], 0
    for name in names:
        header_end = batch.index(b"\n", pos)
        size = int(batch[pos:header_end].split()[2])
        text = batch[header_end + 1 : header_end + 1 + size].decode()
        pos = header_end + 1 + size + 1
        task = parse_task(Path(name), text)
        if task is not None:
            found.append(task)
    return found


def lost_tasks(base_ref: str, root: Path) -> list[str]:
    """IDs recorded at `base_ref` that no spec carries now (front matter dropped or file deleted)."""
    now = {t.id for t in load_tasks(root)}
    return sorted({t.id for t in tasks_at(base_ref, root)} - now, key=natural_key)


def main(argv: list[str] | None, root: Path) -> int:
    parser = argparse.ArgumentParser(prog="factory task", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("show")
    p.add_argument("section", nargs="?")
    p = sub.add_parser("render")
    p.add_argument("--check", action="store_true", help="exit 1 if a block is stale; write nothing")
    sub.add_parser("check")
    p = sub.add_parser("check-pr")
    p.add_argument("--base", required=True)
    p = sub.add_parser("next-id")
    p.add_argument("--priority", type=int, required=True)
    p = sub.add_parser("new")
    p.add_argument("--priority", type=int, default=2)
    p.add_argument("--title", required=True)
    p.add_argument("--bug", action="store_true")
    p.add_argument("--version", default=Section.INBOX)
    p.add_argument("--body-file", type=Path, help="append this file's text below the new spec's heading")
    sub.add_parser("init")
    p = sub.add_parser("exists")
    p.add_argument("query")
    p = sub.add_parser("start")
    p.add_argument("id")
    p = sub.add_parser("subtask")
    p.add_argument("parent")
    p.add_argument("suffix")
    p.add_argument("--title", required=True)
    p.add_argument("--name", required=True, help="file becomes design-<name>.md")
    p = sub.add_parser("complete")
    p.add_argument("id")
    p = sub.add_parser("close")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p = sub.add_parser("set")
    p.add_argument("id")
    p.add_argument("assignments", nargs="+", metavar="key=value")
    for name in ("start", "subtask", "complete"):
        sub.choices[name].add_argument("--date", default=dt.date.today().isoformat())
    args = parser.parse_args(argv)

    try:
        if args.cmd == "show":
            tasks = load_tasks(root)
            sections = [args.section] if args.section else sorted(all_sections(root))
            for s in sections:
                view = COMPLETED_MD if s == Section.COMPLETED else BACKLOG_MD
                print(f"## {s}\n\n{render_table(s, tasks, view)}")
        elif args.cmd == "render":
            changed = render_all(root, write=not args.check)
            for view in changed:
                print(f"{'stale' if args.check else 'rendered'}: {view}")
            return 1 if args.check and changed else 0
        elif args.cmd == "check":
            errors = validate(load_tasks(root), all_sections(root), root)
            for e in errors:
                print(e, file=sys.stderr)
            return 1 if errors else 0
        elif args.cmd == "check-pr":
            changed = changed_blocks(args.base, root)
            for c in changed:
                print(
                    f"{c}: generated table edited on a branch. Change the spec's front matter "
                    f"(factory task) and restore the view: git checkout {args.base} -- docs/BACKLOG.md "
                    "docs/COMPLETED.md (then re-apply any prose edits). The bot on main regenerates it.",
                    file=sys.stderr,
                )
            lost = lost_tasks(args.base, root)
            for task_id in lost:
                print(
                    f"task {task_id}: its spec lost its front matter or was deleted. Restore the "
                    "`---` block at the top of the file (close a task with `factory task close`).",
                    file=sys.stderr,
                )
            return 1 if changed or lost else 0
        elif args.cmd == "next-id":
            print(next_id(args.priority, root))
        elif args.cmd == "new":
            task = cmd_new(
                args.priority, args.title, args.bug, args.version, root, include_git=True
            )
            if args.body_file:
                with (root / task.path).open("a") as spec_file:
                    spec_file.write("\n" + args.body_file.read_text())
            print(task.path)
        elif args.cmd == "init":
            for path in cmd_init(root):
                print(f"created {path}")
        elif args.cmd == "exists":
            return 0 if exists(args.query, root) else 1
        elif args.cmd == "start":
            print(cmd_start(args.id, args.date, root).path)
        elif args.cmd == "subtask":
            print(
                cmd_subtask(args.parent, args.suffix, args.title, args.name, args.date, root).path
            )
        elif args.cmd == "complete":
            for t in cmd_complete(args.id, args.date, root):
                print(f"done {t.id}: {t.path}")
        elif args.cmd == "close":
            for t in cmd_close(args.id, args.reason, root):
                print(f"closed {t.id}: {t.path}")
        elif args.cmd == "set":
            print(cmd_set(args.id, args.assignments, root).path)
    except TaskError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


