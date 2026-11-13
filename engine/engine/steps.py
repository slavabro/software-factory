"""Steps a run executes in a child process, so an activity can cancel them by killing the process group:
`python -m engine.steps prepare` reads a StepInput as JSON on stdin and prints {"code": int, "message": str}."""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from engine.factory import config, runner
from engine.models import RunPaths, StepInput

NOT_OPTED_IN = "not opted in"
# Results the run can't fix by retrying; any other non-zero code is worth another attempt.
FINAL = {runner._NOT_OPTED_IN, runner._INVALID_CONFIG}


def run_env(cfg: Any, paths: RunPaths, run_name: str) -> dict[str, str]:
    """The environment a run's commands see: the caller's, scrubbed, plus the run's names (as `factory run` sets)."""
    env = {k: v for k, v in os.environ.items() if k not in runner.SCRUB}
    env.update(
        REPO=str(cfg.root), WORKTREE=paths.worktree, FACTORY_RUN=run_name, FACTORY_BRANCH=paths.branch,
        FACTORY_REPORT=runner.REPORT_REL, FACTORY_DEFAULT_BRANCH=cfg.default_branch,
    )
    return env


def prepare(inp: StepInput) -> tuple[int, str]:
    cfg = config.load(Path(inp.ref.repo))
    Path(inp.paths.worktree).parent.mkdir(parents=True, exist_ok=True)
    Path(inp.paths.log).parent.mkdir(parents=True, exist_ok=True)
    env = run_env(cfg, inp.paths, Path(inp.paths.worktree).name)
    with open(inp.paths.log, "a") as log:
        log.write(f"factory engine: {inp.ref.team} run in {inp.paths.worktree}\n")
        code, _ = runner.prepare_worktree(cfg, Path(inp.paths.worktree), inp.paths.branch, env, log, reuse=True)
    messages = {
        0: "",
        runner._NOT_OPTED_IN: f"{NOT_OPTED_IN}: no .factory.toml on {cfg.default_branch}",
        runner._INVALID_CONFIG: f"the repo's .factory.toml is invalid; log {inp.paths.log}",
        runner._NOT_CREATED: f"could not create the worktree; log {inp.paths.log}",
        runner._SETUP_FAILED: f"the repo's worktree setup failed; log {inp.paths.log}",
    }
    return code, messages.get(code, f"prepare failed (code {code}); log {inp.paths.log}")


def _exit_on_term(*_: object) -> None:
    raise SystemExit(143)


def main() -> None:
    # A cancel kills this process group with SIGTERM. Turned into an exception, it lets the runner kill the setup
    # command it started in a group of its own (runner._sh stops its child on any exception), instead of leaving it.
    signal.signal(signal.SIGTERM, _exit_on_term)
    if sys.argv[1:] != ["prepare"]:
        raise SystemExit("usage: python -m engine.steps prepare < step.json")
    code, message = prepare(StepInput.model_validate_json(sys.stdin.read()))
    print(json.dumps({"code": code, "message": message}))


if __name__ == "__main__":
    main()
