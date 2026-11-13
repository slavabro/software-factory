"""The process engine against a real (time-skipping) test server: the four interpreters as a product declares them
(tests/process_workflows.py), step policies, for_each, the query and the resume_step update and signal, a result
over the payload limit, and the plug-in step contract (per-run state fetched once, unique ids inside for_each,
REQUEST_CANCEL children). Ported from FamilyOffice's engine tests."""

import asyncio
import json
import re
import unittest
from datetime import timedelta
from typing import Any
from uuid import uuid4

from temporalio import activity
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError

from factory_runtime.testing import make_worker, time_skipping_env
from tests.process_server_workflows import CancellableRecordingChild, MockChildWorkflow, MockSlowChildWorkflow
from tests.process_workflows import (
    DEFAULTS_ACTIVITY,
    PROCESS_WORKFLOWS,
    ConditionalProcessWorkflow,
    HybridProcessWorkflow,
    LinearProcessWorkflow,
    ParallelProcessWorkflow,
    RecordingChild,
)

SERVER_PAYLOAD_LIMIT_BYTES = 2 * 1024 * 1024
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


def named_value_activity(name: str, result: Any) -> Any:
    @activity.defn(name=name)
    async def value(*args: Any) -> Any:
        return result

    return value


def step_a(result: Any = "step_a_result") -> Any:
    return named_value_activity("step_a_activity", result)


def step_b(result: Any = "step_b_result") -> Any:
    return named_value_activity("step_b_activity", result)


def step_c(result: Any = "step_c_result") -> Any:
    return named_value_activity("step_c_activity", result)


fast_step_a = named_value_activity("fast_step_a", "step_a_result")
fast_step_b = named_value_activity("fast_step_b", "step_b_result")
fast_activity = named_value_activity("fast_activity", "fast")


def produce_items(items: Any) -> Any:
    return named_value_activity("produce_items_activity", {"items": items})


def capture(name: str, sink: list, result: Any = None) -> Any:
    @activity.defn(name=name)
    async def record(params: Any) -> Any:
        sink.append(params)
        return result if result is not None else {"processed": True, "input": params}

    return record


def failing_on(fail_on_values: set) -> Any:
    @activity.defn(name="for_each_test_activity")
    async def maybe_fail(params: dict) -> dict:
        if isinstance(params, dict) and params.get("i") in fail_on_values:
            raise ApplicationError(f"Simulated failure on item i={params['i']}", non_retryable=True)
        return {"processed": True, "input": params}

    return maybe_fail


def always_fails(attempts: list, error_type: str | None = None) -> Any:
    @activity.defn(name="always_fails_activity")
    async def fail() -> dict:
        attempts.append(activity.info().attempt)
        raise ApplicationError("boom", type=error_type)

    return fail


@activity.defn(name="report_heartbeat_timeout_activity")
async def report_heartbeat_timeout_activity() -> float | None:
    hb = activity.info().heartbeat_timeout
    return hb.total_seconds() if hb else None


@activity.defn(name="large_plan_activity")
async def large_plan_activity(*args: Any) -> dict:
    """About 1.5 MB of rows."""
    return {
        "orders": [
            {"symbol": f"S{i}", "side": "sell", "quantity": "29.0", "rationale": "over its target weight by a margin"}
            for i in range(15000)
        ]
    }


def defaults_activity(calls: list, **overrides: Any) -> Any:
    @activity.defn(name=DEFAULTS_ACTIVITY)
    async def defaults() -> dict:
        calls.append(1)
        return {"label": "default label", **overrides}

    return defaults


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


def activity_step(step_id: str, activity_name: str, **extra: Any) -> dict:
    return {"id": step_id, "type": "activity", "activity": activity_name, "args": [], "timeout": 10, **extra}


def for_each_process(
    items_source: str = "{{results.produce_items.items}}",
    inner_step_type: str = "activity",
    on_error: str | None = None,
    inner_step_overrides: dict | None = None,
    include_downstream: bool = False,
) -> dict:
    """produce_items -> for_each `fan_out` over its items -> (optionally) a step consuming {{results.fan_out}}."""
    inner_step: dict[str, Any] = {"type": inner_step_type}
    if inner_step_type == "activity":
        inner_step.update({"activity": "for_each_test_activity", "args": ["{{item}}"], "timeout": 10})
    elif inner_step_type == "workflow":
        inner_step.update({"workflow_name": "MockChildWorkflow", "args": "{{item}}", "wait_for_result": True})
    if inner_step_overrides:
        inner_step.update(inner_step_overrides)
    fan_out: dict[str, Any] = {"id": "fan_out", "type": "for_each", "items": items_source, "step": inner_step}
    if on_error:
        fan_out["on_error"] = on_error
    steps = [activity_step("produce_items", "produce_items_activity"), fan_out]
    if include_downstream:
        steps.append(activity_step("consume_results", "consume_results_activity", args=["{{results.fan_out}}"]))
    return {"steps": steps}


async def wait_until(predicate: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.1)
    raise TimeoutError("condition not met in time")


class ProcessServerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.env = await time_skipping_env()
        self.addAsyncCleanup(self.env.shutdown)
        self.client = self.env.client
        self.queue = f"process-tests-{uuid4()}"

    def worker(self, activities: list, workflows: list | None = None) -> Any:
        return make_worker(
            self.client,
            task_queue=self.queue,
            workflows=workflows if workflows is not None else PROCESS_WORKFLOWS,
            activities=activities,
        )

    async def run_process(self, run: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.client.execute_workflow(
            run, args=list(args), id=f"test-{uuid4()}", task_queue=self.queue, **kwargs
        )

    async def start_process(self, run: Any, *args: Any) -> Any:
        return await self.client.start_workflow(run, args=list(args), id=f"test-{uuid4()}", task_queue=self.queue)

    async def wait_for_step(self, handle: Any, step_name: str, timeout: float = 10.0) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                result = await handle.query(ConditionalProcessWorkflow.get_step_results)
                if step_name in result.get("completed_steps", []):
                    return result
            except Exception:
                pass
            await asyncio.sleep(0.2)
        raise TimeoutError(f"Step '{step_name}' did not complete within {timeout}s")


# ===========================================================================
# Linear
# ===========================================================================


class LinearTest(ProcessServerCase):
    async def test_linear_two_activity_steps(self):
        process_def = {"steps": [activity_step("step_a", "step_a_activity"), activity_step("step_b", "step_b_activity")]}
        async with self.worker([step_a(), step_b()]):
            result = await self.run_process(LinearProcessWorkflow.run, process_def)
        self.assertIn("step_a", result)
        self.assertIn("step_b", result)
        self.assertEqual(result["step_a"], "step_a_result")
        self.assertEqual(result["step_b"], "step_b_result")

    async def test_linear_with_metadata(self):
        process_def = {"steps": [activity_step("step_a", "step_a_activity")]}
        async with self.worker([step_a()]):
            result = await self.run_process(LinearProcessWorkflow.run, process_def, {}, {"department": "test"})
        self.assertIn("step_a", result)

    async def test_linear_empty_steps(self):
        async with self.worker([]):
            result = await self.run_process(LinearProcessWorkflow.run, {"steps": []})
        self.assertEqual(result, {})

    async def test_linear_none_process_def_raises(self):
        # ValueError fails the workflow task (retried), so the run ends by its execution timeout.
        async with self.worker([]):
            with self.assertRaises(WorkflowFailureError):
                await self.run_process(LinearProcessWorkflow.run, None, execution_timeout=timedelta(seconds=5))

    async def test_linear_step_references_previous(self):
        @activity.defn(name="producer_activity")
        async def producer(*args: Any) -> dict:
            return {"value": 42}

        @activity.defn(name="consumer_activity")
        async def consumer(data: Any, *args: Any) -> dict:
            return {"received_value": data.get("value") if isinstance(data, dict) else data}

        process_def = {
            "steps": [
                activity_step("produce", "producer_activity"),
                activity_step("consume", "consumer_activity", args=["{{results.produce}}"]),
            ]
        }
        async with self.worker([producer, consumer]):
            result = await self.run_process(LinearProcessWorkflow.run, process_def)
        self.assertEqual(result["produce"], {"value": 42})
        self.assertEqual(result["consume"]["received_value"], 42)


# ===========================================================================
# Conditional
# ===========================================================================


class ConditionalTest(ProcessServerCase):
    async def run_conditional(self, steps: list, activities: list) -> dict:
        async with self.worker(activities):
            return await self.run_process(ConditionalProcessWorkflow.run, {"steps": steps})

    async def test_conditional_no_condition_always_executes(self):
        result = await self.run_conditional([activity_step("step_a", "step_a_activity")], [step_a()])
        self.assertIn("step_a", result)

    async def test_conditional_true_condition_executes(self):
        result = await self.run_conditional(
            [activity_step("step_a", "step_a_activity", condition="True")], [step_a()]
        )
        self.assertIn("step_a", result)

    async def test_conditional_false_condition_skips(self):
        result = await self.run_conditional(
            [activity_step("step_a", "step_a_activity", condition="False")], [step_a()]
        )
        self.assertNotIn("step_a", result)

    async def test_conditional_result_based_condition(self):
        result = await self.run_conditional(
            [
                activity_step("step_a", "step_a_activity"),
                activity_step("step_b", "step_b_activity", condition="{{results.step_a}} == 'go'"),
            ],
            [step_a("go"), step_b()],
        )
        self.assertIn("step_a", result)
        self.assertIn("step_b", result)

    async def test_conditional_skipped_step_not_in_results(self):
        result = await self.run_conditional(
            [
                activity_step("step_a", "step_a_activity"),
                activity_step("step_b", "step_b_activity", condition="{{results.step_a}} == 'nope'"),
            ],
            [step_a("go"), step_b()],
        )
        self.assertIn("step_a", result)
        self.assertNotIn("step_b", result)

    async def test_conditional_multiple_steps_mixed(self):
        result = await self.run_conditional(
            [
                activity_step("step_a", "step_a_activity"),
                activity_step("step_b", "step_b_activity", condition="True"),
                activity_step("step_c", "step_c_activity", condition="False"),
            ],
            [step_a(), step_b(), step_c()],
        )
        self.assertIn("step_a", result)
        self.assertIn("step_b", result)
        self.assertNotIn("step_c", result)


# ===========================================================================
# Hybrid (runs exactly like Conditional)
# ===========================================================================


class HybridTest(ProcessServerCase):
    async def test_hybrid_condition_support(self):
        process_def = {
            "steps": [
                activity_step("step_a", "step_a_activity"),
                activity_step("step_b", "step_b_activity", condition="{{results.step_a}} == 'step_a_result'"),
            ]
        }
        async with self.worker([step_a(), step_b()]):
            result = await self.run_process(HybridProcessWorkflow.run, process_def)
        self.assertIn("step_a", result)
        self.assertIn("step_b", result)

    async def test_hybrid_all_steps_no_conditions(self):
        process_def = {"steps": [activity_step("step_a", "step_a_activity"), activity_step("step_b", "step_b_activity")]}
        async with self.worker([step_a(), step_b()]):
            result = await self.run_process(HybridProcessWorkflow.run, process_def)
        self.assertIn("step_a", result)
        self.assertIn("step_b", result)

    async def test_hybrid_skips_false_condition(self):
        process_def = {"steps": [activity_step("step_a", "step_a_activity", condition="False")]}
        async with self.worker([step_a()]):
            result = await self.run_process(HybridProcessWorkflow.run, process_def)
        self.assertNotIn("step_a", result)


# ===========================================================================
# Parallel
# ===========================================================================


class ParallelTest(ProcessServerCase):
    async def test_parallel_two_activities(self):
        process_def = {"steps": [activity_step("step_a", "step_a_activity"), activity_step("step_b", "step_b_activity")]}
        async with self.worker([step_a(), step_b()]):
            result = await self.run_process(ParallelProcessWorkflow.run, process_def)
        self.assertIn("step_a", result)
        self.assertIn("step_b", result)

    async def test_parallel_empty_steps(self):
        async with self.worker([]):
            result = await self.run_process(ParallelProcessWorkflow.run, {"steps": []})
        self.assertEqual(result, {})

    async def test_parallel_results_contain_all_step_ids(self):
        process_def = {
            "steps": [
                activity_step("step_a", "step_a_activity"),
                activity_step("step_b", "step_b_activity"),
                activity_step("step_c", "step_c_activity"),
            ]
        }
        async with self.worker([step_a(), step_b(), step_c()]):
            result = await self.run_process(ParallelProcessWorkflow.run, process_def)
        for base_id in ["step_a", "step_b", "step_c"]:
            self.assertIn(base_id, result)


# ===========================================================================
# get_step_results query, all_handlers_finished, metadata parity
# ===========================================================================


class QueryAndHandlersTest(ProcessServerCase):
    async def test_query_after_completion(self):
        process_def = {"steps": [activity_step("step_a", "fast_step_a"), activity_step("step_b", "fast_step_b")]}
        async with self.worker([fast_step_a, fast_step_b]):
            handle = await self.start_process(ConditionalProcessWorkflow.run, process_def)
            await handle.result()
            query_result = await handle.query(ConditionalProcessWorkflow.get_step_results)
        self.assertIn("step_a", query_result["completed_steps"])
        self.assertIn("step_b", query_result["completed_steps"])
        self.assertIn("step_a", query_result["results"])
        self.assertIn("step_b", query_result["results"])

    async def test_query_empty_before_steps(self):
        async with self.worker([]):
            handle = await self.start_process(ConditionalProcessWorkflow.run, {"steps": []})
            await handle.result()
            query_result = await handle.query(ConditionalProcessWorkflow.get_step_results)
        self.assertEqual(query_result["completed_steps"], [])
        self.assertEqual(query_result["results"], {})

    async def test_query_returns_results_mid_workflow(self):
        process_def = {"steps": [activity_step("step_a", "fast_step_a"), {"id": "wait_here", "type": "pause"}]}
        async with self.worker([fast_step_a]):
            handle = await self.start_process(ConditionalProcessWorkflow.run, process_def)
            query_result = await self.wait_for_step(handle, "step_a")
            self.assertIn("step_a", query_result["completed_steps"])
            self.assertEqual(query_result["results"]["step_a"], "step_a_result")
            await handle.execute_update(ConditionalProcessWorkflow.resume_step, args=["wait_here", {"resumed": True}])
            await handle.result()

    async def test_conditional_no_pending_handlers(self):
        async with self.worker([fast_step_a]):
            result = await self.run_process(
                ConditionalProcessWorkflow.run, {"steps": [activity_step("step_a", "fast_step_a")]}
            )
        self.assertIn("step_a", result)

    async def test_conditional_waits_for_handlers(self):
        process_def = {"steps": [activity_step("step_a", "fast_step_a"), {"id": "wait_step", "type": "pause"}]}
        async with self.worker([fast_step_a]):
            handle = await self.start_process(ConditionalProcessWorkflow.run, process_def)
            await self.wait_for_step(handle, "step_a")
            for _ in range(50):  # the update is rejected until the pause step waits
                try:
                    await handle.execute_update(
                        ConditionalProcessWorkflow.resume_step, args=["wait_step", {"action": "go"}]
                    )
                    break
                except WorkflowUpdateFailedError:
                    await asyncio.sleep(0.1)
            result = await handle.result()
        self.assertIn("step_a", result)
        self.assertIn("wait_step", result)

    async def test_every_interpreter_accepts_metadata(self):
        process_def = {"steps": [activity_step("step_a", "fast_step_a")]}
        runs = [
            (ConditionalProcessWorkflow.run, "investment"),
            (HybridProcessWorkflow.run, "risk"),
            (ParallelProcessWorkflow.run, "data"),
        ]
        async with self.worker([fast_step_a]):
            for run, department in runs:
                with self.subTest(run=run):
                    result = await self.run_process(run, process_def, {}, {"department": department})
                    self.assertIn("step_a", result)


# ===========================================================================
# for_each
# ===========================================================================


class ForEachTest(ProcessServerCase):
    async def test_activity_items_produce_results_list(self):
        items = [{"name": "alpha"}, {"name": "beta"}, {"name": "gamma"}]
        captured: list = []
        async with self.worker([produce_items(items), capture("for_each_test_activity", captured)]):
            result = await self.run_process(LinearProcessWorkflow.run, for_each_process())
        self.assertEqual(captured, [{"name": "alpha"}, {"name": "beta"}, {"name": "gamma"}])
        fan_out = result["fan_out"]
        self.assertEqual(len(fan_out), 3)
        self.assertEqual(fan_out[0]["input"], {"name": "alpha"})

    async def test_workflow_children_started_per_item(self):
        async with self.worker([produce_items([{"id": 1}, {"id": 2}])], PROCESS_WORKFLOWS + [MockChildWorkflow]):
            result = await self.run_process(LinearProcessWorkflow.run, for_each_process(inner_step_type="workflow"))
        fan_out = result["fan_out"]
        self.assertEqual(len(fan_out), 2)
        self.assertEqual(fan_out[0]["child_received"], {"id": 1})
        self.assertEqual(fan_out[1]["child_received"], {"id": 2})

    async def test_fire_and_forget_children_parent_completes_immediately(self):
        process_def = for_each_process(
            inner_step_type="workflow",
            inner_step_overrides={
                "workflow_name": "MockSlowChildWorkflow",
                "wait_for_result": False,
                "parent_close_policy": "abandon",
            },
        )
        async with self.worker([produce_items([{"id": "a"}, {"id": "b"}])], PROCESS_WORKFLOWS + [MockSlowChildWorkflow]):
            result = await self.run_process(LinearProcessWorkflow.run, process_def)
        fan_out = result["fan_out"]
        self.assertEqual(len(fan_out), 2)
        self.assertIn("child_workflow_id", fan_out[0])
        self.assertIn("child_workflow_id", fan_out[1])

    async def test_nested_item_placeholder_resolves(self):
        items = [
            {"name": "task1", "metadata": {"priority": "high"}},
            {"name": "task2", "metadata": {"priority": "low"}},
        ]
        captured: list = []
        process_def = for_each_process(
            inner_step_overrides={"args": [{"task_name": "{{item.name}}", "priority": "{{item.metadata.priority}}"}]}
        )
        async with self.worker([produce_items(items), capture("for_each_test_activity", captured)]):
            await self.run_process(LinearProcessWorkflow.run, process_def)
        self.assertEqual(
            captured, [{"task_name": "task1", "priority": "high"}, {"task_name": "task2", "priority": "low"}]
        )

    async def test_result_stored_and_accessible_by_downstream_step(self):
        captured: list = []
        downstream: list = []
        async with self.worker(
            [
                produce_items([{"v": 1}, {"v": 2}]),
                capture("for_each_test_activity", captured),
                capture("consume_results_activity", downstream, {"consumed": True}),
            ]
        ):
            await self.run_process(LinearProcessWorkflow.run, for_each_process(include_downstream=True))
        self.assertEqual(len(downstream), 1)
        self.assertIsInstance(downstream[0], list)
        self.assertEqual(len(downstream[0]), 2)

    async def test_empty_list_produces_empty_result_no_iterations(self):
        captured: list = []
        async with self.worker([produce_items([]), capture("for_each_test_activity", captured)]):
            result = await self.run_process(LinearProcessWorkflow.run, for_each_process())
        self.assertEqual(result["fan_out"], [])
        self.assertEqual(len(captured), 0)

    async def test_none_items_produces_empty_result(self):
        captured: list = []
        process_def = for_each_process(items_source="{{results.produce_items.nonexistent_key}}")
        async with self.worker([produce_items([{"a": 1}]), capture("for_each_test_activity", captured)]):
            result = await self.run_process(LinearProcessWorkflow.run, process_def)
        self.assertEqual(result["fan_out"], [])
        self.assertEqual(len(captured), 0)

    async def test_single_non_list_item_wrapped_as_list(self):
        captured: list = []
        async with self.worker([produce_items({"single": True}), capture("for_each_test_activity", captured)]):
            result = await self.run_process(LinearProcessWorkflow.run, for_each_process())
        self.assertEqual(len(result["fan_out"]), 1)
        self.assertEqual(captured, [{"single": True}])

    async def test_on_error_continue_skips_failed_items(self):
        items = [{"i": 0}, {"i": 1}, {"i": 2}]
        async with self.worker([produce_items(items), failing_on({1})]):
            result = await self.run_process(LinearProcessWorkflow.run, for_each_process(on_error="continue"))
        fan_out = result["fan_out"]
        self.assertEqual(len(fan_out), 3)
        self.assertIs(fan_out[0]["processed"], True)
        self.assertEqual(fan_out[0]["input"], {"i": 0})
        self.assertIsNone(fan_out[1])
        self.assertIs(fan_out[2]["processed"], True)
        self.assertEqual(fan_out[2]["input"], {"i": 2})

    async def test_on_error_default_raises_on_first_failure(self):
        items = [{"i": 0}, {"i": 1}, {"i": 2}]
        async with self.worker([produce_items(items), failing_on({0})]):
            with self.assertRaises(WorkflowFailureError):
                await self.run_process(LinearProcessWorkflow.run, for_each_process())

    async def test_inner_step_on_error_ignored_with_warning(self):
        captured: list = []
        process_def = for_each_process(inner_step_overrides={"on_error": "continue"})
        async with self.worker([produce_items([{"x": 1}, {"x": 2}]), capture("for_each_test_activity", captured)]):
            result = await self.run_process(LinearProcessWorkflow.run, process_def)
        self.assertEqual(len(captured), 2)
        self.assertEqual(len(result["fan_out"]), 2)

    async def test_abandon_policy_children_survive_parent(self):
        process_def = for_each_process(
            inner_step_type="workflow",
            inner_step_overrides={
                "workflow_name": "MockSlowChildWorkflow",
                "wait_for_result": False,
                "parent_close_policy": "abandon",
            },
        )
        async with self.worker([produce_items([{"id": "c1"}, {"id": "c2"}])], PROCESS_WORKFLOWS + [MockSlowChildWorkflow]):
            result = await self.run_process(LinearProcessWorkflow.run, process_def)
        fan_out = result["fan_out"]
        self.assertEqual(len(fan_out), 2)
        self.assertIn("child_workflow_id", fan_out[0])
        self.assertIn("child_workflow_id", fan_out[1])
        self.assertNotEqual(fan_out[0]["child_workflow_id"], fan_out[1]["child_workflow_id"])

    async def test_unknown_policy_uses_default_terminate(self):
        process_def = for_each_process(
            inner_step_type="workflow", inner_step_overrides={"parent_close_policy": "bogus_value"}
        )
        async with self.worker([produce_items([{"id": "x"}])], PROCESS_WORKFLOWS + [MockChildWorkflow]):
            result = await self.run_process(LinearProcessWorkflow.run, process_def)
        self.assertEqual(len(result["fan_out"]), 1)

    async def test_fan_out_starts_a_child_per_mapped_item(self):
        """produce mapped items -> for_each fire-and-forget children (abandon), on_error: continue."""
        inputs = [{"subject": "standup", "action": "review"}, {"subject": "standup", "action": "check"}]
        process_def = {
            "steps": [
                activity_step("build_inputs", "produce_items_activity"),
                {
                    "id": "start_children",
                    "type": "for_each",
                    "items": "{{results.build_inputs.items}}",
                    "step": {
                        "type": "workflow",
                        "workflow_name": "MockChildWorkflow",
                        "args": "{{item}}",
                        "wait_for_result": False,
                        "parent_close_policy": "abandon",
                    },
                    "on_error": "continue",
                },
            ]
        }
        async with self.worker([produce_items(inputs)], PROCESS_WORKFLOWS + [MockChildWorkflow]):
            result = await self.run_process(LinearProcessWorkflow.run, process_def)
        fan_out = result["start_children"]
        self.assertEqual(len(fan_out), 2)
        self.assertIn("child_workflow_id", fan_out[0])
        self.assertIn("child_workflow_id", fan_out[1])


# ===========================================================================
# Step policies: retry, heartbeat
# ===========================================================================


class StepPolicyTest(ProcessServerCase):
    async def test_retry_max_attempts_honoured(self):
        attempts: list[int] = []
        process_def = {
            "steps": [
                activity_step(
                    "flaky",
                    "always_fails_activity",
                    timeout=5,
                    retry={"max_attempts": 2, "initial_interval_seconds": 0.01},
                    on_error="continue",
                ),
                activity_step("after", "fast_activity", timeout=5),
            ]
        }
        async with self.worker([always_fails(attempts), fast_activity]):
            result = await self.run_process(LinearProcessWorkflow.run, process_def)
        self.assertEqual(attempts, [1, 2])
        self.assertIsNone(result["flaky"])
        self.assertEqual(result["after"], "fast")

    async def test_default_is_three_attempts(self):
        attempts: list[int] = []
        process_def = {"steps": [activity_step("flaky", "always_fails_activity", timeout=5, on_error="continue")]}
        async with self.worker([always_fails(attempts)]):
            result = await self.run_process(LinearProcessWorkflow.run, process_def)
        self.assertEqual(attempts, [1, 2, 3])
        self.assertIsNone(result["flaky"])

    async def test_exhausted_retries_fail_workflow_without_on_error(self):
        attempts: list[int] = []
        process_def = {
            "steps": [
                activity_step(
                    "flaky", "always_fails_activity", timeout=5, retry={"max_attempts": 2, "initial_interval_seconds": 0.01}
                )
            ]
        }
        async with self.worker([always_fails(attempts)]):
            with self.assertRaises(WorkflowFailureError):
                await self.run_process(LinearProcessWorkflow.run, process_def)
        self.assertEqual(attempts, [1, 2])

    async def test_non_retryable_error_types_short_circuit(self):
        attempts: list[int] = []
        process_def = {
            "steps": [
                activity_step(
                    "flaky",
                    "always_fails_activity",
                    timeout=5,
                    retry={"max_attempts": 5, "non_retryable_error_types": ["FatalError"]},
                    on_error="continue",
                )
            ]
        }
        async with self.worker([always_fails(attempts, error_type="FatalError")]):
            await self.run_process(LinearProcessWorkflow.run, process_def)
        self.assertEqual(attempts, [1])

    async def test_heartbeat_timeout_seconds_passed_through(self):
        process_def = {
            "steps": [
                activity_step("with_hb", "report_heartbeat_timeout_activity", timeout=5, heartbeat_timeout_seconds=7),
                activity_step("without_hb", "report_heartbeat_timeout_activity", timeout=5),
            ]
        }
        async with self.worker([report_heartbeat_timeout_activity]):
            result = await self.run_process(LinearProcessWorkflow.run, process_def)
        self.assertEqual(result["with_hb"], 7)
        self.assertIsNone(result["without_hb"])


# ===========================================================================
# resume_step update and signal
# ===========================================================================


class ResumeStepTest(ProcessServerCase):
    PROCESS = {"steps": [activity_step("first", "fast_activity", timeout=5), {"id": "gate", "type": "pause"}]}

    async def test_validator_rejects_step_not_waiting(self):
        async with self.worker([fast_activity]):
            handle = await self.start_process(ConditionalProcessWorkflow.run, self.PROCESS)
            await self.wait_for_step(handle, "first")
            with self.assertRaises(WorkflowUpdateFailedError) as caught:
                await handle.execute_update("resume_step", args=["nope", {"action": "go"}])
            self.assertIn("not waiting", str(caught.exception.cause))

            # The gate is untouched: a valid resume still goes through.
            status = await handle.execute_update("resume_step", args=["gate", {"action": "go"}])
            result = await handle.result()
        self.assertEqual(status["step_id"], "gate")
        self.assertEqual(status["status"], "resumed")
        self.assertEqual(status["result"], {"action": "go"})
        self.assertIn("gate", status["completed_steps"])
        self.assertEqual(result["gate"], {"action": "go"})

    async def test_signal_alias_still_resumes(self):
        async with self.worker([fast_activity]):
            handle = await self.start_process(ConditionalProcessWorkflow.run, self.PROCESS)
            await self.wait_for_step(handle, "first")
            await handle.signal("resume_step", args=["gate", {"via": "signal"}])
            result = await handle.result()
        self.assertEqual(result["gate"], {"via": "signal"})


# ===========================================================================
# A result over the server's payload limit
# ===========================================================================


class ProcessResultSizeTest(ProcessServerCase):
    async def test_a_process_result_over_the_payload_limit_completes(self):
        """Two ~1.5 MB steps: over 2 MiB as JSON, each step's result stored once and compressed by the shared
        converter, so the completion is accepted."""
        process_def = {
            "steps": [
                activity_step("plan", "large_plan_activity", timeout=30),
                activity_step("content", "large_plan_activity", timeout=30),
            ]
        }
        async with self.worker([large_plan_activity]):
            result = await asyncio.wait_for(
                self.run_process(LinearProcessWorkflow.run, process_def, execution_timeout=timedelta(minutes=2)),
                timeout=120,
            )
        self.assertGreater(len(json.dumps(result)), SERVER_PAYLOAD_LIMIT_BYTES)
        self.assertEqual(set(result), {"plan", "content"})
        self.assertEqual(len(result["plan"]["orders"]), 15000)


# ===========================================================================
# The plug-in step contract (tests/process_workflows.py: fetch_once)
# ===========================================================================


class PluginStepTest(ProcessServerCase):
    async def test_per_run_state_is_fetched_once_across_steps_and_for_each(self):
        calls: list = []
        process_def = {
            "metadata": {"department": "ops"},
            "steps": [
                {"id": "gate_a", "type": "fetch_once"},
                {"id": "gate_b", "type": "fetch_once", "label": "explicit {{input.x}}"},
                {"id": "fan", "type": "for_each", "items": "{{input.items}}", "step": {"type": "fetch_once", "label": "{{item}}"}},
            ],
        }
        async with self.worker([defaults_activity(calls)]):
            result = await self.run_process(
                LinearProcessWorkflow.run, process_def, {"x": 1, "items": ["i0", "i1"]}, {"started_by": "test"}
            )
        self.assertEqual(len(calls), 1, "per-run state fetched once")
        first, second = result["gate_a"], result["gate_b"]
        self.assertEqual(first["seen"]["label"], "default label")
        self.assertEqual(second["seen"]["label"], "explicit {{input.x}}")  # only whole strings resolve
        self.assertEqual(first["seen"]["department"], "ops")
        self.assertEqual(first["seen"]["run_metadata"], {"started_by": "test"})
        self.assertIsNone(first["seen"]["item"])
        self.assertRegex(first["child_id"], rf"^gate_a_{UUID}-")
        fan = result["fan"]
        self.assertEqual([r["seen"]["label"] for r in fan], ["i0", "i1"])
        self.assertEqual([r["seen"]["item"] for r in fan], ["i0", "i1"])
        self.assertEqual({r["seen"]["base_id"] for r in fan}, {"fan"})
        ids = [r["child_id"] for r in fan]
        self.assertRegex(ids[0], rf"^fan_{UUID}\[0\]-")
        self.assertRegex(ids[1], rf"^fan_{UUID}\[1\]-")
        self.assertEqual(re.sub(r"\[\d\]", "", ids[0]), re.sub(r"\[\d\]", "", ids[1]))  # one uuid for the step

    async def test_every_run_fetches_its_own_state(self):
        calls: list = []
        process_def = {"steps": [{"id": "gate", "type": "fetch_once"}]}
        async with self.worker([defaults_activity(calls)]):
            await self.run_process(LinearProcessWorkflow.run, process_def)
            await self.run_process(LinearProcessWorkflow.run, process_def)
        self.assertEqual(len(calls), 2)

    async def test_cancelled_parent_lets_request_cancel_child_record_it(self):
        """Parent cancel -> the REQUEST_CANCEL child sees the cancel, records it, and ends CANCELED."""
        started: list = []
        cancelled: list = []
        workflows = [w for w in PROCESS_WORKFLOWS if w is not RecordingChild] + [CancellableRecordingChild]
        activities = [
            defaults_activity([]),
            capture("child_started_activity", started, {"recorded": True}),
            capture("child_cancelled_activity", cancelled, {"recorded": True}),
        ]
        async with self.worker(activities, workflows):
            handle = await self.start_process(LinearProcessWorkflow.run, {"steps": [{"id": "gate", "type": "fetch_once"}]})
            await wait_until(lambda: len(started) == 1)
            await handle.cancel()
            with self.assertRaises(WorkflowFailureError):
                await handle.result()
            await wait_until(lambda: len(cancelled) == 1)
            child = self.client.get_workflow_handle(started[0]["child_id"])
            for _ in range(50):
                desc = await child.describe()
                if desc.status != WorkflowExecutionStatus.RUNNING:
                    break
                await asyncio.sleep(0.1)
        self.assertEqual(cancelled[0]["status"], "CANCELLED")
        self.assertEqual(desc.status, WorkflowExecutionStatus.CANCELED)


if __name__ == "__main__":
    unittest.main()
