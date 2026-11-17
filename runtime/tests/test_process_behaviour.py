"""What the process engine does since v0.8, when the quirks it kept for replay were fixed (runtime/README.md,
docs/designs/process-engine.md section 7): placeholders inside text, conditions everywhere and failing loudly, no
uuid4 draw per step, definition errors failing the run, `current_step`, late signals dropped, a fetch shared by
parallel steps, and the validator. Also the plug-in contract and how a product declares its workflow classes."""

import asyncio
import contextlib
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest import mock

from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError

from factory_runtime.process import (
    BUILTIN_STEP_TYPES,
    ConditionError,
    ProcessValidationError,
    ProcessWorkflowBase,
    StepContext,
    StepType,
    evaluate_condition,
    process_problems,
    resolve_argument,
    validate_process,
)
from factory_runtime.process.files import load_process
from factory_runtime.testing import make_worker, query_eventually, time_skipping_env
from tests.process_workflows import (
    PROCESS_WORKFLOWS,
    ConditionalProcessWorkflow,
    LinearProcessWorkflow,
    ParallelProcessWorkflow,
)

QUEUE = "process-behaviour"


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


class TemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("factory_runtime.process.templates.workflow")
        self.wf = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_whole_string_gives_the_value_itself(self) -> None:
        self.assertEqual(resolve_argument("{{input.a}}", {}, {"a": {"b": 1}}), {"b": 1})
        self.assertEqual(resolve_argument("{{ input.a }}", {}, {"a": 1}), 1)

    def test_a_placeholder_inside_text_is_filled_in(self) -> None:
        self.assertEqual(resolve_argument("Approve {{input.label}}", {}, {"label": "alpha"}), "Approve alpha")
        self.assertEqual(resolve_argument("{{input.a}}/{{input.b}}", {}, {"a": 1, "b": [2]}), "1/[2]")
        self.assertEqual(resolve_argument("missing: {{input.nope}}", {}, {}), "missing: None")
        self.assertEqual(resolve_argument("{{item.n}} of {{results.s}}", {"s": 3}, {}, loop_item={"n": 2}), "2 of 3")

    def test_anything_else_in_braces_stays_as_written(self) -> None:
        for text in ("{{env.HOME}}", "x {{results}} y", "{{input}}"):
            self.assertEqual(resolve_argument(text, {}, {"a": 1}), text)

    def test_a_missing_template_path_gives_none(self) -> None:
        self.assertIsNone(resolve_argument("{{results.nope.x}}", {}, {}))
        self.assertIsNone(resolve_argument("{{input.nope}}", {}, {}))
        self.assertIsNone(resolve_argument("{{item.x}}", {}, {}, loop_item={"y": 1}))

    def test_a_condition_compares_single_values_only(self) -> None:
        self.assertTrue(evaluate_condition("{{input.s}} == 'q' and {{input.n}} > 1", {}, {"s": "q", "n": 2}))
        self.assertTrue(evaluate_condition("{{results.gone}} == None", {}, {}))
        for value in ({"a": 1}, [1]):
            with self.subTest(value=value), self.assertRaisesRegex(ConditionError, "name a field of it"):
                evaluate_condition("{{results.x}} == None", {"x": value}, {})


class CommandOrderTest(unittest.IsolatedAsyncioTestCase):
    """No uuid4 per step: only a child with no `workflow_id` and a step type that reads `ctx.unique_id` draw one."""

    async def test_linear_draws_a_uuid_only_for_a_default_child_id(self) -> None:
        process = {
            "steps": [
                act("a"),
                {"id": "c", "type": "workflow", "workflow_name": "Child"},
                {"id": "f", "type": "for_each", "items": [1, 2], "step": {"type": "workflow", "workflow_name": "Kid"}},
                {"id": "n", "type": "workflow", "workflow_name": "Named", "workflow_id": "fixed"},
            ]
        }
        with recorded_workflow() as wf:
            await LinearProcessWorkflow().run_linear(process, {}, None)
        self.assertEqual(
            commands(wf),
            [
                ("execute_activity", ("a_activity", None)),
                ("uuid4", 1),
                ("execute_child_workflow", ("Child", f"Child_{u(1)}")),
                ("uuid4", 2),
                ("execute_child_workflow", ("Kid", f"Kid_{u(2)}")),
                ("uuid4", 3),
                ("execute_child_workflow", ("Kid", f"Kid_{u(3)}")),
                ("execute_child_workflow", ("Named", "fixed")),
            ],
        )

    async def test_every_loop_honours_condition_and_skips_absent_from_results(self) -> None:
        process = {"steps": [act("a", condition="1 == 2"), act("b", condition="{{input.go}} == True"), act("c")]}
        for run in (
            lambda: LinearProcessWorkflow().run_linear(process, {"go": True}, None),
            lambda: ConditionalProcessWorkflow().run_conditional(process, {"go": True}, None),
            lambda: ParallelProcessWorkflow().run_parallel(process, {"go": True}, None),
        ):
            with recorded_workflow() as wf:
                results = await run()
            self.assertEqual(
                commands(wf), [("execute_activity", ("b_activity", None)), ("execute_activity", ("c_activity", None))]
            )
            self.assertEqual(list(results), ["b", "c"])

    async def test_parallel_starts_every_step_in_order(self) -> None:
        process = {"steps": [act("a"), act("b"), act("c")]}
        with recorded_workflow() as wf:
            await ParallelProcessWorkflow().run_parallel(process, {}, None)
        self.assertEqual([name for _, (name, _) in commands(wf)], ["a_activity", "b_activity", "c_activity"])


class ContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_step_function_gets_the_workflow_its_step_and_the_context(self) -> None:
        seen: list[tuple[Any, dict[str, Any], StepContext]] = []

        async def custom(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> Any:
            seen.append((wf, step, ctx))
            wf.step_state["n"] = wf.step_state.get("n", 0) + 1
            return [ctx.resolve(step["value"]), ctx.unique_id]

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
        self.assertEqual(
            results, {"one": [7, f"one_{u(1)}"], "many": [["p", f"many_{u(2)}[0]"], ["q", f"many_{u(2)}[1]"]]}
        )
        self.assertEqual(wf_instance.step_state, {"n": 3})
        (_, _, top), (_, _, first), _ = seen
        self.assertEqual((top.base_id, top.item, first.item), ("one", None, "p"))
        self.assertIs(top.process_def, process)
        self.assertEqual(top.metadata, {"by": "test"})
        self.assertIs(top.results, results)

    async def test_a_step_type_with_its_own_keys_is_validated(self) -> None:
        async def custom(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> Any:
            return step["subject"]

        types = {
            **BUILTIN_STEP_TYPES,
            "custom": StepType(custom, for_each=True, required=frozenset({"subject"}), optional=frozenset({"notes"})),
            "loose": StepType(custom),  # keys unchecked
        }
        self.assertEqual(
            process_problems(
                {"steps": [{"id": "a", "type": "custom", "notes": "n", "extra": 1}, {"id": "b", "type": "loose"}]},
                types,
            ),
            [
                "step 'a': missing `subject:`",
                "step 'a': unknown keys ['extra'] (allowed: ['condition', 'description', 'id', 'notes', 'on_error', "
                "'subject', 'type'])",
            ],
        )

    def test_the_builtin_map_is_read_only(self) -> None:
        with self.assertRaises(TypeError):
            BUILTIN_STEP_TYPES["x"] = StepType(BUILTIN_STEP_TYPES["activity"].run)  # type: ignore[index]
        self.assertEqual({name for name, t in BUILTIN_STEP_TYPES.items() if t.for_each}, {"activity", "workflow"})

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


class ProgressTest(unittest.IsolatedAsyncioTestCase):
    async def test_current_step_is_the_running_step_and_none_when_idle(self) -> None:
        wf_instance = LinearProcessWorkflow()
        answers: list[Any] = []

        async def probe(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> Any:
            answers.append(wf.get_step_results()["current_step"])
            return 1

        wf_instance.step_types = {**BUILTIN_STEP_TYPES, "probe": StepType(probe)}
        with recorded_workflow():
            self.assertIsNone(wf_instance.get_step_results()["current_step"])
            await wf_instance.run_linear({"steps": [{"id": "x", "type": "probe"}, {"id": "y", "type": "probe"}]})
        self.assertEqual(answers, ["x", "y"])
        self.assertIsNone(wf_instance.get_step_results()["current_step"])

    async def test_completed_steps_survive_a_fatal_failure_in_parallel(self) -> None:
        async def ok(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> Any:
            await asyncio.sleep(0)
            return "done"

        async def boom(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> Any:
            for _ in range(3):
                await asyncio.sleep(0)
            raise ApplicationError("child failed", type="Boom")

        wf_instance = ParallelProcessWorkflow()
        wf_instance.step_types = {**BUILTIN_STEP_TYPES, "ok": StepType(ok), "boom": StepType(boom)}
        with recorded_workflow(), self.assertRaisesRegex(ApplicationError, "child failed"):
            await wf_instance.run_parallel({"steps": [{"id": "fine", "type": "ok"}, {"id": "bad", "type": "boom"}]})
        state = wf_instance.get_step_results()
        self.assertEqual((state["completed_steps"], state["current_step"]), (["fine"], None))


class ResumeTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_signal_after_the_pause_expired_is_dropped(self) -> None:
        wf_instance = LinearProcessWorkflow()
        with recorded_workflow() as wf:
            wf.wait_condition.side_effect = asyncio.TimeoutError()
            expired = await wf_instance.wait_for_resume({"timeout": 5}, "gate")
            await wf_instance.resume_step_signal("gate", {"late": True})
            await wf_instance.resume_step_signal("later_step", {"early": True})
        self.assertEqual(expired, {"action": "expired", "timeout_seconds": 5})
        self.assertEqual(wf_instance._pause_results, {"later_step": {"early": True}})  # an early one is kept
        wf.logger.warning.assert_called_once()


class OnceTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_callers_share_one_call_and_a_failure_is_forgotten(self) -> None:
        wf_instance = ProcessWorkflowBase()
        calls: list[int] = []

        async def fetch() -> dict[str, int]:
            calls.append(1)
            await asyncio.sleep(0)
            return {"n": len(calls)}

        first, second = await asyncio.gather(wf_instance.once("k", fetch), wf_instance.once("k", fetch))
        self.assertEqual((first, second, len(calls)), ({"n": 1}, {"n": 1}, 1))
        self.assertEqual(await wf_instance.once("k", fetch), {"n": 1})

        async def failing() -> dict[str, int]:
            raise RuntimeError("down")

        with self.assertRaises(RuntimeError):
            await wf_instance.once("f", failing)
        self.assertEqual(await wf_instance.once("f", fetch), {"n": 2})


class ValidationTest(unittest.TestCase):
    def problems(self, steps: list[dict[str, Any]], **kwargs: Any) -> list[str]:
        return process_problems({"steps": steps}, **kwargs)

    def test_a_valid_process_has_no_problems(self) -> None:
        steps = [
            act("a", args=[{"x": "{{input.x}}"}], options={"summary": "Run {{input.x}}"}, description="d"),
            act("b", condition="{{results.a.ok}} == True", on_error="continue"),
            {
                "id": "f",
                "type": "for_each",
                "items": "{{results.a.items}}",
                "step": {"type": "activity", "activity": "x", "args": ["{{item.name}} for {{results.b}}"]},
            },
        ]
        self.assertEqual(self.problems(steps), [])

    def test_each_problem_names_its_step(self) -> None:
        self.assertEqual(
            self.problems(
                [
                    {"type": "activity", "activity": "x"},
                    act("a", kwargs={"summary": "s"}, args=[1, 2]),
                    {"id": "p", "type": "pause", "signal_name": "go"},
                    {"id": "c", "type": "activity", "activity": "x", "condition": ""},
                    {"id": "f", "type": "for_each", "items": [1], "step": {"type": "for_each"}},
                    act("r", args=[{"x": "{{results.later}}", "y": "{{results.r}}"}]),
                    act("later"),
                ]
            ),
            [
                "step 1: needs an `id:` (text)",
                "step 'a': `kwargs:` was renamed `options:` (they are execute_activity options, not activity arguments)",
                "step 'a': `args:` has 2 elements; an activity takes one argument: pass a dict (or one value)",
                "step 'p': `signal_name:` was removed: a pause step is resumed by `resume_step(step_id, data)` under "
                "its own id",
                "step 'c': `condition:` must be a non-empty expression",
                "step 'f': for_each can't run an inner step of type 'for_each' (it can run: activity, workflow)",
                "step 'r': {{results.later}}: step 'later' runs later",
                "step 'r': {{results.r}}: a step can't read its own result",
            ],
        )

    def test_a_parallel_step_cant_read_another_steps_result(self) -> None:
        steps = [act("a"), act("b", args=["{{results.a}}"], condition="{{input.x}} == 1")]
        self.assertEqual(self.problems(steps), [])
        self.assertEqual(
            self.problems(steps, parallel=True),
            [
                "step 'b': {{results.a}}: a parallel process runs its steps at once, so a step can't read another "
                "step's result"
            ],
        )
        with self.assertRaises(ProcessValidationError):  # read from the file's interpreter
            validate_process({"interpreter": "ParallelProcessWorkflow", "steps": steps})

    def test_load_process_validates_and_names_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "good.yaml").write_text("steps:\n  - {id: a, type: activity, activity: x}\n")
            (root / "bad.yaml").write_text("steps:\n  - {id: a, type: teleport}\n")
            self.assertEqual(load_process("good.yaml", root)["steps"][0]["id"], "a")
            with self.assertRaisesRegex(ProcessValidationError, r"^bad\.yaml: .*step 'a': unknown type 'teleport'"):
                load_process("bad.yaml", root)


@activity.defn(name="a_activity")
async def a_activity(*args: Any) -> Any:
    return "a"


@activity.defn(name="b_activity")
async def b_activity(*args: Any) -> Any:
    return {"saw": list(args)}


class ServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.env = await time_skipping_env()
        self.addAsyncCleanup(self.env.shutdown)
        self.client = self.env.client
        worker = make_worker(
            self.client, task_queue=QUEUE, workflows=PROCESS_WORKFLOWS, activities=[a_activity, b_activity]
        )
        await worker.__aenter__()
        self.addAsyncCleanup(worker.__aexit__, None, None, None)

    async def run_process(self, interpreter: Any, process: Any, *args: Any) -> Any:
        return await self.client.execute_workflow(
            interpreter.run, args=[process, *args], id=f"q-{uuid.uuid4()}", task_queue=QUEUE
        )

    async def failure(self, interpreter: Any, process: Any) -> tuple[ApplicationError, set[Any]]:
        wid = f"q-{uuid.uuid4()}"
        with self.assertRaises(WorkflowFailureError) as caught:
            await self.client.execute_workflow(interpreter.run, args=[process], id=wid, task_queue=QUEUE)
        history = await self.client.get_workflow_handle(wid).fetch_history()
        cause = caught.exception.cause
        assert isinstance(cause, ApplicationError), cause
        return cause, {e.event_type for e in history.events}

    async def test_every_interpreter_honours_condition(self) -> None:
        process = {"steps": [act("a", condition="1 == 2"), act("b", condition="1 == 1")]}
        for interpreter in (LinearProcessWorkflow, ConditionalProcessWorkflow, ParallelProcessWorkflow):
            with self.subTest(interpreter.__name__):
                self.assertEqual(await self.run_process(interpreter, process), {"b": {"saw": []}})

    async def test_a_misconfigured_step_fails_the_run_not_the_task(self) -> None:
        for process, message in (
            (None, "Process definition is None"),
            ({"steps": [act("b", args=[1, 2])]}, "step 'b': `args:` has 2 elements"),
            ({"steps": [act("b", options={"bogus": 1})]}, "unknown `options:` ['bogus']"),
            ({"steps": [act("a"), act("b", condition="{{results.a}} == None")]}, None),  # a str: fine
            (
                {"steps": [act("b"), act("a", condition="{{results.b}} == None", on_error="continue")]},
                "{{results.b}} is a dict",
            ),
        ):
            with self.subTest(message=message):
                if message is None:
                    await self.run_process(LinearProcessWorkflow, process)
                    continue
                cause, events = await self.failure(LinearProcessWorkflow, process)
                self.assertIn(message, cause.message)
                self.assertEqual((cause.type, cause.non_retryable), ("ProcessDefinitionError", True))
                self.assertNotIn(EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED, events)

    async def test_a_dict_args_is_passed_whole(self) -> None:
        process = {"steps": [act("b", args={"k": "{{input.v}}", "text": "v is {{input.v}}"})]}
        self.assertEqual(
            await self.run_process(LinearProcessWorkflow, process, {"v": 3}),
            {"b": {"saw": [{"k": 3, "text": "v is 3"}]}},
        )

    async def test_a_parallel_sibling_reference_is_rejected(self) -> None:
        process = {"steps": [act("a"), act("b", args=["{{results.a}}"])]}
        cause, _ = await self.failure(ParallelProcessWorkflow, process)
        self.assertIn("can't read another step's result", cause.message)
        self.assertEqual(
            await self.run_process(LinearProcessWorkflow, process), {"a": "a", "b": {"saw": ["a"]}}
        )

    async def test_default_child_id_is_name_and_uuid(self) -> None:
        process = {"steps": [{"id": "c", "type": "workflow", "workflow_name": "EchoChild", "wait_for_result": False}]}
        result = await self.run_process(LinearProcessWorkflow, process)
        name, _, rest = result["c"]["child_workflow_id"].partition("_")
        self.assertEqual(name, "EchoChild")
        uuid.UUID(rest)

    async def test_a_signal_for_a_finished_pause_is_dropped(self) -> None:
        process = {"steps": [{"id": "p", "type": "pause", "timeout": 10}, {"id": "q", "type": "pause"}]}
        handle = await self.client.start_workflow(
            LinearProcessWorkflow.run, args=[process], id=f"q-{uuid.uuid4()}", task_queue=QUEUE
        )
        await handle.signal("resume_step", args=["p", {"go": 1}])
        await query_eventually(handle, "get_step_results", lambda answer: "p" in answer["completed_steps"])
        await handle.signal("resume_step", args=["p", {"go": 2}])  # p has finished: dropped, not kept for later
        await handle.signal("resume_step", args=["q", {"go": 3}])
        self.assertEqual(await handle.result(), {"p": {"go": 1}, "q": {"go": 3}})
        state = await handle.query("get_step_results")
        self.assertEqual(state["completed_steps"], ["p", "q"])
        self.assertIsNone(state["current_step"])


if __name__ == "__main__":
    unittest.main()
