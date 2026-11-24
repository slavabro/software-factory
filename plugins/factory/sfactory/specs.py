"""Capability specs: one file per capability saying what it promises today, each promise proven by named tests.

`factory spec check` keeps them true without running anything (format: docs/factory-toml.md [specs]):

1. Front matter: `capability` equals the file name; every `owners` directory exists and its CLAUDE.md links the spec.
2. Requirements: unique `## <slug>` headings, each with SHALL or MUST and a `Proven by:` list.
3. Every proof resolves through the repo's resolver (pytest: the test exists, found by AST, never imported).
4. No history: no task references (the repo's `[tasks]` ID pattern) and no dates.
5. The specs folder matches no workflow's `paths-ignore` glob (GitHub Actions glob semantics), so a PR that only
   edits a spec still runs CI.

Every failure names the file, the requirement slug when there is one, and the fix.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import frontmatter

NOT_SPECS = {"CLAUDE.md", "README.md"}  # a specs folder's own notes, not capabilities
FRONT_MATTER_KEYS = {"capability", "owners"}
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
PROMISE = re.compile(r"\b(?:SHALL|MUST)\b")
PROVEN_BY = "Proven by:"
LIST_ITEM = re.compile(r"[-*]\s+(?P<item>.+)")
FENCE = re.compile(r"\s*(```|~~~)")
MONTHS = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?"
)
DATES = re.compile(
    rf"\b\d{{4}}[-/]\d{{1,2}}[-/]\d{{1,2}}\b"  # 2026-11-22, 2026/11/22
    rf"|\b\d{{1,2}}[-/.]\d{{1,2}}[-/.]\d{{4}}\b"  # 22.11.2026, 11/22/2026
    rf"|\b(?:\d{{1,2}}(?:st|nd|rd|th)?\s+)?{MONTHS}\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?\d{{4}}\b"  # 22 Nov 2026
)
MARKDOWN_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)|^\s*\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)", re.M)
WORKFLOWS = Path(".github/workflows")
PATHS_IGNORE = re.compile(r"""^(?P<indent>\s*)(?P<q>['"]?)paths-ignore(?P=q)\s*:\s*(?P<rest>.*)$""")


@dataclass
class Failure:
    file: str
    slug: str  # the requirement, or "" for the file as a whole
    problem: str
    fix: str

    def __str__(self) -> str:
        where = f"{self.file} [{self.slug}]" if self.slug else self.file
        return f"{where}: {self.problem}\n    fix: {self.fix}"


@dataclass
class Requirement:
    slug: str
    line: int
    text: list[str] = field(default_factory=list)
    listed: bool = False  # it has a `Proven by:` line
    in_list: bool = False  # the lines being read are that list
    proofs: list[tuple[int, str]] = field(default_factory=list)  # (line, test ID)


@dataclass
class Report:
    specs: int = 0
    requirements: int = 0
    proofs: int = 0
    failures: list[Failure] = field(default_factory=list)


# ---- resolvers: a `Proven by:` entry → None if the test exists, else the problem --------------------------------


class _PytestFiles:
    """pytest node IDs `path::function[param]` and `path::Class::method[param]`, resolved by AST."""

    def __init__(self, root: Path):
        self.root = root
        self.trees: dict[Path, ast.Module | str] = {}

    def _tree(self, path: Path) -> ast.Module | str:
        if path not in self.trees:
            try:
                self.trees[path] = ast.parse(path.read_text(), filename=str(path))
            except (SyntaxError, UnicodeDecodeError, ValueError) as e:
                self.trees[path] = f"cannot parse {path.relative_to(self.root)} ({e})"
        return self.trees[path]

    def __call__(self, node_id: str) -> str | None:
        rel, sep, rest = node_id.partition("::")
        if not sep or not rest:
            return f"`{node_id}` is not a pytest node ID (`path::test` or `path::Class::test`)"
        names = rest.split("::")
        if "[" in names[-1]:
            name, _, param = names[-1].partition("[")
            if not param.endswith("]") or not param[:-1]:
                return f"`{node_id}` has a malformed `[param]` suffix"
            names[-1] = name
        if any(not n.isidentifier() for n in names):
            return f"`{node_id}` names something that is not a Python identifier"
        path = (self.root / rel).resolve()
        if Path(rel).is_absolute() or not path.is_relative_to(self.root.resolve()):
            return f"`{rel}` is not a path inside the repo"
        if not path.is_file():
            return f"no test file {rel}"
        tree = self._tree(path)
        if isinstance(tree, str):
            return tree
        scope: list[ast.stmt] = tree.body
        for i, name in enumerate(names):
            last = i == len(names) - 1
            found = _find(scope, name)
            if found is None:
                owner = "::".join([rel, *names[:i]])
                return f"no {'test' if last else 'class'} `{name}` in {owner}"
            if last and isinstance(found, ast.ClassDef):
                return f"`{'::'.join([rel, *names])}` is a class; cite a test in it"
            if not last and not isinstance(found, ast.ClassDef):
                return f"`{name}` in {rel} is not a class"
            if isinstance(found, ast.ClassDef):
                scope = _class_body(found, tree.body)
        return None


def _find(scope: list[ast.stmt], name: str) -> ast.stmt | None:
    for node in reversed(scope):  # the last definition wins, as at import
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            return node
    return None


def _class_body(cls: ast.ClassDef, module: list[ast.stmt], seen: frozenset[str] = frozenset()) -> list[ast.stmt]:
    """A class's own definitions, then those it inherits from base classes defined in the same file."""
    body = list(cls.body)
    for base in cls.bases:
        if isinstance(base, ast.Name) and base.id not in seen:
            parent = _find(module, base.id)
            if isinstance(parent, ast.ClassDef):
                body = _class_body(parent, module, seen | {cls.name}) + body  # own definitions come last, so win
    return body


RESOLVERS: dict[str, Callable[[Path], Callable[[str], str | None]]] = {"pytest": _PytestFiles}


# ---- GitHub Actions path filters -----------------------------------------------------------------------------


def actions_glob(pattern: str) -> re.Pattern[str]:
    """A workflow path filter as a regex, with GitHub Actions' semantics: `**` crosses `/`, `*` does not, `?` is zero
    or one of the preceding character, `+` one or more of it, `[...]` a character class, `\\` escapes."""
    atoms: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**", i):
            atoms.append(".*")
            i += 2
            continue
        if ch == "*":
            atoms.append("[^/]*")
        elif ch in "?+" and atoms:
            atoms[-1] = f"(?:{atoms[-1]}){ch}"
        elif ch == "[" and (end := pattern.find("]", i + 2)) > 0:  # `[]]` is a class holding `]`
            atoms.append("[" + pattern[i + 1:end].replace("\\", "\\\\").replace("[", "\\[") + "]")
            i = end
        elif ch == "\\" and i + 1 < len(pattern):
            i += 1
            atoms.append(re.escape(pattern[i]))
        else:
            atoms.append(re.escape(ch))
        i += 1
    return re.compile("".join(atoms))


def _yaml_item(raw: str) -> str:
    text = raw.strip()
    if text[:1] in "'\"":
        end = text.find(text[0], 1)
        return text[1:end] if end > 0 else text[1:]
    return text.split(" #", 1)[0].strip()


def paths_ignore(text: str) -> list[str]:
    """Every `paths-ignore` glob in a workflow file (block or one-line flow list), read without a YAML library."""
    lines = text.splitlines()
    found: list[str] = []
    for n, line in enumerate(lines):
        m = PATHS_IGNORE.match(line)
        if not m:
            continue
        rest = m["rest"].strip()
        if rest.startswith("["):  # a one-line flow list
            found += [_yaml_item(x) for x in rest[1:].rsplit("]", 1)[0].split(",") if x.strip()]
            continue
        indent = len(m["indent"])
        for follow in lines[n + 1:]:
            stripped = follow.strip()
            if not stripped or stripped.startswith("#"):
                continue
            depth = len(follow) - len(follow.lstrip())
            if not stripped.startswith("-") or depth < indent:
                break
            found.append(_yaml_item(stripped[1:]))
    return [g for g in found if g]


def ignoring_workflows(root: Path, paths: list[str]) -> list[tuple[str, str, str]]:
    """(workflow, glob, path) for every workflow `paths-ignore` glob that matches one of `paths`."""
    hits = []
    folder = root / WORKFLOWS
    files = sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")]) if folder.is_dir() else []
    for wf in files:
        for glob in paths_ignore(wf.read_text(errors="replace")):
            if glob.startswith("!"):  # a negation puts paths back; it never ignores one
                continue
            rx = actions_glob(glob)
            hit = next((p for p in paths if rx.fullmatch(p)), None)
            if hit:
                hits.append((wf.relative_to(root).as_posix(), glob, hit))
    return hits


# ---- the check -------------------------------------------------------------------------------------------------


def _links(text: str) -> set[str]:
    return {(m[1] or m[2]).split("#", 1)[0] for m in MARKDOWN_LINK.finditer(text)} - {""}


def _history(line: str, task_id: re.Pattern[str]) -> str | None:
    if m := task_id.search(line):
        return f"task reference `{m[0]}`"
    if m := DATES.search(line):
        return f"date `{m[0]}`"
    return None


HISTORY_FIX = "say what the capability promises today; why and when live in the finished task specs and in git"


def check_spec(
    root: Path, spec: Path, resolve: Callable[[str], str | None], task_id: re.Pattern[str], report: Report
) -> None:
    rel = spec.relative_to(root).as_posix()

    def fail(slug: str, problem: str, fix: str) -> None:
        report.failures.append(Failure(rel, slug, problem, fix))

    text = spec.read_text()
    report.specs += 1
    if not text.startswith("---\n") or (end := text.find("\n---\n", 3)) < 0:
        fail("", "no front matter", f"start the file with `---`, `capability: {spec.stem}`, `owners: [dir, …]`, `---`")
        return
    try:
        meta = frontmatter.load(text[4:end + 1], f"{rel} front matter", sequences=True)
    except frontmatter.FrontMatterError as e:
        fail("", str(e), "write front matter as flat `key: value` lines; `owners: [dir, …]`")
        return
    for key in sorted(set(meta) - FRONT_MATTER_KEYS):
        fail("", f"unknown front matter key `{key}`", f"keep only {', '.join(sorted(FRONT_MATTER_KEYS))}")
    if meta.get("capability") != spec.stem:
        fail("", f"capability is {meta.get('capability')!r}, the file is {spec.name}",
             f"set `capability: {spec.stem}`, or rename the file to match")
    owners = meta.get("owners")
    if not isinstance(owners, list) or not owners or not all(isinstance(o, str) and o for o in owners):
        fail("", "owners must be a non-empty list of directories", "set `owners: [dir, …]`: the code this capability lives in")
        owners = []
    for owner in owners:
        folder = (root / owner).resolve()
        if Path(owner).is_absolute() or not folder.is_relative_to(root.resolve()) or not folder.is_dir():
            fail("", f"owner `{owner}` is not a directory in the repo", "name an existing repo-relative directory")
            continue
        claude = folder / "CLAUDE.md"
        link = Path(*([".."] * len(folder.relative_to(root.resolve()).parts)), rel).as_posix()
        if not claude.is_file():
            fail("", f"owner `{owner}` has no CLAUDE.md", f"create {owner}/CLAUDE.md with a link to the spec: ({link})")
        elif not any((folder / target).resolve() == spec.resolve() for target in _links(claude.read_text())):
            fail("", f"{owner}/CLAUDE.md does not link the spec", f"add one line to it linking `{link}`")

    body_start = text.count("\n", 0, end + 5) + 1
    requirements: list[Requirement] = []
    current: Requirement | None = None
    fenced = False
    for n, line in enumerate(text[end + 5:].splitlines(), body_start):
        if FENCE.match(line):
            fenced = not fenced
        stripped = line.strip()
        if not fenced and line.startswith("## "):
            current = Requirement(line[3:].strip(), n)
            requirements.append(current)
            if found := _history(current.slug, task_id):
                fail(current.slug, f"line {n}: the slug holds a {found}", HISTORY_FIX)
            continue
        if current is not None and current.in_list:
            if not stripped:
                continue
            item = LIST_ITEM.fullmatch(stripped)
            if item is None or fenced:
                fail(current.slug, f"line {n}: `{stripped[:60]}` after the Proven by list",
                     "end the requirement with its Proven by list; one test ID per `- ` line")
                current.in_list = False  # report the stray block once; it is prose from here on
            else:
                current.proofs.append((n, item["item"].strip().strip("`")))
                continue
        if current is not None and stripped == PROVEN_BY and not current.listed:
            current.listed = current.in_list = True
            continue
        if current is not None and stripped:
            current.text.append(stripped)
        if found := _history(line, task_id):
            fail(current.slug if current else "", f"line {n}: {found}", HISTORY_FIX)

    seen: dict[str, int] = {}
    for req in requirements:
        report.requirements += 1
        if not SLUG.fullmatch(req.slug):
            fail(req.slug, f"line {req.line}: `{req.slug}` is not a slug", "name the requirement in lowercase words joined by `-`")
        if req.slug in seen:
            fail(req.slug, f"line {req.line}: the slug is already used on line {seen[req.slug]}",
                 "give each requirement its own slug")
        seen.setdefault(req.slug, req.line)
        if not PROMISE.search(" ".join(req.text)):
            fail(req.slug, "no SHALL or MUST", "state the promise in one or two sentences with SHALL or MUST")
        if not req.proofs:
            fail(req.slug, "no tests under `Proven by:`" if req.listed else "no `Proven by:` list",
                 "end the requirement with `Proven by:` and one `- <test ID>` line per test that proves it")
        for n, proof in req.proofs:
            report.proofs += 1
            if problem := resolve(proof):
                fail(req.slug, f"line {n}: {problem}",
                     "cite the test that proves this promise now; if none does, write one or change the promise")


def check(root: Path, specs_dir: str, resolver: str, task_id_pattern: str) -> Report:
    report = Report()
    root = root.resolve()
    folder = root / specs_dir
    if not folder.is_dir():
        report.failures.append(Failure(specs_dir, "", "the specs folder does not exist",
                                       f"create {specs_dir}/ with one <capability>.md per capability, or set [specs] dir"))
        return report
    resolve = RESOLVERS[resolver](root)
    task_id = re.compile(task_id_pattern)
    specs = sorted(p for p in folder.glob("*.md") if p.name not in NOT_SPECS)
    for spec in specs:
        check_spec(root, spec, resolve, task_id, report)
    rel_dir = folder.relative_to(root).as_posix()
    paths = [p.relative_to(root).as_posix() for p in specs] + [f"{rel_dir}/capability.md"]
    for workflow, glob, path in ignoring_workflows(root, paths):
        report.failures.append(Failure(
            workflow, "", f"paths-ignore `{glob}` matches {path}: a PR that only edits specs skips this workflow",
            f"narrow that glob, or move the specs folder ([specs] dir) out of it",
        ))
    return report
