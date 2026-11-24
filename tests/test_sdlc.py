"""The SDLC layer: the factory catalog, blocks and templates, a project's processes and gates, and the CLI.
Run: python3 -m unittest discover -s tests"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "factory"))

from sfactory import cli, config, sdlc  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "factory"


def process(**over):
    """A minimal valid process: one check step in one stage, started by a click."""
    base = {
        "name": "Sample",
        "runs_in": ["engine"],
        "triggers": [{"manual": {}}],
        "stages": [{"name": "Checks", "steps": ["verify"]}],
        "steps": [{"id": "verify", "type": "check", "block": "factory:verify"}],
    }
    base.update(over)
    return base


class Project(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text if isinstance(text, str) else json.dumps(text))

    def proc(self, pid, data):
        self.write(f".factory/processes/{pid}.json", data)

    def errors(self):
        return sdlc.check(config.load(self.root)).errors

    def assertProblem(self, needle):
        errors = self.errors()
        self.assertTrue(any(needle in e for e in errors), f"{needle!r} not in {errors}")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--repo", str(self.root), "sdlc", *argv])
        return code, out.getvalue(), err.getvalue()


class FactoryLayer(unittest.TestCase):
    def test_catalog_and_blocks_load(self):
        catalog = sdlc.load_catalog()
        blocks = sdlc.factory_blocks(catalog)
        self.assertIn("factory:upgrade", blocks)
        self.assertEqual({b.kind for b in blocks.values()} - set(sdlc.KINDS), set())

    def test_every_available_agent_runs_a_skill_that_exists(self):
        for block in sdlc.factory_blocks().values():
            if block.kind == "agent" and not block.planned:
                self.assertTrue((PLUGIN / "skills" / block.skill / "SKILL.md").is_file(), block.ref)

    def test_every_planned_block_names_its_task(self):
        for block in sdlc.factory_blocks().values():
            if block.planned:
                self.assertRegex(block.task, r"^F-\d+$", block.ref)

    def test_run_from_names_a_config_setting(self):
        names = {f.name for f in fields(config.Config)}
        for block in sdlc.factory_blocks().values():
            if block.run_from:
                self.assertIn(block.run_from, names, block.ref)

    def test_every_template_validates_in_a_plain_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = sdlc.load(config.load(Path(tmp)))
            events = set(project.catalog.events)
            for tid, path in sdlc.templates().items():
                data = json.loads(path.read_text())
                self.assertEqual(sdlc.process_problems(data, project, events | sdlc.emitted(data, project.blocks)), [], tid)

    def test_a_broken_catalog_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = (sdlc.FACTORY_DIR / "catalog.toml").read_text().replace('pr = "pr"', 'pr = "admin"')
            (Path(tmp) / "catalog.toml").write_text(text)
            with self.assertRaisesRegex(sdlc.SdlcError, "autonomy level"):
                sdlc.load_catalog(Path(tmp))


class Cron(unittest.TestCase):
    def test_valid(self):
        for cron in ("0 6 * * 1", "*/15 * * * *", "0 2 1-7 * 0,6", "30 22 * 1-12/3 7"):
            self.assertIsNone(sdlc.cron_problem(cron), cron)

    def test_invalid(self):
        for cron in ("0 6 * *", "60 * * * *", "0 24 * * *", "0 0 0 * *", "* * * * 8", "a * * * *", "*/0 * * * *", 5):
            self.assertIsNotNone(sdlc.cron_problem(cron), cron)


class Processes(Project):
    def test_a_valid_process_passes(self):
        self.proc("sample", process())
        self.assertEqual(self.errors(), [])

    def test_unknown_keys_and_types(self):
        self.proc("sample", process(colour="blue"))
        self.assertProblem("unknown key `colour`")
        self.proc("sample", process(steps=[{"id": "verify", "type": "robot", "block": "factory:verify"}]))
        self.assertProblem("`type` is one of")

    def test_block_must_exist_match_and_be_available(self):
        self.proc("sample", process(steps=[{"id": "verify", "type": "check", "block": "factory:nope"}]))
        self.assertProblem("block `factory:nope` not found")
        self.proc("sample", process(steps=[{"id": "verify", "type": "agent", "block": "factory:verify"}]))
        self.assertProblem("is a check, the step's type is agent")
        self.proc("sample", process(steps=[{"id": "verify", "type": "agent", "block": "factory:architect"}]))
        self.assertProblem("planned (F-11)")

    def test_block_must_run_where_the_process_runs(self):
        self.proc("sample", process(runs_in=["ci"], stages=[{"name": "A", "steps": ["up"]}],
                                    steps=[{"id": "up", "type": "agent", "block": "factory:upgrade"}]))
        self.assertProblem("can't run in ci")

    def test_settings_are_checked_against_the_catalog_and_block(self):
        steps = [{"id": "up", "type": "agent", "block": "factory:upgrade", "model": "gpt", "tools": ["browser"],
                  "outputs": {"pr": 9, "deploy": 1}, "budget_minutes": 0}]
        self.proc("sample", process(stages=[{"name": "A", "steps": ["up"]}], steps=steps))
        for needle in ("model: `gpt`", "tools: `browser`", "outputs.pr: 9 is more", "outputs: `deploy`", "budget_minutes"):
            self.assertProblem(needle)

    def test_autonomy_cap_and_output_autonomy(self):
        self.write(".factory/gates.json", {"autonomy_cap": "edit"})
        self.proc("sample", process(stages=[{"name": "A", "steps": ["up"]}],
                                    steps=[{"id": "up", "type": "agent", "block": "factory:upgrade"}]))
        self.assertProblem("autonomy `pr` is above the project's cap `edit`")
        self.write(".factory/gates.json", {})
        self.proc("sample", process(stages=[{"name": "A", "steps": ["up"]}],
                                    steps=[{"id": "up", "type": "agent", "block": "factory:upgrade", "autonomy": "read"}]))
        self.assertProblem("output `pr` needs autonomy `pr`")

    def test_triggers(self):
        self.proc("sample", process(triggers=[{"schedule": "0 25 * * *"}]))
        self.assertProblem("outside 0-23")
        self.proc("sample", process(triggers=[{"event": "nothing.happened"}]))
        self.assertProblem("unknown event `nothing.happened`")
        self.proc("sample", process(triggers=[{"event": "pr.merged", "schedule": "0 1 * * *"}]))
        self.assertProblem("exactly one of event, schedule, manual")
        self.proc("sample", process(triggers=[{"manual": {"inputs": {"what": "colour"}}}]))
        self.assertProblem("unknown type `colour`")

    def test_stages_hold_every_top_level_step_once_in_order(self):
        steps = [{"id": "a", "type": "check", "block": "factory:verify"},
                 {"id": "b", "type": "check", "block": "factory:tasks"}]
        self.proc("sample", process(steps=steps, stages=[{"name": "A", "steps": ["a"]}]))
        self.assertProblem("step `b` is in no stage")
        self.proc("sample", process(steps=steps, stages=[{"name": "A", "steps": ["b", "a"]}]))
        self.assertProblem("different order")
        self.proc("sample", process(steps=steps, stages=[{"name": "A", "steps": ["a", "b", "zz"]}]))
        self.assertProblem("`zz`, which is not a top-level step")

    def test_human_exits_and_check_on_fail(self):
        steps = [{"id": "a", "type": "check", "block": "factory:verify", "on_fail": {"back_to": "b", "retries": 2}},
                 {"id": "b", "type": "human", "block": "factory:approve", "exits": {"approved": "nowhere", "maybe": "end"}}]
        self.proc("sample", process(steps=steps, stages=[{"name": "A", "steps": ["a", "b"]}]))
        for needle in ("on_fail.back_to: `b` is not an earlier step", "exits.approved: `nowhere`", "exits: `maybe`"):
            self.assertProblem(needle)
        steps[1] = {"id": "b", "type": "human", "block": "factory:approve", "asks": "when"}
        self.proc("sample", process(steps=steps, stages=[{"name": "A", "steps": ["a", "b"]}]))
        self.assertProblem("asks `when` needs `ask_when`")

    def test_parallel_steps(self):
        steps = [{"id": "both", "type": "parallel", "steps": [{"id": "x", "type": "check", "block": "factory:verify"}]}]
        self.proc("sample", process(steps=steps, stages=[{"name": "A", "steps": ["both"]}]))
        self.assertProblem("two or more `steps`")
        steps[0]["steps"].append({"id": "x", "type": "check", "block": "factory:tasks"})
        self.proc("sample", process(steps=steps, stages=[{"name": "A", "steps": ["both"]}]))
        self.assertProblem("step id `x` is used twice")

    def test_needs_a_config_setting(self):
        self.write(".factory.toml", "[verify]\ncommands = []\n")
        self.proc("sample", process())
        self.assertProblem("needs `verify` set in .factory.toml")

    def test_an_event_cycle_is_a_warning(self):
        def loop(name, on, emits):
            return process(triggers=[{"event": on}], stages=[{"name": "A", "steps": ["v", "e"]}],
                           steps=[{"id": "v", "type": "check", "block": "factory:verify"},
                                  {"id": "e", "type": "event", "block": "factory:emit", "event": emits}])
        self.proc("one", loop("one", "a.done", "b.done"))
        self.proc("two", loop("two", "b.done", "a.done"))
        report = sdlc.check(config.load(self.root))
        self.assertEqual(report.errors, [])
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("one → two → one", report.warnings[0])


class ProjectLayer(Project):
    def test_project_blocks_and_their_skills(self):
        self.write(".factory/blocks/design.toml", textwrap.dedent("""
            kind = "agent"
            name = "Design"
            does = "Writes a design."
            skill = "design-task"
            runs_in = ["session"]
            outputs = { files = 1 }
            [defaults]
            autonomy = "edit"
        """))
        self.proc("sample", process(runs_in=["session"], stages=[{"name": "A", "steps": ["d"]}],
                                    steps=[{"id": "d", "type": "agent", "block": "repo:design"}]))
        self.assertProblem("skill `design-task` not found in .claude/skills")
        self.write(".claude/skills/design-task/SKILL.md", "---\nname: design-task\n---\n")
        self.assertEqual(self.errors(), [])

    def test_sdlc_settings_extend_the_catalog(self):
        self.write(".factory.toml", '[sdlc]\ndir = "ops/factory"\nextra_models = ["local"]\nextra_events = ["deploy.paged"]\n')
        self.write("ops/factory/processes/sample.json", json.dumps(process(
            triggers=[{"event": "deploy.paged"}], stages=[{"name": "A", "steps": ["up"]}],
            steps=[{"id": "up", "type": "agent", "block": "factory:upgrade", "model": "local"}])))
        self.assertEqual(self.errors(), [])

    def test_sdlc_dir_must_stay_inside_the_repo(self):
        self.write(".factory.toml", '[sdlc]\ndir = "../elsewhere"\n')
        with self.assertRaisesRegex(config.ConfigError, "sdlc.dir"):
            config.load(self.root)

    def test_gates(self):
        self.write(".factory/gates.json", {"autonomy_cap": "root", "gates": [
            {"id": "merge", "name": "Merge", "on": "merge", "asks": "always", "trusted_since": "2026-12-01"},
            {"id": "factory-config", "name": "Mine", "on": "merge"},
            {"id": "ship", "name": "Ship", "on": "launch", "asks": "until-trusted", "trusted_since": "soon"}]})
        for needle in ("autonomy_cap", "belongs only to a gate that asks until-trusted", "is a factory floor",
                       "`on` is one of", "a date, YYYY-MM-DD"):
            self.assertProblem(needle)

    def test_default_gates_apply_without_a_file(self):
        project = sdlc.load(config.load(self.root))
        self.assertEqual([g["id"] for g in project.gates["gates"]], ["merge", "production"])
        self.assertEqual(sdlc.gates_problems(project), [])


class Cli(Project):
    def test_new_from_a_template_then_check_and_show(self):
        code, out, _ = self.run_cli("new", "upgrade", "--from", "upgrade")
        self.assertEqual(code, 0, out)
        written = json.loads((self.root / ".factory/processes/upgrade.json").read_text())
        self.assertEqual(written["template"], "upgrade")
        self.assertEqual(self.run_cli("check")[0], 0)
        code, out, _ = self.run_cli("show", "upgrade", "--json")
        settings = json.loads(out)["steps"][0]["settings"]
        self.assertEqual(settings["autonomy"], {"value": "pr", "from": "block"})
        self.assertEqual(settings["fresh_context"], {"value": False, "from": "catalog"})
        self.assertEqual(self.run_cli("new", "upgrade", "--from", "upgrade")[0], 1)  # never overwrites

    def test_block_card_lists_where_it_is_used(self):
        self.proc("sample", process())
        code, out, _ = self.run_cli("block", "factory:verify", "--json")
        self.assertEqual(json.loads(out)["used_by"], ["sample:verify"])
        self.assertEqual(self.run_cli("block", "factory:nope")[0], 1)

    def test_check_fails_with_every_problem_named(self):
        self.proc("sample", process(runs_in=["mars"]))
        code, out, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("sample.json: runs_in: unknown executor `mars`", out)
        self.assertIn("FAILED", err)

    def test_the_writer_refuses_an_invalid_process(self):
        project = sdlc.load(config.load(self.root))
        with self.assertRaises(sdlc.SdlcError):
            sdlc.write_process(project, "bad", process(steps=[]))
        self.assertFalse((self.root / ".factory/processes/bad.json").exists())


if __name__ == "__main__":
    unittest.main()
