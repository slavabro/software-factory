"""Read the versions a repo uses today, from the manifests every ecosystem already has.

pip: `requirements*.txt` pins (`name==x`) and `pyproject.toml` `[project] dependencies` (`==` / `>=`).
npm: `package.json` names, with the installed version from the sibling `package-lock.json` when there is one.
docker: `image:` lines in compose files and `FROM` lines in Dockerfiles (Docker Hub images with a tag).
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from dataclasses import asdict, dataclass
from fnmatch import fnmatch
from pathlib import Path

SKIP = ("node_modules/", ".venv/", "venv/", "site-packages/", "dist/", "build/", ".next/")
REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(==|>=|~=)\s*([0-9][^\s;#,]*)")
IMAGE_RE = re.compile(r"^\s*image:\s*[\"']?([a-z0-9][a-z0-9._/-]*):([A-Za-z0-9._-]+)[\"']?\s*(#.*)?$")
FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?([a-z0-9][a-z0-9._/-]*):([A-Za-z0-9._-]+)", re.I)


@dataclass
class Dep:
    ecosystem: str  # pip | npm | docker
    name: str
    current: str
    manifest: str  # repo-relative
    line: int
    spec: str = "=="  # how it is pinned

    def as_dict(self) -> dict:
        return asdict(self)


def _tracked(root: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, timeout=30, check=True)
        return [f for f in out.stdout.splitlines() if not any(s in f for s in SKIP)]
    except (OSError, subprocess.SubprocessError):
        return [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and not any(s in p.as_posix() for s in SKIP)]


def detect(root: Path) -> list[str]:
    def ok(f: str) -> bool:
        name = f.rsplit("/", 1)[-1]
        return (
            fnmatch(name, "requirements*.txt") or name in ("pyproject.toml", "package.json")
            or fnmatch(name, "docker-compose*.yml") or fnmatch(name, "docker-compose*.yaml")
            or fnmatch(name, "compose*.yml") or name == "Dockerfile" or fnmatch(name, "*.Dockerfile")
        )

    return sorted(f for f in _tracked(root) if ok(f))


def _pip_requirements(root: Path, rel: str) -> list[Dep]:
    deps = []
    for n, line in enumerate((root / rel).read_text().splitlines(), 1):
        m = REQ_RE.match(line)
        if m:
            deps.append(Dep("pip", m.group(1).lower().replace("_", "-"), m.group(4), rel, n, m.group(3)))
    return deps


def _pyproject(root: Path, rel: str) -> list[Dep]:
    text = (root / rel).read_text()
    data = tomllib.loads(text)
    lines = text.splitlines()
    deps = []
    for spec in data.get("project", {}).get("dependencies", []):
        m = REQ_RE.match(spec)
        if not m:
            continue
        n = next((i for i, l in enumerate(lines, 1) if spec in l), 0)
        deps.append(Dep("pip", m.group(1).lower().replace("_", "-"), m.group(4), rel, n, m.group(3)))
    return deps


def _npm(root: Path, rel: str) -> list[Dep]:
    pkg_path = root / rel
    pkg = json.loads(pkg_path.read_text())
    lock_path = pkg_path.parent / "package-lock.json"
    installed = {}
    if lock_path.is_file():
        lock = json.loads(lock_path.read_text())
        for key, meta in lock.get("packages", {}).items():
            if key.startswith("node_modules/") and key.count("node_modules/") == 1:
                installed[key[len("node_modules/"):]] = meta.get("version", "")
    lines = pkg_path.read_text().splitlines()
    deps = []
    for section in ("dependencies", "devDependencies"):
        for name, rng in pkg.get(section, {}).items():
            if not re.search(r"\d", rng) or rng.startswith(("file:", "link:", "git", "http", "workspace:")):
                continue
            current = installed.get(name) or re.sub(r"^[^\d]*", "", rng)
            n = next((i for i, l in enumerate(lines, 1) if f'"{name}"' in l), 0)
            deps.append(Dep("npm", name, current, rel, n, rng))
    return deps


def _docker(root: Path, rel: str) -> list[Dep]:
    rx = FROM_RE if rel.rsplit("/", 1)[-1].lower().endswith("dockerfile") else IMAGE_RE
    deps = []
    for n, line in enumerate((root / rel).read_text().splitlines(), 1):
        m = rx.match(line)
        if m and "$" not in line and not m.group(2) == "latest":
            image = m.group(1)
            if "." in image.split("/", 1)[0] and "/" in image:  # another registry host (ghcr.io/...): not Docker Hub
                continue
            deps.append(Dep("docker", image, m.group(2), rel, n, "tag"))
    return deps


def read(root: Path, manifests: list[str] | None = None, ecosystems: list[str] | None = None) -> list[Dep]:
    files = manifests or detect(root)
    eco = set(ecosystems or ["pip", "npm", "docker"])
    deps: list[Dep] = []
    for rel in files:
        name = rel.rsplit("/", 1)[-1]
        if not (root / rel).is_file():
            continue
        if "pip" in eco and fnmatch(name, "requirements*.txt"):
            deps += _pip_requirements(root, rel)
        elif "pip" in eco and name == "pyproject.toml":
            deps += _pyproject(root, rel)
        elif "npm" in eco and name == "package.json":
            deps += _npm(root, rel)
        elif "docker" in eco and (name.lower().endswith("dockerfile") or "compose" in name):
            deps += _docker(root, rel)
    return deps
