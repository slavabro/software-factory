"""The process-wide client cache, without a server: Client.connect is replaced by a fake that records its call."""

import asyncio
import unittest
from unittest import mock

from factory_runtime.temporal import (
    TemporalSettings,
    cached_client,
    data_converter,
    get_client,
    reset_clients,
    set_client,
)
from factory_runtime.temporal import client as client_module


class FakeConnect:
    def __init__(self):
        self.calls = []

    async def __call__(self, url, **kwargs):
        await asyncio.sleep(0.01)  # let a second caller race the first
        self.calls.append((url, kwargs))
        return object()


class ClientTest(unittest.TestCase):
    def setUp(self):
        reset_clients()
        self.addCleanup(reset_clients)
        self.connect = FakeConnect()
        patcher = mock.patch.object(client_module.Client, "connect", self.connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_client_per_settings_with_the_shared_converter(self):
        s = TemporalSettings(task_queue="q", namespace="factory", compress_min_bytes=10, warn_bytes=20)

        async def go():
            return await asyncio.gather(get_client(s), get_client(s))

        a, b = asyncio.run(go())
        self.assertIs(a, b)
        self.assertEqual(len(self.connect.calls), 1)
        url, kwargs = self.connect.calls[0]
        self.assertEqual((url, kwargs["namespace"], kwargs["tls"]), ("localhost:7233", "factory", False))
        self.assertIs(kwargs["data_converter"], data_converter(10, 20))
        self.assertNotIn("api_key", kwargs)

    def test_different_settings_get_different_clients(self):
        one = asyncio.run(get_client(TemporalSettings(task_queue="q", namespace="a")))
        two = asyncio.run(get_client(TemporalSettings(task_queue="q", namespace="b")))
        self.assertIsNot(one, two)

    def test_an_api_key_means_tls_and_is_passed_on(self):
        asyncio.run(get_client(TemporalSettings(task_queue="q", server_url="x.tmprl.cloud:7233", api_key="sekrit-123")))
        _, kwargs = self.connect.calls[0]
        self.assertEqual((kwargs["tls"], kwargs["api_key"]), (True, "sekrit-123"))
        self.assertNotIn("sekrit", repr(TemporalSettings(task_queue="q", api_key="sekrit-123")))

    def test_connect_options_pass_through(self):
        asyncio.run(get_client(TemporalSettings(task_queue="q"), identity="worker-1"))
        self.assertEqual(self.connect.calls[0][1]["identity"], "worker-1")

    def test_cached_set_and_reset(self):
        s = TemporalSettings(task_queue="q")
        self.assertIsNone(cached_client(s))
        injected = object()
        set_client(s, injected)
        self.assertIs(cached_client(s), injected)
        self.assertIs(asyncio.run(get_client(s)), injected)
        self.assertEqual(self.connect.calls, [])
        set_client(s, None)
        self.assertIsNone(cached_client(s))
        asyncio.run(get_client(s))
        reset_clients()
        self.assertIsNone(cached_client(s))

    def test_racing_first_use_on_a_second_event_loop(self):
        """A fresh loop per test (IsolatedAsyncioTestCase, pytest-asyncio) must not hit a lock from an old loop."""
        s = TemporalSettings(task_queue="q")
        for _ in range(2):
            reset_clients()

            async def go():
                return await asyncio.gather(get_client(s), get_client(s))

            a, b = asyncio.run(go())
            self.assertIs(a, b)


class ConverterIdentityTest(unittest.TestCase):
    def test_one_object_however_the_limits_are_spelled(self):
        c = data_converter()
        self.assertIs(c, data_converter(64 * 1024, 1024 * 1024))
        self.assertIs(c, data_converter(min_bytes=64 * 1024, warn_bytes=1024 * 1024))


if __name__ == "__main__":
    unittest.main()
