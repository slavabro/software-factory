"""The factory's standard-library package (`sfactory`, in plugins/factory) from this engine's checkout: the runner
and the config reader the activities reuse, so a team run behaves like `factory run`."""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "factory"
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

from sfactory import config, runner  # noqa: E402

__all__ = ["config", "runner"]
