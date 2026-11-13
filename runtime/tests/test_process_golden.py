"""Golden runs pin what the built-in step types do, exactly as the engine did before it moved here.

Where the reference comes from: before the engine moved into this runtime (docs/designs/process-engine.md, section 1
"Proof" and section 8 step 1), the consuming product's own interpreters ran a set of synthetic process files in the
time-skipping test server with stub activities and children, and saved each run's history and normalized commands.
The cases that use only built-in step types (`scope: generic`) are copied here unchanged, as
fixtures/process_golden/{processes,recorded}/; they are the reference and are not re-recorded from this code. The
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

Harness, stubs and the process-file format: tests/process_golden.py.
"""

import json
import unittest
from typing import Any, Callable, Coroutine

from factory_runtime.testing import time_skipping_env
from tests.process_golden import (
    PROCESS_DIR,
    RECORDED_DIR,
    case_id,
    dump_golden,
    golden_cases,
    golden_path,
    history_path,
    load_process_files,
    replay_case,
    run_case,
)

CASES = golden_cases()


class GoldenRunTest(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    async def check_case(self, name: str, interpreter: str) -> None:
        spec = load_process_files()[name]
        self.assertEqual(spec.get("scope"), "generic", "only built-in step cases belong here")

        replay = await replay_case(name, interpreter, history_path(name, interpreter).read_text())
        self.assertIsNone(replay.replay_failure, replay.replay_failure)

        env = await time_skipping_env()
        self.addAsyncCleanup(env.shutdown)
        golden = await run_case(env, name, spec, interpreter)
        saved = golden_path(name, interpreter).read_text()
        self.assertEqual(golden, json.loads(saved))
        self.assertEqual(dump_golden(golden), saved)  # key order too: it is the order results were stored in

    def test_every_case_has_a_recording_and_no_recording_is_orphaned(self) -> None:
        expected = {f"{case_id(*case)}.{kind}.json" for case in CASES for kind in ("history", "golden")}
        present = {path.name for path in RECORDED_DIR.glob("*.json")}
        self.assertTrue(CASES, f"no process files in {PROCESS_DIR}")
        self.assertEqual(expected, present)


def _case_test(name: str, interpreter: str) -> Callable[[GoldenRunTest], Coroutine[Any, Any, None]]:
    async def test(self: GoldenRunTest) -> None:
        await self.check_case(name, interpreter)

    return test


for _name, _interpreter in CASES:
    setattr(GoldenRunTest, f"test_{_name}__{_interpreter}", _case_test(_name, _interpreter))
