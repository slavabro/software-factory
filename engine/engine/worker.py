"""The engine's one worker: task queue `factory`, Worker Versioning with Pinned workflows (docs/designs/engine.md,
section 6). Its Build ID is a hash of the code that can change workflow behaviour (engine/, runtime/, the lockfile),
so a docs-only merge deploys nothing."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from temporalio.client import Client
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig

from factory_runtime.temporal import build_workflow_runner, get_client
from engine import settings as settings_mod
from engine.activities import all_activities
from engine.settings import DEPLOYMENT, TASK_QUEUE, EngineSettings
from engine.workflows import WORKFLOWS

REPO = Path(__file__).resolve().parents[2]
VERSIONED = ("engine", "runtime")  # directories whose code is the engine's behaviour
PASSTHROUGH = ("engine.models",)


def build_id(root: Path = REPO) -> str:
    """sha256 over the engine's and runtime's source and the lockfile, by path then content; 12 hex characters."""
    digest = hashlib.sha256()
    files = [p for d in VERSIONED for p in sorted((root / d).rglob("*")) if p.is_file() and _counts(p, root)]
    for path in files:
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def _counts(path: Path, root: Path) -> bool:
    rel = path.relative_to(root).parts
    if any(p in {"__pycache__", "tests", ".venv", "build"} or p.endswith(".egg-info") for p in rel):
        return False
    return path.suffix in {".py", ".toml", ".lock", ".txt"}


def make_worker(
    client: Client,
    settings: EngineSettings,
    *,
    versioned: bool = True,
    build: str | None = None,
    activities: list[Callable[..., Any]] | None = None,
) -> Worker:
    """The engine's worker; `activities` replaces the real ones (tests register stand-ins under the same names)."""
    deployment = None
    if versioned:
        deployment = WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(deployment_name=DEPLOYMENT, build_id=build or build_id()),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        )
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=WORKFLOWS,
        activities=activities if activities is not None else all_activities(client, settings),
        workflow_runner=build_workflow_runner(PASSTHROUGH),
        deployment_config=deployment,
        graceful_shutdown_timeout=timedelta(seconds=60),
    )


async def main() -> None:
    parser = argparse.ArgumentParser(prog="engine.worker", description=__doc__.splitlines()[0])
    parser.add_argument("--build-id", help="override the computed Build ID")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = settings_mod.load()
    client = await get_client(settings.temporal)
    build = args.build_id or build_id()
    logging.info(f"factory engine worker: deployment {DEPLOYMENT}, build {build}, queue {TASK_QUEUE}")
    await make_worker(client, settings, build=build).run()


if __name__ == "__main__":
    asyncio.run(main())
