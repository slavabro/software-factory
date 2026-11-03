"""PreToolUse guard for unattended factory runs: refuse the few actions a run must never take, whatever the
permission mode or allowlist says. Active only when the runner set FACTORY_RUN; a no-op in normal sessions, and
fail-closed inside a run (any error blocks).

Blocked: merging a PR (CLI, REST or GraphQL), pushing / force-pushing / deleting the default branch, pushing
HEAD from anything but a factory/* branch, write calls through `gh api`, repo and secret changes, `find -exec`.
Exit 2 with a reason blocks the tool call (Claude Code hook contract).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# git's global options may sit between `git` and the subcommand: `git -C dir push`, `git -c k=v push`.
GIT = r"\bgit(?:\s+(?:-C\s+\S+|-c\s+\S+|--git-dir[= ]\S+|--work-tree[= ]\S+|--no-pager|-P|--bare))*\s+"
GH = r"\bgh(?:\s+(?:-R\s+\S+|--repo[= ]\S+))*\s+"


def rules(default_branch: str) -> list[tuple[re.Pattern, str]]:
    b = re.escape(default_branch)
    return [
        (re.compile(GH + r"pr\s+merge\b"), "merging is the repo's gate's job, never a factory run's"),
        (re.compile(GH + r"api\b[^|;&]*(-X|--method)\s*['\"]?(POST|PUT|PATCH|DELETE)", re.I), "gh api is read-only in a factory run"),
        (re.compile(GH + r"api\b[^|;&]*/(merges?|pulls/\d+/merge)\b"), "merging is the repo's gate's job"),
        (re.compile(GH + r"api\s+graphql\b[^|;&]*(mutation|merge|automerge)", re.I), "gh api graphql is read-only in a factory run"),
        (re.compile(GH + r"(repo|secret|ruleset|variable|auth)\s"), "repo settings, secrets and auth are out of scope"),
        (re.compile(GIT + r"push\b[^|;&]*(\s--force\b|\s-f\b|\s--force-with-lease\b|\s--delete\b|\s-d\b|\s\+|\s--mirror\b|\s--all\b)"),
         "no force pushes, remote deletes or bulk pushes"),
        (re.compile(GIT + rf"push\b[^|;&]*(\s|:|/){b}(\s|$|[;&|])"), f"never push {default_branch}; open a PR"),
        (re.compile(GIT + r"push(\s+-u)?(\s+origin)?\s*($|[;&|])"), "push a named factory/* branch explicitly"),
        (re.compile(r"\bfind\b[^|;&]*\s-(exec|execdir|ok|okdir|delete)\b"), "find may only list files in a factory run"),
    ]


HEAD_PUSH = re.compile(GIT + r"push\b[^|;&]*\sHEAD(\s|$|[;&|])")


def check(command: str, default_branch: str, cwd: str | None = None) -> str | None:
    for rx, why in rules(default_branch):
        if rx.search(command):
            return why
    if HEAD_PUSH.search(command):
        branch = subprocess.run(
            ["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=cwd or None, capture_output=True, text=True
        ).stdout.strip()
        if not branch.startswith("factory/"):
            return f"HEAD is {branch or 'unknown'}, not a factory/* branch"
    return None


def main() -> int:
    if not os.environ.get("FACTORY_RUN"):
        return 0
    try:
        event = json.load(sys.stdin)
        if event.get("tool_name") != "Bash":
            return 0
        command = (event.get("tool_input") or {}).get("command", "")
        why = check(command, os.environ.get("FACTORY_DEFAULT_BRANCH", "main"), event.get("cwd"))
    except Exception as exc:  # fail closed inside a run
        print(f"factory guard: could not check the command ({exc}); blocked", file=sys.stderr)
        return 2
    if why:
        print(f"factory guard: blocked `{command[:120]}` — {why}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
