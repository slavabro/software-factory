"""The test kit builds workers the way production does, and lets a test override that."""

import unittest
import unittest.mock

from temporalio.worker import UnsandboxedWorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from factory_runtime.temporal import BASE_PASSTHROUGH_MODULES, STRICT_IMPORT_POLICY
from factory_runtime.testing import TEST_PASSTHROUGH_MODULES, make_worker
from tests.workflows import PriceOrder, price


class FakeClient:
    """Enough of a Client for Worker.__init__: the worker reads its service client and config but connects later."""


class MakeWorkerTest(unittest.TestCase):
    def build(self, **kwargs):
        captured = {}

        def fake_worker(client, **kw):
            captured.update(kw)
            return "worker"

        with unittest.mock.patch("factory_runtime.testing.Worker", fake_worker):
            make_worker(FakeClient(), task_queue="q", workflows=[PriceOrder], activities=[price], **kwargs)
        return captured

    def test_production_runner_with_strict_policy_and_test_modules(self):
        runner = self.build(passthrough=["myproduct.models"])["workflow_runner"]
        self.assertIsInstance(runner, SandboxedWorkflowRunner)
        modules = runner.restrictions.passthrough_modules
        self.assertTrue({*BASE_PASSTHROUGH_MODULES, *TEST_PASSTHROUGH_MODULES, "myproduct.models"} <= modules)
        self.assertEqual(runner.restrictions.import_notification_policy, STRICT_IMPORT_POLICY)

    def test_an_explicit_runner_wins(self):
        mine = UnsandboxedWorkflowRunner()
        self.assertIs(self.build(workflow_runner=mine)["workflow_runner"], mine)


if __name__ == "__main__":
    unittest.main()
