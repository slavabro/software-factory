import importlib.util
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "fleet", Path(__file__).resolve().parent.parent / "home/skills/fleet/scripts/fleet.py")
fleet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleet)


class DefaultBranchCI(unittest.TestCase):
    def run_with(self, runs):
        repo = {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}}
        with mock.patch.object(fleet, "gh_json", side_effect=[repo, runs]) as gh:
            return fleet.default_branch_ci("."), gh.call_args_list[1].args

    def test_any_workflow_name_counts(self):
        out, args = self.run_with([{"conclusion": "success", "status": "completed", "displayTitle": "t",
                                    "url": "u", "event": "push"}])
        self.assertEqual(out["outcome"], "success")
        self.assertNotIn("--workflow", args)

    def test_dependabot_update_runs_are_skipped(self):
        out, _ = self.run_with([
            {"conclusion": "failure", "status": "completed", "displayTitle": "npm", "url": "d", "event": "dynamic"},
            {"conclusion": "success", "status": "completed", "displayTitle": "merge", "url": "c", "event": "push"},
        ])
        self.assertEqual((out["outcome"], out["url"]), ("success", "c"))

    def test_only_dependabot_runs_means_no_ci(self):
        out, _ = self.run_with([{"conclusion": "success", "status": "completed", "displayTitle": "npm",
                                 "url": "d", "event": "dynamic"}])
        self.assertEqual(out["outcome"], "none")


if __name__ == "__main__":
    unittest.main()
