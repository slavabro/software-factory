"""skillcheck rules on throwaway repos. Run: python3 -m unittest discover -s tests"""

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "factory"))

from skillcheck import frontmatter  # noqa: E402
from skillcheck.cli import check_root  # noqa: E402


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"))


def rules(root: Path, **kw) -> set[str]:
    _, findings = check_root(root, scope=kw.get("scope", "project"))
    return {f.rule for f in findings}


GOOD = """
---
name: deploy-check
description: Checks a deploy before it ships. Use when the user asks whether a release is safe to deploy.
---
# Deploy check
See [the notes](references/notes.md).
"""


class FrontmatterTests(unittest.TestCase):
    def test_folded_block_and_lists(self):
        fm = frontmatter.parse("---\nname: x\ndescription: >-\n  one\n  two\ntools:\n  - a\n  - b\nmeta: [p, q]\n---\nbody\n")
        self.assertIsNone(fm.error)
        self.assertEqual(fm.data["description"], "one two")
        self.assertEqual(fm.data["tools"], ["a", "b"])
        self.assertEqual(fm.data["meta"], ["p", "q"])
        self.assertEqual(fm.body_start, 11)

    def test_booleans_and_quotes(self):
        fm = frontmatter.parse('---\ndisable-model-invocation: true\nargument-hint: "[status | stop]"\n---\n')
        self.assertIs(fm.data["disable-model-invocation"], True)
        self.assertEqual(fm.data["argument-hint"], "[status | stop]")

    def test_unclosed_is_an_error(self):
        self.assertIsNotNone(frontmatter.parse("---\nname: x\n").error)


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_skill_has_no_findings(self):
        write(self.root, ".claude/skills/deploy-check/SKILL.md", GOOD)
        write(self.root, ".claude/skills/deploy-check/references/notes.md", "# Notes\n")
        self.assertEqual(rules(self.root), set())

    def test_description_rules(self):
        write(self.root, ".claude/skills/a/SKILL.md", "---\nname: a\n---\nbody\n")
        write(self.root, ".claude/skills/b/SKILL.md", "---\nname: b\ndescription: I help you with <files>.\n---\n")
        write(self.root, ".claude/skills/c/SKILL.md", "---\nname: c\ndescription: " + "x" * 1100 + "\n---\n")
        found = rules(self.root)
        self.assertTrue({"SC002", "SC003", "SC007", "SC008", "SC009"} <= found, found)

    def test_name_and_unknown_key(self):
        write(self.root, ".claude/skills/good-dir/SKILL.md", "---\nname: Other_Name\ndescription: Use when x.\nfoo: 1\n---\n")
        write(self.root, ".claude/skills/dir2/SKILL.md", "---\nname: other\ndescription: Use when x.\n---\n")
        found = rules(self.root)
        self.assertTrue({"SC004", "SC005", "SC006"} <= found, found)

    def test_disable_model_invocation_skips_trigger_rule(self):
        write(self.root, ".claude/skills/x/SKILL.md", "---\ndescription: Reviews a design file.\ndisable-model-invocation: true\n---\n")
        self.assertNotIn("SC009", rules(self.root))

    def test_broken_link_and_dead_path(self):
        write(self.root, "src/real.py", "")
        write(self.root, ".claude/skills/x/SKILL.md", """
        ---
        description: Use when x.
        ---
        Read `src/real.py`, `src/gone.py`, `path/to/thing.py` and `org/[dept]/agents/`.
        [missing](references/nope.md)
        """)
        _, findings = check_root(self.root, scope="project")
        msgs = {(f.rule, f.message.split("`")[1] if "`" in f.message else "") for f in findings}
        self.assertIn(("SC015", "src/gone.py"), msgs)
        self.assertIn(("SC014", "references/nope.md"), msgs)
        self.assertFalse(any(m == "src/real.py" or "path/to" in m for _, m in msgs), msgs)

    def test_paths_resolve_by_suffix_and_skip_in_user_scope(self):
        write(self.root, "common/notifications/routing.py", "")
        write(self.root, "common/notifications/CLAUDE.md", "Routing lives in `notifications/routing.py`.\n")
        self.assertNotIn("SC015", rules(self.root))
        write(self.root, "x/SKILL.md", "---\ndescription: Use when x.\n---\n`gone/file.py`\n")
        self.assertNotIn("SC015", rules(self.root, scope="user"))

    def test_reference_rules(self):
        write(self.root, ".claude/skills/x/SKILL.md", "---\ndescription: Use when x.\n---\nSee references/a.md\n")
        write(self.root, ".claude/skills/x/references/a.md", "# A\n" + "line\n" * 120 + "[b](b.md)\n")
        write(self.root, ".claude/skills/x/references/b.md", "# B\n")
        found = rules(self.root)
        self.assertTrue({"SC012", "SC013", "SC023"} <= found, found)

    def test_legacy_command_home_path_and_claude_md_budget(self):
        write(self.root, ".claude/commands/go.md", "---\ndescription: Use when going.\n---\nsee /Users/someone/Dev/x\n")
        write(self.root, "CLAUDE.md", "x\n" * 210)
        found = rules(self.root)
        self.assertTrue({"SC016", "SC017", "SC018"} <= found, found)

    def test_banned_pattern_and_suppression(self):
        write(self.root, ".skillcheck.toml", '[[banned]]\npattern = "(?i)crewai"\nmessage = "removed"\n')
        write(self.root, ".claude/skills/x/SKILL.md", "---\ndescription: Use when x.\n---\nuse CrewAI\n<!-- skillcheck: ignore SC021 -->\nCrewAI was removed\n")
        _, findings = check_root(self.root, scope="project")
        banned = [f for f in findings if f.rule == "SC021"]
        self.assertEqual([f.line for f in banned], [4])

    def test_duplicate_long_line(self):
        long = "This rule is long enough to count as a copied paragraph that will drift away from its owner over time, sadly."
        write(self.root, "CLAUDE.md", long + " " + long + "\n")
        write(self.root, ".claude/skills/x/SKILL.md", "---\ndescription: Use when x.\n---\n" + long + " " + long + "\n")
        self.assertIn("SC020", rules(self.root))

    def test_subagent_needs_name_and_description(self):
        write(self.root, ".claude/agents/r.md", "---\ntools: Read\n---\nYou review.\n")
        self.assertIn("SC022", rules(self.root))

    def test_scan_include_checks_extra_docs(self):
        write(self.root, ".skillcheck.toml", '[scan]\ninclude = ["tests/GUIDE.md"]\n[paths]\ninclude = ["tests/GUIDE.md"]\n[[banned]]\npattern = "python -m pytest"\nmessage = "use the tier script"\n')
        write(self.root, "tests/GUIDE.md", "Run `python -m pytest` or read `docs/gone.md`.\n")
        found = rules(self.root)
        self.assertTrue({"SC015", "SC021"} <= found, found)

    def test_severity_override(self):
        write(self.root, ".skillcheck.toml", '[severity]\nSC017 = "off"\n')
        write(self.root, ".claude/commands/go.md", "---\ndescription: Use when going.\n---\n")
        self.assertNotIn("SC017", rules(self.root))


if __name__ == "__main__":
    unittest.main()
