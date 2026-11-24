"""`factory spec check`: capability specs, the pytest resolver, history refusal, workflow paths-ignore globs.

The fixture tests/fixtures/specs/evaluations.md is FamilyOffice's worked example (docs/research/openspec-comparison.md
§5.2 there), copied verbatim; each test here builds the repo it cites around it.
Run: python3 -m unittest discover -s tests"""

import contextlib
import io
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "factory"))

from sfactory import cli, config, frontmatter, specs  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixtures" / "specs" / "evaluations.md").read_text()
FACTORY_IDS = config.TASK_ID_PATTERNS["factory"]

# The tests the worked example cites, as FamilyOffice has them: functions at module level.
CITED = {
    "tests/database/test_rule_pass.py": [
        "test_rates_holds_and_yields",
        "test_a_rule_rating_rewrites_when_its_facts_move_and_its_words_do_not",
        "test_disabled_criterion_prunes_rule_ratings_only",
        "test_cleared_fact_prunes_and_counts_missing_and_a_skip_does_not",
        "test_unknown_kind_is_named",
    ],
    "tests/temporal/test_evaluation_rules_workflow.py": [
        "test_a_pass_that_changed_a_rating_starts_the_review",
        "test_a_pass_that_changed_nothing_starts_no_review",
        "test_a_failed_start_never_fails_the_rule_pass",
    ],
    "tests/database/test_policy_change_decision.py": [
        "test_methodology_decision_applies_through_store",
        "test_decision_wrong_lane_fails_action",
        "test_draft_stale_at_save_writes_nothing",
    ],
    "tests/standalone/test_evaluation_criteria_files.py": ["test_nothing_rates_where_it_can_be_bought"],
}


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip("\n"))


class SpecRepo(unittest.TestCase):
    """A repo holding the worked example and everything it cites."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        write(self.root, "specs/evaluations.md", FIXTURE)
        write(self.root, "specs/CLAUDE.md", "# Capability specs\nThe format: see the factory.\n")
        for owner in ("common/evaluations", "common/policies"):
            write(self.root, f"{owner}/CLAUDE.md", "# Rules\n\nPromises: [evaluations](../../specs/evaluations.md).\n")
        for path, names in CITED.items():
            write(self.root, path, "".join(f"def {n}():\n    pass\n\n\n" for n in names))

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, task_ids: str = FACTORY_IDS) -> specs.Report:
        return specs.check(self.root, "specs", "pytest", task_ids)

    def spec(self, body: str, name: str = "evaluations") -> None:
        head = f"---\ncapability: {name}\nowners: [common/evaluations]\n---\n# Title\n\n"
        write(self.root, f"specs/{name}.md", head + textwrap.dedent(body).lstrip("\n"))

    def problems(self, report: specs.Report) -> list[str]:
        return [f"{f.file}|{f.slug}|{f.problem}" for f in report.failures]

    def assertFails(self, report: specs.Report, *fragments: str) -> None:
        text = "\n".join(self.problems(report))
        for fragment in fragments:
            self.assertIn(fragment, text)


class WorkedExampleTests(SpecRepo):
    def test_the_worked_example_passes(self):
        report = self.check()
        self.assertEqual(report.failures, [])
        self.assertEqual((report.specs, report.requirements, report.proofs), (1, 6, 12))

    def test_renaming_a_cited_test_fails_with_the_requirement_it_orphaned(self):
        path = self.root / "tests/database/test_rule_pass.py"
        path.write_text(path.read_text().replace("def test_unknown_kind_is_named", "def test_unknown_kind_is_reported"))
        report = self.check()
        self.assertEqual(len(report.failures), 1)
        failure = report.failures[0]
        self.assertEqual((failure.file, failure.slug), ("specs/evaluations.md", "no-rating-on-a-missing-fact"))
        self.assertIn("no test `test_unknown_kind_is_named` in tests/database/test_rule_pass.py", failure.problem)
        self.assertIn("cite the test", failure.fix)
        self.assertIn("specs/evaluations.md [no-rating-on-a-missing-fact]: line ", str(failure))

    def test_a_deleted_test_file_fails(self):
        (self.root / "tests/standalone/test_evaluation_criteria_files.py").unlink()
        self.assertFails(self.check(), "no-criterion-rates-where-to-buy|line 56: no test file tests/standalone/")


class FrontMatterTests(SpecRepo):
    def test_capability_must_equal_the_file_name(self):
        (self.root / "specs/evaluations.md").write_text(FIXTURE.replace("capability: evaluations", "capability: ratings"))
        self.assertFails(self.check(), "specs/evaluations.md||capability is 'ratings', the file is evaluations.md")

    def test_missing_front_matter_and_unknown_keys(self):
        write(self.root, "specs/a.md", "# A\n")
        write(self.root, "specs/b.md", "---\ncapability: b\nowners: [common/policies]\nsince: x\n---\n")
        self.assertFails(self.check(), "specs/a.md||no front matter", "specs/b.md||unknown front matter key `since`")

    def test_owners_must_exist_and_link_the_spec(self):
        (self.root / "common/policies/CLAUDE.md").write_text("# Rules\n\nSee [specs](../../specs/CLAUDE.md).\n")
        write(self.root, "specs/x.md", "---\ncapability: x\nowners: [nowhere, common]\n---\n")
        (self.root / "common/CLAUDE.md").unlink(missing_ok=True)
        report = self.check()
        self.assertFails(
            report,
            "evaluations.md||common/policies/CLAUDE.md does not link the spec",
            "x.md||owner `nowhere` is not a directory in the repo",
            "x.md||owner `common` has no CLAUDE.md",
        )
        fix = next(f.fix for f in report.failures if "common/policies" in f.problem)
        self.assertIn("../../specs/evaluations.md", fix)

    def test_owners_must_be_a_list(self):
        write(self.root, "specs/x.md", "---\ncapability: x\nowners: common\n---\n")
        self.assertFails(self.check(), "x.md||owners must be a non-empty list")

    def test_a_reference_style_link_and_an_anchor_count(self):
        (self.root / "common/policies/CLAUDE.md").write_text("Promises: [ev].\n\n[ev]: ../../specs/evaluations.md#x\n")
        self.assertEqual(self.check().failures, [])

    def test_flow_lists_are_read_only_when_asked(self):
        self.assertEqual(frontmatter.load('o: [a/b, "c, d", \'e\']', sequences=True), {"o": ["a/b", "c, d", "e"]})
        self.assertEqual(frontmatter.load("o: []", sequences=True), {"o": []})
        for bad in ("o: [a, [b]]", "o: [a,, b]", 'o: ["a]', "o: [a"):
            with self.assertRaises(frontmatter.FrontMatterError, msg=bad):
                frontmatter.load(bad, sequences=True)
        with self.assertRaises(frontmatter.FrontMatterError):
            frontmatter.load("o: [a, b]")  # task specs stay scalar-only


class RequirementTests(SpecRepo):
    def test_slugs_unique_and_well_formed(self):
        self.spec("""
            ## same
            It SHALL hold.

            Proven by:
            - tests/database/test_rule_pass.py::test_rates_holds_and_yields

            ## same
            It SHALL hold.

            Proven by:
            - tests/database/test_rule_pass.py::test_rates_holds_and_yields

            ## Not A Slug
            It SHALL hold.

            Proven by:
            - tests/database/test_rule_pass.py::test_rates_holds_and_yields
            """)
        self.assertFails(self.check(), "|same|line 13: the slug is already used on line 7", "|Not A Slug|line 19: `Not A Slug` is not a slug")

    def test_every_requirement_promises_and_is_proven(self):
        self.spec("""
            ## no-promise
            It should probably hold.

            Proven by:
            - tests/database/test_rule_pass.py::test_rates_holds_and_yields

            ## no-list
            It MUST hold.

            ## empty-list
            It SHALL hold.

            Proven by:

            ## stray
            It SHALL hold.

            Proven by:
            - tests/database/test_rule_pass.py::test_rates_holds_and_yields

            Some prose after the list.
            """)
        self.assertFails(
            self.check(),
            "|no-promise|no SHALL or MUST",
            "|no-list|no `Proven by:` list",
            "|empty-list|no tests under `Proven by:`",
            "|stray|line 27: `Some prose after the list.` after the Proven by list",
        )

    def test_headings_inside_a_code_fence_are_not_requirements(self):
        self.spec("""
            Intro with an example:

            ```
            ## not-a-requirement
            ```

            ## real
            It SHALL hold.

            Proven by:
            - `tests/database/test_rule_pass.py::test_rates_holds_and_yields`
            """)
        report = self.check()
        self.assertEqual(report.failures, [])
        self.assertEqual(report.requirements, 1)


class HistoryTests(SpecRepo):
    def test_task_ids_and_dates_are_refused_wherever_prose_is(self):
        self.spec("""
            Built in task 0.41b.

            ## dated
            Since 2026-11-22 it SHALL hold, as decided on 3 November 2026 and March 2025.

            Proven by:
            - tests/database/test_rule_pass.py::test_rates_holds_and_yields[0.5-2026-01-01]
            """)
        self.assertFails(
            self.check(),
            "||line 7: task reference `0.41b`",
            "|dated|line 10: date `2026-11-22`",
        )
        line = "as decided on 3 November 2026 and March 2025"
        self.assertEqual(specs._history(line, __import__("re").compile(FACTORY_IDS)), "date `3 November 2026`")
        self.assertEqual(len(self.check().failures), 2, "the proof's parameters are a test ID, not prose")

    def test_numbers_that_are_not_task_ids_pass(self):
        rx = __import__("re").compile(FACTORY_IDS)
        for text in ("Python 3.11.4", "v1.2", "1,000 rows", "at 5 percent", "rule 3"):
            self.assertIsNone(specs._history(text, rx), text)
        for text in ("task 1.60.", "(0.41b)", "see 2.7a"):
            self.assertIsNotNone(specs._history(text, rx), text)

    def test_the_pattern_comes_from_tasks(self):
        write(self.root, ".factory.toml", '[tasks]\nsystem = "commands"\n')
        self.assertEqual(config.load(self.root).tasks_id_pattern, config.TASK_ID_PATTERNS["commands"])
        write(self.root, ".factory.toml", '[tasks]\nid_pattern = "\\\\bFO-\\\\d+\\\\b"\n')
        pattern = config.load(self.root).tasks_id_pattern
        self.spec("""
            ## r
            It SHALL hold, as FO-12 asked and 0.41b did.

            Proven by:
            - tests/database/test_rule_pass.py::test_rates_holds_and_yields
            """)
        self.assertEqual(self.problems(self.check(pattern)), ["specs/evaluations.md|r|line 8: task reference `FO-12`"])


class PytestResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        write(self.root, "tests/test_x.py", """
            class Base:
                def test_inherited(self):
                    pass

            class TestThing(Base):
                async def test_async(self):
                    pass

                class TestInner:
                    def test_deep(self):
                        pass

            def test_param(value):
                pass

            test_not_a_def = 1
            """)
        write(self.root, "tests/test_broken.py", "def (:\n")
        self.resolve = specs.RESOLVERS["pytest"](self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_what_resolves(self):
        for node in (
            "tests/test_x.py::test_param",
            "tests/test_x.py::test_param[a-1]",
            "tests/test_x.py::TestThing::test_async",
            "tests/test_x.py::TestThing::test_inherited",
            "tests/test_x.py::TestThing::TestInner::test_deep[x]",
        ):
            self.assertIsNone(self.resolve(node), node)

    def test_what_does_not(self):
        cases = {
            "tests/test_x.py": "not a pytest node ID",
            "tests/test_x.py::test_gone": "no test `test_gone` in tests/test_x.py",
            "tests/test_x.py::test_not_a_def": "no test `test_not_a_def`",
            "tests/test_x.py::TestGone::test_async": "no class `TestGone` in tests/test_x.py",
            "tests/test_x.py::TestThing::test_gone": "no test `test_gone` in tests/test_x.py::TestThing",
            "tests/test_x.py::TestThing": "is a class; cite a test in it",
            "tests/test_x.py::test_param::test_async": "`test_param` in tests/test_x.py is not a class",
            "tests/test_x.py::test_param[": "malformed `[param]`",
            "tests/test_x.py::test-dash": "not a Python identifier",
            "tests/test_y.py::test_param": "no test file tests/test_y.py",
            "../outside.py::test_param": "not a path inside the repo",
            "tests/test_broken.py::test_param": "cannot parse tests/test_broken.py",
        }
        for node, problem in cases.items():
            self.assertIn(problem, self.resolve(node) or "", node)


class WorkflowGlobTests(SpecRepo):
    def test_actions_glob_semantics(self):
        cases = [
            ("docs/**", "docs/a/b.md", True),
            ("docs/*", "docs/a/b.md", False),
            ("*.md", "specs/x.md", False),
            ("**.md", "specs/x.md", True),
            ("**/*.md", "specs/x.md", True),
            ("spec?s/*", "specs/x.md", True),
            ("spec?s/*", "spes/x.md", True),
            ("spe+cs/*", "speeecs/x.md", True),
            ("spe+cs/*", "spcs/x.md", False),
            ("[st]pecs/*", "specs/x.md", True),
            ("[a-r]pecs/*", "specs/x.md", False),
            ("specs/x\\*.md", "specs/x*.md", True),
            ("specs/x\\*.md", "specs/xy.md", False),
            ("sp.cs/**", "specs/x.md", False),
        ]
        for glob, path, hit in cases:
            self.assertEqual(bool(specs.actions_glob(glob).fullmatch(path)), hit, f"{glob} vs {path}")

    def test_paths_ignore_is_read_in_every_shape(self):
        text = textwrap.dedent("""
            on:
              pull_request:
                paths-ignore:
                  - 'docs/**'   # docs-only
                  - "*.md"
                  # a comment
                  - README
                branches: [main]
              push:
                "paths-ignore": ['a/**', "b/**" ]  # trailing
                paths:
                - keep/**
            jobs: {}
            """)
        self.assertEqual(specs.paths_ignore(text), ["docs/**", "*.md", "README", "a/**", "b/**"])

    def test_a_specs_folder_a_workflow_ignores_fails(self):
        write(self.root, ".github/workflows/test.yml", "on:\n  push:\n    paths-ignore:\n      - '*.md'\n      - 'docs/**'\n")
        write(self.root, ".github/workflows/lint.yaml", "on:\n  push:\n    paths-ignore: ['!specs/**']\n")
        self.assertEqual(self.check().failures, [], "`*.md` stops at `/`; a negation ignores nothing")
        write(self.root, ".github/workflows/ci.yml", "on:\n  pull_request:\n    paths-ignore:\n      - '**.md'\n")
        report = self.check()
        self.assertEqual(len(report.failures), 1)
        failure = report.failures[0]
        self.assertEqual((failure.file, failure.slug), (".github/workflows/ci.yml", ""))
        self.assertIn("paths-ignore `**.md` matches specs/evaluations.md", failure.problem)
        self.assertIn("[specs] dir", failure.fix)

    def test_an_empty_specs_folder_under_docs_still_fails(self):
        (self.root / "docs/capabilities").mkdir(parents=True)
        write(self.root, ".github/workflows/test.yml", "on:\n  push:\n    paths-ignore:\n      - 'docs/**'\n")
        report = specs.check(self.root, "docs/capabilities", "pytest", FACTORY_IDS)
        self.assertFails(report, ".github/workflows/test.yml||paths-ignore `docs/**` matches docs/capabilities/")


class ConfigAndCliTests(SpecRepo):
    def test_specs_settings(self):
        self.assertEqual((config.load(self.root).specs.dir, config.load(self.root).specs.resolver), ("specs", "pytest"))
        write(self.root, ".factory.toml", '[specs]\ndir = "capabilities"\n')
        self.assertEqual(config.load(self.root).specs.dir, "capabilities")
        for bad, message in (
            ('[specs]\nresolver = "jest"\n', "specs.resolver must be one of pytest"),
            ('[specs]\ndir = "../elsewhere"\n', "specs.dir must be a folder inside the repo"),
            ('[specs]\ndir = 3\n', "specs.dir must be str"),
            ('[specs]\nfolder = "x"\n', "unknown key specs.folder"),
            ('[tasks]\nid_pattern = "("\n', "tasks.id_pattern is not a regular expression"),
        ):
            write(self.root, ".factory.toml", bad)
            with self.assertRaisesRegex(config.ConfigError, message.replace("[", "\\[").replace(".", "\\.")):
                config.load(self.root)

    def run_cli(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--repo", str(self.root), "spec", "check"])
        return code, out.getvalue(), err.getvalue()

    def test_cli(self):
        self.assertEqual(self.run_cli(), (0, "spec check passed: 1 specs, 6 requirements, 12 proofs\n", ""))
        (self.root / "tests/database/test_policy_change_decision.py").unlink()
        code, out, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("specs/evaluations.md [desks-change-criteria-by-decision]: line ", out)
        self.assertIn("    fix: cite the test", out)
        self.assertIn("spec check FAILED: 3 problems", err)

    def test_a_missing_specs_folder_fails(self):
        write(self.root, ".factory.toml", '[specs]\ndir = "capabilities"\n')
        code, out, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("capabilities: the specs folder does not exist", out)


if __name__ == "__main__":
    unittest.main()
