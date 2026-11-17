"""The worker definition, the replay check's report and parsing, and the patch report, without a server."""

import json
import unittest
from collections.abc import Sequence
from unittest import mock

from temporalio import activity, workflow
from temporalio.common import RawValue
from temporalio.api.common.v1 import Payload, Payloads
from temporalio.api.history.v1 import (
    HistoryEvent,
    MarkerRecordedEventAttributes,
    WorkflowExecutionSignaledEventAttributes,
    WorkflowExecutionUpdateAcceptedEventAttributes,
)
from temporalio.api.update.v1 import Input, Request

from factory_runtime.temporal import WorkerDefinition, activity_names
from factory_runtime.temporal.definition import DYNAMIC
from factory_runtime.temporal.patches import PatchReport, PatchStatus, RunRef, history_markers
from factory_runtime.temporal.replay_check import (
    MISSING_HANDLER,
    NONDETERMINISM,
    RunFailure,
    ReplayReport,
    failure_text,
    load_definition,
    received_messages,
)
from tests.replay_workflows import ACTIVITIES, CURRENT, QUEUE, OrderV1, definition, reserve


@workflow.defn(dynamic=True)
class AnyWorkflow:
    @workflow.run
    async def run(self, args: Sequence[RawValue]) -> None:
        pass

    @workflow.signal(dynamic=True)
    def any_signal(self, name: str, args: Sequence[RawValue]) -> None:
        pass


@activity.defn(dynamic=True)
async def any_activity(args: Sequence[RawValue]) -> None:
    pass


class WorkerDefinitionTest(unittest.TestCase):
    def test_it_needs_queues_and_workflow_classes(self) -> None:
        with self.assertRaises(ValueError):
            WorkerDefinition(task_queues=(), workflows=(OrderV1,))
        with self.assertRaises(ValueError):
            WorkerDefinition(task_queues=(QUEUE,), workflows=())
        with self.assertRaises(Exception):
            WorkerDefinition(task_queues=(QUEUE,), workflows=(str,))

    def test_registered_types_activities_and_handlers(self) -> None:
        self.assertIs(CURRENT.workflow_class("Order"), OrderV1)
        self.assertIsNone(CURRENT.workflow_class("Legacy"))
        self.assertTrue(CURRENT.serves_activity("reserve"))
        self.assertFalse(CURRENT.serves_activity("charge"))
        self.assertTrue(definition(OrderV1, external=frozenset({"charge"})).serves_activity("charge"))
        handlers = WorkerDefinition.handlers(OrderV1)
        self.assertEqual(handlers.signals, {"go", "nudge"})
        self.assertEqual(handlers.queries, {"current_stage"})
        self.assertFalse(handlers.dynamic_signal)

    def test_dynamic_workflows_and_activities_serve_every_name(self) -> None:
        defn = WorkerDefinition(
            task_queues=(QUEUE,), workflows=(OrderV1, AnyWorkflow), activity_names=activity_names([any_activity])
        )
        self.assertIs(defn.workflow_class("Order"), OrderV1)
        self.assertIs(defn.workflow_class("Legacy"), AnyWorkflow)
        self.assertEqual(defn.activity_names, {DYNAMIC})
        self.assertTrue(defn.serves_activity("anything"))
        self.assertTrue(WorkerDefinition.handlers(AnyWorkflow).dynamic_signal)

    def test_the_worker_registers_exactly_the_named_activities_on_a_served_queue(self) -> None:
        client = mock.MagicMock()
        with self.assertRaisesRegex(ValueError, r"missing \['reserve_v2', 'ship'\]"):
            CURRENT.worker(client, activities=[reserve])
        with self.assertRaisesRegex(ValueError, "not one of"):
            CURRENT.worker(client, activities=ACTIVITIES, task_queue="elsewhere")
        two = WorkerDefinition(task_queues=("a", "b"), workflows=(OrderV1,))
        with self.assertRaisesRegex(ValueError, "name the task queue"):
            two.worker(client)
        with mock.patch("factory_runtime.temporal.definition.Worker") as worker:
            CURRENT.worker(client, activities=ACTIVITIES, identity="w1")
        kwargs = worker.call_args.kwargs
        self.assertEqual(kwargs["task_queue"], QUEUE)
        self.assertEqual(kwargs["workflows"], [OrderV1])
        self.assertIs(kwargs["workflow_runner"], CURRENT.workflow_runner)
        self.assertEqual(kwargs["identity"], "w1")

    def test_the_replayer_uses_the_same_parts(self) -> None:
        config = CURRENT.replayer(namespace="prod").config()
        self.assertEqual(config["workflows"], [OrderV1])
        self.assertIs(config["workflow_runner"], CURRENT.workflow_runner)
        self.assertIs(config["data_converter"], CURRENT.data_converter)
        self.assertEqual(config["namespace"], "prod")

    def test_load_definition(self) -> None:
        self.assertIs(load_definition("tests.replay_workflows:CURRENT"), CURRENT)
        self.assertIsInstance(load_definition("tests.replay_workflows:patched_definition"), WorkerDefinition)
        for spec in ("tests.replay_workflows", "tests.replay_workflows:QUEUE"):
            with self.subTest(spec=spec), self.assertRaises((ValueError, TypeError)):
                load_definition(spec)


class ReportTest(unittest.TestCase):
    def report(self) -> ReplayReport:
        return ReplayReport(
            namespace="default",
            task_queues=("q",),
            checked={"Order": 3},
            other_queues={"other": {"Unrelated": 2}},
            failures=[
                RunFailure(NONDETERMINISM, "order-1", "Order", "run-1", "[TMPRL1100] Nondeterminism error: ..."),
                RunFailure(MISSING_HANDLER, "order-2", "Order", "run-2", "received signal 'nudge', ..."),
            ],
        )

    def test_failures_are_split_and_named_in_words(self) -> None:
        report = self.report()
        self.assertFalse(report.passed)
        text = report.render()
        self.assertTrue(text.startswith("Replay check FAIL: 2 problem(s)"))
        self.assertIn("nondeterminism (1):\n  [nondeterminism] Order order-1 run run-1", text)
        self.assertIn("other failures (1):\n  [missing_handler] Order order-2 run run-2", text)
        self.assertIn("  other / Unrelated: 2", text)
        data = report.as_dict()
        self.assertEqual([f["run_id"] for f in data["nondeterminism"]], ["run-1"])
        self.assertEqual([f["run_id"] for f in data["failures"]], ["run-2"])
        self.assertEqual(set(data["failures"][0]), {"kind", "workflow_id", "workflow_type", "run_id", "error"})
        json.dumps(data)

    def test_an_empty_report_passes(self) -> None:
        report = ReplayReport(namespace="default", task_queues=("q",))
        self.assertTrue(report.passed)
        self.assertTrue(report.render().startswith("Replay check PASS"))

    def test_failure_text_keeps_the_message_not_the_dump(self) -> None:
        dump = (
            'Workflow activation completion failed: Failure { failure: Some(Failure { message: "[TMPRL1100] '
            'Nondeterminism error: said \\"no\\"\\nsecond line", source: "", stack_trace: "  File \\"x.py\\"" }) }'
        )
        self.assertEqual(failure_text(RuntimeError(dump)), '[TMPRL1100] Nondeterminism error: said "no"')
        self.assertEqual(failure_text(RuntimeError("plain\nmore")), "plain")
        self.assertEqual(failure_text(RuntimeError("")), "RuntimeError")
        self.assertEqual(len(failure_text(RuntimeError("x" * 2000))), 500)


class HistoryTest(unittest.TestCase):
    def test_received_signals_and_updates(self) -> None:
        events = [
            HistoryEvent(
                workflow_execution_signaled_event_attributes=WorkflowExecutionSignaledEventAttributes(signal_name="go")
            ),
            HistoryEvent(
                workflow_execution_update_accepted_event_attributes=WorkflowExecutionUpdateAcceptedEventAttributes(
                    accepted_request=Request(input=Input(name="approve"))
                )
            ),
            HistoryEvent(),
        ]
        self.assertEqual(received_messages(events), ({"go"}, {"approve"}))

    def test_patch_markers(self) -> None:
        def marker(name: str, patch_id: str, deprecated: bool) -> HistoryEvent:
            raw = json.dumps({"id": patch_id, "deprecated": deprecated}).encode()
            data = Payload(metadata={"encoding": b"json/plain"}, data=raw)
            attrs = MarkerRecordedEventAttributes(marker_name=name, details={"patch-data": Payloads(payloads=[data])})
            return HistoryEvent(marker_recorded_event_attributes=attrs)

        events = [
            marker("core_patch", "a", False),
            marker("core_patch", "b", True),
            marker("core_local_activity", "c", False),
        ]
        self.assertEqual(history_markers(events), {"a": False, "b": True})


class PatchReportTest(unittest.TestCase):
    def test_the_lifecycle_step_each_patch_allows(self) -> None:
        run = RunRef("order-1", "Order", "run-1")
        report = PatchReport(
            runs_checked=3,
            patches={
                "keep": PatchStatus("keep", old_side=[run], others=2),
                "deprecate": PatchStatus("deprecate", marked=[run], others=2),
                "remove": PatchStatus("remove", others=3),
            },
        )
        self.assertTrue(report.complete)
        text = report.render()
        self.assertIn("keep: old side 1, non-deprecated marker 0, others 2 -> keep patched()", text)
        self.assertIn("  old side: Order order-1 run run-1", text)
        self.assertIn("deprecate: old side 0, non-deprecated marker 1, others 2 -> may be deprecated", text)
        self.assertIn("remove: old side 0, non-deprecated marker 0, others 3 -> may be removed", text)
        data = report.as_dict()
        old_side = [{"workflow_id": "order-1", "workflow_type": "Order", "run_id": "run-1"}]
        self.assertEqual(data["patches"]["keep"]["old_side"], old_side)
        self.assertFalse(data["patches"]["keep"]["can_deprecate"])
        json.dumps(data)

    def test_runs_left_out_make_it_incomplete(self) -> None:
        report = PatchReport(failures=[RunFailure(NONDETERMINISM, "w", "Order", "r", "boom")])
        self.assertFalse(report.complete)
        self.assertIn("INCOMPLETE: 1 run(s) left out", report.render())


if __name__ == "__main__":
    unittest.main()
