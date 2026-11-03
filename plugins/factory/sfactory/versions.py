"""Version parsing and comparison without third-party packages, per ecosystem's own rules.

- npm (semver): anything after `-` in the version is a pre-release (`6.0.0-insiders.1`, `2.0.0-0`).
- pip (PEP 440): `a` / `b` / `rc` / `c` / `alpha` / `beta` / `pre` / `dev` markers are pre-releases; `.post` is not.
- docker: a tag is a release unless one of its words is a pre-release word; it only competes with tags of the same
  shape (`16-alpine` with `17-alpine`, never with `17` or `17-bookworm`).
Date-based versions (`2025.8.3`) bump by year as `minor`, otherwise `patch` — never `major`.
"""

from __future__ import annotations

import re

NUM = re.compile(r"\d+")
PEP440_PRE = re.compile(r"(?i)(?:\d|[._-])(a|b|c|rc|alpha|beta|pre|preview|dev)[._-]?\d*(?=$|[._+-])")
PRE_WORDS = {"rc", "alpha", "beta", "dev", "pre", "preview", "canary", "next", "nightly", "snapshot", "insiders",
             "experimental", "edge", "test", "unstable"}
CALVER_YEAR = 1990


def _words(tag: str) -> list[str]:
    return [w for w in re.split(r"[^A-Za-z0-9]+", tag.lower()) if w]


def is_release(version: str, ecosystem: str = "pip") -> bool:
    core = version.split("+", 1)[0]
    if not NUM.search(core):
        return False
    if ecosystem == "npm":
        return re.fullmatch(r"\d+(\.\d+)*", core.lstrip("v")) is not None
    if ecosystem == "docker":
        return not any(re.sub(r"\d+$", "", w) in PRE_WORDS for w in _words(core)) and not PEP440_PRE.search(core)
    return not PEP440_PRE.search(core.lstrip("v"))


def key(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in NUM.findall(version.split("+", 1)[0].split("-", 1)[0]))


def _sort_key(version: str) -> tuple:
    # Same numbers: a plain release outranks a suffixed one, then the full string decides (stable, deterministic)
    return key(version), "-" not in version, version


def shape(tag: str) -> str:
    """`16-alpine` -> `N-alpine`; `1.32.0` -> `N.N.N`; `3.13-slim` -> `N.N-slim`."""
    return NUM.sub("N", tag)


def is_calver(version: str) -> bool:
    k = key(version)
    return bool(k) and k[0] >= CALVER_YEAR


def bump(current: str, latest: str) -> str:
    """patch | minor | major | none — by the first differing numeric part."""
    a, b = key(current), key(latest)
    if b <= a:
        return "none"
    if is_calver(current) and is_calver(latest):
        return "minor" if a[0] != b[0] else "patch"
    for i, (x, y) in enumerate(zip(a + (0,) * 3, b + (0,) * 3)):
        if x != y:
            # 0.x: a minor bump is breaking by semver convention
            if i == 0 or (i == 1 and a[:1] == (0,)):
                return "major"
            return "minor" if i == 1 else "patch"
    return "patch"


def newer(current: str, candidates: list[str], ecosystem: str = "pip") -> list[str]:
    """Releases newer than `current`, oldest first (the last one is the latest)."""
    cur = key(current)
    docker = ecosystem == "docker"
    out = [
        v for v in candidates
        if is_release(v, ecosystem) and key(v) > cur and (not docker or shape(v) == shape(current))
    ]
    return sorted(set(out), key=_sort_key)
