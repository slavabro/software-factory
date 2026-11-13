"""A throwaway repo with an origin and a .factory.toml whose agent is a shell command, so activities run for real."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_repo(agent_script: str, extra_toml: str = "") -> tuple[Path, Path, tempfile.TemporaryDirectory]:
    """(repo, state dir, the temp dir to clean). The agent runs `sh -c <agent_script>` in the worktree."""
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    origin, repo, state = base / "origin.git", base / "repo", base / "state"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=base)
    git("init", "-q", "-b", "main", str(repo), cwd=base)
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git("config", key, value, cwd=repo)
    (repo / ".factory.toml").write_text(
        f'[repo]\nname = "demo"\n\n[agent]\ncommand = ["sh", "-c", {agent_script!r}]\n\n'
        f'[teams.qa]\nprompt = "/factory:qa"\nschedule = "0 2 * * *"\nwake_on = ["ci_red"]\nbudget_minutes = 5\n{extra_toml}'
    )
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    git("remote", "add", "origin", str(origin), cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    os.environ["FACTORY_ENGINE_STATE"] = str(state)
    return repo, state, tmp
