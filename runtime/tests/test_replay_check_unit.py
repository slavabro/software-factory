"""The worker definition, the replay check's report and parsing, and the patch report, without a server."""

import asyncio
import json
import os
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
from temporalio.client import Interceptor as ClientInterceptor
from temporalio.worker import Interceptor, Plugin

from factory_runtime.temporal import WorkerDefinition, activity_names
from factory_runtime.temporal.definition import DYNAMIC
from factory_runtime.temporal.patches import PatchReport, PatchStatus, RunRef, history_markers
from factory_runtime.temporal import replay_check
from factory_runtime.temporal.replay_check import (
    MISSING_HANDLER,
    NONDETERMINISM,
    RunFailure,
    ReplayReport,
    VisibilityUnsettled,
    cli_parser,
    connect,
    failure_text,
    list_open_runs,
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


class BothInterceptor(Interceptor, ClientInterceptor):
    """A worker interceptor that is also a client interceptor (as tracing interceptors are)."""


class FailureOne(Exception):
    pass


def client_for(defn: WorkerDefinition, *, interceptors: Sequence[object] = (), plugins: Sequence[object] = ()) -> mock.MagicMock:
    client = mock.MagicMock()
    client.data_converter = defn.data_converter
    client.config.return_value = {"interceptors": list(interceptors), "plugins": list(plugins)}
    return client


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
        client = client_for(CURRENT)
        with self.assertRaisesRegex(ValueError, r"missing \['reserve_v2', 'ship', 'stamp'\]"):
            CURRENT.worker(client, activities=[reserve])
        with self.assertRaisesRegex(ValueError, "not one of"):
            CURRENT.worker(client, activities=ACTIVITIES, task_queue="elsewhere")
        two = WorkerDefinition(task_queues=("a", "b"), workflows=(OrderV1,))
        with self.assertRaisesRegex(ValueError, "name the task queue"):
            two.worker(client_for(two))
        with mock.patch("factory_runtime.temporal.definition.Worker") as worker:
            CURRENT.worker(client, activities=ACTIVITIES, identity="w1")
        kwargs = worker.call_args.kwargs
        self.assertEqual(kwargs["task_queue"], QUEUE)
        self.assertEqual(kwargs["workflows"], [OrderV1])
        self.assertIs(kwargs["workflow_runner"], CURRENT.workflow_runner)
        self.assertEqual(kwargs["identity"], "w1")

    def test_the_worker_and_the_replayer_use_the_same_parts(self) -> None:
        tracing, other = BothInterceptor(), Interceptor()
        defn = WorkerDefinition(
            task_queues=(QUEUE,),
            workflows=(OrderV1,),
            activity_names=activity_names(ACTIVITIES),
            interceptors=(tracing, other),
            workflow_failure_exception_types=(FailureOne,),
        )
        replayer = defn.replayer(namespace="prod").config()
        self.assertEqual(replayer["workflows"], [OrderV1])
        self.assertIs(replayer["workflow_runner"], defn.workflow_runner)
        self.assertIs(replayer["data_converter"], defn.data_converter)
        self.assertEqual(replayer["namespace"], "prod")
        self.assertEqual(replayer["interceptors"], [tracing, other])
        self.assertEqual(replayer["workflow_failure_exception_types"], [FailureOne])
        for client_interceptors, given in (([], [tracing, other]), ([tracing], [other])):
            with self.subTest(client_interceptors=client_interceptors):
                # The worker prepends the client's worker interceptors: together they must be the definition's.
                with mock.patch("factory_runtime.temporal.definition.Worker") as worker:
                    defn.worker(client_for(defn, interceptors=client_interceptors), activities=ACTIVITIES)
                kwargs = worker.call_args.kwargs
                self.assertEqual(kwargs["interceptors"], given)
                self.assertEqual(kwargs["workflow_failure_exception_types"], [FailureOne])
                self.assertIs(kwargs["workflow_runner"], defn.workflow_runner)

    def test_the_worker_refuses_what_its_replayer_would_not_have(self) -> None:
        tracing = BothInterceptor()
        with_tracing = WorkerDefinition(task_queues=(QUEUE,), workflows=(OrderV1,), interceptors=(Interceptor(), tracing))
        other_converter = client_for(CURRENT)
        other_converter.data_converter = mock.MagicMock()
        cases = {
            "data converter": (CURRENT, other_converter, {}),
            "client interceptor": (CURRENT, client_for(CURRENT, interceptors=[tracing]), {}),
            "client interceptor out of order": (with_tracing, client_for(with_tracing, interceptors=[tracing]), {}),
            "client plugin": (CURRENT, client_for(CURRENT, plugins=[mock.MagicMock(spec=Plugin)]), {}),
            "interceptors option": (CURRENT, client_for(CURRENT), {"interceptors": [Interceptor()]}),
            "plugins option": (CURRENT, client_for(CURRENT), {"plugins": [mock.MagicMock(spec=Plugin)]}),
            "runner option": (CURRENT, client_for(CURRENT), {"workflow_runner": mock.MagicMock()}),
            "unsandboxed runner option": (CURRENT, client_for(CURRENT), {"unsandboxed_workflow_runner": mock.MagicMock()}),
            "failure types option": (CURRENT, client_for(CURRENT), {"workflow_failure_exception_types": [FailureOne]}),
        }
        for name, (defn, client, options) in cases.items():
            activities = ACTIVITIES if defn is CURRENT else []
            with self.subTest(name), mock.patch("factory_runtime.temporal.definition.Worker") as worker:
                with self.assertRaises(ValueError):
                    defn.worker(client, activities=activities, **options)
                worker.assert_not_called()
        # A client plugin that isn't a worker plugin, and a client-only interceptor, change nothing in the worker.
        with mock.patch("factory_runtime.temporal.definition.Worker"):
            CURRENT.worker(client_for(CURRENT, interceptors=[ClientInterceptor()], plugins=[object()]), activities=ACTIVITIES)

    def test_connect_uses_the_definitions_converter(self) -> None:
        with mock.patch("factory_runtime.temporal.definition.Client.connect", new=mock.AsyncMock()) as connect_:
            asyncio.run(CURRENT.connect("host:1", namespace="ns"))
        args, kwargs = connect_.call_args
        self.assertEqual(args, ("host:1",))
        self.assertIs(kwargs["data_converter"], CURRENT.data_converter)
        self.assertEqual(kwargs["namespace"], "ns")

    def test_load_definition(self) -> None:
        self.assertIs(load_definition("tests.replay_workflows:CURRENT"), CURRENT)
        self.assertIsInstance(load_definition("tests.replay_workflows:patched_definition"), WorkerDefinition)
        for spec in ("tests.replay_workflows", "tests.replay_workflows:QUEUE"):
            with self.subTest(spec=spec), self.assertRaises((ValueError, TypeError)):
                load_definition(spec)


class FakeRun:
    def __init__(self, run_id: str) -> None:
        self.id, self.run_id = f"wf-{run_id}", run_id


class FakeClient:
    """`list_workflows` yields each listing in turn (the last one repeats)."""

    def __init__(self, *listings: list[str]) -> None:
        self.listings = list(listings)
        self.calls = 0

    def list_workflows(self, query: str) -> object:
        listing = self.listings[min(self.calls, len(self.listings) - 1)]
        self.calls += 1

        async def runs() -> object:
            for run_id in listing:
                yield FakeRun(run_id)

        return runs()


class VisibilityTest(unittest.TestCase):
    def test_two_listings_that_agree_are_the_open_runs(self) -> None:
        client = FakeClient(["a"], ["a", "b"], ["b", "a"])
        runs = asyncio.run(list_open_runs(client, pause=0))  # type: ignore[arg-type]
        self.assertEqual(sorted(r.run_id for r in runs), ["a", "b"])
        self.assertEqual(client.calls, 3)

    def test_a_listing_that_keeps_changing_couldnt_be_checked(self) -> None:
        client = FakeClient(*[[str(i)] for i in range(20)])
        with self.assertRaises(VisibilityUnsettled):
            asyncio.run(list_open_runs(client, pause=0, attempts=4))  # type: ignore[arg-type]
        self.assertEqual(client.calls, 5)


class ConnectTest(unittest.TestCase):
    def tls(self, argv: list[str], env_key: str | None, settings_key: str | None = None) -> tuple[bool, object]:
        from factory_runtime.temporal import TemporalSettings

        defn = WorkerDefinition(
            task_queues=(QUEUE,), workflows=(OrderV1,), settings=TemporalSettings(task_queue=QUEUE, api_key=settings_key)
        )
        args = cli_parser("x", "y").parse_args(["m:a", *argv])
        env = {k: v for k, v in os.environ.items() if k != "TEMPORAL_API_KEY"}
        if env_key is not None:
            env["TEMPORAL_API_KEY"] = env_key
        with mock.patch.dict(os.environ, env, clear=True), mock.patch(
            "factory_runtime.temporal.definition.Client.connect", new=mock.AsyncMock()
        ) as connect_:
            asyncio.run(connect(defn, args))
        kwargs = connect_.call_args.kwargs
        return kwargs["tls"], kwargs["api_key"]

    def test_tls_follows_the_api_key_and_the_flags(self) -> None:
        self.assertEqual(self.tls([], None), (False, None))
        self.assertEqual(self.tls([], ""), (False, None))  # an empty key is no key
        self.assertEqual(self.tls([], "k"), (True, "k"))
        self.assertEqual(self.tls(["--no-tls"], "k"), (False, "k"))
        self.assertEqual(self.tls(["--tls"], None), (True, None))
        self.assertEqual(self.tls([], None, settings_key="s"), (True, "s"))


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
