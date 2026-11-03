"""The deterministic half of the upgrade capability: what can move, how far, and where its notes are.

The judgement half (read the notes, change the code, verify, land) is the `/factory:upgrade` skill.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatch

from . import manifests, registries, versions
from .config import Config


@dataclass
class Candidate:
    ecosystem: str
    name: str
    current: str
    latest: str
    bump: str  # patch | minor | major
    risky: bool
    newer_count: int
    locations: list[str] = field(default_factory=list)  # every manifest:line that pins it
    notes: list[str] = field(default_factory=list)
    required_by: list[str] = field(default_factory=list)  # other scanned packages that depend on it (move together)


@dataclass
class ScanResult:
    candidates: list[Candidate]
    up_to_date: int
    errors: list[str]

    def as_dict(self) -> dict:
        return {"candidates": [asdict(c) for c in self.candidates], "up_to_date": self.up_to_date, "errors": self.errors}


def _match(name: str, globs: list[str]) -> bool:
    return any(fnmatch(name, g) for g in globs)


def scan(cfg: Config, fetch: registries.Fetch = registries.fetch_json, workers: int = 8) -> ScanResult:
    up = cfg.upgrade
    deps = [d for d in manifests.read(cfg.root, up.manifests or None, up.ecosystems) if not _match(d.name, up.ignore)]
    unique = {(d.ecosystem, d.name) for d in deps}

    def look(key):
        eco, name = key
        try:
            return key, registries.LOOKUP[eco](name, fetch), None
        except Exception as exc:  # one registry hiccup must not lose the rest of the scan
            return key, None, f"{eco}:{name}: {exc}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        looked = list(pool.map(look, sorted(unique)))
    info = {k: v for k, v, _ in looked if v}
    errors = [e for _, _, e in looked if e]

    required_by: dict[str, list[str]] = {}
    for (eco, name), found in info.items():
        for req in found[2]:
            required_by.setdefault(req, []).append(name)

    grouped: dict[tuple[str, str, str], list] = {}
    for d in deps:
        grouped.setdefault((d.ecosystem, d.name, d.current), []).append(d)
    candidates, fresh = [], 0
    for (eco, name, current), group in grouped.items():
        found = info.get((eco, name))
        if not found:
            continue
        all_versions, notes, _ = found
        newer = versions.newer(current, all_versions, eco)
        if not newer:
            fresh += 1
            continue
        latest = newer[-1]
        candidates.append(Candidate(
            eco, name, current, latest, versions.bump(current, latest),
            risky=_match(name, up.risky), newer_count=len(newer),
            locations=[f"{d.manifest}:{d.line}" for d in group], notes=notes,
            required_by=sorted(set(required_by.get(name, [])) & {n for _, n in unique}) if eco == "pip" else [],
        ))
    order = {"major": 0, "minor": 1, "patch": 2}
    candidates.sort(key=lambda c: (c.risky, order.get(c.bump, 3), c.ecosystem, c.name))
    return ScanResult(candidates, fresh, errors)


def markdown(result: ScanResult) -> str:
    rows = ["| Package | Ecosystem | Now | Latest | Bump | Risky | Required by | Notes |", "|---|---|---|---|---|---|---|---|"]
    for c in result.candidates:
        rows.append(
            f"| {c.name} | {c.ecosystem} | {c.current} | {c.latest} | {c.bump} | {'yes — task' if c.risky else 'no'} | "
            f"{', '.join(c.required_by) or '—'} | " + (c.notes[0] if c.notes else "—") + " |"
        )
    tail = f"\n{len(result.candidates)} can move · {result.up_to_date} up to date"
    if result.errors:
        tail += f" · {len(result.errors)} lookups failed: " + "; ".join(result.errors[:5])
    return "\n".join(rows) + tail
