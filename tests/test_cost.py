"""Agent spend: the transcript reader (sfactory/usage.py), attribution and budgets (cost.py, costconfig.py), the run
gate, the SessionStart line and the page (costpage.py). Every transcript here is synthetic.
Run: python3 -m unittest discover -s tests"""

import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "factory"))

from sfactory import cli, config, cost, costconfig, costpage, runner, usage  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
SECRET = "PRIVATE-PROMPT-TEXT-never-on-the-page"


def turn(mid, ts, cwd, *, sid="s1", branch="main", model="claude-opus-5-5", inp=10, out=100, cw=0, cw1h=0, cr=0,
         tools=(), text=SECRET, speed="standard", geo="global", searches=0, side=False):
    content = [{"type": "text", "text": text}] + [{"type": "tool_use", "name": t, "input": {"cmd": SECRET}} for t in tools]
    return {
        "type": "assistant", "sessionId": sid, "cwd": str(cwd), "gitBranch": branch, "timestamp": ts,
        "isSidechain": side, "uuid": f"u-{mid}-{len(tools)}",
        "message": {"id": mid, "model": model, "role": "assistant", "content": content, "usage": {
            "input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": cw,
            "cache_creation": {"ephemeral_5m_input_tokens": cw - cw1h, "ephemeral_1h_input_tokens": cw1h},
            "cache_read_input_tokens": cr, "speed": speed, "inference_geo": geo,
            "server_tool_use": {"web_search_requests": searches}}},
    }


def user_line(cwd):
    return {"type": "user", "sessionId": "s1", "cwd": str(cwd), "message": {"role": "user", "content": SECRET}}


def write_jsonl(path: Path, rows, append=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a" if append else "w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


class Sandbox(unittest.TestCase):
    """Isolated projects dir, cache, run roots and user config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.projects = self.base / "projects"
        self.cache = self.base / "cache"
        self.user_cfg = self.base / "user" / "factory.toml"
        self.work = self.base / "work"
        self.work.mkdir()
        for target, value in ((usage, {"PROJECTS": self.projects, "CACHE": self.cache, "RUNS": self.base / "runs",
                                       "ENGINE_WORKTREES": self.base / "state" / "worktrees"}),
                              (costconfig, {"USER_CONFIG": self.user_cfg})):
            for name, v in value.items():
                p = mock.patch.object(target, name, v)
                p.start()
                self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def git_repo(self, name: str, toml: str = "") -> Path:
        root = self.work / name
        root.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        if toml:
            (root / ".factory.toml").write_text(textwrap.dedent(toml))
        return root

    def user(self, toml: str):
        self.user_cfg.parent.mkdir(parents=True, exist_ok=True)
        self.user_cfg.write_text(textwrap.dedent(toml))

    def recs(self, **kw):
        return list(usage.records(projects=self.projects, cache=self.cache, **kw))


class ReaderTests(Sandbox):
    def test_streamed_blocks_are_one_turn_with_every_tool(self):
        cwd = self.work
        write_jsonl(self.projects / "p" / "s1.jsonl", [
            user_line(cwd),
            turn("m1", "2026-11-17T10:00:00Z", cwd, tools=["Read"]),
            turn("m1", "2026-11-17T10:00:00Z", cwd, tools=["Bash", "Read"]),
            turn("m2", "2026-11-17T10:01:00Z", cwd, model="<synthetic>"),
        ])
        recs = self.recs()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].tool_names, ("Bash", "Read"))
        self.assertEqual((recs[0].input, recs[0].output), (10, 100))

    def test_subagent_transcripts_carry_their_type_and_dedupe_across_files(self):
        cwd = self.work
        write_jsonl(self.projects / "p" / "s1.jsonl", [turn("m1", "2026-11-17T10:00:00Z", cwd)])
        sub = self.projects / "p" / "s1" / "subagents" / "agent-a1.jsonl"
        write_jsonl(sub, [turn("m2", "2026-11-17T10:02:00Z", cwd, side=True), turn("m1", "2026-11-17T10:00:00Z", cwd)])
        sub.with_suffix(".meta.json").write_text(json.dumps({"agentType": "Explore", "description": SECRET}))
        recs = self.recs()
        self.assertEqual([(r.message_id, r.is_subagent, r.agent_type) for r in recs],
                         [("m1", False, ""), ("m2", True, "Explore")])

    def test_cache_skips_unchanged_files_and_reads_only_what_was_appended(self):
        path = self.projects / "p" / "s1.jsonl"
        write_jsonl(path, [turn("m1", "2026-11-17T10:00:00Z", self.work)])
        self.assertEqual(len(self.recs()), 1)
        starts = []
        real = usage._read

        def spy(p, start, turns):
            starts.append(start)
            return real(p, start, turns)

        with mock.patch.object(usage, "_read", spy):
            self.assertEqual(len(self.recs()), 1)
            self.assertEqual(starts, [])  # unchanged: not opened
            size = path.stat().st_size
            write_jsonl(path, [turn("m2", "2026-11-17T10:05:00Z", self.work)], append=True)
            self.assertEqual(len(self.recs()), 2)
            self.assertEqual(starts, [size])  # appended: read from the old end
        for f in (self.cache / "files").glob("*.json"):
            self.assertNotIn(SECRET, f.read_text())

    def test_since_and_until(self):
        write_jsonl(self.projects / "p" / "s1.jsonl", [
            turn("m1", "2026-11-01T10:00:00Z", self.work), turn("m2", "2026-11-17T10:00:00Z", self.work)])
        self.assertEqual([r.message_id for r in self.recs(since=dt.datetime(2026, 11, 10, tzinfo=UTC))], ["m2"])
        self.assertEqual([r.message_id for r in self.recs(until=dt.datetime(2026, 11, 10, tzinfo=UTC))], ["m1"])


class WhereTests(Sandbox):
    def test_runner_and_engine_worktrees_give_repo_and_capability(self):
        self.assertEqual(usage.resolve_repo(str(usage.RUNS / "shop" / "upgrade-20261109-0600")),
                         usage.Where("shop", "upgrade", None))
        self.assertEqual(usage.resolve_repo(str(usage.ENGINE_WORKTREES / "shop" / "qa" / "r1" / "sub")),
                         usage.Where("shop", "qa", None))

    def test_a_worktree_counts_for_its_main_checkout_and_a_gone_cwd_for_its_ancestor(self):
        root = self.git_repo("main-checkout", '[repo]\nname = "shop"\n')
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=root, check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@t"})
        wt = self.work / "wt-feature"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "f", str(wt)], cwd=root, check=True)
        self.assertEqual(usage.resolve_repo(str(wt / "src")).repo, "shop")
        self.assertEqual(usage.resolve_repo(str(root / ".claude" / "worktrees" / "gone")).repo, "shop")
        self.assertEqual(usage.resolve_repo(str(self.work / "nogit" / "deleted")).repo, "work")


class PriceTests(unittest.TestCase):
    def test_every_token_kind_fast_us_only_and_web_search(self):
        p = usage.DEFAULT_PRICES
        usd, priced = p.cost("claude-opus-5-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertTrue(priced)
        self.assertAlmostEqual(usd, 4 + 20 + 5 + 8 + 0.2)
        self.assertAlmostEqual(p.cost("claude-opus-5-5", 1_000_000, 0, 0, 0, 0, speed="fast")[0], 8)
        self.assertAlmostEqual(p.cost("claude-opus-5-5", 1_000_000, 0, 0, 0, 0, geo="us")[0], 4.4)
        self.assertAlmostEqual(p.cost("claude-opus-5-5", 0, 0, 0, 0, 0, web_searches=3)[0], 0.03)

    def test_haiku_long_prompts_and_unknown_models(self):
        p = usage.DEFAULT_PRICES
        self.assertAlmostEqual(p.cost("claude-haiku-5-5", 100_000, 0, 0, 0, 0)[0], 0.01)
        self.assertAlmostEqual(p.cost("claude-haiku-5-5", 100_001, 0, 0, 0, 0)[0], 100_001 * 0.5 / 1e6)
        self.assertEqual(p.cost("claude-imaginary-9", 1000, 1000, 0, 0, 0), (0.0, False))

    def test_model_names_and_overrides(self):
        self.assertEqual(usage.canonical_model("us.anthropic.claude-opus-4-8-20260101[1m]"), "claude-opus-4-8")
        self.assertEqual(usage.canonical_model("anthropic/claude-haiku-4-5@20251001"), "claude-haiku-4-5")
        p = usage.DEFAULT_PRICES.with_overrides({"claude-opus-5-5": {"input": 1.0}})
        self.assertEqual((p.models["claude-opus-5-5"].input, p.models["claude-opus-5-5"].output), (1.0, 20))
        with self.assertRaises(ValueError):
            usage.DEFAULT_PRICES.with_overrides({"claude-new": {"input": 1.0}})


class ConfigTests(Sandbox):
    def test_repo_file_takes_the_cost_sections_and_still_refuses_typos(self):
        cfg = config.parse(self.base, textwrap.dedent("""
            [repo]
            name = "shop"
            [cost]
            task_pattern = '(T\\d+)'
            [cost.prices."claude-opus-5-5"]
            input = 3
            [departments.web]
            branches = ["web/*"]
            [[budgets]]
            department = "web"
            monthly_usd = 50
            """), Path("x"))
        self.assertEqual(cfg.cost.departments[0].branches, ["web/*"])
        self.assertEqual((cfg.cost.budgets[0].owner_repo, cfg.cost.budgets[0].monthly_usd), ("shop", 50.0))
        bad = {
            "[cost]\nprize = 1\n": "unknown key cost.prize",
            "[[budgets]]\nmonthly_usd = 5\ndepartment = 'a'\nagent = 'b'\n": "one scope at most",
            "[[budgets]]\ndepartment = 'a'\n": "at least one of",
            "[[budgets]]\nmonthly_usd = 5\nwarn = 95\ndanger = 90\n": "warn < danger",
            "[departments.x]\n": "at least one of",
            "[cost]\ntask_pattern = '('\n": "not a valid regex",
            "[cost.prices.m]\ninput = -1\n": "≥ 0",
            "[budget]\n": "unknown section",
        }
        for text, msg in bad.items():
            with self.assertRaises(config.ConfigError, msg=text) as e:
                config.parse(self.base, text, Path("x"))
            self.assertIn(msg, str(e.exception))

    def test_user_file_holds_only_the_cost_sections(self):
        self.user("[verify]\ncommands = []\n")
        with self.assertRaises(costconfig.CostConfigError):
            costconfig.load_user()


class AttributionTests(unittest.TestCase):
    def test_agents(self):
        base = dict(session_id="s", ts=NOW, cwd="/", repo="r", branch="", model="m", input=0, output=0, cache_write=0,
                    cache_read=0, cost_usd=0, tool_names=())
        self.assertEqual(cost.agent_of(usage.UsageRecord(is_subagent=False, agent_type="", **base)), "interactive")
        self.assertEqual(cost.agent_of(usage.UsageRecord(is_subagent=True, agent_type="Explore", **base)), "subagent:Explore")
        self.assertEqual(cost.agent_of(usage.UsageRecord(is_subagent=True, agent_type="Explore", capability="upgrade",
                                                         **base)), "run:upgrade")

    def test_task_ids_from_branch_then_worktree_and_never_from_stamps(self):
        pat = __import__("re").compile(costconfig.DEFAULT_TASK_PATTERN)
        cases = {
            ("factory/f-33-costs-budgets", ""): "F-33",
            ("slavabro/1.106d-impl", ""): "1.106d",
            ("main", "proj-7-x"): "PROJ-7",
            ("factory/upgrade-20261109-0600", "upgrade-20261109-0600"): cost.NO_TASK,
            ("worktree-agent-a1a417940a7da5ede", "agent-a1a417940a7da5ede"): cost.NO_TASK,
            ("release/v0.2", ""): cost.NO_TASK,
        }
        for (branch, wt), want in cases.items():
            self.assertEqual(cost.task_of(branch, wt, pat), want, branch)

    def test_first_matching_department_wins_and_every_rule_must_match(self):
        depts = [costconfig.Department("upkeep", capabilities=["*"]),
                 costconfig.Department("web", repos=["shop"], branches=["web/*"]),
                 costconfig.Department("shop", repos=["shop"])]
        self.assertEqual(cost.department_of("shop", "upgrade", "web/x", "/", "run:upgrade", depts), "upkeep")
        self.assertEqual(cost.department_of("shop", None, "web/x", "/", "interactive", depts), "web")
        self.assertEqual(cost.department_of("shop", None, "main", "/", "interactive", depts), "shop")
        self.assertEqual(cost.department_of("blog", None, "web/x", "/", "interactive", depts), cost.NO_DEPARTMENT)


def entry(cost_usd, ts=NOW, repo="shop", department="web", agent="interactive", task="T-1", tokens=0):
    rec = usage.UsageRecord(session_id="s", ts=ts, cwd="/", repo=repo, branch="", model="m", input=tokens, output=0,
                            cache_write=0, cache_read=0, cost_usd=cost_usd, is_subagent=False, agent_type="", tool_names=())
    return cost.Entry(rec, department, agent, task)


class BudgetTests(unittest.TestCase):
    def test_states_follow_the_product_thresholds(self):
        self.assertEqual([cost.state_of(p, 75, 90) for p in (10, 75, 90, 99.9, 100, 130)],
                         ["within", "approaching", "near", "near", "over", "over"])

    def test_windows_scopes_globs_and_repo_ownership(self):
        yesterday = NOW - dt.timedelta(days=1)
        rows = [entry(40, yesterday), entry(30, task="T-2"), entry(5, repo="blog", department="blog", task="T-9")]
        budgets = [
            costconfig.Budget(department="web", monthly_usd=60, daily_usd=20),
            costconfig.Budget(task="*", monthly_usd=35),
            costconfig.Budget(monthly_tokens=10, owner_repo="blog"),
        ]
        rows[2] = entry(5, repo="blog", department="blog", task="T-9", tokens=12)
        found = {(m.label, m.window, m.unit): m for m in cost.meters(rows, budgets, NOW)}
        self.assertEqual(found[("department web", "this month", "usd")].state, cost.OVER)  # 70 of 60
        self.assertEqual(found[("department web", "today", "usd")].state, cost.OVER)  # 30 of 20
        self.assertEqual(found[("task T-1", "this month", "usd")].state, cost.OVER)  # 40 of 35
        self.assertEqual(found[("task T-2", "this month", "usd")].state, cost.APPROACHING)  # 30 of 35 = 86 %
        self.assertTrue(found[("task T-1", "this month", "usd")].each)
        self.assertEqual(found[("blog: all spend", "this month", "tokens")].used, 12)

    def test_near_never_reads_as_one_hundred_percent(self):
        m = cost.meters([entry(249.71)], [costconfig.Budget(monthly_usd=250)], NOW)[0]
        self.assertEqual((m.state, m.pct_text), (cost.NEAR, "99%"))

    def test_a_run_is_stopped_only_by_hard_budgets_over_the_limit_that_cover_it(self):
        settings = cost.Settings(costconfig.CostConfig(departments=[costconfig.Department("upkeep", capabilities=["*"])]))
        probe = cost.run_probe(settings, "shop", "upgrade")
        self.assertEqual((probe.department, probe.agent), ("upkeep", "run:upgrade"))
        mk = lambda **kw: cost.Meter(**{**dict(label="x", window="this month", unit="usd", used=2, limit=1, pct=200,  # noqa: E731
                                            state=cost.OVER, hard=True, warn=75, danger=90, scope_key=None,
                                            scope_value=None, owner_repo=None, source="f"), **kw})
        self.assertEqual(len(cost.blockers([mk()], probe)), 1)
        self.assertEqual(cost.blockers([mk(hard=False), mk(state=cost.NEAR), mk(owner_repo="blog"),
                                        mk(scope_key="department", scope_value="web"),
                                        mk(scope_key="task", scope_value="T-1")], probe), [])
        self.assertEqual(len(cost.blockers([mk(scope_key="agent", scope_value="run:upgrade")], probe)), 1)


class GateAndHookTests(Sandbox):
    def seed(self, usd_tokens=1_000_000):
        repo = self.git_repo("shop")
        run_cwd = usage.RUNS / "shop" / "upgrade-20261117-0600"
        now = dt.datetime.now(UTC).isoformat()
        write_jsonl(self.projects / "p" / "s1.jsonl", [turn("m1", now, run_cwd, inp=usd_tokens, out=0)])  # $4
        return repo

    def test_factory_run_refuses_to_start_over_a_hard_limit_and_says_why(self):
        repo = self.seed()
        self.user('[departments.upkeep]\ncapabilities = ["*"]\n[[budgets]]\ndepartment = "upkeep"\nmonthly_usd = 1\n')
        cfg = config.load(repo)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(runner, "HOME", self.base / "h"):
            self.assertEqual(runner.run(cfg, "upgrade"), 1)
        self.assertIn("not started — budget ✕ Over limit: department upkeep, this month — $4.00 of $1.00", out.getvalue())
        self.assertFalse((self.base / "h" / "runs").exists())  # nothing was created
        self.assertEqual(cost.run_blockers(repo, "other-team"), cost.run_blockers(repo, "upgrade"))  # still upkeep

    def test_soft_and_unrelated_budgets_let_it_run(self):
        repo = self.seed()
        self.user('[[budgets]]\nmonthly_usd = 1\nhard = false\n[[budgets]]\nrepo = "blog"\nmonthly_usd = 1\n')
        self.assertEqual(cost.run_blockers(repo, "upgrade"), [])
        self.assertEqual(cost.run_blockers(self.git_repo("nobudgets"), "upgrade"), [])

    def test_the_hook_is_silent_when_healthy_and_reads_only_the_saved_state(self):
        self.seed()
        self.assertEqual(cost.hook(cache=self.cache, spawn=False), "")  # no state yet
        self.user("[[budgets]]\nmonthly_usd = 100\n")
        cost.evaluate(cost.Settings.load(None))
        self.assertEqual(cost.hook(cache=self.cache, spawn=False), "")  # 4 %
        self.user("[[budgets]]\nmonthly_usd = 4.2\n")
        cost.evaluate(cost.Settings.load(None))
        with mock.patch.object(usage, "records", side_effect=AssertionError("the hook must not scan")):
            line = cost.hook(cache=self.cache, spawn=False)
        self.assertIn("! Near limit: all spend, this month — $4.00 of $4.20 (95%)", line)

    def test_the_hook_refreshes_in_the_background_only_when_stale(self):
        self.seed()
        self.user("[[budgets]]\nmonthly_usd = 100\n")
        cost.evaluate(cost.Settings.load(None))
        with mock.patch.object(cost, "_refresh_in_background") as bg:
            cost.hook(cache=self.cache)
            bg.assert_not_called()
            cost.hook(cache=self.cache, now=dt.datetime.now().astimezone() + dt.timedelta(hours=7))
            bg.assert_called_once()


class CommandTests(Sandbox):
    def seed(self):
        self.git_repo("shop")
        today = dt.datetime.now(UTC).replace(microsecond=0)
        rows = []
        for i in range(6):
            ts = (today - dt.timedelta(days=i)).isoformat()
            rows.append(turn(f"m{i}", ts, self.work / "shop", branch=f"feature/t-{i}", inp=100_000, cw=50_000,
                             cw1h=10_000, cr=400_000, tools=["Bash"]))
        rows.append(turn("mx", today.isoformat(), self.work / "shop", model="claude-imaginary-9"))
        write_jsonl(self.projects / "p" / "s1.jsonl", rows)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["cost", *argv])
        return code, out.getvalue()

    def test_report_by_every_cut_and_as_json(self):
        self.seed()
        for by in cost.BY:
            code, text = self.run_cli("--by", by, "--since", "7d")
            self.assertEqual(code, 0)
            self.assertIn(f"Spend by {by}, last 7 days", text)
        self.assertIn("! No price for claude-imaginary-9", text)
        code, text = self.run_cli("--by", "task", "--json")
        data = json.loads(text)
        self.assertEqual({r["name"] for r in data["rows"]}, {f"T-{i}" for i in range(6)} | {cost.NO_TASK})
        self.assertEqual(data["total"]["turns"], 7)

    def test_budgets_exit_one_while_any_is_over(self):
        self.seed()
        self.assertEqual(self.run_cli("budgets")[0], 0)
        self.user("[[budgets]]\nmonthly_usd = 0.01\n")
        code, text = self.run_cli("budgets")
        self.assertEqual(code, 1)
        self.assertIn("✕ Over limit", text)

    def test_the_page_is_self_contained_and_holds_no_transcript_text(self):
        self.seed()
        self.user('[cost]\nplan_monthly_usd = 200\n[[budgets]]\nmonthly_usd = 0.5\n[[budgets]]\ntask = "*"\nmonthly_usd = 100\n')
        out = self.base / "costs.html"
        code, _ = self.run_cli("page", "--out", str(out))
        self.assertEqual(code, 0)
        page = out.read_text()
        self.assertNotIn(SECRET, page)
        self.assertNotIn("http", page.replace("http://www.w3.org", ""))  # nothing fetched
        for words in ("Projected this month", "Spending budgets", "Over limit", "Within budget", "more tasks",
                      "Where the spend went", "costliest sessions", "claude-imaginary-9", "No price",
                      "The cache served", "subscription $200/month"):
            self.assertIn(words, page)
        for state in ("over", "within"):  # a state is a word and a shape, never colour alone
            self.assertRegex(page, rf'data-state="{state}"><svg[^>]*>.*?</svg><span>\w')


class PageUnitTests(unittest.TestCase):
    def test_fold_keeps_the_total_and_names_the_rest(self):
        rows = [(f"g{i}", cost.Totals(cost=10 - i, turns=1)) for i in range(9)]
        folded = costpage.fold(rows, limit=6)
        self.assertEqual(len(folded), 6)
        self.assertEqual((folded[-1]["name"], folded[-1]["other"]), ("Other", 4))
        self.assertAlmostEqual(sum(r["t"].cost for r in folded), sum(t.cost for _, t in rows))
        self.assertEqual(costpage.share_title(folded, "x"), f"g0 drove {round(10 / 54 * 100)}% of spend")


if __name__ == "__main__":
    unittest.main()
