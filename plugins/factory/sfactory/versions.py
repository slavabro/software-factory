"""Version parsing and comparison without third-party packages.

Good enough for release ordering across PyPI, npm and image tags: numeric parts compare as numbers, anything with a
pre-release marker is not a release, and a tag only competes with tags of the same shape (`16-alpine` with
`17-alpine`, never with `17`).
"""

from __future__ import annotations

import re

PRE = re.compile(r"(?i)\d(a|b|c|rc|dev)\d*$|\d(a|b|rc|dev)\d+|[-.](alpha|beta|rc|dev|pre|preview|canary|next|nightly|snapshot)(\d|\b)")
NUM = re.compile(r"\d+")


def is_release(version: str) -> bool:
    core = version.split("+", 1)[0]
    return bool(NUM.search(core)) and not PRE.search(core)


def key(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in NUM.findall(version.split("+", 1)[0].split("-", 1)[0]))


def shape(tag: str) -> str:
    """`16-alpine` -> `N-alpine`; `1.32.0` -> `N.N.N`; `3.13-slim` -> `N.N-slim`."""
    return NUM.sub("N", tag)


def bump(current: str, latest: str) -> str:
    """patch | minor | major | none — by the first differing numeric part."""
    a, b = key(current), key(latest)
    if b <= a:
        return "none"
    for i, (x, y) in enumerate(zip(a + (0,) * 3, b + (0,) * 3)):
        if x != y:
            # 0.x: a minor bump is breaking by semver convention
            if i == 0 or (i == 1 and a[:1] == (0,)):
                return "major"
            return "minor" if i == 1 else "patch"
    return "patch"


def newer(current: str, candidates: list[str], same_shape: bool = False) -> list[str]:
    """Releases newer than `current`, oldest first."""
    cur = key(current)
    out = [
        v for v in candidates
        if is_release(v) and key(v) > cur and (not same_shape or shape(v) == shape(current))
    ]
    return sorted(set(out), key=key)
