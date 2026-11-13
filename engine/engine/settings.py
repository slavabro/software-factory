"""The engine's settings: `~/.config/software-factory/engine.toml` (path in FACTORY_ENGINE_CONFIG), defaults below.

```toml
repos = ["~/Dev/FamilyOffice", "~/Dev/software-factory"]   # repos whose teams the engine runs
server_port = 7243        # host port of the engine's own Temporal server (container port stays 7233)
ui_port = 8243
unattended = true         # false: no team starts an agent session (the spend switch)
```
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from factory_runtime.temporal import TemporalSettings

CONFIG_PATH = Path(os.environ.get("FACTORY_ENGINE_CONFIG", "~/.config/software-factory/engine.toml")).expanduser()


def default_state() -> Path:
    """Logs, worktrees and team notebooks (FACTORY_ENGINE_STATE overrides, read when settings are built)."""
    return Path(os.environ.get("FACTORY_ENGINE_STATE", "~/.local/state/software-factory")).expanduser()


NAMESPACE = "factory"
TASK_QUEUE = "factory"
DEPLOYMENT = "factory-engine"
SEARCH_ATTRIBUTES = ("FactoryRepo", "FactoryTeam")  # Keyword, registered by the setup container
KEYS = {"repos", "server_host", "server_port", "ui_port", "unattended", "search_attributes"}


@dataclass(frozen=True)
class EngineSettings:
    repos: tuple[str, ...] = ()
    server_host: str = "localhost"
    server_port: int = 7243
    ui_port: int = 8243
    unattended: bool = True
    search_attributes: bool = True  # tests run without them (the test server has none registered)
    state: Path = field(default_factory=default_state)

    @property
    def temporal(self) -> TemporalSettings:
        return TemporalSettings(task_queue=TASK_QUEUE, server_url=f"{self.server_host}:{self.server_port}", namespace=NAMESPACE)

    def repo_paths(self) -> list[Path]:
        return [Path(r).expanduser().resolve() for r in self.repos]


def load(path: Path = CONFIG_PATH) -> EngineSettings:
    if not path.is_file():
        return EngineSettings()
    raw = tomllib.loads(path.read_text())
    unknown = set(raw) - KEYS
    if unknown:
        raise ValueError(f"{path}: unknown key(s) {', '.join(sorted(unknown))} (known: {', '.join(sorted(KEYS))})")
    if "repos" in raw:
        raw["repos"] = tuple(str(r) for r in raw["repos"])
    return EngineSettings(**raw)
