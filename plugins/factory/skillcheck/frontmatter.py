"""Frontmatter for SKILL.md / command / agent files.

A deliberately small YAML subset, so the linter runs on any Python 3.11+ with no dependencies and gives the same
answer everywhere: scalars, quoted strings, block scalars (`>`, `>-`, `|`, `|-`), inline `[a, b]` lists, and one
level of nested lists or maps. Anything else is reported as a parse error rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s+(.*))?$")


@dataclass
class Frontmatter:
    present: bool
    data: dict = field(default_factory=dict)
    lines: dict[str, int] = field(default_factory=dict)  # key -> 1-based line in the file
    error: str | None = None
    error_line: int = 0
    body_start: int = 1  # 1-based line where the body starts


def _scalar(raw: str) -> object:
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] in "\"'" and raw[-1] == raw[0] and len(raw) >= 2:
        inner = raw[1:-1]
        return inner.replace("\\\"", '"') if raw[0] == '"' else inner.replace("''", "'")
    if raw.startswith("[") and raw.endswith("]"):
        return [str(_scalar(p)) for p in raw[1:-1].split(",") if p.strip()]
    if " #" in raw:
        raw = raw.split(" #", 1)[0].rstrip()
    low = raw.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    return raw


def parse(text: str) -> Frontmatter:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return Frontmatter(present=False)
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return Frontmatter(present=True, error="frontmatter has no closing ---", error_line=1)

    fm = Frontmatter(present=True, body_start=end + 2)
    i = 1
    while i < end:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[0] in " \t":
            fm.error, fm.error_line = f"unexpected indentation: {line.strip()[:40]}", i + 1
            return fm
        m = _KEY.match(line)
        if not m:
            fm.error, fm.error_line = f"not a `key: value` line: {line.strip()[:40]}", i + 1
            return fm
        key, rest = m.group(1), (m.group(2) or "").strip()
        fm.lines[key] = i + 1
        i += 1
        if rest in (">", ">-", "|", "|-", ">+", "|+"):
            block = []
            while i < end and (not lines[i].strip() or lines[i][0] in " \t"):
                block.append(lines[i].strip())
                i += 1
            joiner = "\n" if rest.startswith("|") else " "
            fm.data[key] = joiner.join(b for b in block if b or joiner == "\n").strip()
        elif rest == "":
            items: list = []
            mapping: dict = {}
            while i < end and (not lines[i].strip() or lines[i][0] in " \t"):
                s = lines[i].strip()
                if s.startswith("- "):
                    items.append(_scalar(s[2:]))
                elif s:
                    sub = _KEY.match(s)
                    if not sub:
                        fm.error, fm.error_line = f"cannot parse nested line: {s[:40]}", i + 1
                        return fm
                    mapping[sub.group(1)] = _scalar(sub.group(2) or "")
                i += 1
            fm.data[key] = items if items else (mapping if mapping else "")
        else:
            fm.data[key] = _scalar(rest)
    return fm
