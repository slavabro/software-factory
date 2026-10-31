"""skillcheck — lint Claude Code skills, commands, subagents and CLAUDE.md files.

    skillcheck [ROOT ...] [--scope auto|project|user] [--format text|github|json] [--strict] [--config FILE]
    skillcheck --hook        # SessionStart hook: one short note in the session, only when something is wrong

Exit 1 when a finding at or above `fail_on` (config, or --strict → warning) exists.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import config as config_mod
from .checks import RULES, Checker, discover

USER_SKILLS = Path.home() / ".claude" / "skills"


def _git_root(path: Path) -> Path | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 else None


def _scope(root: Path, requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        root.resolve().relative_to((Path.home() / ".claude").resolve())
        return "user"
    except ValueError:
        return "project"


def check_root(root: Path, scope: str = "auto", config_path: Path | None = None):
    root = root.resolve()
    cfg = config_mod.load(root, config_path)
    checker = Checker(cfg, _scope(root, scope))
    return cfg, checker.run(discover(cfg))


def _fails(findings, fail_on: str) -> bool:
    levels = {"error": ("error",), "warning": ("error", "warning")}[fail_on]
    return any(f.severity in levels for f in findings)


def hook() -> int:
    """SessionStart: check the session's project and the user skills; say nothing when all is well."""
    notes = []
    project = _git_root(Path.cwd())
    roots = [r for r in (project, USER_SKILLS) if r and r.is_dir()]
    for root in roots:
        if root == project and not ((root / ".claude").is_dir() or (root / "CLAUDE.md").is_file()):
            continue
        try:
            _, findings = check_root(root)
        except Exception as exc:  # a broken linter must never break a session
            return 0
        errors = [f for f in findings if f.severity == "error"]
        if errors:
            sample = "; ".join(f"{f.path}:{f.line} {f.rule}" for f in errors[:3])
            notes.append(f"{len(errors)} skill errors in {root} ({sample}{' …' if len(errors) > 3 else ''})")
    if notes:
        print("skillcheck: " + " · ".join(notes) + ". Run `skillcheck` there for details; fix them when you touch those files.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="skillcheck", description=__doc__.split("\n\n")[0])
    ap.add_argument("roots", nargs="*", type=Path, help="repo root, skills dir or plugin dir (default: git root of cwd)")
    ap.add_argument("--scope", choices=["auto", "project", "user"], default="auto")
    ap.add_argument("--format", choices=["text", "github", "json"], default="text")
    ap.add_argument("--strict", action="store_true", help="fail on warnings too")
    ap.add_argument("--config", type=Path)
    ap.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--rules", action="store_true", help="list rule IDs and exit")
    args = ap.parse_args(argv)

    if args.hook:
        return hook()
    if args.rules:
        for rid, (sev, title) in RULES.items():
            print(f"{rid}  {sev:<7}  {title}")
        return 0

    roots = args.roots or [_git_root(Path.cwd()) or Path.cwd()]
    failed = False
    out = []
    for root in roots:
        cfg, findings = check_root(root, args.scope, args.config)
        fail_on = "warning" if args.strict else cfg.fail_on
        failed |= _fails(findings, fail_on)
        findings.sort(key=lambda f: (f.path, f.line, f.rule))
        if args.format == "json":
            out.extend({"root": str(root), **f.__dict__} for f in findings)
            continue
        for f in findings:
            if args.format == "github":
                level = "error" if f.severity == "error" else "warning"
                print(f"::{level} file={f.path},line={max(f.line, 1)},title={f.rule}::{f.message}")
            else:
                print(f.text())
        errors = sum(f.severity == "error" for f in findings)
        print(f"skillcheck {root}: {errors} errors, {len(findings) - errors} warnings", file=sys.stderr)
    if args.format == "json":
        print(json.dumps(out, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
