"""The efficiency scout's deterministic half (sfactory/efficiency.py) on synthetic transcripts."""

import datetime as dt
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "factory"))

from sfactory import cli, config, efficiency  # noqa: E402

SECRET = "SECRET-ACCOUNT-4417"  # stands in for private data: must never reach any output
MODEL = "claude-opus-4-5"


class Transcript:
    """Builds a Claude Code style JSONL transcript."""

    def __init__(self, cwd: Path, branch: str = "feat/x", start: float | None = None):
        self.cwd, self.branch = str(cwd), branch
        self.t = start or time.time() - 3600
        self.lines: list[dict] = []
        self.n = 0

    def _base(self, kind: str, advance: float) -> dict:
        self.t += advance
        stamp = dt.datetime.fromtimestamp(self.t, dt.timezone.utc).isoformat().replace("+00:00", "Z")
        return {"type": kind, "timestamp": stamp, "cwd": self.cwd, "gitBranch": self.branch, "sessionId": "s",
                "isSidechain": False}

    def turn(self, *tools: tuple[str, dict], ctx: int = 40000, write: int = 1000, out: int = 200, gap: float = 5,
             model: str = MODEL) -> list[str]:
        self.n += 1
        ids = [f"toolu_{self.n}_{i}" for i in range(len(tools))]
        content = [{"type": "tool_use", "id": i, "name": n, "input": inp} for i, (n, inp) in zip(ids, tools)]
        r = self._base("assistant", gap)
        r["message"] = {"id": f"msg_{self.n}", "model": model, "role": "assistant",
                        "content": content or [{"type": "text", "text": f"thinking about {SECRET}"}],
                        "usage": {"input_tokens": 10, "cache_creation_input_tokens": write,
                                  "cache_read_input_tokens": max(ctx - write - 10, 0), "output_tokens": out}}
        self.lines.append(r)
        return ids

    def result(self, tool_id: str, size: int = 100, error: bool = False, prompted: bool = False) -> None:
        r = self._base("user", 1)
        r["message"] = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": (SECRET + " ") * (size // 20 + 1),
             "is_error": error}]}
        if prompted:
            r["permissionDecision"] = {"decision": "accept", "source": "user_temporary"}
        self.lines.append(r)

    def call(self, name: str, inp: dict, size: int = 100, error: bool = False, prompted: bool = False, **turn) -> None:
        (tid,) = self.turn((name, inp), **turn)
        self.result(tid, size, error, prompted)

    def prompt(self, text: str) -> None:
        r = self._base("user", 30)
        r["message"] = {"role": "user", "content": text}
        r["origin"] = {"kind": "human"}
        r["promptSource"] = "typed"
        self.lines.append(r)

    def compact(self) -> None:
        r = self._base("system", 1)
        r["subtype"] = "compact_boundary"
        self.lines.append(r)

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(x) for x in self.lines) + "\n")
        return path


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name).resolve()
        self.repo = git_repo(self.dir / "repo")
        self.projects = self.dir / "projects"
        self.toml = ""

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, t: Transcript, sid: str, project: str = "p") -> Path:
        return t.write(self.projects / project / f"{sid}.jsonl")

    def cfg(self, extra: str = "") -> config.Config:
        text = f'[efficiency]\nprojects_dir = "{self.projects}"\n' + extra
        return config.parse(self.repo, text, self.repo / ".factory.toml")

    def scan(self, extra: str = "", **kw) -> efficiency.Report:
        return efficiency.scan(self.cfg(extra), since="7d", workers=1, **kw)

    def found(self, report, detector):
        return [f for f in report.findings if f.detector == detector]


class SessionDetectorTests(Base):
    def test_repeated_reads_of_an_unchanged_file(self):
        t = Transcript(self.repo)
        f = str(self.repo / "app.py")
        for _ in range(3):
            t.call("Read", {"file_path": f}, size=4000)
        self.save(t, "s1")
        hit = self.found(self.scan('thresholds = { min_usd = 0 }\n'), "repeat-read")
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0].count, 2)
        self.assertIn("app.py ×3", hit[0].items)  # relative to the worktree

    def test_a_read_after_an_edit_or_a_compaction_is_not_repeated(self):
        t = Transcript(self.repo)
        f = str(self.repo / "app.py")
        t.call("Read", {"file_path": f})
        t.call("Edit", {"file_path": f, "old_string": SECRET, "new_string": "x"})
        t.call("Read", {"file_path": f})
        t.compact()
        t.call("Read", {"file_path": f})
        self.save(t, "s1")
        self.assertEqual(self.found(self.scan('thresholds = { min_usd = 0 }\n'), "repeat-read"), [])

    def test_big_reads_and_oversized_results(self):
        t = Transcript(self.repo)
        t.call("Read", {"file_path": str(self.repo / "big.py")}, size=80000)
        t.call("Read", {"file_path": str(self.repo / "big.py"), "offset": 10, "limit": 50}, size=80000)
        t.call("Bash", {"command": f"cat log.txt | grep {SECRET}"}, size=60000)
        for _ in range(5):
            t.turn()
        self.save(t, "s1")
        report = self.scan('thresholds = { min_usd = 0 }\n')
        (big,) = self.found(report, "big-read")
        self.assertEqual(big.count, 1)  # the ranged read is fine
        (over,) = self.found(report, "oversized-result")
        self.assertEqual(over.items, ["Bash:cat ×1"])
        self.assertGreater(over.usd, 0.1)

    def test_identical_calls_and_unchanged_retries(self):
        t = Transcript(self.repo)
        for _ in range(3):
            t.call("Bash", {"command": "make lint"})
        t.call("Bash", {"command": "make test", "description": "first"}, error=True)
        t.call("Bash", {"command": "make  test", "description": "again"}, error=True)  # same call, other words
        self.save(t, "s1")
        report = self.scan('thresholds = { min_usd = 0 }\n')
        (rep,) = self.found(report, "repeat-call")
        self.assertEqual(rep.items, ["Bash:make lint ×2"])
        (retry,) = self.found(report, "retry-unchanged")
        self.assertEqual(retry.count, 1)

    def test_polling(self):
        t = Transcript(self.repo)
        for _ in range(3):
            t.call("Bash", {"command": "sleep 60 && gh pr checks 12"})
        for _ in range(3):
            t.call("Bash", {"command": "gh run view 99"})
        self.save(t, "s1")
        (poll,) = self.found(self.scan('thresholds = { min_usd = 0 }\n'), "polling")
        self.assertEqual(poll.count, 6)
        self.assertIn("3 min asleep", poll.what)

    def test_cache_rebuild_after_a_pause_and_long_context(self):
        t = Transcript(self.repo)
        t.turn(ctx=200000, write=200000)
        t.turn(ctx=200000, write=1000)
        t.turn(ctx=200000, write=190000, gap=900)  # the cache expired
        t.turn(ctx=200000, write=1000)
        self.save(t, "s1")
        report = self.scan('thresholds = { min_usd = 0 }\n')
        (cache,) = self.found(report, "cache-rebuild")
        self.assertEqual(cache.items, ["pause ×1"])
        self.assertAlmostEqual(cache.usd, 190000 * 5 * 1.15 / 1e6, places=2)
        (long,) = self.found(report, "long-context")
        self.assertEqual(long.count, 4)

    def test_subagents_overspawned_and_rereading_the_parent(self):
        parent = Transcript(self.repo)
        f = str(self.repo / "spec.md")
        parent.call("Read", {"file_path": f}, size=8000)
        for i in range(3):
            parent.call("Agent", {"subagent_type": "general-purpose", "prompt": SECRET})
        path = self.save(parent, "parent")
        for i in range(3):
            child = Transcript(self.dir / "elsewhere", start=parent.t + 10)
            child.call("Read", {"file_path": f}, size=8000)
            sub = child.write(path.with_suffix("") / "subagents" / f"agent-a{i}.jsonl")
            sub.with_suffix(".meta.json").write_text(json.dumps({"agentType": "general-purpose"}))
        report = self.scan('thresholds = { min_usd = 0, max_subagents = 3 }\n')
        self.assertEqual(report.subagents, 3)
        (spawn,) = self.found(report, "subagent-overspawn")
        self.assertEqual((spawn.session, spawn.count, spawn.items), ("parent", 3, ["general-purpose ×3"]))
        reread = self.found(report, "subagent-reread")
        self.assertEqual(len(reread), 3)
        self.assertEqual({f.repo for f in reread}, {"repo"})  # a subagent belongs to its parent's repo

    def test_corrections_keep_the_category_never_the_text(self):
        t = Transcript(self.repo)
        t.prompt(f"build the report for {SECRET}")
        t.call("Bash", {"command": "make report"})
        t.prompt(f"no, I said the {SECRET} one")
        t.prompt("[Request interrupted by user]")
        t.prompt("<command-name>/review-code</command-name>")
        self.save(t, "s1")
        (corr,) = self.found(self.scan(), "user-correction")
        self.assertEqual(corr.count, 2)
        self.assertIn("no ×1", corr.items)
        self.assertIn("interrupt ×1", corr.items)
        custom = self.found(self.scan('corrections = ["\\\\bthe .* one$"]\n'), "user-correction")
        self.assertIn("pattern-1 ×1", custom[0].items)

    def test_permission_prompts(self):
        t = Transcript(self.repo)
        for _ in range(5):
            t.call("Bash", {"command": "npm install left-pad"}, prompted=True)
        t.call("AskUserQuestion", {"questions": []}, prompted=True)  # asking is its job, not a prompt
        self.save(t, "s1")
        (perm,) = self.found(self.scan(), "permission-prompts")
        self.assertEqual((perm.count, perm.items), (5, ["Bash:npm install ×5"]))

    def test_base_context_per_repo(self):
        for i in range(3):
            t = Transcript(self.repo)
            t.turn(ctx=60000, write=60000)
            t.call("Skill", {"skill": "review-code"})
            self.save(t, f"s{i}")
        (base,) = self.found(self.scan('thresholds = { min_usd = 0 }\n'), "base-context")
        self.assertIn("60k tokens", base.what)
        self.assertEqual(base.items, ["review-code ×3"])

    def test_the_same_files_edited_in_two_worktrees(self):
        files = ["a.py", "b.py", "c.py"]
        git = ["git", "-C", str(self.repo), "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        for name in ("wt1", "wt2"):
            wt = self.dir / name
            subprocess.run([*git, "worktree", "add", "-q", "-b", name, str(wt)], check=True, capture_output=True)
            t = Transcript(wt.resolve(), branch=name)
            for f in files:
                t.call("Edit", {"file_path": str(wt.resolve() / f), "old_string": "x", "new_string": SECRET})
            self.save(t, name)
        (dup,) = self.found(self.scan('thresholds = { min_usd = 0 }\n'), "duplicate-effort")
        self.assertEqual(sorted(dup.sessions), ["wt1", "wt2"])
        self.assertEqual(dup.items, files)
        self.assertEqual(dup.repo, "repo")


class ScopeAndPrivacyTests(Base):
    def _two_repos(self):
        other = git_repo(self.dir / "other")
        for i, cwd in enumerate((self.repo, other)):
            t = Transcript(cwd)
            for _ in range(3):
                t.call("Bash", {"command": f"make lint {SECRET}"})
            self.save(t, f"s{i}", project=f"p{i}")

    def test_scope_is_this_repo_unless_all(self):
        self._two_repos()
        mine = self.scan('thresholds = { min_usd = 0 }\n')
        self.assertEqual((mine.sessions, {f.repo for f in mine.findings}), (1, {"repo"}))
        every = self.scan('thresholds = { min_usd = 0 }\n', all_repos=True)
        self.assertEqual((every.sessions, {f.repo for f in every.findings}), (2, {"repo", "other"}))

    def test_disabled_detectors_ignore_globs_and_min_usd(self):
        self._two_repos()
        self.assertEqual(self.found(self.scan('thresholds = { min_usd = 0 }\ndisabled = ["repeat-call"]\n'),
                                    "repeat-call"), [])
        self.assertEqual(self.scan(f'ignore = ["{self.repo}"]\n').sessions, 0)
        self.assertEqual(self.found(self.scan('thresholds = { min_usd = 100 }\n'), "repeat-call"), [])

    def test_factory_runs_are_their_own_source(self):
        run = self.dir / "home" / "runs" / "repo" / "upgrade-20261101-0600"
        run.mkdir(parents=True)
        t = Transcript(run)
        t.call("Bash", {"command": "make ci"})
        self.save(t, "run1")
        old = efficiency.runner.HOME
        efficiency.runner.HOME = self.dir / "home"
        try:
            report = self.scan()
            self.assertEqual(report.factory_runs, 1)
            self.assertEqual(self.scan('sources = ["sessions"]\n').sessions, 0)
        finally:
            efficiency.runner.HOME = old

    def test_no_output_carries_prompt_text_tool_input_or_tool_output(self):
        self._two_repos()
        t = Transcript(self.repo)
        t.prompt(f"no, use {SECRET}")
        t.call("Read", {"file_path": str(self.repo / "x.py")}, size=50000)
        t.call("Grep", {"pattern": SECRET, "path": str(self.repo)}, size=50000)
        t.call("WebFetch", {"url": f"https://example.com/{SECRET}", "prompt": SECRET}, size=50000)
        self.save(t, "leaky")
        report = self.scan('thresholds = { min_usd = 0 }\n', all_repos=True)
        self.assertTrue(report.findings)
        self.assertNotIn(SECRET, json.dumps(report.as_dict()))
        self.assertNotIn(SECRET, efficiency.markdown(report))
        s = efficiency.read_session(str(self.projects / "p" / "leaky.jsonl"))
        efficiency.place(s)
        self.assertNotIn(SECRET, efficiency.show_text(s) + json.dumps(efficiency.timeline(s)))
        self.assertNotIn(SECRET, repr(s))  # not even in memory


class RuntimeTests(Base):
    def _records(self) -> list[dict]:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        same = {"ts": now, "agent": "analyst", "task": "summarise", "model": "claude-opus-4-5", "input_tokens": 3000,
                "output_tokens": 120, "cost_usd": 0.5, "prompt_hash": "h1"}
        return ([same] * 6
                + [{**same, "prompt_hash": "h2", "outcome": "error"}]
                + [{**same, "prompt_hash": "h3", "output_used": False, "task": "draft"}])

    def test_runtime_detectors_from_the_repo_command(self):
        data = self.dir / "usage.jsonl"
        data.write_text("\n".join(json.dumps(r) for r in self._records()) + "\nnot json\n")
        report = self.scan(f'runtime_usage = "cat {data} # {{since}}"\n')
        self.assertEqual(report.runtime_calls, 8)
        self.assertEqual(report.errors, ["runtime usage line 9: Expecting value: line 1 column 1 (char 0)"])
        (rep,) = self.found(report, "runtime-repeat-prompt")
        self.assertEqual((rep.count, rep.usd), (6, 2.5))
        self.assertEqual(self.found(report, "runtime-retry")[0].count, 1)
        self.assertEqual(self.found(report, "runtime-unused")[0].usd, 0.5)
        (exp,) = self.found(report, "runtime-expensive-model")
        self.assertIn("analyst / summarise", exp.what)

    def test_missing_cost_is_priced_and_a_failing_command_is_reported_quietly(self):
        recs, errors = efficiency.parse_runtime(json.dumps(
            {"ts": "2026-11-01T00:00:00Z", "model": "claude-sonnet-4-5", "input_tokens": 1_000_000}))
        self.assertEqual((recs[0]["cost_usd"], errors), (3.0, []))
        report = self.scan(f'runtime_usage = "echo {SECRET} >&2; exit 3"\n')
        self.assertEqual(report.errors, ["runtime_usage exited 3"])


class ConfigTests(Base):
    def test_efficiency_section(self):
        cfg = self.cfg('enabled = true\nschedule = "0 5 * * 2"\nthresholds = { repeat_read_min = 5 }\n')
        self.assertEqual(cfg.efficiency.limit("repeat_read_min"), 5)
        self.assertEqual(cfg.efficiency.limit("poll_min"), config.DEFAULT_THRESHOLDS["poll_min"])
        self.assertEqual((cfg.teams()["efficiency"].prompt, cfg.teams()["efficiency"].schedule),
                         ("/factory:efficiency", "0 5 * * 2"))
        self.assertNotIn("efficiency", self.cfg().teams())  # opt-in: it reads every session transcript
        for bad in ('thresholds = { repeat_reads = 5 }\n', 'thresholds = { poll_min = "5" }\n', 'scope = "mine"\n',
                    'sources = ["logs"]\n', 'corrections = ["("]\n', 'max_tasks = "5"\n'):
            with self.assertRaises(config.ConfigError, msg=bad):
                self.cfg(bad)
        with self.assertRaises(config.ConfigError):
            config.parse(self.repo, '[teams.efficiency]\nprompt = "/x"\n', self.repo / ".factory.toml")

    def test_command_head_keeps_names_only(self):
        cases = {
            f"cd /x && FOO=1 python3 -m pytest -k {SECRET}": "python3 -m pytest",
            "gh pr checks 12 --watch": "gh pr checks",
            f"git commit -m '{SECRET}'": "git commit",
            "sleep 30; gh run view 5": "sleep",
            f"curl -s https://example.com/{SECRET}": "curl",
            "": "",
        }
        for cmd, head in cases.items():
            self.assertEqual(efficiency.command_head(cmd), head, cmd)

    def test_since(self):
        self.assertAlmostEqual(efficiency.parse_since("2d"), time.time() - 172800, delta=5)
        self.assertEqual(efficiency.parse_since("2026-11-01"),
                         dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc).timestamp())
        with self.assertRaises(ValueError):
            efficiency.parse_since("last week")

    def test_cli_scan_and_show(self):
        t = Transcript(self.repo)
        for _ in range(3):
            t.call("Bash", {"command": "make lint"})
        self.save(t, "abcdef12-0000")
        (self.repo / ".factory.toml").write_text(f'[efficiency]\nprojects_dir = "{self.projects}"\n')
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["efficiency", "scan", "--json", "--repo", str(self.repo)]), 0)
            self.assertEqual(cli.main(["efficiency", "show", "abcdef12", "--repo", str(self.repo)]), 0)
        text = out.getvalue()
        self.assertIn('"sessions": 1', text)
        self.assertIn("make lint", text)
        self.assertIn("flags: E = error", text)  # flags are letters, not colours


if __name__ == "__main__":
    unittest.main()
