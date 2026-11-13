"""How to reach Temporal and how big a payload may get. A product builds this from its own config; the factory's
engine reads it from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
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
    tls: bool = False  # True for a server behind TLS (Temporal Cloud); mTLS: pass a TLSConfig to get_client
    api_key: str | None = field(default=None, repr=False)  # Temporal Cloud API key; implies TLS

    def __post_init__(self) -> None:
        if not self.task_queue or not self.server_url or not self.namespace:
            raise ValueError("TemporalSettings needs a task_queue, server_url and namespace")
        if self.compress_min_bytes < 1 or self.warn_bytes < 1:
            raise ValueError("payload limits must be at least 1 byte")

    @classmethod
    def from_env(cls, prefix: str = "TEMPORAL_", **defaults: Any) -> TemporalSettings:
        """`<prefix>` + TASK_QUEUE, SERVER_URL, NAMESPACE, COMPRESS_MIN_BYTES, WARN_BYTES, TLS, API_KEY;
        `defaults` fill what the environment leaves out."""
        values: dict[str, Any] = dict(defaults)
        for f in fields(cls):
            raw = os.environ.get(prefix + f.name.upper())
            if raw is None:
                continue
            if f.name.endswith("_bytes"):
                values[f.name] = int(raw)
            elif f.name == "tls":
                values[f.name] = raw.strip().lower() in {"1", "true", "yes", "on"}
            else:
                values[f.name] = raw
        return cls(**values)
