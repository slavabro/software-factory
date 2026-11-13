"""How to reach Temporal and how big a payload may get. A product builds this from its own config; the factory's
engine reads it from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any

# The server refuses a payload over 2 MiB, and a workflow whose completion is refused retries its task for ever.
DEFAULT_COMPRESS_MIN_BYTES = 64 * 1024
DEFAULT_WARN_BYTES = 1024 * 1024


@dataclass(frozen=True)
class TemporalSettings:
    task_queue: str
    server_url: str = "localhost:7233"
    namespace: str = "default"
    compress_min_bytes: int = DEFAULT_COMPRESS_MIN_BYTES  # payloads at least this big are zlib-compressed
    warn_bytes: int = DEFAULT_WARN_BYTES  # an encoded payload at least this big is logged as a warning

    def __post_init__(self) -> None:
        if not self.task_queue or not self.server_url or not self.namespace:
            raise ValueError("TemporalSettings needs a task_queue, server_url and namespace")
        if self.compress_min_bytes < 1 or self.warn_bytes < 1:
            raise ValueError("payload limits must be at least 1 byte")

    @classmethod
    def from_env(cls, prefix: str = "TEMPORAL_", **defaults: Any) -> TemporalSettings:
        """`TEMPORAL_TASK_QUEUE`, `TEMPORAL_SERVER_URL`, `TEMPORAL_NAMESPACE`, `TEMPORAL_COMPRESS_MIN_BYTES`,
        `TEMPORAL_WARN_BYTES`; `defaults` fill what the environment leaves out."""
        values: dict[str, Any] = dict(defaults)
        for f in fields(cls):
            raw = os.environ.get(prefix + f.name.upper())
            if raw is not None:
                values[f.name] = int(raw) if f.name.endswith("_bytes") else raw
        return cls(**values)
