"""The one Temporal data converter: every client, worker and test environment uses it.

Pydantic JSON (Decimal, datetime and models round-trip exactly) plus a zlib codec for large payloads. Every payload
(workflow and activity inputs and results, child workflows, queries, updates, signals, continue-as-new) passes
through it, so the size limit is handled once instead of at each call site.

Why compress: the server refuses a payload over 2 MiB, and a workflow whose completion (or a command it issues) is
refused cannot complete; its workflow task is retried for ever (TMPRL1103 in the worker log). JSON compresses about
tenfold. Payloads under `min_bytes` stay as they are, so everyday ones remain readable in the Temporal UI and CLI.
Decoding goes by the payload's own encoding, so uncompressed history (and a changed threshold) always decodes. A
compressed payload shows as `binary/zlib` bytes; read it with `zlib.decompress(base64.b64decode(data))`, which gives
a serialized `Payload` whose `data` is the JSON.
"""

from __future__ import annotations

import dataclasses
import logging
import zlib
from functools import lru_cache
from typing import Sequence

from temporalio.api.common.v1 import Payload
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import DataConverter, PayloadCodec

from factory_runtime.temporal.settings import DEFAULT_COMPRESS_MIN_BYTES, DEFAULT_WARN_BYTES

logger = logging.getLogger(__name__)

ZLIB_ENCODING = b"binary/zlib"


class CompressionCodec(PayloadCodec):
    """zlib-compress each payload whose serialized size is at least `min_bytes`; warn about any encoded payload of
    `warn_bytes` or more (nearing the server's limit)."""

    def __init__(self, min_bytes: int, warn_bytes: int) -> None:
        self._min_bytes = min_bytes
        self._warn_bytes = warn_bytes

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        encoded = []
        for payload in payloads:
            raw = payload.SerializeToString()
            if len(raw) < self._min_bytes:
                encoded.append(payload)
            else:
                encoded.append(Payload(metadata={"encoding": ZLIB_ENCODING}, data=zlib.compress(raw)))
            size = encoded[-1].ByteSize()
            if size >= self._warn_bytes:
                logger.warning(
                    f"Temporal payload of {size} bytes ({len(raw)} before compression) is near "
                    "the server's 2 MiB limit: a larger one would stall its workflow"
                )
        return encoded

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [
            Payload.FromString(zlib.decompress(p.data)) if p.metadata.get("encoding") == ZLIB_ENCODING else p
            for p in payloads
        ]


def data_converter(
    min_bytes: int = DEFAULT_COMPRESS_MIN_BYTES, warn_bytes: int = DEFAULT_WARN_BYTES
) -> DataConverter:
    """The converter every `Client.connect` and `WorkflowEnvironment` passes: one object per limits pair, however
    the limits are spelled."""
    return _converter(int(min_bytes), int(warn_bytes))


@lru_cache(maxsize=None)
def _converter(min_bytes: int, warn_bytes: int) -> DataConverter:
    return dataclasses.replace(pydantic_data_converter, payload_codec=CompressionCodec(min_bytes, warn_bytes))
