"""The engine's settings: `~/.config/software-factory/engine.toml`, defaults below. Its path has one owner,
`sfactory/engine.py` `SETTINGS` (FACTORY_ENGINE_CONFIG names the file, else it is in FACTORY_ENGINE_CONFIG_DIR).

```toml
repos = ["~/Dev/FamilyOffice", "~/Dev/software-factory"]   # repos whose teams the engine runs
server_port = 7243        # host port of the engine's own Temporal server (container port stays 7233)
ui_port = 8243
unattended = true         # false: no team starts an agent session (the spend switch)
time_zone = "Asia/Jerusalem"  # the teams' cron schedules are in this zone; default: this machine's
desk_port = 8245          # the Factory Desk's port on 127.0.0.1 (docs/designs/desk.md)
```

The file is machine-written (`sfactory/engine.py`'s settings writer: install, the CLI and the desk), so it keeps no
comments: this docstring is its reference. Every build reads it and refuses keys it doesn't know, and old builds keep
running while their runs drain, so nothing the factory writes by itself adds a new key: a new key (like `desk_port`)
is set by hand, and only once every build still installed knows it. Machine state that isn't a setting (the current
build, the checkout it came from) lives next to the builds in the share dir instead.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from factory_runtime.temporal import TemporalSettings
from engine.factory import install



def config_path() -> Path:
    """engine.toml's path, read from its one owner each time (tests and the CLI point it elsewhere)."""
    path: Path = install.SETTINGS
    return path


def default_state() -> Path:
    """Logs, worktrees and team notebooks (FACTORY_ENGINE_STATE overrides, read when settings are built); its one
    owner is `sfactory/engine.py`, which the desk's service writes its log under too."""
    path: Path = install.state_dir()
    return path


NAMESPACE = "factory"
TASK_QUEUE = "factory"
DEPLOYMENT = "factory-engine"
SEARCH_ATTRIBUTES = ("FactoryRepo", "FactoryTeam")  # Keyword, registered by the setup container
KEYS = {
    "repos", "server_host", "server_port", "ui_port", "unattended", "search_attributes", "time_zone", "desk_port",
}


def local_time_zone() -> str:
    """This machine's IANA time zone (TZ, else /etc/localtime's target), or UTC when it can't be told."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if "/" in tz:
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return "UTC"
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else "UTC"


@dataclass(frozen=True)
class EngineSettings:
    repos: tuple[str, ...] = ()
    server_host: str = "localhost"
    server_port: int = 7243
    ui_port: int = 8243
    unattended: bool = True
    search_attributes: bool = True  # tests run without them (the test server has none registered)
    time_zone: str = field(default_factory=local_time_zone)  # what the teams' cron schedules mean
    desk_port: int = 8245
    state: Path = field(default_factory=default_state)

    @property
    def temporal(self) -> TemporalSettings:
        return TemporalSettings(task_queue=TASK_QUEUE, server_url=f"{self.server_host}:{self.server_port}", namespace=NAMESPACE)

    def repo_paths(self) -> list[Path]:
        return [Path(r).expanduser().resolve() for r in self.repos]


def load(path: Path | None = None) -> EngineSettings:
    path = path or config_path()
    if not path.is_file():
        return EngineSettings()
    raw = tomllib.loads(path.read_text())
    unknown = set(raw) - KEYS
    if unknown:
        raise ValueError(f"{path}: unknown key(s) {', '.join(sorted(unknown))} (known: {', '.join(sorted(KEYS))})")
    if "repos" in raw:
        raw["repos"] = tuple(str(r) for r in raw["repos"])
    return EngineSettings(**raw)
