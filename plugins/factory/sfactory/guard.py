"""PreToolUse guard for unattended factory runs: refuse the few actions a run must never take, whatever the
permission mode or allowlist says. Active only when the runner set FACTORY_RUN; a no-op in normal sessions.

Blocked: merging a PR, pushing / force-pushing / deleting the default branch, write calls through `gh api`, repo
and secret changes. Exit 2 with a reason blocks the tool call (Claude Code hook contract).
"""

from __future__ import annotations

import json
import os
import re
import sys


def rules(default_branch: str) -> list[tuple[re.Pattern, str]]:
    b = re.escape(default_branch)
    return [
        (re.compile(r"\bgh\s+pr\s+merge\b"), "merging is the repo's gate's job, never a factory run's"),
        (re.compile(r"\bgh\s+api\b[^|;&]*(-X|--method)\s*['\"]?(POST|PUT|PATCH|DELETE)", re.I), "gh api is read-only in a factory run"),
        (re.compile(r"\bgh\s+api\b[^|;&]*/(merges?|pulls/\d+/merge)\b"), "merging is the repo's gate's job"),
        (re.compile(r"\bgh\s+(repo|secret|ruleset|variable)\s"), "repo settings and secrets are out of scope"),
        (re.compile(r"\bgit\s+push\b[^|;&]*(\s--force\b|\s-f\b|\s--force-with-lease\b|\s--delete\b|\s-d\b|\s\+)"),
         "no force pushes or remote deletes"),
        (re.compile(rf"\bgit\s+push\b[^|;&]*(\s|:|/){b}(\s|$|[;&|])"), f"never push {default_branch}; open a PR"),
        (re.compile(rf"\bgit\s+push\b\s*$|\bgit\s+push\s+origin\s*$"), "push a named factory/* branch explicitly"),
    ]


def check(command: str, default_branch: str) -> str | None:
    for rx, why in rules(default_branch):
        if rx.search(command):
            return why
    return None


def main() -> int:
    if not os.environ.get("FACTORY_RUN"):
        return 0
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if event.get("tool_name") != "Bash":
        return 0
    command = (event.get("tool_input") or {}).get("command", "")
    why = check(command, os.environ.get("FACTORY_DEFAULT_BRANCH", "main"))
    if why:
        print(f"factory guard: blocked `{command[:120]}` — {why}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
