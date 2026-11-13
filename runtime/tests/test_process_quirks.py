"""Each known quirk of the process engine (runtime/README.md, design section 7), pinned so changing one is a visible,
deliberate decision (under Worker Versioning: running workflows replay against this code). Also the plug-in
contract and how a product declares its workflow classes."""

import asyncio
import contextlib
import unittest
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Iterator
from unittest import mock

from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError

from factory_runtime.process import (
    BUILTIN_STEP_TYPES,
    ProcessWorkflowBase,
    StepContext,
    StepType,
    evaluate_condition,
    resolve_argument,
)
from factory_runtime.testing import make_worker, time_skipping_env
from tests.process_workflows import (
    PROCESS_WORKFLOWS,
    ConditionalProcessWorkflow,
    HybridProcessWorkflow,
    LinearProcessWorkflow,
    ParallelProcessWorkflow,
)

QUEUE = "process-quirks"


@contextlib.contextmanager
def recorded_workflow() -> Iterator[mock.MagicMock]:
    """`workflow` in the engine's modules replaced by one mock, so the order of uuid4 calls and scheduled
    commands can be read from `mock_calls`. uuid4 gives 00..01, 00..02, ...; activities and children return
    their own name."""
    wf = mock.MagicMock()
    counter = iter(range(1, 1000))
    wf.uuid4.side_effect = lambda: uuid.UUID(int=next(counter))
    wf.info.return_value = SimpleNamespace(task_queue="q", workflow_type="T", workflow_id="wid-12345678")

    async def command(name: Any, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0)  # a real command suspends the coroutine
        return name

    wf.execute_activity = mock.AsyncMock(side_effect=command)
    wf.execute_child_workflow = mock.AsyncMock(side_effect=command)
    wf.wait_condition = mock.AsyncMock()
    wf.ParentClosePolicy = workflow.ParentClosePolicy
    with (
        mock.patch("factory_runtime.process.base.workflow", wf),
        mock.patch("factory_runtime.process.steps.workflow", wf),
        mock.patch("factory_runtime.process.templates.workflow", wf),
    ):
        yield wf


def commands(wf: mock.MagicMock) -> list[tuple[str, Any]]:
    """uuid4 calls and scheduled commands, in order: ("uuid4", n) / ("execute_activity", name) / ..."""
    out: list[tuple[str, Any]] = []
    uuids = 0
    for name, args, kwargs in wf.mock_calls:
        if name == "uuid4":
            uuids += 1
            out.append(("uuid4", uuids))
        elif name in ("execute_activity", "execute_child_workflow", "start_child_workflow"):
            out.append((name, (args[0], kwargs.get("id"))))
    return out


def u(n: int) -> str:
    return str(uuid.UUID(int=n))


def act(step_id: str, **extra: Any) -> dict[str, Any]:
    return {"id": step_id, "type": "activity", "activity": f"{step_id}_activity", **extra}


class TemplateQuirksTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("factory_runtime.process.templates.workflow")
        self.wf = patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_a_whole_string_is_a_placeholder(self) -> None:
        self.assertEqual(resolve_argument("{{input.a}}", {}, {"a": 1}), 1)
        self.assertEqual(resolve_argument("x {{input.a}}", {}, {"a": 1}), "x {{input.a}}")
        self.assertEqual(resolve_argument("{{input.a}} y", {}, {"a": 1}), "{{input.a}} y")

    def test_a_condition_substitutes_each_placeholder_strings_as_repr(self) -> None:
        self.assertTrue(evaluate_condition("{{input.s}} == 'q' and {{input.n}} > 1", {}, {"s": "q", "n": 2}))
        # Inside a quoted literal the repr's own quotes show: 'x {{input.s}}' becomes 'x 'q''
        self.assertTrue(evaluate_condition("\"x {{input.s}}\" == \"x 'q'\"", {}, {"s": "q"}))

    def test_a_missing_template_path_gives_none(self) -> None:
        self.assertIsNone(resolve_argument("{{results.nope.x}}", {}, {}))
        self.assertIsNone(resolve_argument("{{input.nope}}", {}, {}))
        self.assertIsNone(resolve_argument("{{item.x}}", {}, {}, loop_item={"y": 1}))


class CommandOrderTest(unittest.IsolatedAsyncioTestCase):
    """One workflow.uuid4() per executed step, its value unused except for default child ids."""

    async def test_linear_draws_one_uuid_per_step_before_its_commands(self) -> None:
        process = {
            "steps": [
                act("a"),
                {"id": "c", "type": "workflow", "workflow_name": "Child"},
                {"id": "f", "type": "for_each", "items": [1, 2], "step": {"type": "workflow", "workflow_name": "Kid"}},
                {"id": "p", "type": "unknown"},
            ]
        }
        with recorded_workflow() as wf:
            await LinearProcessWorkflow().run_linear(process, {}, None)
        self.assertEqual(
            commands(wf),
            [
                ("uuid4", 1),
                ("execute_activity", ("a_activity", None)),
                ("uuid4", 2),
                ("uuid4", 3),
                ("execute_child_workflow", ("Child", f"Child_{u(3)}")),
                ("uuid4", 4),
                ("uuid4", 5),
                ("execute_child_workflow", ("Kid", f"Kid_{u(5)}")),
                ("uuid4", 6),
                ("execute_child_workflow", ("Kid", f"Kid_{u(6)}")),
                ("uuid4", 7),  # an unknown step type still draws its uuid
            ],
        )

    async def test_conditional_draws_a_uuid_only_after_the_condition_passed(self) -> None:
        process = {"steps": [act("a", condition="1 == 2"), act("b", condition="1 == 1"), act("c")]}
        with recorded_workflow() as wf:
            results = await ConditionalProcessWorkflow().run_conditional(process, {}, None)
        self.assertEqual(
            commands(wf),
            [
                ("uuid4", 1),
                ("execute_activity", ("b_activity", None)),
                ("uuid4", 2),
                ("execute_activity", ("c_activity", None)),
            ],
        )
        self.assertNotIn("a", results)  # a skipped step is absent from the results

    async def test_parallel_draws_each_uuid_inside_its_coroutine_before_its_first_await(self) -> None:
        process = {"steps": [act("a"), act("b"), act("c")]}
        with recorded_workflow() as wf:
            await ParallelProcessWorkflow().run_parallel(process, {}, None)
        self.assertEqual(
            commands(wf),
            [
                ("uuid4", 1),
                ("execute_activity", ("a_activity", None)),
                ("uuid4", 2),
                ("execute_activity", ("b_activity", None)),
                ("uuid4", 3),
                ("execute_activity", ("c_activity", None)),
            ],
        )

    async def test_yaml_kwargs_are_execute_activity_options_not_activity_arguments(self) -> None:
        process = {"steps": [act("a", args=["{{input.x}}"], kwargs={"summary": "{{input.s}}"})]}
        with recorded_workflow() as wf:
            await LinearProcessWorkflow().run_linear(process, {"x": 1, "s": "shown in the UI"}, None)
        args, kwargs = wf.execute_activity.call_args
        self.assertEqual(args, ("a_activity", 1))
        self.assertEqual(kwargs["summary"], "shown in the UI")
        self.assertEqual(kwargs["start_to_close_timeout"], timedelta(seconds=60))


class ContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_step_function_gets_the_workflow_its_step_and_the_context(self) -> None:
        seen: list[tuple[Any, dict[str, Any], StepContext]] = []

        async def custom(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> Any:
            seen.append((wf, step, ctx))
            wf.step_state["n"] = wf.step_state.get("n", 0) + 1
            return ctx.resolve(step["value"])

        class Product(ProcessWorkflowBase):
            step_types = {**BUILTIN_STEP_TYPES, "custom": StepType(custom, for_each=True)}

        process = {
            "metadata": {"department": "ops"},
            "steps": [
                {"id": "one", "type": "custom", "value": "{{input.x}}"},
                {"id": "many", "type": "for_each", "items": ["p", "q"], "step": {"type": "custom", "value": "{{item}}"}},
            ],
        }
        wf_instance = Product()
        with recorded_workflow():
            results = await wf_instance.run_linear(process, {"x": 7}, {"by": "test"})
        self.assertEqual(results, {"one": 7, "many": ["p", "q"]})
        self.assertEqual(wf_instance.step_state, {"n": 3})
        (_, _, top), (_, _, first), (_, _, second) = seen
        self.assertEqual((top.base_id, top.unique_id, top.item), ("one", f"one_{u(1)}", None))
        self.assertEqual((first.base_id, first.unique_id, first.item), ("many", f"many_{u(2)}[0]", "p"))
        self.assertEqual(second.unique_id, f"many_{u(2)}[1]")
        self.assertIs(top.process_def, process)
        self.assertEqual(top.metadata, {"by": "test"})
        self.assertIs(top.results, results)

    async def test_a_step_not_flagged_for_each_is_refused_inside_one(self) -> None:
        called: list[Any] = []

        async def custom(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> Any:
            called.append(ctx)

        class Product(ProcessWorkflowBase):
            step_types = {**BUILTIN_STEP_TYPES, "custom": StepType(custom)}

        process = {"steps": [{"id": "f", "type": "for_each", "items": [1], "step": {"type": "custom"}}]}
        with recorded_workflow():
            results = await Product().run_linear(process, {}, None)
        self.assertEqual((results, called), ({"f": [None]}, []))

    def test_the_builtin_map_is_read_only(self) -> None:
        with self.assertRaises(TypeError):
            BUILTIN_STEP_TYPES["x"] = StepType(BUILTIN_STEP_TYPES["activity"].run)  # type: ignore[index]
        self.assertEqual(
            {name for name, t in BUILTIN_STEP_TYPES.items() if t.for_each}, {"activity", "workflow"}
        )

    def test_handlers_are_inherited_and_run_is_declared_per_class(self) -> None:
        defn = workflow._Definition.must_from_class(LinearProcessWorkflow)
        self.assertEqual(defn.name, "LinearProcessWorkflow")
        self.assertIn("get_step_results", defn.queries)
        self.assertIn("resume_step", defn.updates)
        self.assertIn("resume_step", defn.signals)
        with self.assertRaises(ValueError):

            @workflow.defn(name="NoRun")
            class NoRun(ProcessWorkflowBase):
                pass


@activity.defn(name="a_activity")
async def a_activity(*args: Any) -> Any:
    return "a"


@activity.defn(name="b_activity")
async def b_activity(*args: Any) -> Any:
    return {"saw": list(args)}


class ServerQuirksTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.env = await time_skipping_env()
        self.addAsyncCleanup(self.env.shutdown)
        self.client = self.env.client
        worker = make_worker(
            self.client, task_queue=QUEUE, workflows=PROCESS_WORKFLOWS, activities=[a_activity, b_activity]
        )
        await worker.__aenter__()
        self.addAsyncCleanup(worker.__aexit__, None, None, None)

    async def run_process(self, interpreter: Any, process: dict[str, Any], *args: Any) -> Any:
        return await self.client.execute_workflow(
            interpreter.run, args=[process, *args], id=f"q-{uuid.uuid4()}", task_queue=QUEUE
        )

    async def test_linear_and_parallel_ignore_condition_conditional_and_hybrid_do_not(self) -> None:
        process = {"steps": [act("a", condition="1 == 2")]}
        for interpreter, ran in (
            (LinearProcessWorkflow, True),
            (ParallelProcessWorkflow, True),
            (ConditionalProcessWorkflow, False),
            (HybridProcessWorkflow, False),
        ):
            with self.subTest(interpreter.__name__):
                self.assertEqual("a" in await self.run_process(interpreter, process), ran)

    async def test_parallel_steps_share_one_results_dict(self) -> None:
        # Every step starts before any finishes, so a sibling's result is not there yet when args resolve.
        process = {"steps": [act("a"), act("b", args=["{{results.a}}"])]}
        self.assertEqual(await self.run_process(ParallelProcessWorkflow, process), {"a": "a", "b": {"saw": [None]}})
        self.assertEqual(
            await self.run_process(LinearProcessWorkflow, process), {"a": "a", "b": {"saw": ["a"]}}
        )

    async def test_default_child_id_is_name_and_uuid(self) -> None:
        process = {"steps": [{"id": "c", "type": "workflow", "workflow_name": "EchoChild", "wait_for_result": False}]}
        result = await self.run_process(LinearProcessWorkflow, process)
        name, _, rest = result["c"]["child_workflow_id"].partition("_")
        self.assertEqual(name, "EchoChild")
        uuid.UUID(rest)

    async def test_signal_name_is_ignored_and_current_step_is_always_none(self) -> None:
        process = {"steps": [act("a"), {"id": "p", "type": "pause", "signal_name": "resume_p"}]}
        handle = await self.client.start_workflow(
            LinearProcessWorkflow.run, args=[process], id=f"q-{uuid.uuid4()}", task_queue=QUEUE
        )
        await handle.signal("resume_step", args=["p", {"go": True}])
        self.assertEqual(await handle.result(), {"a": "a", "p": {"go": True}})
        state = await handle.query("get_step_results")
        self.assertEqual(state, {"current_step": None, "completed_steps": ["a", "p"], "results": state["results"]})

    async def test_an_unknown_step_type_stores_none(self) -> None:
        process = {"steps": [{"id": "x", "type": "teleport"}, act("a")]}
        self.assertEqual(await self.run_process(LinearProcessWorkflow, process), {"x": None, "a": "a"})

    async def test_more_than_one_args_element_fails_the_workflow_task(self) -> None:
        wid = f"q-{uuid.uuid4()}"
        with self.assertRaises(WorkflowFailureError):
            await self.client.execute_workflow(
                LinearProcessWorkflow.run,
                args=[{"steps": [act("b", args=[1, 2])]}],
                id=wid,
                task_queue=QUEUE,
                execution_timeout=timedelta(seconds=30),
            )
        history = await self.client.get_workflow_handle(wid).fetch_history()
        kinds = {e.event_type for e in history.events}
        self.assertIn(EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED, kinds)
        self.assertIn(EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT, kinds)
        self.assertNotIn(EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED, kinds)


if __name__ == "__main__":
    unittest.main()
