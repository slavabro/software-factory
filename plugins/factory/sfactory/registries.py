"""Versions, release-notes links and (PyPI) requirements from the public registries (read-only, no credentials).

Each lookup returns (versions, notes, requires).

`fetch_json` is injectable so tests never touch the network.
"""

from __future__ import annotations

import json
import re
import ssl
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

Fetch = Callable[[str], dict]
UA = {"User-Agent": "software-factory-upgrade-scout"}
GITHUB_RE = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:[/#?].*)?$")


CA_FILES = ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt")


def _ssl_context() -> ssl.SSLContext:
    """Some Pythons (python.org macOS builds) ship without a CA store: use certifi or the system bundle."""
    try:
        import certifi  # optional

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs():
        for path in CA_FILES:
            if Path(path).is_file():
                return ssl.create_default_context(cafile=path)
    return ctx


_CTX = None


def fetch_json(url: str) -> dict:
    global _CTX
    _CTX = _CTX or _ssl_context()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20, context=_CTX) as resp:  # noqa: S310 — fixed https registry URLs
        return json.loads(resp.read().decode())


def _github(url: str | None) -> str | None:
    m = GITHUB_RE.search(url or "")
    return f"https://github.com/{m.group(1)}/{m.group(2)}" if m else None


def pypi(name: str, fetch: Fetch = fetch_json) -> tuple[list[str], list[str], list[str]]:
    data = fetch(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
    versions = [v for v, files in data.get("releases", {}).items() if files and not all(f.get("yanked") for f in files)]
    info = data.get("info", {})
    urls = info.get("project_urls") or {}
    notes = [u for k, u in urls.items() if re.search(r"(?i)change|release|history|news", k)]
    repo = next((_github(u) for u in [*urls.values(), info.get("home_page")] if _github(u)), None)
    if repo:
        notes.append(f"{repo}/releases")
    requires = [re.split(r"[\s;<>=!~\[(]", req, 1)[0].lower().replace("_", "-") for req in info.get("requires_dist") or [] if "extra ==" not in req]
    return versions, list(dict.fromkeys(notes)), requires


def npm(name: str, fetch: Fetch = fetch_json) -> tuple[list[str], list[str], list[str]]:
    data = fetch("https://registry.npmjs.org/" + name.replace("/", "%2F"))
    versions = [v for v, meta in data.get("versions", {}).items() if not meta.get("deprecated")]
    repo = data.get("repository") or {}
    repo_url = repo.get("url") if isinstance(repo, dict) else repo
    gh = _github(repo_url)
    notes = [f"{gh}/releases", f"{gh}/blob/HEAD/CHANGELOG.md"] if gh else []
    return versions, notes, []


def docker(image: str, fetch: Fetch = fetch_json, pages: int = 3) -> tuple[list[str], list[str], list[str]]:
    ns, _, repo = image.rpartition("/")
    ns = ns or "library"
    url = f"https://hub.docker.com/v2/repositories/{ns}/{repo}/tags?page_size=100&ordering=last_updated"
    tags: list[str] = []
    for _ in range(pages):
        data = fetch(url)
        tags += [t["name"] for t in data.get("results", [])]
        url = data.get("next")
        if not url:
            break
    hub = f"https://hub.docker.com/_/{repo}" if ns == "library" else f"https://hub.docker.com/r/{ns}/{repo}"
    notes = [hub]
    if ns != "library":
        notes.append(f"https://github.com/{ns}/{repo}/releases")
    return tags, notes, []


LOOKUP = {"pip": pypi, "npm": npm, "docker": docker}
