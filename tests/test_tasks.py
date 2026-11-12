"""The task system (`factory task`): front matter is the record, BACKLOG/COMPLETED tables are views.

Each test builds a small docs/ tree in a throwaway git repo. Run: python3 -m unittest discover -s tests"""

import contextlib
import datetime as dt
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "factory"))

from sfactory import cli, config, frontmatter  # noqa: E402
from sfactory import tasks as tk  # noqa: E402

BACKLOG = """# Backlog

## In Progress

<!-- tasks:begin in-progress -->
<!-- tasks:end -->

## Inbox

<!-- tasks:begin inbox -->
<!-- tasks:end -->

## v0.2 — Source of Truth

> Prose stays hand-written.

<!-- tasks:begin v0.2 -->
<!-- tasks:end -->

## Icebox

<!-- tasks:begin icebox -->
<!-- tasks:end -->

## Closed

<!-- tasks:begin closed -->
<!-- tasks:end -->
"""

COMPLETED = """# Completed Tasks

<!-- tasks:begin completed -->
<!-- tasks:end -->
"""


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def commit(root: Path, message: str) -> None:
    git(root, "add", ".")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)


def spec(root: Path, rel: str, body: str = "# body\n", **fm) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tk.dump_front_matter(fm) + body if fm else body)
    return path


def quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


class TaskRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = root = Path(self.tmp.name)
        (root / "docs").mkdir()
        (root / "docs/BACKLOG.md").write_text(BACKLOG)
        (root / "docs/COMPLETED.md").write_text(COMPLETED)
        spec(root, "docs/tasks/backlog/1.60-basis.md", id="1.60", title="Basis", status="open", version="v0.2",
             order=20, size="S", deps="—")
        spec(root, "docs/tasks/backlog/1.57-matching.md", id="1.57", title="Matching | listing", status="open",
             version="v0.2", order=10, size="M", deps="1.60")
        spec(root, "docs/tasks/active/2.15-tax/design-panel.md", id="2.15a", title="Panel", status="open",
             version="v0.2", order=30, size="M")
        spec(root, "docs/tasks/completed/0.38-desk/design.md", id="0.38", title="Desk", status="done",
             completed="2026-10-11")
        spec(root, "docs/tasks/completed/0.40-bug.md", id="0.40", title="[BUG] Bug", status="done",
             completed="2026-10-15")
        git(root, "init", "-q", "-b", "main")
        commit(root, "init")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cmd(self, *argv: str) -> int:
        return quiet(tk.main, list(argv), self.repo)

    def task(self, task_id: str):
        return tk.find_task(tk.load_tasks(self.repo), task_id)

    def errors(self) -> list[str]:
        return tk.validate(tk.load_tasks(self.repo), tk.all_sections(self.repo), self.repo)


class FrontMatterTests(unittest.TestCase):
    def test_round_trip_keeps_text_ids_and_dates(self):
        data = {"id": "1.60", "title": "A: colon", "status": "done", "completed": "2026-10-15", "order": 10,
                "deps": "1.60; 1.63 (soft)"}
        text = tk.dump_front_matter(data)
        self.assertIn('id: "1.60"', text)  # a bare 1.60 would read back as the float 1.6
        self.assertIn('title: "A: colon"', text)
        parsed = tk.parse_task(Path("docs/tasks/x.md"), text + "# body\n")
        self.assertEqual(parsed.front_matter(), data)
        self.assertEqual(parsed.body, "# body\n")

    def test_values_read_as_yaml_reads_them(self):
        got = frontmatter.load('a: 1.6\nb: "1.6"\nc: yes\nd: 2026-10-15\ne: ~\nf: 10\ng: x # note\nh: \'it\'\'s\'\n'
                               "i: 12:30\nj: 0x1F\nk: Y\n")
        self.assertEqual(got, {"a": 1.6, "b": "1.6", "c": True, "d": dt.date(2026, 10, 15), "e": None, "f": 10,
                               "g": "x", "h": "it's", "i": 750, "j": 31, "k": "Y"})

    def test_scalar_quotes_only_what_would_change(self):
        for value, text in [("Basis", "Basis"), ("1.60", '"1.60"'), ("[BUG] Bug", '"[BUG] Bug"'), ("on", '"on"'),
                            ("2026-10-15", "2026-10-15"), ("x #y", '"x #y"'), ("—", "—"), (10, "10")]:
            self.assertEqual(frontmatter.scalar(value), text)
            self.assertEqual(str(frontmatter.load(f"k: {text}")["k"]), str(value))

    def test_rich_yaml_is_refused_not_guessed(self):
        for block in ("deps: [1, 2]", "body: |", "  nested: x", "a: b: c", "a: 2026-13-45", "a: 1\na: 2"):
            with self.assertRaises(frontmatter.FrontMatterError):
                frontmatter.load(block)

    def test_numeric_id_is_rejected(self):
        with self.assertRaisesRegex(tk.TaskError, "quote it"):
            tk.parse_task(Path("x.md"), "---\nid: 1.6\ntitle: T\nstatus: open\n---\n")

    def test_unknown_key_is_rejected(self):
        with self.assertRaisesRegex(tk.TaskError, "unknown"):
            tk.parse_task(Path("x.md"), '---\nid: "1"\ntitle: T\nstatus: open\nstate: x\n---\n')

    def test_file_without_front_matter_is_not_a_task(self):
        self.assertIsNone(tk.parse_task(Path("x.md"), "# just a design note\n"))


class RenderTests(TaskRepo):
    def test_tables_follow_order_mark_parked_and_escape(self):
        self.assertEqual(self.run_cmd("render"), 0)
        text = (self.repo / "docs/BACKLOG.md").read_text()
        v02 = tk.blocks(text)["v0.2"].splitlines()
        self.assertEqual(v02[0], "| ID | Task | Size | Deps | Spec |")
        self.assertTrue(v02[2].startswith("| 1.57 | Matching \\| listing | M | 1.60 |"))
        self.assertEqual(v02[3], "| 1.60 | Basis | S | — | [spec](tasks/backlog/1.60-basis.md) |")
        self.assertIn("📐 Panel", v02[4])
        self.assertEqual(tk.blocks(text)["inbox"], tk.EMPTY_TABLE)
        self.assertIn("> Prose stays hand-written.", text)

    def test_completed_is_newest_first(self):
        self.run_cmd("render")
        rows = tk.blocks((self.repo / "docs/COMPLETED.md").read_text())["completed"].splitlines()[2:]
        self.assertEqual([r.split(" | ")[0] for r in rows], ["| 0.40", "| 0.38"])

    def test_render_is_idempotent_and_check_detects_stale(self):
        self.assertEqual(self.run_cmd("render", "--check"), 1)
        self.run_cmd("render")
        self.assertEqual(self.run_cmd("render", "--check"), 0)
        self.assertEqual(tk.render_all(self.repo, write=False), [])


class CheckTests(TaskRepo):
    def test_real_shape_passes(self):
        self.assertEqual(self.errors(), [])

    def test_duplicate_id_fails(self):
        spec(self.repo, "docs/tasks/backlog/1.60-again.md", id="1.60", title="Again", status="open",
             version="v0.2", order=40)
        self.assertTrue(any("1.60 is already used" in e for e in self.errors()))

    def test_legacy_duplicates_come_from_config(self):
        for name in ("a", "b"):
            spec(self.repo, f"docs/tasks/completed/0.9-{name}.md", id="0.9", title=name, status="done",
                 completed="2026-02-18")
        self.assertTrue(any("0.9 is already used" in e for e in self.errors()))
        (self.repo / ".factory.toml").write_text('[tasks]\nlegacy_duplicate_ids = ["0.9"]\n')
        self.assertEqual(self.errors(), [])

    def test_version_without_a_block_fails(self):
        spec(self.repo, "docs/tasks/backlog/1.70-x.md", id="1.70", title="X", status="open", version="v0.9",
             order=10)
        self.assertTrue(any("`v0.9` has no tasks:begin block" in e for e in self.errors()))

    def test_status_in_wrong_folder_fails(self):
        spec(self.repo, "docs/tasks/backlog/1.71-x.md", id="1.71", title="X", status="done", completed="2026-10-01")
        self.assertTrue(any("cannot live in docs/tasks/backlog/" in e for e in self.errors()))

    def test_backlog_spec_without_front_matter_fails(self):
        spec(self.repo, "docs/tasks/backlog/1.72-x.md")
        spec(self.repo, "docs/tasks/active/2.15-tax/REVIEW.md")  # notes in a design folder are fine
        self.assertEqual(self.errors(), [
            "docs/tasks/backlog/1.72-x.md: spec has no front matter (every backlog/closed spec is a task)"])

    def test_missing_required_fields_fail(self):
        spec(self.repo, "docs/tasks/backlog/1.73-x.md", id="1.73", title="X", status="open", version="icebox",
             order=10)
        self.assertTrue(any("iced task needs `reason`" in e for e in self.errors()))


class LifecycleTests(TaskRepo):
    def test_new_takes_next_id_across_every_branch(self):
        git(self.repo, "checkout", "-qb", "other")
        spec(self.repo, "docs/tasks/backlog/1.99-elsewhere.md", id="1.99", title="E", status="open",
             version="inbox", order=10, priority="P1")
        commit(self.repo, "other")
        git(self.repo, "checkout", "-q", "main")
        self.assertEqual(tk.next_id(1, self.repo), "1.100")
        self.assertEqual(tk.next_id(1, self.repo, include_git=False), "1.61")

    def test_new_goes_to_the_end_of_the_inbox(self):
        new = tk.cmd_new(1, "Next thing", bug=False, version="inbox", root=self.repo, include_git=False)
        self.assertEqual((new.id, new.status, new.version, new.priority), ("1.61", "open", "inbox", "P1"))
        self.assertTrue((self.repo / new.path).read_text().endswith("# 1.61: Next thing\n"))
        second = tk.cmd_new(1, "Another", bug=False, version="inbox", root=self.repo, include_git=False)
        self.assertGreater(second.order, new.order)

    def test_new_appends_a_body_file(self):
        body = self.repo / "body.md"
        body.write_text("Upgrade notes.\n")
        self.assertEqual(self.run_cmd("new", "--title", "Upgrade x", "--body-file", str(body)), 0)
        task = [t for t in tk.load_tasks(self.repo) if t.title == "Upgrade x"][0]
        self.assertEqual(task.priority, "P2")
        self.assertTrue((self.repo / task.path).read_text().endswith("Upgrade notes.\n"))

    def test_number_bug_goes_to_the_top_of_its_version_without_touching_others(self):
        before = (self.repo / "docs/tasks/backlog/1.57-matching.md").read_text()
        bug = tk.cmd_new(0, "Wrong total", bug=True, version="v0.2", root=self.repo, include_git=False)
        self.assertEqual(bug.title, "[BUG] Wrong total")
        self.assertLess(bug.order, min(t.order for t in tk.load_tasks(self.repo) if t.id in ("1.57", "1.60")))
        self.assertEqual((self.repo / "docs/tasks/backlog/1.57-matching.md").read_text(), before)

    def test_start_moves_the_spec_into_an_active_folder(self):
        self.assertEqual(self.run_cmd("start", "1.60", "--date", "2026-10-15"), 0)
        t = self.task("1.60")
        self.assertEqual(t.path, Path("docs/tasks/active/1.60-basis/design.md"))
        self.assertEqual((t.status, t.started), ("in_progress", "2026-10-15"))
        self.assertFalse((self.repo / "docs/tasks/backlog/1.60-basis.md").exists())
        self.assertIn("1.60-basis/design.md", git(self.repo, "status", "--short"))

    def test_subtask_lives_in_the_parent_folder(self):
        self.run_cmd("start", "1.60", "--date", "2026-10-15")
        self.assertEqual(self.run_cmd("subtask", "1.60", "a", "--title", "Basis — model", "--name", "model",
                                      "--date", "2026-10-15"), 0)
        sub = self.task("1.60a")
        self.assertEqual(sub.path, Path("docs/tasks/active/1.60-basis/design-model.md"))
        self.assertEqual(sub.status, "in_progress")

    def test_complete_parent_takes_its_subtasks_to_completed(self):
        self.run_cmd("start", "1.60", "--date", "2026-10-14")
        self.run_cmd("subtask", "1.60", "a", "--title", "A", "--name", "a", "--date", "2026-10-14")
        self.assertEqual(self.run_cmd("complete", "1.60", "--date", "2026-10-15"), 0)
        for task_id in ("1.60", "1.60a"):
            t = self.task(task_id)
            self.assertEqual((t.status, t.completed), ("done", "2026-10-15"))
            self.assertTrue(tk.is_under(t.path, tk.COMPLETED_DIR))
        self.assertFalse((self.repo / "docs/tasks/active/1.60-basis").exists())

    def test_complete_subtask_alone_stays_in_its_folder(self):
        self.run_cmd("start", "1.60", "--date", "2026-10-14")
        self.run_cmd("subtask", "1.60", "a", "--title", "A", "--name", "a", "--date", "2026-10-14")
        self.run_cmd("complete", "1.60a", "--date", "2026-10-15")
        self.assertEqual(self.task("1.60a").path, Path("docs/tasks/active/1.60-basis/design-a.md"))
        self.assertEqual(self.task("1.60").status, "in_progress")

    def test_close_moves_to_closed_with_reason(self):
        self.assertEqual(self.run_cmd("close", "1.57", "--reason", "Merged into 1.60"), 0)
        t = self.task("1.57")
        self.assertEqual((t.status, t.reason, t.path),
                         ("closed", "Merged into 1.60", Path("docs/tasks/closed/1.57-matching.md")))
        self.run_cmd("render")
        closed = tk.blocks((self.repo / "docs/BACKLOG.md").read_text())["closed"]
        self.assertIn("| 1.57 | Matching \\| listing | Merged into 1.60 |", closed)

    def test_close_active_parent_takes_its_folder_and_open_subtasks(self):
        self.run_cmd("start", "1.60", "--date", "2026-10-14")
        self.run_cmd("subtask", "1.60", "a", "--title", "A", "--name", "a", "--date", "2026-10-14")
        self.run_cmd("subtask", "1.60", "b", "--title", "B", "--name", "b", "--date", "2026-10-14")
        self.run_cmd("complete", "1.60a", "--date", "2026-10-15")
        self.assertEqual(self.run_cmd("close", "1.60", "--reason", "Done elsewhere"), 0)
        self.assertFalse((self.repo / "docs/tasks/active/1.60-basis").exists())
        self.assertEqual(self.task("1.60").path, Path("docs/tasks/closed/1.60-basis/design.md"))
        self.assertEqual(self.task("1.60b").status, "closed")
        self.assertEqual(self.task("1.60a").status, "done")  # finished work stays finished
        self.assertEqual(self.run_cmd("check"), 0)

    def test_close_subtask_alone_and_parked_design(self):
        self.run_cmd("start", "1.60", "--date", "2026-10-14")
        self.run_cmd("subtask", "1.60", "a", "--title", "A", "--name", "a", "--date", "2026-10-14")
        self.assertEqual(self.run_cmd("close", "1.60a", "--reason", "Dropped"), 0)
        self.assertEqual(self.task("1.60a").path, Path("docs/tasks/active/1.60-basis/design-a.md"))
        self.assertEqual(self.run_cmd("close", "2.15a", "--reason", "Dropped"), 0)  # parked 📐 subtask
        self.assertEqual(self.run_cmd("check"), 0)
        self.assertEqual(self.run_cmd("complete", "1.60", "--date", "2026-10-15"), 0)
        self.assertEqual(self.task("1.60a").status, "closed")  # rides along to completed/
        self.assertEqual(self.run_cmd("check"), 0)

    def test_complete_refuses_before_writing_when_destination_is_taken(self):
        self.run_cmd("start", "1.60", "--date", "2026-10-14")
        (self.repo / "docs/tasks/completed/1.60-basis").mkdir()
        self.assertEqual(self.run_cmd("complete", "1.60", "--date", "2026-10-15"), 1)
        self.assertEqual(self.task("1.60").status, "in_progress")

    def test_set_version_puts_the_task_at_the_end_of_its_new_table(self):
        self.assertEqual(self.run_cmd("set", "1.57", "version=icebox", "reason=No pain yet"), 0)
        t = self.task("1.57")
        self.assertEqual((t.version, t.order, t.reason), ("icebox", 10, "No pain yet"))
        self.assertEqual(self.run_cmd("set", "1.57", "status=done"), 1)  # lifecycle goes through its commands

    def test_lifecycle_output_stays_valid(self):
        self.run_cmd("start", "1.60", "--date", "2026-10-14")
        self.run_cmd("complete", "1.60", "--date", "2026-10-15")
        self.run_cmd("close", "1.57", "--reason", "Gone")
        self.assertEqual(self.run_cmd("check"), 0)
        self.assertEqual(self.run_cmd("render"), 0)

    def test_exists_finds_open_specs_only(self):
        self.assertTrue(tk.exists("matching", self.repo))
        self.assertFalse(tk.exists("Desk", self.repo))  # completed work never blocks a new task


class CheckPrTests(TaskRepo):
    def test_branch_that_edits_a_generated_block_fails(self):
        self.run_cmd("render")
        commit(self.repo, "render")
        self.run_cmd("start", "1.60", "--date", "2026-10-15")
        self.assertEqual(self.run_cmd("check-pr", "--base", "HEAD"), 0)  # spec change alone is fine
        self.run_cmd("render")
        self.assertEqual(tk.changed_blocks("HEAD", self.repo), ["docs/BACKLOG.md: in-progress", "docs/BACKLOG.md: v0.2"])
        self.assertEqual(self.run_cmd("check-pr", "--base", "HEAD"), 1)

    def test_prose_edits_pass(self):
        view = self.repo / "docs/BACKLOG.md"
        view.write_text(view.read_text().replace("Prose stays", "Prose still stays"))
        self.assertEqual(self.run_cmd("check-pr", "--base", "HEAD"), 0)

    def test_dropped_front_matter_or_deleted_spec_fails(self):
        path = self.repo / "docs/tasks/backlog/1.60-basis.md"
        path.write_text(tk.split_front_matter(path.read_text())[1])
        (self.repo / "docs/tasks/completed/0.40-bug.md").unlink()
        self.assertEqual(tk.lost_tasks("HEAD", self.repo), ["0.40", "1.60"])
        self.assertEqual(self.run_cmd("check-pr", "--base", "HEAD"), 1)

    def test_moves_and_closes_keep_every_task(self):
        self.run_cmd("start", "1.60", "--date", "2026-10-15")
        self.run_cmd("close", "1.57", "--reason", "Gone")
        self.assertEqual(tk.lost_tasks("HEAD", self.repo), [])


class InitAndCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q", "-b", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_makes_a_working_task_system_and_never_overwrites(self):
        self.assertEqual(quiet(cli.main, ["--repo", str(self.root), "task", "init"]), 0)
        self.assertEqual(quiet(cli.main, ["task", "--repo", str(self.root), "new", "--title", "First"]), 0)
        self.assertEqual(quiet(cli.main, ["--repo", str(self.root), "task", "render"]), 0)
        self.assertIn("| 2.1 | First |", (self.root / "docs/BACKLOG.md").read_text())
        (self.root / "docs/COMPLETED.md").write_text("mine\n")
        self.assertEqual(tk.cmd_init(self.root), [])
        self.assertEqual((self.root / "docs/COMPLETED.md").read_text(), "mine\n")

    def test_commands_mode_runs_the_repo_templates(self):
        out = self.root / "out.txt"
        (self.root / ".factory.toml").write_text(f'[tasks]\nnew = "printf %s {{title}} > {out}"\n')
        self.assertEqual(config.load(self.root).tasks_system, "commands")
        body = self.root / "b.md"
        body.write_text("x")
        rc = quiet(cli.main, ["--repo", str(self.root), "task", "new", "--title", "a b; rm -rf /", "--body-file",
                              str(body)])
        self.assertEqual((rc, out.read_text()), (0, "a b; rm -rf /"))

    def test_system_is_validated(self):
        (self.root / ".factory.toml").write_text('[tasks]\nsystem = "jira"\n')
        with self.assertRaises(config.ConfigError):
            config.load(self.root)


if __name__ == "__main__":
    unittest.main()
