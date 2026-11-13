"""Golden runs pin what the built-in step types do, exactly as the engine did before it moved here.

Where the reference comes from: before the engine moved into this runtime (docs/designs/process-engine.md, section 1
"Proof" and section 8 step 1), the consuming product's own interpreters ran a set of synthetic process files in the
time-skipping test server with stub activities and children, and saved each run's history and normalized commands.
The cases that use only built-in step types (`scope: generic`) are copied here unchanged, as
fixtures/process_golden/{processes,recorded}/; they are the reference and are not re-recorded from this code. (The
histories are byte for byte the product's; the goldens were later re-derived from those same histories when the
harness stopped keeping Python's and the SDK's wording of a workflow task failure, so only that field changed.) The
cases that use the product's own decision step (`scope: decision`) stay in the product (FamilyOffice, with its
decision step), which runs the same comparison against its binding to this runtime. All data in them is synthetic.

For each case this test:

(a) replays the saved history with temporalio's Replayer against the interpreter classes in tests/process_golden.py
    (`ProcessWorkflowBase` with `BUILTIN_STEP_TYPES`): the kind, order and type of commands and the child ids must
    still match;
(b) runs the case again and compares the normalized commands (activity and child types, ids, inputs, timeouts,
    retry and close policies, timers, the result or failure), the number of `workflow.uuid4()` draws, and the query,
    update and signal answers to the saved golden: exactly, key order included, since replay checks neither options
    nor payloads.

A difference is a change in what the engine does, which running workflows would replay against. Behaviour changes
only under Worker Versioning (docs/designs/process-engine.md, section 7); then the recordings are made again from
the new code, in the product and here, and the diff of recorded/ is reviewed in the PR.

Harness, generic stubs, the process-file format and how task failures are kept: factory_runtime.testing.golden
(shared with the product, so both normalize the same way); the interpreters it runs here: tests/process_golden.py.
"""

import json
import unittest
from typing import Any, Callable, Coroutine

from temporalio.api.failure.v1 import ApplicationFailureInfo, Failure

from factory_runtime.testing import time_skipping_env
from factory_runtime.testing.golden import dump_golden, task_failure
from tests.process_golden import HARNESS

CASES = HARNESS.cases()


class GoldenRunTest(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    async def check_case(self, name: str, interpreter: str) -> None:
        spec = HARNESS.load_process_files()[name]
        self.assertEqual(spec.get("scope"), "generic", "only built-in step cases belong here")

        replay = await HARNESS.replay_case(name, interpreter)
        self.assertIsNone(replay.replay_failure, replay.replay_failure)

        env = await time_skipping_env()
        self.addAsyncCleanup(env.shutdown)
        golden = (await HARNESS.run_case(env, name, spec, interpreter)).golden
        saved = HARNESS.golden_path(name, interpreter).read_text()
        self.assertEqual(golden, json.loads(saved))
        self.assertEqual(dump_golden(golden), saved)  # key order too: it is the order results were stored in

    def test_every_case_has_a_recording_and_no_recording_is_orphaned(self) -> None:
        self.assertTrue(CASES, f"no process files in {HARNESS.process_dir}")
        self.assertEqual(HARNESS.missing_and_orphaned(), ([], []))


class TaskFailureTest(unittest.TestCase):
    """A workflow task failure keeps the engine's own message, and of anyone else's only the exception type."""

    @staticmethod
    def failure(kind: str, message: str) -> Failure:
        return Failure(message=message, application_failure_info=ApplicationFailureInfo(type=kind))

    def test_the_engines_message_is_kept(self) -> None:
        self.assertEqual(
            task_failure(self.failure("ValueError", "Process definition is None")),
            {"type": "ValueError", "message": "Process definition is None", "info": "application_failure_info"},
        )

    def test_a_python_or_sdk_message_is_dropped(self) -> None:
        for kind, message in (
            ("AttributeError", "'NoneType' object has no attribute 'get'"),
            ("TypeError", "execute_activity() takes from 1 to 2 positional arguments but 3 were given"),
            ("ValueError", "Process definition is None, and more"),  # full match only
        ):
            self.assertEqual(
                task_failure(self.failure(kind, message)), {"type": kind, "info": "application_failure_info"}
            )

    def test_a_product_names_its_own_stable_messages(self) -> None:
        self.assertEqual(
            task_failure(
                self.failure("ValueError", "Decision step d_1 needs a subject"), [r"Decision step \S+ needs.*"]
            ),
            {"type": "ValueError", "message": "Decision step d_1 needs a subject", "info": "application_failure_info"},
        )


def _case_test(name: str, interpreter: str) -> Callable[[GoldenRunTest], Coroutine[Any, Any, None]]:
    async def test(self: GoldenRunTest) -> None:
        await self.check_case(name, interpreter)

    return test


for _name, _interpreter in CASES:
    setattr(GoldenRunTest, f"test_{_name}__{_interpreter}", _case_test(_name, _interpreter))
