"""Discover skills, commands, subagents, references and CLAUDE.md files under a root, and check them.

Rule IDs, defaults and sources: docs/RULES.md. Suppress a finding on one line with an HTML comment on that line or
the line above: `<!-- skillcheck: ignore SC021 -->` (no IDs = every rule).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from . import frontmatter
from .config import Config

RULES: dict[str, tuple[str, str]] = {
    "SC001": ("error", "frontmatter missing or unparseable"),
    "SC002": ("error", "description missing"),
    "SC003": ("error", "description too long"),
    "SC004": ("error", "invalid name"),
    "SC005": ("warning", "name differs from its directory"),
    "SC006": ("warning", "unknown frontmatter key (silently ignored by Claude Code)"),
    "SC007": ("warning", "angle brackets in description"),
    "SC008": ("warning", "description not in third person"),
    "SC009": ("warning", "model-invocable description has no trigger clause"),
    "SC010": ("warning", "SKILL.md body over the line budget"),
    "SC011": ("warning", "SKILL.md body over the token budget"),
    "SC012": ("warning", "long reference without a table of contents"),
    "SC013": ("warning", "reference links another reference (keep one level deep)"),
    "SC014": ("error", "broken relative link"),
    "SC015": ("error", "path in backticks does not exist"),
    "SC016": ("error", "absolute home path in a shared file"),
    "SC017": ("warning", "legacy .claude/commands file (prefer skills/<name>/SKILL.md)"),
    "SC018": ("warning", "CLAUDE.md over the line budget"),
    "SC019": ("warning", "skill descriptions exceed the listing budget"),
    "SC020": ("warning", "long line duplicated across files (copy that will drift)"),
    "SC021": ("error", "banned pattern"),
    "SC022": ("error", "subagent missing name or description"),
    "SC023": ("warning", "reference file never mentioned by its SKILL.md"),
}

SKILL_KEYS = {
    "name", "description", "when_to_use", "argument-hint", "arguments", "disable-model-invocation",
    "user-invocable", "allowed-tools", "disallowed-tools", "model", "effort", "context", "agent", "background",
    "hooks", "paths", "shell", "metadata", "license", "compatibility",
}
AGENT_KEYS = {
    "name", "description", "tools", "disallowedTools", "model", "permissionMode", "maxTurns", "autoCompactWindow",
    "skills", "mcpServers", "hooks", "memory", "background", "omitClaudeMd", "effort", "isolation", "color",
    "initialPrompt", "experimental",
}
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
PERSON_RE = re.compile(r"(?i)(^|[.!?]\s+)(i|we|you)\b|\b(i can|i will|i'll|you can|you should|you will|let me)\b")
TRIGGER_RE = re.compile(
    r"(?i)\b(use (it |this |this skill |only )?(when|for|to|whenever|before|after|on|with)|when (the )?(user|you)"
    r"|triggers?\b|invoke (it |this )?(when|with)|whenever|before (any|every|the)|for (any|every))"
)
HOME_RE = re.compile(r"(?<![\w~])(/Users|/home)/[A-Za-z0-9_.-]+/")
LINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
CODE_RE = re.compile(r"`([^`\n]+)`")
PATH_RE = re.compile(r"^[\w.@-]+(/[\w.@-]+)+/?$")
PLACEHOLDER_RE = re.compile(r"(?i)(^|/)(my_|my-|your|example|path/to|foo|bar)|\.\.\.|^\.?/")
SUPPRESS_RE = re.compile(r"skillcheck:\s*ignore\s*([A-Z0-9, ]*)")
TOC_RE = re.compile(r"(?im)^#+\s*(contents|table of contents)\b")


@dataclass
class Finding:
    rule: str
    severity: str
    path: str
    line: int
    message: str

    def text(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.severity}: {self.message}"


@dataclass
class Doc:
    path: Path
    rel: str
    kind: str  # skill | command | agent | reference | claude_md
    text: str
    fm: frontmatter.Frontmatter | None = None
    owner: Path | None = None  # skill directory, for skills and references


def _match(rel: str, pattern: str) -> bool:
    return fnmatch(rel, pattern) or (pattern.startswith("**/") and fnmatch(rel, pattern[3:]))


def _is_plugin_dir(path: Path) -> bool:
    return (path / ".claude-plugin").is_dir()


def discover(cfg: Config) -> list[Doc]:
    root = cfg.root
    docs: list[Doc] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dirnames[:] = [d for d in dirnames if d not in cfg.skip_dirs]
        here = Path(dirpath)
        for name in filenames:
            if not name.endswith(".md"):
                continue
            path = here / name
            rel = path.relative_to(root).as_posix()
            if any(_match(rel, p) for p in cfg.exclude):
                continue
            parent, grand = here.name, here.parent
            kind = None
            owner = None
            if name == "SKILL.md":
                kind, owner = "skill", here
            elif name == "CLAUDE.md":
                kind = "claude_md"
            elif parent == "commands" and (grand.name == ".claude" or _is_plugin_dir(grand)):
                kind = "command"
            elif parent == "agents" and (grand.name == ".claude" or _is_plugin_dir(grand)):
                kind = "agent"
            else:
                owner = next((p for p in [here, *here.parents] if (p / "SKILL.md").is_file() and _within(p, root)), None)
                if owner is not None:
                    kind = "reference"
            if kind is None:
                continue
            text = path.read_text(errors="replace")
            fm = frontmatter.parse(text) if kind in ("skill", "command", "agent") else None
            docs.append(Doc(path, rel, kind, text, fm, owner))
    return docs


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _repo_index(root: Path) -> set[str]:
    """Every file and directory in the repo, plus every trailing sub-path of each, for lenient path lookup."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, text=True, timeout=30, check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        out = [
            (Path(d) / f).relative_to(root).as_posix()
            for d, _, fs in os.walk(root) if ".git" not in d and "node_modules" not in d for f in fs
        ]
    index: set[str] = set()
    for f in out:
        parts = f.split("/")
        for i in range(len(parts)):  # c.py, b/c.py, a/b/c.py
            index.add("/".join(parts[i:]))
        for j in range(1, len(parts)):  # a/, a/b/, b/
            for i in range(j):
                index.add("/".join(parts[i:j]) + "/")
    return index


def _lines_outside_fences(text: str) -> list[tuple[int, str]]:
    out, fenced = [], False
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced:
            out.append((n, line))
    return out


def _suppressed(lines: list[str], n: int, rule: str) -> bool:
    for i in (n - 1, n - 2):
        if 0 <= i < len(lines):
            m = SUPPRESS_RE.search(lines[i])
            if m and (not m.group(1).strip() or rule in m.group(1)):
                return True
    return False


class Checker:
    def __init__(self, cfg: Config, scope: str):
        self.cfg = cfg
        self.scope = scope  # project | user
        self.findings: list[Finding] = []
        self._index: set[str] | None = None

    def add(self, rule: str, doc_or_rel, line: int, message: str, lines: list[str] | None = None) -> None:
        severity = self.cfg.severity.get(rule, RULES[rule][0])
        if severity == "off":
            return
        rel = doc_or_rel.rel if isinstance(doc_or_rel, Doc) else doc_or_rel
        if lines is None and isinstance(doc_or_rel, Doc):
            lines = doc_or_rel.text.splitlines()
        if lines and line and _suppressed(lines, line, rule):
            return
        self.findings.append(Finding(rule, severity, rel, line, message))

    @property
    def index(self) -> set[str]:
        if self._index is None:
            self._index = _repo_index(self.cfg.root)
        return self._index

    # -- per-file checks ---------------------------------------------------------------------------------------

    def run(self, docs: list[Doc]) -> list[Finding]:
        for doc in docs:
            if doc.kind in ("skill", "command"):
                self.check_invocable(doc)
            if doc.kind == "agent":
                self.check_agent(doc)
            if doc.kind == "reference":
                self.check_reference(doc)
            if doc.kind == "claude_md":
                n = len(doc.text.splitlines())
                if n > self.cfg.claude_md_lines:
                    self.add("SC018", doc, 1, f"{n} lines (budget {self.cfg.claude_md_lines}); move detail into directory CLAUDE.md files or skills")
            if doc.kind == "command" and doc.path.parent.parent.name == ".claude":
                self.add("SC017", doc, 1, f"move to .claude/skills/{doc.path.stem}/SKILL.md")
            self.check_links(doc)
            self.check_paths(doc)
            self.check_banned(doc)
            if self.scope == "project":
                self.check_home_paths(doc)
        self.check_listing_budget(docs)
        self.check_duplicates(docs)
        return self.findings

    def check_invocable(self, doc: Doc) -> None:
        fm = doc.fm
        assert fm is not None
        if doc.kind == "skill" and not fm.present:
            self.add("SC001", doc, 1, "no --- frontmatter block; the description cannot match")
            return
        if fm.error:
            self.add("SC001", doc, fm.error_line, fm.error)
            return
        data = fm.data
        desc = str(data.get("description", "") or "").strip()
        dline = fm.lines.get("description", 1)
        if not desc:
            self.add("SC002", doc, dline, "add a description: what it does and when to use it")
        else:
            total = desc + str(data.get("when_to_use", "") or "")
            if len(desc) > self.cfg.description_chars:
                self.add("SC003", doc, dline, f"{len(desc)} chars (max {self.cfg.description_chars})")
            if "<" in desc or ">" in desc:
                self.add("SC007", doc, dline, "angle brackets render as &lt;/&gt; in the listing; use [x] or argument-hint")
            if PERSON_RE.search(desc):
                self.add("SC008", doc, dline, "write the description in third person (it is injected into the system prompt)")
            invocable = data.get("disable-model-invocation") is not True
            if invocable and not TRIGGER_RE.search(total):
                self.add("SC009", doc, dline, "say when to use it (e.g. 'Use when …') so the model triggers it, or set disable-model-invocation: true")
        for key in data:
            if key not in SKILL_KEYS:
                self.add("SC006", doc, fm.lines.get(key, 1), f"`{key}` is not a skill frontmatter key")
        expected = doc.path.parent.name if doc.kind == "skill" else doc.path.stem
        name = data.get("name")
        if name is not None:
            name = str(name)
            if len(name) > 64 or not NAME_RE.match(name):
                self.add("SC004", doc, fm.lines.get("name", 1), f"`{name}`: lowercase a-z, 0-9 and single hyphens, max 64")
            elif name != expected:
                self.add("SC005", doc, fm.lines.get("name", 1), f"name `{name}` vs directory `{expected}`")
        if doc.kind == "skill":
            body = "\n".join(doc.text.splitlines()[fm.body_start - 1:])
            n = body.count("\n") + 1
            if n > self.cfg.skill_lines:
                self.add("SC010", doc, fm.body_start, f"{n} lines (budget {self.cfg.skill_lines}); move detail to references/")
            tokens = len(body) // 4
            if tokens > self.cfg.skill_tokens:
                self.add("SC011", doc, fm.body_start, f"~{tokens} tokens (budget {self.cfg.skill_tokens}); move detail to references/")

    def check_agent(self, doc: Doc) -> None:
        fm = doc.fm
        assert fm is not None
        if not fm.present or fm.error:
            self.add("SC001", doc, fm.error_line or 1, fm.error or "subagent needs a --- frontmatter block")
            return
        missing = [k for k in ("name", "description") if not fm.data.get(k)]
        if missing:
            self.add("SC022", doc, 1, f"missing {', '.join(missing)}")
        for key in fm.data:
            if key not in AGENT_KEYS:
                self.add("SC006", doc, fm.lines.get(key, 1), f"`{key}` is not a subagent frontmatter key")

    def check_reference(self, doc: Doc) -> None:
        lines = doc.text.splitlines()
        if len(lines) > self.cfg.reference_toc_lines:
            head = "\n".join(lines[:40])
            anchors = len(re.findall(r"\]\(#", head))
            if not TOC_RE.search(head) and anchors < 3:
                self.add("SC012", doc, 1, f"{len(lines)} lines; start with a table of contents so a partial read still finds the rest")
        assert doc.owner is not None
        skill_text = (doc.owner / "SKILL.md").read_text(errors="replace")
        if doc.path.name not in skill_text:
            self.add("SC023", doc, 1, f"not mentioned in {doc.owner.name}/SKILL.md — link it or delete it")
        for n, line in _lines_outside_fences(doc.text):
            for target in LINK_RE.findall(line):
                t = (doc.path.parent / target.split("#")[0]).resolve()
                if target.endswith(".md") and t != doc.path.resolve() and _within(t, doc.owner.resolve()) and t.name != "SKILL.md":
                    self.add("SC013", doc, n, f"links {target}; link it from SKILL.md instead")

    # -- content checks ---------------------------------------------------------------------------------------

    def check_links(self, doc: Doc) -> None:
        for n, line in _lines_outside_fences(doc.text):
            for target in LINK_RE.findall(line):
                if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                    continue
                path = target.split("#")[0]
                if not path:
                    continue
                if not (doc.path.parent / path).exists():
                    self.add("SC014", doc, n, f"`{target}` does not exist")

    def check_paths(self, doc: Doc) -> None:
        cfg = self.cfg
        if not cfg.check_paths or self.scope != "project":
            return
        if not any(_match(doc.rel, p) for p in cfg.path_include) or any(_match(doc.rel, p) for p in cfg.path_ignore):
            return
        for n, line in _lines_outside_fences(doc.text):
            for span in CODE_RE.findall(line):
                cand = re.sub(r"(:\d+(-\d+)?|#[\w-]*)$", "", span.strip())
                if not PATH_RE.match(cand) or PLACEHOLDER_RE.search(cand):
                    continue
                if not (cand.endswith("/") or re.search(r"\.[A-Za-z0-9]{1,6}$", cand)):
                    continue
                if any(fnmatch(cand, a) for a in cfg.path_allow):
                    continue
                bases = [cfg.root, doc.path.parent] + ([doc.owner] if doc.owner else [])
                if any((b / cand).exists() for b in bases):
                    continue
                if cand.rstrip("/") in self.index or cand in self.index or cand.rstrip("/") + "/" in self.index:
                    continue
                self.add("SC015", doc, n, f"`{span}` not found in the repo — renamed or deleted?")

    def check_banned(self, doc: Doc) -> None:
        lines = doc.text.splitlines()
        for b in self.cfg.banned:
            if not any(_match(doc.rel, p) for p in b.paths):
                continue
            rx = re.compile(b.pattern)
            for n, line in enumerate(lines, 1):
                if rx.search(line) and not _suppressed(lines, n, "SC021"):
                    sev = self.cfg.severity.get("SC021", b.severity)
                    if sev != "off":
                        self.findings.append(Finding("SC021", sev, doc.rel, n, b.message))

    def check_home_paths(self, doc: Doc) -> None:
        for n, line in enumerate(doc.text.splitlines(), 1):
            m = HOME_RE.search(line)
            if m:
                self.add("SC016", doc, n, f"`{m.group(0)}…` only exists on one machine; use a repo-relative path or ~")

    # -- cross-file checks ------------------------------------------------------------------------------------

    def check_listing_budget(self, docs: list[Doc]) -> None:
        total, count = 0, 0
        for d in docs:
            if d.kind in ("skill", "command") and d.fm and not d.fm.error and d.fm.data.get("disable-model-invocation") is not True:
                total += len(str(d.fm.data.get("description", ""))) + len(str(d.fm.data.get("when_to_use", "")))
                count += 1
        if total > self.cfg.listing_chars:
            self.add("SC019", ".", 0, f"{count} model-invocable skills use {total} description chars (budget {self.cfg.listing_chars}); shorten them or set disable-model-invocation on user-only workflows")

    def check_duplicates(self, docs: list[Doc]) -> None:
        seen: dict[str, list[tuple[Doc, int]]] = defaultdict(list)
        for d in docs:
            for n, line in _lines_outside_fences(d.text):
                s = " ".join(line.split()).lstrip("-*> ").strip()
                if len(s) < self.cfg.duplicate_min_chars or s.startswith("|") or s.startswith("#"):
                    continue
                seen[s].append((d, n))
        for s, hits in seen.items():
            files = {h[0].rel for h in hits}
            if len(files) < 2 or not any(h[0].kind in ("skill", "command", "reference") for h in hits):
                continue
            first = hits[0]
            for d, n in hits[1:]:
                if d.rel != first[0].rel and d.kind in ("skill", "command", "reference"):
                    self.add("SC020", d, n, f"same line as {first[0].rel}:{first[1]} — keep one owner and point to it")
