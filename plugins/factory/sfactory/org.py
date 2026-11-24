"""The factory's org: departments are functions (Chief of Staff, Product, Design, Engineering, Quality, Release,
Upkeep, Direct), and a project (repo) is a second dimension that cuts across them.

The shape follows a product's org registry: a department has an id, a name, a description, who it reports to, and
the processes and agents it owns: skills (as invoked, "factory:upgrade" or "fleet"), subagent types and factory
capabilities (`factory run` / engine teams). Who says what:

    sfactory/org.toml                    the default org (generic)
    SKILL.md `metadata: department: x`   a factory skill declares its own department
    user file [org] / [[org.departments]] renames, describes or extends a department, or adds one; [org.skills] etc.
                                         map any skill, subagent type or capability for every project
    a repo's .factory.toml [org.skills] / [org.agents] / [org.capabilities]
                                         map that repo's own skills (its /design-task → design), for its turns only

Attribution of a turn (sfactory/usage.py applies it): a factory run takes its capability's department; a subagent
whose type is mapped takes that department; otherwise the last mapped skill invoked in the session before the turn
(a subagent with none of its own inherits its parent's stage when it was spawned; `/clear` resets the stage); else
Direct, the only inferred bucket. Signals are stored raw and resolved at query time, so a changed org re-buckets
all history. Reference: docs/factory-toml.md, section "Org".
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
DEFAULT_ORG = PACKAGE / "org.toml"
PLUGIN_ROOT = PACKAGE.parent  # plugins/factory
FACTORY_ROOT = PLUGIN_ROOT.parent.parent
DEPARTMENT_KEYS = ("id", "name", "description", "reports_to", "skills", "agents", "capabilities")
MAP_KEYS = ("skills", "agents", "capabilities")  # [org.skills] etc.: name -> department id


class OrgError(ValueError):
    pass


@dataclass
class Department:
    id: str
    name: str = ""
    description: str = ""
    reports_to: str = ""
    skills: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)  # subagent types
    capabilities: list[str] = field(default_factory=list)


@dataclass
class Org:
    name: str = ""
    direct: str = "direct"
    departments: list[Department] = field(default_factory=list)
    # kind ("skills" | "agents" | "capabilities") -> name -> department id, for every project
    maps: dict[str, dict[str, str]] = field(default_factory=lambda: {k: {} for k in MAP_KEYS})
    # project -> kind -> name -> department id: a repo's own mappings, for its turns only
    project_maps: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    declared_by: dict[str, str] = field(default_factory=dict)  # skill -> the SKILL.md that declared it

    def department(self, dept_id: str) -> Department | None:
        return next((d for d in self.departments if d.id == dept_id), None)

    def name_of(self, dept_id: str) -> str:
        d = self.department(dept_id)
        return d.name if d and d.name else dept_id

    def _lookup(self, kind: str, name: str | None, project: str | None) -> str | None:
        if not name:
            return None
        own = self.project_maps.get(project or "", {}).get(kind, {})
        return own.get(name) or self.maps[kind].get(name)

    def skill(self, name: str | None, project: str | None = None) -> str | None:
        return self._lookup("skills", name, project)

    def agent(self, subagent_type: str | None, project: str | None = None) -> str | None:
        return self._lookup("agents", subagent_type, project)

    def capability(self, name: str | None, project: str | None = None) -> str | None:
        return self._lookup("capabilities", name, project)

    def as_dict(self) -> dict:
        """The org in the registry's shape, with every mapping resolved (`factory cost org --json`)."""
        owned: dict[str, dict[str, list[str]]] = {d.id: {k: [] for k in MAP_KEYS} for d in self.departments}
        for kind, names in self.maps.items():
            for name, dept in sorted(names.items()):
                owned.setdefault(dept, {k: [] for k in MAP_KEYS})[kind].append(name)
        return {"organization": {"name": self.name, "direct": self.direct, "departments": [
            {"id": d.id, "name": d.name, "description": d.description, "reports_to": d.reports_to, **owned[d.id]}
            for d in self.departments]}, "projects": self.project_maps}

    # ── Building ──

    def add_department(self, d: Department) -> None:
        existing = self.department(d.id)
        if existing is None:
            self.departments.append(d)
            existing = d
        else:
            for key in ("name", "description", "reports_to"):
                if getattr(d, key):
                    setattr(existing, key, getattr(d, key))
        for kind in MAP_KEYS:
            for name in getattr(d, kind):
                self.maps[kind][name] = d.id

    def check(self, where: str) -> None:
        """Every mapping names a department that exists, and the Direct bucket exists."""
        ids = {d.id for d in self.departments}
        if self.direct not in ids:
            raise OrgError(f"{where}: [org] direct = {self.direct!r} is not a department")
        bad = [(kind, name, dept) for kind, names in self.maps.items() for name, dept in names.items() if dept not in ids]
        bad += [(f"{p}: {kind}", name, dept) for p, maps in self.project_maps.items()
                for kind, names in maps.items() for name, dept in names.items() if dept not in ids]
        if bad:
            kind, name, dept = bad[0]
            raise OrgError(f"{where}: {kind} {name!r} maps to unknown department {dept!r} (known: {', '.join(sorted(ids))})")


def _strings(value, where: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise OrgError(f"{where} must be a list of names")
    return list(value)


def parse_departments(raw: list, where: str) -> list[Department]:
    if not isinstance(raw, list):
        raise OrgError(f"{where}: departments are [[org.departments]] entries")
    out = []
    for i, body in enumerate(raw, 1):
        if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not body["id"]:
            raise OrgError(f"{where}: department {i} needs an id")
        bad = set(body) - set(DEPARTMENT_KEYS)
        if bad:
            raise OrgError(f"{where}: department {body['id']}: unknown {', '.join(sorted(bad))} "
                           f"(known: {', '.join(DEPARTMENT_KEYS)})")
        kw = {k: str(body[k]) for k in ("name", "description", "reports_to") if k in body}
        kw.update({k: _strings(body[k], f"{where}: department {body['id']}.{k}") for k in MAP_KEYS if k in body})
        out.append(Department(body["id"], **kw))
    return out


def parse_maps(raw: dict, where: str) -> dict[str, dict[str, str]]:
    """[org.skills] / [org.agents] / [org.capabilities]: name = "department id"."""
    out: dict[str, dict[str, str]] = {}
    for kind in MAP_KEYS:
        table = raw.get(kind, {})
        if not isinstance(table, dict) or not all(isinstance(v, str) and v for v in table.values()):
            raise OrgError(f"{where}: [org.{kind}] maps a name to a department id")
        if table:
            out[kind] = dict(table)
    return out


ORG_KEYS = {"name", "direct", "departments", *MAP_KEYS}


def check_section(raw: dict, where: str, repo: bool) -> None:
    if not isinstance(raw, dict):
        raise OrgError(f"{where}: [org] must be a table")
    allowed = set(MAP_KEYS) if repo else ORG_KEYS
    bad = set(raw) - allowed
    if bad:
        hint = " (a repo maps its own skills; the org itself lives in the user file)" if repo else ""
        raise OrgError(f"{where}: unknown org.{', org.'.join(sorted(bad))}{hint} (known: {', '.join(sorted(allowed))})")


def skill_declarations(roots: list[tuple[Path, str]] | None = None) -> dict[str, tuple[str, str]]:
    """skill as invoked -> (department, SKILL.md path), from `metadata: department:` in the factory's own skills.
    `roots` is a list of (skills dir, prefix): the plugin's skills are invoked as "<plugin>:<name>"."""
    from skillcheck import frontmatter  # the same parser skillcheck uses for SKILL.md files

    if roots is None:
        try:
            plugin = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())["name"]
        except (OSError, ValueError, KeyError):
            plugin = PLUGIN_ROOT.name
        roots = [(PLUGIN_ROOT / "skills", f"{plugin}:"), (FACTORY_ROOT / "home" / "skills", "")]
    out = {}
    for base, prefix in roots:
        for skill_md in sorted(base.glob("*/SKILL.md")):
            fm = frontmatter.parse(skill_md.read_text())
            meta = fm.data.get("metadata") if fm.present and not fm.error else None
            dept = meta.get("department") if isinstance(meta, dict) else None
            if dept:
                name = fm.data.get("name") or skill_md.parent.name
                out[f"{prefix}{name}"] = (str(dept), str(skill_md))
    return out


def load(user_raw: dict | None = None, where: str = "the user file",
         declarations: dict[str, tuple[str, str]] | None = None) -> Org:
    """The default org, then the factory skills' own declarations, then the user file's [org]."""
    base = tomllib.loads(DEFAULT_ORG.read_text())["org"]
    org = Org(name=base.get("name", ""), direct=base.get("direct", "direct"))
    for d in parse_departments(base["departments"], str(DEFAULT_ORG)):
        org.add_department(d)
    for skill, (dept, path) in (skill_declarations() if declarations is None else declarations).items():
        org.maps["skills"][skill] = dept
        org.declared_by[skill] = path
    if user_raw:
        check_section(user_raw, where, repo=False)
        org.name = str(user_raw.get("name", org.name))
        org.direct = str(user_raw.get("direct", org.direct))
        for d in parse_departments(user_raw.get("departments", []), where):
            org.add_department(d)
        for kind, names in parse_maps(user_raw, where).items():
            org.maps[kind].update(names)
    org.check(where)
    return org


def add_project(org: Org, project: str, raw: dict | None, where: str) -> None:
    """A repo's own mappings, which apply to its turns only."""
    if not raw:
        return
    check_section(raw, where, repo=True)
    org.project_maps[project] = parse_maps(raw, where)
    org.check(where)
