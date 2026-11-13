"""The engine's one worker: task queue `factory`, Worker Versioning with Pinned workflows (docs/designs/engine.md,
section 6). Its Build ID is a hash of the code that can change workflow behaviour (engine/, runtime/, the lockfile),
so a docs-only merge deploys nothing (the hash: sfactory/engine.py build_id)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from temporalio.client import Client
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig

from factory_runtime.temporal import build_workflow_runner, get_client
from engine import settings as settings_mod
from engine.activities import all_activities
from engine.factory import install
from engine.settings import DEPLOYMENT, TASK_QUEUE, EngineSettings
from engine.workflows import WORKFLOWS

REPO = Path(__file__).resolve().parents[2]
PASSTHROUGH = ("engine.models",)


def build_id(root: Path = REPO) -> str:
    """The Build ID of the code at `root` (one owner: sfactory/engine.py, which install and upgrade also use)."""
    result: str = install.build_id(root)
    return result


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
        # On stop: 10 s for activities to finish, then they are cancelled and an agent session kills its process
        # group (up to KILL_GRACE); the service manager allows SHUTDOWN_ALLOWANCE for all of it.
        graceful_shutdown_timeout=timedelta(seconds=10),
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
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):  # launchd / systemd stop: shut down gracefully, never orphan agents
        loop.add_signal_handler(sig, stop.set)
    async with make_worker(client, settings, build=build):
        await stop.wait()
    logging.info("factory engine worker: stopped")


if __name__ == "__main__":
    asyncio.run(main())
