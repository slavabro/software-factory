"""The Temporal core without a server: settings, data converter, sandbox, activity helpers."""

import asyncio
import json
import os
import unittest
from decimal import Decimal
from unittest import mock

from pydantic import ValidationError
from temporalio.api.common.v1 import Payload
from temporalio.testing import ActivityEnvironment

from factory_runtime.temporal import (
    BASE_PASSTHROUGH_MODULES,
    DATA_SHAPE_ERRORS,
    STRICT_IMPORT_POLICY,
    ZLIB_ENCODING,
    CompressionCodec,
    TemporalSettings,
    activity_idempotency_key,
    build_workflow_runner,
    data_converter,
)

SERVER_PAYLOAD_LIMIT_BYTES = 2 * 1024 * 1024


def big_result() -> dict:
    """A few MB of JSON: big enough that, uncompressed, the server would refuse it and stall the workflow."""
    row = {"account": "Example – Individual (…000)", "qty": Decimal("29.0"), "value": Decimal("3789.43")}
    rows = [{**row, "id": f"row-{i}"} for i in range(20000)]
    return {"plan": {"rows": rows}, "decision": {"rows": rows}}


def roundtrip(value, converter=None):
    converter = converter or data_converter()

    async def go():
        payloads = await converter.encode([value])
        return payloads, (await converter.decode(payloads, [type(value)]))[0]

    return asyncio.run(go())


def as_json(value):
    return json.loads(json.dumps(value, default=str))


class SettingsTest(unittest.TestCase):
    def test_defaults_and_environment(self):
        env = {"TEMPORAL_TASK_QUEUE": "q", "TEMPORAL_NAMESPACE": "factory", "TEMPORAL_WARN_BYTES": "5"}
        with mock.patch.dict(os.environ, env, clear=True):
            s = TemporalSettings.from_env(server_url="localhost:7243")
        self.assertEqual((s.task_queue, s.namespace, s.server_url, s.warn_bytes), ("q", "factory", "localhost:7243", 5))
        self.assertEqual(s.compress_min_bytes, 64 * 1024)

    def test_environment_wins_over_defaults_and_a_prefix_isolates(self):
        with mock.patch.dict(os.environ, {"FACTORY_TEMPORAL_TASK_QUEUE": "engine"}, clear=True):
            s = TemporalSettings.from_env(prefix="FACTORY_TEMPORAL_", task_queue="fallback")
        self.assertEqual(s.task_queue, "engine")

    def test_refuses_what_cannot_work(self):
        with self.assertRaises(ValueError):
            TemporalSettings(task_queue="")
        with self.assertRaises(ValueError):
            TemporalSettings(task_queue="q", compress_min_bytes=0)


class ConverterTest(unittest.TestCase):
    def test_everyday_payload_stays_readable(self):
        value = {"written": 3, "price": Decimal("1.0797")}
        payloads, decoded = roundtrip(value)
        self.assertEqual(as_json(decoded), as_json(value))
        self.assertNotEqual(payloads[0].metadata.get("encoding"), ZLIB_ENCODING)
        self.assertIn(b"1.0797", payloads[0].data)

    def test_large_payload_is_compressed_under_the_server_limit_and_round_trips(self):
        value = big_result()
        self.assertGreater(len(json.dumps(value, default=str)), SERVER_PAYLOAD_LIMIT_BYTES)
        payloads, decoded = roundtrip(value)
        self.assertEqual(payloads[0].metadata["encoding"], ZLIB_ENCODING)
        self.assertLess(payloads[0].ByteSize(), SERVER_PAYLOAD_LIMIT_BYTES)
        self.assertEqual(as_json(decoded), as_json(value))

    def test_uncompressed_history_decodes_after_the_threshold_changes(self):
        value = {"k": "v" * 500}
        payloads, _ = roundtrip(value, data_converter(min_bytes=10**9))
        decoded = asyncio.run(data_converter(min_bytes=1).decode(payloads, [dict]))[0]
        self.assertEqual(decoded, value)

    def test_one_converter_per_limits(self):
        self.assertIs(data_converter(), data_converter())
        self.assertIsNot(data_converter(1, 2), data_converter())

    def test_a_payload_near_the_limit_is_warned_about(self):
        codec = CompressionCodec(min_bytes=10**9, warn_bytes=100)
        with self.assertLogs("factory_runtime.temporal.converter", "WARNING") as logs:
            asyncio.run(codec.encode([Payload(metadata={"encoding": b"json/plain"}, data=b"x" * 200)]))
            asyncio.run(codec.encode([Payload(metadata={"encoding": b"json/plain"}, data=b"x" * 10)]))
        self.assertEqual(len([m for m in logs.output if "near the server's 2 MiB limit" in m]), 1)


class SandboxTest(unittest.TestCase):
    def test_base_and_product_modules_pass_through(self):
        runner = build_workflow_runner(["myproduct.models"])
        modules = runner.restrictions.passthrough_modules
        self.assertTrue(set(BASE_PASSTHROUGH_MODULES) | {"myproduct.models"} <= modules)

    def test_strict_policy_is_applied_only_when_asked(self):
        strict = build_workflow_runner(import_policy=STRICT_IMPORT_POLICY)
        self.assertEqual(strict.restrictions.import_notification_policy, STRICT_IMPORT_POLICY)
        self.assertNotEqual(build_workflow_runner().restrictions.import_notification_policy, STRICT_IMPORT_POLICY)


class ActivityHelpersTest(unittest.TestCase):
    def test_key_is_workflow_run_and_activity_plus_parts(self):
        env = ActivityEnvironment()
        info = env.info
        key = env.run(activity_idempotency_key, "rule", 7)
        self.assertEqual(key, f"{info.workflow_id}:{info.workflow_run_id}:{info.activity_id}:rule:7")
        self.assertEqual(env.run(activity_idempotency_key), f"{info.workflow_id}:{info.workflow_run_id}:{info.activity_id}")

    def test_data_shape_errors_cover_validation(self):
        self.assertIn(ValidationError, DATA_SHAPE_ERRORS)
        self.assertTrue(issubclass(KeyError, DATA_SHAPE_ERRORS))


if __name__ == "__main__":
    unittest.main()
