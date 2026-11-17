"""Golden runs pin what the built-in step types do, command by command.

Where the reference comes from: the recordings in fixtures/process_golden/recorded/ were made from this runtime's
own engine at v0.8, when the process engine's quirks were fixed (docs/designs/process-engine.md, section 7): the
consuming product is greenfield, so the behaviour changed directly and every recording was made again from the new
engine in the same change. (Before v0.8 they were copies of the product's recordings from before the engine moved
here; those pinned the old behaviour and are gone.) The cases that use the product's own decision step
(`scope: decision`) stay in the product, which runs the same comparison against its binding. All data in them is
synthetic.

For each case this test:

(a) replays the saved history with temporalio's Replayer against the interpreter classes in tests/process_golden.py
    (`ProcessWorkflowBase` with `BUILTIN_STEP_TYPES`): the kind, order and type of commands and the child ids must
    still match;
(b) runs the case again and compares the normalized commands (activity and child types, ids, inputs, timeouts,
    retry and close policies, timers, the result or failure), the number of `workflow.uuid4()` draws, and the query,
    update and signal answers to the saved golden: exactly, key order included, since replay checks neither options
    nor payloads.

A difference is a change in what the engine does, which running workflows replay against. When the change is
deliberate, record again from the new code and review the diff of recorded/ in the PR:

    cd runtime && GOLDEN_RECORD=1 python -m unittest tests.test_process_golden

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

        env = await time_skipping_env()
        self.addAsyncCleanup(env.shutdown)
        if HARNESS.recording():
            await HARNESS.record_case(env, name, spec, interpreter)

        replay = await HARNESS.replay_case(name, interpreter)
        self.assertIsNone(replay.replay_failure, replay.replay_failure)

        golden = (await HARNESS.run_case(env, name, spec, interpreter)).golden
        saved = HARNESS.golden_path(name, interpreter).read_text()
        self.assertEqual(golden, json.loads(saved))
        self.assertEqual(dump_golden(golden), saved)  # key order too: it is the order results were stored in

    def test_zz_every_case_has_a_recording_and_no_recording_is_orphaned(self) -> None:
        # zz: runs after the cases, so a recording run has written every file before this checks
        self.assertTrue(CASES, f"no process files in {HARNESS.process_dir}")
        self.assertEqual(HARNESS.missing_and_orphaned(), ([], []))


class TaskFailureTest(unittest.TestCase):
    """A workflow task failure keeps the engine's own message, and of anyone else's only the exception type."""

    @staticmethod
    def failure(kind: str, message: str) -> Failure:
        return Failure(message=message, application_failure_info=ApplicationFailureInfo(type=kind))

    def test_the_engine_names_no_task_failure_of_its_own(self) -> None:
        # Since v0.8 a definition the engine can't run fails the run (ProcessDefinitionError), not the task.
        self.assertEqual(
            task_failure(self.failure("ValueError", "Process definition is None")),
            {"type": "ValueError", "info": "application_failure_info"},
        )

    def test_a_python_or_sdk_message_is_dropped(self) -> None:
        for kind, message in (
            ("AttributeError", "'NoneType' object has no attribute 'get'"),
            ("TypeError", "execute_activity() takes from 1 to 2 positional arguments but 3 were given"),
        ):
            self.assertEqual(
                task_failure(self.failure(kind, message)), {"type": kind, "info": "application_failure_info"}
            )
        self.assertEqual(  # full match only
            task_failure(self.failure("ValueError", "Decision step d_1 needs a subject, and more"), [r"Decision \S+"]),
            {"type": "ValueError", "info": "application_failure_info"},
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
