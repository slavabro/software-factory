"""The Temporal core against a real (time-skipping) test server: Pydantic round-trips through workflows and
activities, a too-big result completes because it is compressed, a failed run explains itself, signal-with-start
starts once and then joins, and the strict sandbox accepts the product's own modules."""

import unittest
import uuid
from decimal import Decimal

from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError

from factory_runtime.temporal import ZLIB_ENCODING, get_run_failure, signal_with_start
from factory_runtime.testing import make_worker, query_eventually, time_skipping_env
from tests.models import Order
from tests.workflows import BigResult, Failing, Inbox, PriceOrder, Waiting, explode, price

QUEUE = "runtime-tests"


class ServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await time_skipping_env()
        self.addAsyncCleanup(self.env.shutdown)
        self.client = self.env.client
        self.worker = make_worker(
            self.client,
            task_queue=QUEUE,
            workflows=[PriceOrder, BigResult, Failing, Inbox, Waiting],
            activities=[price, explode],
        )
        await self.worker.__aenter__()
        self.addAsyncCleanup(self.worker.__aexit__, None, None, None)

    def wid(self, name):
        return f"{name}-{uuid.uuid4()}"

    async def test_pydantic_and_decimal_round_trip_through_workflow_and_activity(self):
        out = await self.client.execute_workflow(
            PriceOrder.run, Order(symbol="EXMPL", qty=Decimal("3")), id=self.wid("price"), task_queue=QUEUE
        )
        self.assertEqual(out, Order(symbol="EXMPL", qty=Decimal("3"), total=Decimal("7.50")))

    async def test_a_result_over_the_server_limit_is_stored_compressed(self):
        # The test server does not enforce the 2 MiB limit, so check what history actually holds.
        wid = self.wid("big")
        out = await self.client.execute_workflow(BigResult.run, 30000, id=wid, task_queue=QUEUE)
        self.assertEqual(len(out["rows"]), 30000)
        history = await self.client.get_workflow_handle(wid).fetch_history()
        done = next(e for e in history.events if e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED)
        stored = done.workflow_execution_completed_event_attributes.result.payloads[0]
        self.assertEqual(stored.metadata["encoding"], ZLIB_ENCODING)
        self.assertLess(stored.ByteSize(), 2 * 1024 * 1024)

    async def test_a_failed_run_explains_itself(self):
        wid = self.wid("fail")
        with self.assertRaises(WorkflowFailureError):
            await self.client.execute_workflow(Failing.run, id=wid, task_queue=QUEUE)
        reason = await get_run_failure(self.client, wid, None, max_chars=500)
        self.assertIn("the broker said no", reason)
        ok = self.wid("ok")
        await self.client.execute_workflow(BigResult.run, 1, id=ok, task_queue=QUEUE)
        self.assertIsNone(await get_run_failure(self.client, ok, None, max_chars=500))

    async def test_a_cancelled_run_says_so(self):
        wid = self.wid("cancel")
        handle = await self.client.start_workflow(Waiting.run, id=wid, task_queue=QUEUE)
        await handle.cancel()
        with self.assertRaises(WorkflowFailureError):
            await handle.result()
        self.assertEqual(await get_run_failure(self.client, wid, None, max_chars=500), "The run was cancelled.")

    async def test_signal_with_start_starts_once_then_joins(self):
        wid = self.wid("inbox")
        first = await signal_with_start(
            self.client, "Inbox", wid, task_queue=QUEUE, signal="event", signal_args=["a"], args=["go"]
        )
        again = await signal_with_start(
            self.client, "Inbox", wid, task_queue=QUEUE, signal="event", signal_args=["b"], args=["ignored"]
        )
        self.assertEqual(first.result_run_id, again.result_run_id)
        handle = self.client.get_workflow_handle(wid)
        await query_eventually(handle, Inbox.seen, lambda e: len(e) == 3)
        await handle.signal(Inbox.event, "stop")
        # One run, its input from the first call only; the start signal is handled before run() begins.
        self.assertEqual(await handle.result(), ["a", "start:go", "b"])


if __name__ == "__main__":
    unittest.main()
