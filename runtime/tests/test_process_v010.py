"""What v0.10 changed after the second review of the process engine (runtime/README.md, "Since v0.10"): conditions
short-circuit and a comparison that can't be made is false on its own, mistyped placeholders are refused, a file
without `interpreter:` is checked as the default interpreter, one escape rule for the runtime and the validator, stricter
durations and `retry:` / `options:` values, non-finite numbers in a condition, and Temporal errors pass the run-start
check."""

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from temporalio.worker.workflow_sandbox import RestrictedWorkflowAccessError

from factory_runtime.process import (
    BUILTIN_STEP_TYPES,
    ProcessValidationError,
    StepType,
    evaluate_condition,
    process_problems,
    resolve_argument,
)
from factory_runtime.process.files import load_process
from factory_runtime.process.settings import ProcessSettings
from tests.process_workflows import LinearProcessWorkflow
from tests.test_process_behaviour import act, recorded_workflow


def problems(steps: list[Any], **kwargs: Any) -> list[str]:
    return process_problems({"steps": steps}, BUILTIN_STEP_TYPES, **kwargs)


class _MockedWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("factory_runtime.process.templates.workflow")
        self.wf = patcher.start()
        self.addCleanup(patcher.stop)


class ShortCircuitConditionTest(_MockedWorkflow):
    def test_a_guard_protects_the_comparison_after_it(self) -> None:
        self.assertTrue(evaluate_condition("{{input.n}} == None or {{input.n}} > 0", {}, {}))
        self.assertTrue(evaluate_condition("{{input.n}} == None or {{input.n}} > 0", {}, {"n": 2}))
        self.assertFalse(evaluate_condition("{{input.n}} == None or {{input.n}} > 0", {}, {"n": -1}))
        self.assertFalse(evaluate_condition("{{input.n}} != None and {{input.n}} > 0", {}, {}))
        self.assertTrue(evaluate_condition("{{input.n}} != None and {{input.n}} > 0", {}, {"n": 2}))
        self.wf.logger.warning.assert_not_called()  # the guarded comparison was never made

    def test_a_comparison_that_cant_be_made_is_false_on_its_own(self) -> None:
        for condition, data, expected in (
            ("{{input.n}} > 0 or {{input.flag}} == True", {"flag": True}, True),
            ("not ({{input.n}} > 0)", {}, True),
            ("{{input.n}} > 0 and True", {}, False),
            ("({{input.a}} / {{input.b}} > 1) or {{input.b}} == 0", {"a": 1, "b": 0}, True),
        ):
            with self.subTest(condition=condition):
                self.wf.reset_mock()
                self.assertIs(evaluate_condition(condition, {}, data), expected)
                self.wf.logger.warning.assert_called_once()

    def test_and_or_give_python_values(self) -> None:
        self.assertTrue(evaluate_condition("{{input.s}} and {{input.s}} == 'x'", {}, {"s": "x"}))
        self.assertFalse(evaluate_condition("{{input.s}} and {{input.s}} > 1", {}, {"s": ""}))


class NonFiniteConditionTest(_MockedWorkflow):
    def test_a_non_finite_number_cant_be_compared(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.wf.reset_mock()
                self.assertFalse(evaluate_condition("{{input.f}} > 0", {}, {"f": value}))
                self.assertTrue(evaluate_condition("{{input.f}} == None or {{input.f}} > 0 or True", {}, {"f": value}))
                self.wf.logger.warning.assert_called()

    def test_a_finite_float_still_compares(self) -> None:
        self.assertTrue(evaluate_condition("{{input.f}} > 0.5", {}, {"f": 1.5}))


class MistypedPlaceholderTest(unittest.TestCase):
    def test_near_misses_of_the_roots_are_problems(self) -> None:
        for text in ("{{inputs.x}}", "{{ inputs.x }}", "{{result.a}}", "{{Input.x}}", "{{RESULTS.a}}",
                     "{{items.name}}", "{{Item.name}}", "see {{input_s.x}} {{inputs.x}}"):
            with self.subTest(text=text):
                found = problems([act("a", args={"t": text})])
                self.assertEqual(len(found), 1, found)
                self.assertIn("is not a placeholder", found[0])

    def test_other_text_in_braces_is_still_literal(self) -> None:
        for text in ("{{name}}", "{{inputs}}", "{{ result }}", "{{env.HOME}}", "{{inputs.}}", "{{inputs.1x}}",
                     r"\{{inputs.x}}", r"\{{result.a}}"):
            with self.subTest(text=text):
                self.assertEqual(problems([act("a", args={"t": text})]), [])


class DefaultInterpreterTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "processes").mkdir()
        (self.root / "processes" / "p.yaml").write_text(
            "steps:\n  - {id: a, type: activity, activity: x}\n"
            "  - {id: b, type: activity, activity: y, args: ['{{results.a}}']}\n"
        )

    def test_a_parallel_default_interpreter_is_checked(self) -> None:
        settings = ProcessSettings(
            project_root=self.root, step_types=BUILTIN_STEP_TYPES, default_interpreter="ParallelProcessWorkflow"
        )
        with self.assertRaisesRegex(ProcessValidationError, "parallel process"):
            load_process("processes/p.yaml", settings)

    def test_an_unregistered_default_interpreter_is_refused(self) -> None:
        settings = ProcessSettings(
            project_root=self.root, step_types=BUILTIN_STEP_TYPES, interpreters=("ConditionalProcessWorkflow",)
        )
        with self.assertRaisesRegex(ProcessValidationError, "LinearProcessWorkflow"):
            load_process("processes/p.yaml", settings)
        ok = ProcessSettings(project_root=self.root, step_types=BUILTIN_STEP_TYPES, interpreters=("LinearProcessWorkflow",))
        self.assertEqual(load_process("processes/p.yaml", ok)["steps"][0]["id"], "a")


class EscapeRuleTest(_MockedWorkflow):
    def test_the_validator_reads_escapes_as_the_runtime_does(self) -> None:
        text = r"\{{{results.nope}}"
        self.assertEqual(resolve_argument(text, {}, {}), "{{{results.nope}}")
        self.assertEqual(problems([act("a", args={"t": text})]), [])

    def test_an_escaped_backslash_before_a_placeholder(self) -> None:
        self.assertEqual(resolve_argument(r"\\{{input.x}}", {}, {"x": 1}), "\\1")
        self.assertEqual(resolve_argument(r"\\\{{input.x}}", {}, {"x": 1}), "\\{{input.x}}")
        self.assertEqual(resolve_argument(r"a\b {{input.x}}", {}, {"x": 1}), r"a\b 1")
        self.assertEqual(resolve_argument(r"\\ {{input.x}}", {}, {"x": 1}), r"\\ 1")
        self.assertEqual(
            problems([act("a", args={"t": r"\\{{results.nope}}"})]),
            ["step 'a': {{results.nope}}: step 'nope' is not a step of this process"],
        )


class StrictValuesTest(unittest.TestCase):
    def test_durations_must_be_finite_and_sensible(self) -> None:
        for value in (float("inf"), float("nan"), 10**30, 11 * 365 * 24 * 3600, "60", True):
            with self.subTest(value=value):
                self.assertEqual(
                    problems([act("a", timeout=value)]),
                    ["step 'a': `timeout:` must be seconds (more than 0, at most 10 years)"],
                )
        self.assertEqual(problems([act("a", timeout=9 * 365 * 24 * 3600)]), [])

    def test_retry_values_are_typed(self) -> None:
        bad = [
            act("a", retry={"max_attempts": "x"}),
            act("b", retry={"max_attempts": 0}),
            act("c", retry={"max_attempts": 2.5}),
            act("d", retry={"initial_interval_seconds": float("nan")}),
            act("e", retry={"initial_interval_seconds": 0}),
            act("f", retry={"non_retryable_error_types": "ValueError"}),
            act("g", retry={"non_retryable_error_types": ["ValueError", 3]}),
        ]
        self.assertEqual(
            [p.split(":", 1)[0] for p in problems(bad)], ["step 'a'", "step 'b'", "step 'c'", "step 'd'", "step 'e'",
                                                         "step 'f'", "step 'g'"]
        )
        good = act("ok", retry={"max_attempts": 1, "initial_interval_seconds": 0.5,
                                "non_retryable_error_types": ["ValueError"]})
        self.assertEqual(problems([good]), [])

    def test_option_values_are_typed(self) -> None:
        found = problems(
            [
                act("a", options={"summary": ["x"]}),
                act("b", options={"activity_id": 5}),
                act("c", options={"task_queue": None}),
                act("d", options={"schedule_to_start_timeout": float("inf")}),
            ]
        )
        self.assertEqual(
            found,
            [
                "step 'a': `options.summary` must be text",
                "step 'b': `options.activity_id` must be text",
                "step 'c': `options.task_queue` must be text",
                "step 'd': `options.schedule_to_start_timeout` must be seconds (more than 0, at most 10 years)",
            ],
        )
        self.assertEqual(problems([act("ok", options={"summary": "Echo {{input.x}}", "task_queue": "q"})]), [])


class RunStartCheckTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_sandbox_restriction_fails_the_task_not_the_run(self) -> None:
        def restricted_check(step: dict[str, Any]) -> list[str]:
            raise RestrictedWorkflowAccessError("os.getenv")

        wf_instance = LinearProcessWorkflow()
        wf_instance.step_types = {**BUILTIN_STEP_TYPES, "odd": StepType(act_step, check=restricted_check)}
        with recorded_workflow(), self.assertRaises(RestrictedWorkflowAccessError):
            await wf_instance.run_linear({"steps": [{"id": "x", "type": "odd"}]}, {}, None)


async def act_step(wf: Any, step: dict[str, Any], ctx: Any) -> Any:
    return None


if __name__ == "__main__":
    unittest.main()
