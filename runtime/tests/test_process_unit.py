"""The process engine's workflow-side code without a server: conditions, placeholders, the built-in steps, dispatch,
pauses and resumes, with `temporalio.workflow` replaced by a mock (ported from FamilyOffice's engine unit tests)."""

import asyncio
import unittest
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from factory_runtime.process import (
    BUILTIN_STEP_TYPES,
    PROCESS_DEFINITION_ERROR,
    ConditionError,
    ProcessWorkflowBase,
    StepContext,
    StepType,
    activity_step,
    evaluate_condition,
    resolve_argument,
    safe_eval_condition,
    workflow_step,
)

MODULES = ("base", "steps", "templates")


@contextmanager
def mock_workflow():
    """One MagicMock standing in for `temporalio.workflow` in every engine module."""
    mock_wf = MagicMock()
    mock_wf.logger = MagicMock()
    patches = [patch(f"factory_runtime.process.{m}.workflow", mock_wf) for m in MODULES]
    for p in patches:
        p.start()
    try:
        yield mock_wf
    finally:
        for p in reversed(patches):
            p.stop()


def ctx(results=None, input_data=None, base_id="step", item=None):
    return StepContext(
        base_id=base_id,
        results=results if results is not None else {},
        input=input_data if input_data is not None else {},
        process_def={},
        metadata={},
        item=item,
    )


def with_steps(**types):
    """A workflow whose step map replaces the named types with mocks."""
    wf = ProcessWorkflowBase()
    wf.step_types = {**BUILTIN_STEP_TYPES, **{k: StepType(v) for k, v in types.items()}}
    return wf


# A. safe_eval_condition


class SafeEvalConditionTest(unittest.TestCase):
    def test_expressions(self):
        cases = {
            "1 == 1": True,
            "1 == 2": False,
            "'a' != 'b'": True,
            "3 < 5": True,
            "5 >= 5": True,
            "True and False": False,
            "False or True": True,
            "not False": True,
            "2 + 3 == 5": True,
            "10 % 3 == 1": True,
            "'approved' == 'approved'": True,
            "1 < 2 < 3": True,
            "None == None": True,
        }
        for expr, expected in cases.items():
            with self.subTest(expr=expr):
                self.assertIs(safe_eval_condition(expr), expected)

    def test_invalid_expression_raises(self):
        with self.assertRaises(ValueError):
            safe_eval_condition("import os")


# B. resolve_argument


class ResolveArgumentTest(unittest.TestCase):
    def _resolve(self, arg, results=None, input_data=None, strict=False):
        with mock_workflow():
            return resolve_argument(arg, results or {}, input_data or {}, strict=strict)

    def test_top_level_results(self):
        self.assertEqual(self._resolve("{{results.step_a}}", {"step_a": "value_a"}), "value_a")

    def test_nested_results_one_level(self):
        self.assertEqual(self._resolve("{{results.step_a.status}}", {"step_a": {"status": "ready"}}), "ready")

    def test_nested_results_two_levels(self):
        self.assertEqual(self._resolve("{{results.step_a.data.value}}", {"step_a": {"data": {"value": 42}}}), 42)

    def test_nested_results_three_levels(self):
        self.assertEqual(self._resolve("{{results.a.b.c.d}}", {"a": {"b": {"c": {"d": "deep"}}}}), "deep")

    def test_input_top_level(self):
        self.assertEqual(self._resolve("{{input.user_id}}", input_data={"user_id": "u123"}), "u123")

    def test_input_nested(self):
        self.assertEqual(self._resolve("{{input.config.threshold}}", input_data={"config": {"threshold": 0.5}}), 0.5)

    def test_missing_step_nonstrict_returns_none(self):
        self.assertIsNone(self._resolve("{{results.missing}}", {}, strict=False))

    def test_missing_step_strict_raises(self):
        with self.assertRaisesRegex(ValueError, "step 'missing' not found"):
            self._resolve("{{results.missing}}", {}, strict=True)

    def test_missing_nested_field_nonstrict(self):
        self.assertIsNone(self._resolve("{{results.step_a.nonexistent}}", {"step_a": {"status": "ok"}}))

    def test_missing_nested_field_strict_raises(self):
        with self.assertRaisesRegex(ValueError, "property 'nonexistent' not found"):
            self._resolve("{{results.step_a.nonexistent}}", {"step_a": {"status": "ok"}}, strict=True)

    def test_dict_argument(self):
        self.assertEqual(self._resolve({"key": "{{results.step_a}}"}, {"step_a": "val"}), {"key": "val"})

    def test_list_argument(self):
        self.assertEqual(self._resolve(["{{results.step_a}}", "{{input.x}}"], {"step_a": "a"}, {"x": "b"}), ["a", "b"])

    def test_non_placeholder_string_passthrough(self):
        self.assertEqual(self._resolve("plain string", {}), "plain string")

    def test_non_string_passthrough(self):
        self.assertEqual(self._resolve(42, {}), 42)
        self.assertIs(self._resolve(True, {}), True)
        self.assertIsNone(self._resolve(None, {}))

    def test_nested_dict_in_list(self):
        self.assertEqual(self._resolve([{"a": "{{results.x}}"}], {"x": "resolved"}), [{"a": "resolved"}])

    def test_result_value_is_list(self):
        self.assertEqual(self._resolve("{{results.step_a}}", {"step_a": [1, 2, 3]}), [1, 2, 3])


# B2. resolve_argument: the caller decides whether a missing path is logged


class ResolveArgumentLogMissingTest(unittest.TestCase):
    def _resolve_capturing_warnings(self, arg, results=None, **kwargs):
        with mock_workflow() as mock_wf:
            value = resolve_argument(arg, results or {}, {}, **kwargs)
            return value, mock_wf.logger.warning.call_count

    def test_missing_step_logs_warning_by_default(self):
        self.assertEqual(self._resolve_capturing_warnings("{{results.missing}}"), (None, 1))

    def test_missing_field_logs_warning_by_default(self):
        self.assertEqual(
            self._resolve_capturing_warnings("{{results.step_a.nonexistent}}", {"step_a": {"other": 1}}), (None, 1)
        )

    def test_missing_step_silent_with_log_missing_false(self):
        self.assertEqual(self._resolve_capturing_warnings("{{results.missing}}", log_missing=False), (None, 0))

    def test_missing_field_silent_with_log_missing_false(self):
        value = self._resolve_capturing_warnings(
            "{{results.step_a.nonexistent}}", {"step_a": {"other": 1}}, log_missing=False
        )
        self.assertEqual(value, (None, 0))

    def test_missing_input_silent_with_log_missing_false(self):
        with mock_workflow() as mock_wf:
            self.assertIsNone(resolve_argument("{{input.nope}}", {}, {}, log_missing=False))
            self.assertEqual(mock_wf.logger.warning.call_count, 0)

    def test_log_missing_propagates_through_dict_recursion(self):
        value = self._resolve_capturing_warnings({"inner": "{{results.missing}}"}, log_missing=False)
        self.assertEqual(value, ({"inner": None}, 0))

    def test_log_missing_propagates_through_list_recursion(self):
        value = self._resolve_capturing_warnings(
            ["{{results.missing}}", "{{results.also_missing}}"], log_missing=False
        )
        self.assertEqual(value, ([None, None], 0))

    def test_strict_overrides_log_missing(self):
        with mock_workflow(), self.assertRaisesRegex(ValueError, "step 'missing' not found"):
            resolve_argument("{{results.missing}}", {}, {}, strict=True, log_missing=False)

    def test_log_missing_true_preserves_strict(self):
        with mock_workflow(), self.assertRaisesRegex(ValueError, "step 'missing' not found"):
            resolve_argument("{{results.missing}}", {}, {}, strict=True)


# B3. evaluate_condition is silent on gate-style absence


class EvaluateConditionSilentOnMissingTest(unittest.TestCase):
    def _run(self, condition, results=None, input_data=None):
        with mock_workflow() as mock_wf:
            result = evaluate_condition(condition, results or {}, input_data or {})
            return result, mock_wf.logger.warning.call_count

    def test_missing_field_does_not_log(self):
        self.assertEqual(self._run("{{results.build.reason}} == None", {"build": {"payload": {"title": "x"}}}), (True, 0))

    def test_missing_step_does_not_log(self):
        result = self._run("{{results.send.handler_count}} != None", {"build": {"reason": "already_sent"}})
        self.assertEqual(result, (False, 0))

    def test_present_field_evaluates_correctly(self):
        self.assertEqual(self._run("{{results.send.handler_count}} != None", {"send": {"handler_count": 2}}), (True, 0))

    def test_actual_none_value_still_matches_none(self):
        self.assertEqual(self._run("{{results.step_a.field}} == None", {"step_a": {"field": None}}), (True, 0))


# C. evaluate_condition with placeholders (and P: nested paths)


class EvaluateConditionTest(unittest.TestCase):
    def _eval(self, condition, results=None, input_data=None):
        with mock_workflow():
            return evaluate_condition(condition, results or {}, input_data or {})

    def test_top_level_string_equality(self):
        self.assertIs(self._eval("{{results.step_a}} == 'approved'", {"step_a": "approved"}), True)

    def test_top_level_string_inequality(self):
        self.assertIs(self._eval("{{results.step_a}} == 'approved'", {"step_a": "rejected"}), False)

    def test_top_level_numeric(self):
        self.assertIs(self._eval("{{results.step_a}} > 100", {"step_a": 200}), True)

    def test_top_level_boolean(self):
        self.assertIs(self._eval("{{results.step_a}}", {"step_a": True}), True)

    def test_input_placeholder(self):
        self.assertIs(self._eval("{{input.threshold}} < 50", input_data={"threshold": 30}), True)

    def test_mixed_results_and_input(self):
        self.assertIs(self._eval("{{results.a}} == {{input.expected}}", {"a": 42}, {"expected": 42}), True)

    def test_unresolved_placeholder_returns_false(self):
        self.assertIs(self._eval("{{results.missing}} == 'x'", {}), False)

    def test_multiple_top_level_and(self):
        self.assertIs(self._eval("{{results.a}} > 0 and {{results.b}} > 0", {"a": 5, "b": 10}), True)

    def test_a_whole_dict_or_list_raises(self):
        for value in ({"status": "x"}, [1, 2]):
            with self.subTest(value=value), self.assertRaisesRegex(ConditionError, "name a field of it"):
                self._eval("{{results.step_a}} == 'x'", {"step_a": value})

    def test_an_expression_that_cant_be_evaluated_raises(self):
        for condition in ("{{results.a}} ~~ 1", "__import__('os') == None", "{{env.x}} == 1"):
            with self.subTest(condition=condition), self.assertRaises(ConditionError):
                self._eval(condition, {"a": 1})

    def test_none_result(self):
        self.assertIs(self._eval("{{results.step_a}} == None", {"step_a": None}), True)

    def test_literals(self):
        self.assertIs(self._eval("True"), True)
        self.assertIs(self._eval("False"), False)

    def test_nested_one_level(self):
        self.assertIs(self._eval("{{results.step_a.status}} == 'ready'", {"step_a": {"status": "ready"}}), True)

    def test_nested_two_levels(self):
        self.assertIs(self._eval("{{results.step_a.data.value}} == 42", {"step_a": {"data": {"value": 42}}}), True)

    def test_nested_multiple_placeholders(self):
        result = self._eval("{{results.a.x}} == 'y' and {{results.b.z}} > 0", {"a": {"x": "y"}, "b": {"z": 5}})
        self.assertIs(result, True)

    def test_nested_missing_path_returns_false(self):
        self.assertIs(self._eval("{{results.step_a.nonexistent}} == 'x'", {"step_a": {"status": "ok"}}), False)

    def test_nested_none_value(self):
        self.assertIs(self._eval("{{results.step_a.field}} == None", {"step_a": {"field": None}}), True)

    def test_input_nested_path(self):
        self.assertIs(self._eval("{{input.config.threshold}} < 50", {}, {"config": {"threshold": 30}}), True)


# D. activity step


class ActivityStepTest(unittest.IsolatedAsyncioTestCase):
    async def test_calls_execute_activity(self):
        step = {"activity": "my_activity", "args": ["arg1"], "timeout": 30}
        with mock_workflow() as mock_wf:
            mock_wf.execute_activity = AsyncMock(return_value={"result": "ok"})
            result = await activity_step(ProcessWorkflowBase(), step, ctx())
        self.assertEqual(result, {"result": "ok"})
        mock_wf.execute_activity.assert_called_once_with(
            "my_activity",
            "arg1",
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

    async def test_default_timeout_60s(self):
        with mock_workflow() as mock_wf:
            mock_wf.execute_activity = AsyncMock(return_value=None)
            await activity_step(ProcessWorkflowBase(), {"activity": "my_activity", "args": []}, ctx())
        self.assertEqual(mock_wf.execute_activity.call_args.kwargs["start_to_close_timeout"], timedelta(seconds=60))

    async def test_custom_timeout(self):
        with mock_workflow() as mock_wf:
            mock_wf.execute_activity = AsyncMock(return_value=None)
            await activity_step(ProcessWorkflowBase(), {"activity": "my_activity", "args": [], "timeout": 180}, ctx())
        self.assertEqual(mock_wf.execute_activity.call_args.kwargs["start_to_close_timeout"], timedelta(seconds=180))

    async def test_resolves_args(self):
        step = {"activity": "my_activity", "args": ["{{results.step_a}}"]}
        with mock_workflow() as mock_wf:
            mock_wf.execute_activity = AsyncMock(return_value=None)
            await activity_step(ProcessWorkflowBase(), step, ctx(results={"step_a": "resolved_value"}))
        self.assertEqual(mock_wf.execute_activity.call_args.args, ("my_activity", "resolved_value"))

    async def test_missing_activity_is_a_definition_error(self):
        with mock_workflow(), self.assertRaisesRegex(ApplicationError, "missing 'activity' key") as caught:
            await activity_step(ProcessWorkflowBase(), {"args": []}, ctx())
        self.assertEqual((caught.exception.type, caught.exception.non_retryable), (PROCESS_DEFINITION_ERROR, True))

    async def test_args_one_element_a_dict_whole_a_value_or_none(self):
        for args, expected in (
            (["{{input.x}}"], ("a", 1)),
            ({"k": "{{input.x}}"}, ("a", {"k": 1})),
            ("{{input.x}}", ("a", 1)),
            ([], ("a",)),
            (None, ("a",)),
        ):
            with self.subTest(args=args):
                step = {"activity": "a"} if args is None else {"activity": "a", "args": args}
                with mock_workflow() as mock_wf:
                    mock_wf.execute_activity = AsyncMock(return_value=None)
                    await activity_step(ProcessWorkflowBase(), step, ctx(input_data={"x": 1}))
                self.assertEqual(mock_wf.execute_activity.call_args.args, expected)

    async def test_two_args_or_an_unknown_option_is_a_definition_error(self):
        for step in ({"activity": "a", "args": [1, 2]}, {"activity": "a", "options": {"nope": 1}}):
            with self.subTest(step=step), mock_workflow() as mock_wf:
                mock_wf.execute_activity = AsyncMock(return_value=None)
                with self.assertRaises(ApplicationError) as caught:
                    await activity_step(ProcessWorkflowBase(), step, ctx())
                self.assertEqual(caught.exception.type, PROCESS_DEFINITION_ERROR)
                mock_wf.execute_activity.assert_not_called()

    async def test_options_are_execute_activity_options_timeouts_in_seconds(self):
        step = {
            "activity": "a",
            "args": [1],
            "options": {"summary": "for {{input.x}}", "activity_id": "fixed", "schedule_to_close_timeout": 90},
        }
        with mock_workflow() as mock_wf:
            mock_wf.execute_activity = AsyncMock(return_value=None)
            await activity_step(ProcessWorkflowBase(), step, ctx(input_data={"x": "q"}))
        kwargs = mock_wf.execute_activity.call_args.kwargs
        self.assertEqual(
            (kwargs["summary"], kwargs["activity_id"], kwargs["schedule_to_close_timeout"]),
            ("for q", "fixed", timedelta(seconds=90)),
        )


# E. workflow step


class WorkflowStepTest(unittest.IsolatedAsyncioTestCase):
    @contextmanager
    def child_mock(self, task_queue="default-queue"):
        with mock_workflow() as mock_wf:
            info = MagicMock()
            info.task_queue = task_queue
            mock_wf.info.return_value = info
            mock_wf.uuid4.return_value = "test-uuid"
            yield mock_wf

    async def test_wait_for_result_true(self):
        step = {"workflow_name": "ChildWorkflow", "args": {"key": "val"}, "wait_for_result": True}
        with self.child_mock() as mock_wf:
            mock_wf.execute_child_workflow = AsyncMock(return_value={"done": True})
            result = await workflow_step(ProcessWorkflowBase(), step, ctx())
        self.assertEqual(result, {"done": True})
        mock_wf.execute_child_workflow.assert_called_once()

    async def test_wait_for_result_false(self):
        step = {"workflow_name": "ChildWorkflow", "args": {}, "wait_for_result": False}
        with self.child_mock() as mock_wf:
            child_handle = MagicMock()
            child_handle.id = "child-123"
            mock_wf.start_child_workflow = AsyncMock(return_value=child_handle)
            result = await workflow_step(ProcessWorkflowBase(), step, ctx())
        self.assertEqual(result, {"child_workflow_id": "child-123"})
        mock_wf.start_child_workflow.assert_called_once()

    async def test_default_wait_true(self):
        with self.child_mock() as mock_wf:
            mock_wf.execute_child_workflow = AsyncMock(return_value="ok")
            await workflow_step(ProcessWorkflowBase(), {"workflow_name": "ChildWorkflow", "args": {}}, ctx())
        mock_wf.execute_child_workflow.assert_called_once()

    async def test_custom_task_queue(self):
        step = {"workflow_name": "ChildWorkflow", "args": {}, "task_queue": "risk-queue"}
        with self.child_mock() as mock_wf:
            mock_wf.execute_child_workflow = AsyncMock(return_value=None)
            await workflow_step(ProcessWorkflowBase(), step, ctx())
        self.assertEqual(mock_wf.execute_child_workflow.call_args.kwargs["task_queue"], "risk-queue")

    async def test_resolves_args(self):
        step = {"workflow_name": "ChildWorkflow", "args": {"data": "{{results.x}}"}}
        with self.child_mock() as mock_wf:
            mock_wf.execute_child_workflow = AsyncMock(return_value=None)
            await workflow_step(ProcessWorkflowBase(), step, ctx(results={"x": "resolved"}))
        self.assertEqual(mock_wf.execute_child_workflow.call_args.args[1], {"data": "resolved"})


# F / R. pause steps and resume_step


class PauseTest(unittest.IsolatedAsyncioTestCase):
    async def test_pause_uses_wait_condition(self):
        wf = ProcessWorkflowBase()
        with mock_workflow() as mock_wf:
            wf._pause_results["step_1"] = {"approved": True}  # resumed before the wait
            mock_wf.wait_condition = AsyncMock(return_value=None)
            result = await wf.wait_for_resume({}, "step_1")
            mock_wf.wait_condition.assert_called_once()
        self.assertEqual(result, {"approved": True})

    async def test_resume_step_stores_in_pause_results(self):
        wf = ProcessWorkflowBase()
        with mock_workflow() as mock_wf:
            mock_wf.wait_condition = AsyncMock(return_value=None)
            status = await wf.resume_step("step_1", {"action": "approve"})
        self.assertEqual(wf._pause_results["step_1"], {"action": "approve"})
        self.assertEqual(status["step_id"], "step_1")

    async def test_pause_returns_resume_data_and_consumes_it(self):
        wf = ProcessWorkflowBase()
        with mock_workflow() as mock_wf:
            wf._pause_results["step_1"] = {"data": "from_signal"}
            mock_wf.wait_condition = AsyncMock(return_value=None)
            result = await wf.wait_for_resume({}, "step_1")
        self.assertEqual(result, {"data": "from_signal"})
        self.assertNotIn("step_1", wf._pause_results)

    async def test_timeout_expires(self):
        wf = ProcessWorkflowBase()
        with mock_workflow() as mock_wf:
            mock_wf.wait_condition = AsyncMock(side_effect=asyncio.TimeoutError())
            result = await wf.wait_for_resume({"timeout": 2}, "step_1")
        self.assertEqual(result, {"action": "expired", "timeout_seconds": 2})

    async def test_timeout_resumed_before_expiry(self):
        wf = ProcessWorkflowBase()
        with mock_workflow() as mock_wf:
            wf._pause_results["step_1"] = {"approved": True}
            mock_wf.wait_condition = AsyncMock(return_value=None)
            result = await wf.wait_for_resume({"timeout": 60}, "step_1")
        self.assertEqual(result, {"approved": True})

    async def test_no_timeout_waits_indefinitely(self):
        for step in ({}, {"timeout": 0}):  # timeout 0 is falsy: no timeout
            with self.subTest(step=step):
                wf = ProcessWorkflowBase()
                with mock_workflow() as mock_wf:
                    wf._pause_results["step_1"] = {"data": "ok"}
                    mock_wf.wait_condition = AsyncMock(return_value=None)
                    await wf.wait_for_resume(step, "step_1")
                self.assertNotIn("timeout", mock_wf.wait_condition.call_args.kwargs)


# G / H. dispatch: the step's type runs it, its result is stored once under its id


def run_step(wf, step, input_data=None):
    return wf._execute_step({"id": "base", **step}, input_data or {})


class DispatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_by_type(self):
        for step_type in ("activity", "workflow", "pause", "approval", "for_each"):
            with self.subTest(step_type=step_type):
                run = AsyncMock(return_value=f"{step_type}_result")
                wf = with_steps(**{step_type: run})
                with mock_workflow():
                    result = await run_step(wf, {"type": step_type})
                self.assertEqual(result, f"{step_type}_result")
                run.assert_called_once()
                self.assertEqual(wf._workflow_results, {"base": f"{step_type}_result"})

    async def test_plugged_in_type_gets_wf_step_and_context(self):
        run = AsyncMock(return_value="decision_result")
        wf = with_steps(decision=run)
        with mock_workflow():
            result = await run_step(wf, {"type": "decision"}, {"k": 1})
        self.assertEqual(result, "decision_result")
        (called_wf, called_step, called_ctx), _ = run.call_args
        self.assertIs(called_wf, wf)
        self.assertEqual(called_step, {"id": "base", "type": "decision"})
        self.assertEqual((called_ctx.base_id, called_ctx.input), ("base", {"k": 1}))
        self.assertIs(called_ctx.results, wf._workflow_results)
        self.assertIsNone(called_ctx.item)

    async def test_unknown_type_is_a_definition_error(self):
        wf = ProcessWorkflowBase()
        with mock_workflow(), self.assertRaises(ApplicationError) as caught:
            await run_step(wf, {"type": "bogus", "on_error": "continue"})
        self.assertEqual(caught.exception.type, PROCESS_DEFINITION_ERROR)
        self.assertEqual(wf._workflow_results, {})

    async def test_result_stored_once_under_the_step_id(self):
        # One copy per step: a second under the unique id doubled every process result.
        wf = with_steps(activity=AsyncMock(side_effect=["first", "second"]))
        with mock_workflow():
            await run_step(wf, {"type": "activity"})
            self.assertEqual(wf._workflow_results, {"base": "first"})
            await run_step(wf, {"type": "activity"})
        self.assertEqual(wf._workflow_results, {"base": "second"})


# on_error: continue


class OnErrorContinueTest(unittest.IsolatedAsyncioTestCase):
    async def test_continue_stores_none_and_proceeds(self):
        for error in (RuntimeError("boom"), ApplicationError("boom", type="X")):
            with self.subTest(error=error):
                wf = with_steps(activity=AsyncMock(side_effect=error))
                with mock_workflow():
                    result = await run_step(wf, {"type": "activity", "on_error": "continue"})
                self.assertIsNone(result)
                self.assertEqual(wf._workflow_results, {"base": None})

    async def test_a_plain_exception_fails_the_run_not_the_task(self):
        wf = with_steps(activity=AsyncMock(side_effect=RuntimeError("boom")))
        with mock_workflow(), self.assertRaisesRegex(ApplicationError, "RuntimeError: boom") as caught:
            await run_step(wf, {"type": "activity"})
        self.assertEqual((caught.exception.type, caught.exception.non_retryable), ("ProcessStepError", True))

    async def test_a_temporal_failure_propagates_as_is(self):
        error = ApplicationError("boom", type="Mine")
        wf = with_steps(activity=AsyncMock(side_effect=error))
        with mock_workflow(), self.assertRaises(ApplicationError) as caught:
            await run_step(wf, {"type": "activity"})
        self.assertIs(caught.exception, error)

    async def test_a_definition_error_is_never_continued(self):
        from factory_runtime.process import definition_error

        wf = with_steps(activity=AsyncMock(side_effect=definition_error("bad step")))
        with mock_workflow(), self.assertRaisesRegex(ApplicationError, "bad step"):
            await run_step(wf, {"type": "activity", "on_error": "continue"})


# I. unique ids: one uuid4 per step, drawn only when a step reads it


class UniqueIdTest(unittest.TestCase):
    def test_drawn_once_on_first_use(self):
        with mock_workflow() as mock_wf:
            mock_wf.uuid4.side_effect = ["uuid-1", "uuid-2"]
            context = ctx(base_id="my_step")
            self.assertEqual(mock_wf.uuid4.call_count, 0)
            self.assertEqual((context.unique_id, context.unique_id), ("my_step_uuid-1", "my_step_uuid-1"))
        self.assertEqual(mock_wf.uuid4.call_count, 1)

    def test_iterations_share_the_steps_uuid(self):
        with mock_workflow() as mock_wf:
            mock_wf.uuid4.side_effect = ["uuid-1", "uuid-2"]
            context = ctx(base_id="fan")
            ids = [context.iteration(i, item).unique_id for i, item in enumerate("ab")]
        self.assertEqual(ids, ["fan_uuid-1[0]", "fan_uuid-1[1]"])


if __name__ == "__main__":
    unittest.main()
