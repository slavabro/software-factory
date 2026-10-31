"""Find deprecation warnings that come from the repo's own code in test/build output.

A warning raised inside a dependency is the dependency's business; one raised from our file is a call we must
change. Recognises Python warnings (pytest summaries and plain stderr), Node runtime deprecations, `npm warn
deprecated` for dependencies, and JS/TS tool output that names a file and says "deprecated".
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

PY = re.compile(r"^\s*(?P<file>[^\s:][^:]*?\.py):(?P<line>\d+): (?P<cat>\w*(?:Deprecation|Future)Warning): (?P<msg>.+)$")
JS = re.compile(r"(?P<file>[\w./@-]+\.(?:js|jsx|ts|tsx|mjs|cjs)):(?P<line>\d+)(?::\d+)?\b.*?(?P<msg>\bdeprecated\b.*)$", re.I)
NODE = re.compile(r"\[(?P<code>DEP\d+)\] DeprecationWarning: (?P<msg>.+)$")
NPM = re.compile(r"npm warn deprecated (?P<pkg>\S+): (?P<msg>.+)$", re.I)
VENDOR = ("site-packages", "dist-packages", "node_modules", "/.venv/", "/venv/", "/lib/python", "<frozen")


@dataclass(frozen=True)
class Deprecation:
    kind: str  # ours | dependency | runtime
    category: str
    message: str
    file: str = ""
    line: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _ours(path: str, root: Path) -> str | None:
    if any(v in path for v in VENDOR):
        return None
    p = Path(path)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return None
    return p.as_posix() if (root / p).exists() else None


def parse(text: str, root: Path) -> list[Deprecation]:
    found: list[Deprecation] = []
    for line in text.splitlines():
        if m := PY.match(line):
            rel = _ours(m.group("file"), root)
            if rel:
                found.append(Deprecation("ours", m.group("cat"), m.group("msg").strip(), rel, int(m.group("line"))))
        elif m := NPM.search(line):
            found.append(Deprecation("dependency", "npm deprecated", f"{m.group('pkg')}: {m.group('msg').strip()}"))
        elif m := NODE.search(line):
            found.append(Deprecation("runtime", m.group("code"), m.group("msg").strip()))
        elif m := JS.search(line):
            rel = _ours(m.group("file"), root)
            if rel:
                found.append(Deprecation("ours", "deprecated", m.group("msg").strip(), rel, int(m.group("line"))))
    return list(dict.fromkeys(found))
