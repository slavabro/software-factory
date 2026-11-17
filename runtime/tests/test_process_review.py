"""What v0.9 changed after the process engine's review (runtime/README.md, "Process engine"): a product's step types
are part of its settings, a condition that can't compare is false, the validator can't crash a run, literal braces
can be written, a workflow step resolves its id and queue, one rule for `args:`, a zero timeout is refused, and the
retired keys belong to the built-in types only."""

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

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
from factory_runtime.process.schedules import ProcessScheduleConfig, ScheduleDefinition, build_schedule
from factory_runtime.process.settings import ProcessSettings
from factory_runtime.temporal import TemporalSettings
from tests.process_workflows import LinearProcessWorkflow
from tests.test_process_behaviour import act, recorded_workflow


async def review_step(wf: ProcessWorkflowBase, step: dict[str, Any], ctx: StepContext) -> Any:
    return ctx.resolve(step["subject"])


PRODUCT_TYPES = {
    **BUILTIN_STEP_TYPES,
    "review": StepType(review_step, for_each=True, required=frozenset({"subject"}), optional=frozenset({"kwargs"})),
    "loose": StepType(review_step),  # keys unchecked
}
REVIEW_FILE = "steps:\n  - {id: r, type: review, subject: s}\n"


def problems(steps: list[Any], types: Any = BUILTIN_STEP_TYPES, **kwargs: Any) -> list[str]:
    return process_problems({"steps": steps}, types, **kwargs)


class StepTypesAreTheProductsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "processes").mkdir()
        (self.root / "processes" / "p.yaml").write_text(REVIEW_FILE)

    def test_settings_need_the_step_types(self) -> None:
        with self.assertRaises(TypeError):
            ProcessSettings(project_root=self.root)  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            validate_process({"steps": []})  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            process_problems({"steps": []})  # type: ignore[call-arg]

    def test_load_process_validates_against_the_settings_types(self) -> None:
        product = ProcessSettings(project_root=self.root, step_types=PRODUCT_TYPES)
        self.assertEqual(load_process("processes/p.yaml", product)["steps"][0]["type"], "review")
        builtin_only = ProcessSettings(project_root=self.root, step_types=BUILTIN_STEP_TYPES)
        with self.assertRaisesRegex(ProcessValidationError, "unknown type 'review'"):
            load_process("processes/p.yaml", builtin_only)

    def test_the_schedule_builder_loads_with_the_settings_types(self) -> None:
        schedule_def = ScheduleDefinition(id="s", cron="0 9 * * *", timezone="UTC")
        config = ProcessScheduleConfig(
            process_path="processes/p.yaml", interpreter="LinearProcessWorkflow", schedules=[schedule_def]
        )
        settings = ProcessSettings(project_root=self.root, step_types=PRODUCT_TYPES)
        schedule, _ = build_schedule(config, schedule_def, settings, TemporalSettings(task_queue="q"))
        self.assertEqual(schedule.action.args[0]["steps"][0]["id"], "r")  # type: ignore[attr-defined]

    def test_an_interpreter_the_product_does_not_register_is_rejected(self) -> None:
        (self.root / "processes" / "h.yaml").write_text("interpreter: HybridProcessWorkflow\n" + REVIEW_FILE)
        (self.root / "processes" / "odd.yaml").write_text("interpreter: [LinearProcessWorkflow]\n" + REVIEW_FILE)
        settings = ProcessSettings(
            project_root=self.root,
            step_types=PRODUCT_TYPES,
            interpreters=("LinearProcessWorkflow", "ParallelProcessWorkflow"),
        )
        self.assertEqual(load_process("processes/p.yaml", settings)["steps"][0]["id"], "r")
        with self.assertRaisesRegex(ProcessValidationError, "unknown `interpreter: HybridProcessWorkflow`"):
            load_process("processes/h.yaml", settings)
        with self.assertRaisesRegex(ProcessValidationError, "`interpreter:` must name"):
            load_process("processes/odd.yaml", settings)
        # unchecked when the product lists none
        unchecked = ProcessSettings(project_root=self.root, step_types=PRODUCT_TYPES)
        self.assertEqual(load_process("processes/h.yaml", unchecked)["interpreter"], "HybridProcessWorkflow")


class SafeConditionTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("factory_runtime.process.templates.workflow")
        self.wf = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_comparison_that_cant_be_made_is_false_and_logged(self) -> None:
        for condition, results, data in (
            ("{{results.f.n}} > 0", {}, {}),  # f was skipped
            ("None > 0", {}, {}),
            ("'5' > 0", {}, {}),
            ("{{input.n}} / 0 == 1", {}, {"n": 3}),
            ("-{{input.s}} < 0", {}, {"s": "x"}),
        ):
            with self.subTest(condition=condition):
                self.wf.reset_mock()
                self.assertFalse(evaluate_condition(condition, results, data))
                self.wf.logger.warning.assert_called_once()

    def test_bad_syntax_and_whole_values_are_still_definition_errors(self) -> None:
        for condition, data in (("{{input.n}} >", {"n": 1}), ("{{input.d}} == 1", {"d": {"a": 1}}), ("x == 1", {})):
            with self.subTest(condition=condition), self.assertRaises(ConditionError):
                evaluate_condition(condition, {}, data)

    def test_a_comparable_condition_is_unchanged(self) -> None:
        self.assertTrue(evaluate_condition("{{input.n}} > 0", {}, {"n": 2}))
        self.assertTrue(evaluate_condition("{{results.f.n}} == None", {}, {}))


class SafeConditionRunTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_skipped_steps_value_skips_the_step_instead_of_failing_the_run(self) -> None:
        process = {"steps": [act("f", condition="1 == 2"), act("g", condition="{{results.f.n}} > 0"), act("h")]}
        with recorded_workflow():
            results = await LinearProcessWorkflow().run_linear(process, {}, None)
        self.assertEqual(list(results), ["h"])


class ValidatorHardeningTest(unittest.IsolatedAsyncioTestCase):
    def test_malformed_values_are_problems_not_crashes(self) -> None:
        found = problems(
            [
                {"id": "w", "type": "workflow", "workflow_name": "C", "parent_close_policy": ["abandon"]},
                act("o", options={True: 1, "summary": "s"}),
                act("r", retry={1: 2, "max_attempts": 2}),
                {"id": "k", "type": "pause", True: "x", "timeout": 5},
                {"id": "f", "type": "for_each", "items": [1], "step": {"type": "activity", "activity": "x", 2: 1}},
            ]
        )
        self.assertEqual(
            found,
            [
                "step 'w': unknown `parent_close_policy: ['abandon']` (known: ['abandon', 'request_cancel', "
                "'terminate'])",
                "step 'o': unknown `options:` [True] (known: ['activity_id', 'schedule_to_close_timeout', "
                "'schedule_to_start_timeout', 'summary', 'task_queue'])",
                "step 'r': unknown `retry:` keys [1] (known: ['initial_interval_seconds', 'max_attempts', "
                "'non_retryable_error_types'])",
                "step 'k': unknown keys [True] (allowed: ['condition', 'description', 'id', 'on_error', 'timeout', "
                "'type'])",
                "step 'f': inner step: unknown keys [2] (allowed: ['activity', 'args', 'description', "
                "'heartbeat_timeout_seconds', 'options', 'retry', 'timeout', 'type'])",
            ],
        )

    async def test_a_validator_that_crashes_fails_the_run_as_a_definition_error(self) -> None:
        def crashing_check(step: dict[str, Any]) -> list[str]:
            raise TypeError("unhashable type: 'list'")

        wf_instance = LinearProcessWorkflow()
        wf_instance.step_types = {**BUILTIN_STEP_TYPES, "odd": StepType(review_step, check=crashing_check)}
        with recorded_workflow(), self.assertRaises(ApplicationError) as caught:
            await wf_instance.run_linear({"steps": [{"id": "x", "type": "odd", "subject": 1}]}, {}, None)
        self.assertEqual((caught.exception.type, caught.exception.non_retryable), ("ProcessDefinitionError", True))
        self.assertIn("TypeError: unhashable type: 'list'", caught.exception.message)


class LiteralBracesTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("factory_runtime.process.templates.workflow")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_escaped_placeholder_is_literal_text(self) -> None:
        self.assertEqual(resolve_argument(r"\{{input.x}}", {}, {"x": 1}), "{{input.x}}")
        self.assertEqual(resolve_argument(r"a \{{input.x}} b {{input.x}}", {}, {"x": 1}), "a {{input.x}} b 1")
        self.assertEqual(resolve_argument({"k": [r"\{{name}}"]}, {}, {}), {"k": ["{{name}}"]})

    def test_only_an_attempted_placeholder_is_flagged(self) -> None:
        self.assertEqual(
            problems(
                [
                    act("a", description="Fills {{results.later}} and {{whatever}}"),
                    act("b", args={"tpl": "Hello {{name}}", "lit": r"\{{results.later}}", "m": "{{ env.x }}"}),
                ]
            ),
            [],
        )
        self.assertEqual(
            problems([act("c", args={"x": "{{results}}", "y": "{{input..a}}", "z": "{{item}}"})]),
            [
                "step 'c': {{results}} is not a placeholder: use {{results.<step>...}}, {{input....}} or, inside "
                "for_each, {{item...}}",
                "step 'c': {{input..a}} is not a placeholder: use {{results.<step>...}}, {{input....}} or, inside "
                "for_each, {{item...}}",
                "step 'c': {{item}} is used outside a for_each inner step",
            ],
        )

    def test_a_condition_still_flags_anything_in_braces(self) -> None:
        self.assertEqual(
            problems([act("c", condition="{{name}} == 1")]),
            [
                "step 'c': {{name}} is not a placeholder: use {{results.<step>...}}, {{input....}} or, inside "
                "for_each, {{item...}}"
            ],
        )


class WorkflowStepTest(unittest.IsolatedAsyncioTestCase):
    async def test_workflow_id_and_task_queue_are_resolved(self) -> None:
        process = {
            "steps": [
                {
                    "id": "c",
                    "type": "workflow",
                    "workflow_name": "Child",
                    "workflow_id": "child-{{input.k}}",
                    "task_queue": "{{input.q}}",
                }
            ]
        }
        with recorded_workflow() as wf:
            await LinearProcessWorkflow().run_linear(process, {"k": 7, "q": "other"}, None)
        _, kwargs = wf.execute_child_workflow.call_args
        self.assertEqual((kwargs["id"], kwargs["task_queue"]), ("child-7", "other"))

    async def test_args_one_element_list_gives_its_element_for_a_child_too(self) -> None:
        process = {
            "steps": [
                {"id": "c", "type": "workflow", "workflow_name": "Child", "args": [{"a": "{{input.k}}"}]},
                {"id": "d", "type": "workflow", "workflow_name": "Child"},
                act("e", args=[{"a": 1}]),
            ]
        }
        with recorded_workflow() as wf:
            await LinearProcessWorkflow().run_linear(process, {"k": 7}, None)
        children = [call.args for call in wf.execute_child_workflow.call_args_list]
        self.assertEqual(children, [("Child", {"a": 7}), ("Child", {})])
        self.assertEqual(wf.execute_activity.call_args.args, ("e_activity", {"a": 1}))

    def test_a_child_takes_one_argument_too(self) -> None:
        self.assertEqual(
            problems([{"id": "c", "type": "workflow", "workflow_name": "C", "args": [1, 2]}]),
            ["step 'c': `args:` has 2 elements; a child workflow takes one argument: pass a dict (or one value)"],
        )


class TimeoutTest(unittest.TestCase):
    def test_a_zero_timeout_is_refused(self) -> None:
        self.assertEqual(
            problems(
                [
                    act("a", timeout=0, heartbeat_timeout_seconds=0, options={"schedule_to_close_timeout": 0}),
                    {"id": "w", "type": "workflow", "workflow_name": "C", "execution_timeout": 0},
                    {"id": "p", "type": "pause", "timeout": 0},
                    act("ok", timeout=0.5),
                ]
            ),
            [
                "step 'a': `options.schedule_to_close_timeout` must be seconds (more than 0, at most 10 years)",
                "step 'a': `timeout:` must be seconds (more than 0, at most 10 years)",
                "step 'a': `heartbeat_timeout_seconds:` must be seconds (more than 0, at most 10 years)",
                "step 'w': `execution_timeout:` must be seconds (more than 0, at most 10 years)",
                "step 'p': `timeout:` must be seconds (more than 0, at most 10 years)",
            ],
        )


class RetiredKeysTest(unittest.TestCase):
    def test_retired_keys_belong_to_the_builtin_types(self) -> None:
        self.assertEqual(
            problems(
                [
                    {"id": "r", "type": "review", "subject": "s", "kwargs": {"a": 1}},  # its own optional key
                    {"id": "l", "type": "loose", "kwargs": 1, "signal_name": "x"},  # keys unchecked
                    act("a", kwargs={}),
                    {"id": "p", "type": "pause", "signal_name": "go"},
                ],
                PRODUCT_TYPES,
            ),
            [
                "step 'a': `kwargs:` was renamed `options:` (they are execute_activity options, not activity "
                "arguments)",
                "step 'p': `signal_name:` was removed: a pause step is resumed by `resume_step(step_id, data)` under "
                "its own id",
            ],
        )


if __name__ == "__main__":
    unittest.main()
