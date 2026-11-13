"""Every activity the engine's worker registers, by the string names workflows call them with."""

from __future__ import annotations

from typing import Any, Callable

from temporalio.client import Client

from engine.activities.agent import run_agent_session
from engine.activities.repo import deliver_report, prepare_worktree, remove_worktree, resolve_team
from engine.activities.teams import TeamActivities
from engine.settings import EngineSettings


def all_activities(client: Client, settings: EngineSettings) -> list[Callable[..., Any]]:
    team = TeamActivities(client, settings)
    return [
        resolve_team,
        prepare_worktree,
        run_agent_session,
        deliver_report,
        remove_worktree,
        team.check_budget,
        team.record_outcome,
        team.wake_team,
    ]
