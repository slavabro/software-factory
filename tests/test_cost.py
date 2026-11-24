"""Agent spend: the transcript reader and store (sfactory/usage.py), the org (org.py), attribution, budgets
(cost.py, costconfig.py), the run gate and per-run cap (runner.py), landed tasks, runtime usage, the SessionStart
line and the page (costpage.py). Every transcript here is synthetic.
Run: python3 -m unittest discover -s tests"""

import contextlib
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "factory"))

from sfactory import cli, config, cost, costconfig, costpage, org, runner, usage  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
SECRET = "PRIVATE-PROMPT-TEXT-never-on-the-page"


def at(minute: int, day: int = 17) -> str:
    return f"2026-11-{day:02d}T10:{minute:02d}:00.000Z"


def turn(mid, ts, cwd, *, sid="s1", branch="main", model="claude-opus-5-5", inp=10, out=100, cw=0, cw1h=0, cr=0,
         tools=(), skill=None, speed="standard", geo="global", searches=0, side=False, agent_id=None):
    content = [{"type": "text", "text": SECRET}] + [
        {"type": "tool_use", "name": t, "input": {"cmd": SECRET}} for t in tools]
    if skill:
        content.append({"type": "tool_use", "name": "Skill", "input": {"skill": skill, "args": SECRET}})
    d = {
        "type": "assistant", "sessionId": sid, "cwd": str(cwd), "gitBranch": branch, "timestamp": ts,
        "isSidechain": side, "uuid": f"u-{mid}-{len(tools)}",
        "message": {"id": mid, "model": model, "role": "assistant", "content": content, "usage": {
            "input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": cw,
            "cache_creation": {"ephemeral_5m_input_tokens": cw - cw1h, "ephemeral_1h_input_tokens": cw1h},
            "cache_read_input_tokens": cr, "speed": speed, "inference_geo": geo,
            "server_tool_use": {"web_search_requests": searches}}},
    }
    if agent_id:
        d["agentId"] = agent_id
    return d


def typed(ts, cwd, command, sid="s1"):
    """The principal types a slash command (Claude Code records it as a user message)."""
    return {"type": "user", "sessionId": sid, "cwd": str(cwd), "timestamp": ts, "message": {
        "role": "user", "content": f"<command-name>/{command}</command-name><command-args>{SECRET}</command-args>"}}


def write_jsonl(path: Path, rows, append=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a" if append else "w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


class Sandbox(unittest.TestCase):
    """Isolated projects dir, store, run roots and user config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.projects = self.base / "projects"
        self.user_cfg = self.base / "user" / "factory.toml"
        self.work = self.base / "work"
        self.work.mkdir()
        patches = {usage: {"PROJECTS": self.projects, "DB": self.base / "usage.db", "CACHE": self.base / "cache",
                           "RUNS": self.base / "runs", "ENGINE_WORKTREES": self.base / "state" / "worktrees"},
                   costconfig: {"USER_CONFIG": self.user_cfg}}
        for target, values in patches.items():
            for name, v in values.items():
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
        return usage.records(**kw)

    def session(self, rows, name="s1"):
        write_jsonl(self.projects / "p" / f"{name}.jsonl", rows)


def test_org(**maps) -> org.Org:
    o = org.load(None, declarations={})
    for kind, names in maps.items():
        o.maps[kind].update(names)
    return o


class StoreTests(Sandbox):
    def test_streamed_blocks_are_one_turn_with_every_tool(self):
        self.session([
            turn("m1", at(0), self.work, tools=["Read"]), turn("m1", at(0), self.work, tools=["Bash", "Read"]),
            turn("m2", at(1), self.work, model="<synthetic>"),
        ])
        recs = self.recs()
        self.assertEqual([(r.message_id, r.tool_names, r.input_tokens, r.output_tokens) for r in recs],
                         [("m1", ("Bash", "Read"), 10, 100)])

    def test_subagents_carry_their_type_and_dedupe_across_files(self):
        self.session([turn("m1", at(0), self.work)])
        sub = self.projects / "p" / "s1" / "subagents" / "agent-a1.jsonl"
        write_jsonl(sub, [turn("m2", at(2), self.work, side=True), turn("m1", at(0), self.work)])
        sub.with_suffix(".meta.json").write_text(json.dumps({"agentType": "Explore", "description": SECRET}))
        recs = self.recs()
        self.assertEqual([(r.message_id, r.is_subagent, r.subagent_type, r.agent) for r in recs],
                         [("m1", False, "", "interactive"), ("m2", True, "Explore", "subagent:Explore")])

    def test_unchanged_files_are_not_read_appends_are_and_nothing_private_is_stored(self):
        path = self.projects / "p" / "s1.jsonl"
        write_jsonl(path, [turn("m1", at(0), self.work)])
        self.assertEqual(usage.scan(), 1)
        self.assertEqual(usage.scan(), 0)
        write_jsonl(path, [turn("m2", at(5), self.work)], append=True)
        self.assertEqual(usage.scan(), 1)
        self.assertEqual(len(self.recs()), 2)
        write_jsonl(path, [turn("m3", at(9), self.work)])  # rewritten, shorter: read again from the start
        self.assertEqual([r.message_id for r in self.recs()], ["m3"])
        for f in self.base.glob("usage.db*"):
            self.assertNotIn(SECRET.encode(), f.read_bytes())

    def test_since_until_and_the_shared_row_format(self):
        self.session([turn("m1", at(0, day=1), self.work), turn("m2", at(0), self.work)])
        self.assertEqual([r.message_id for r in self.recs(since=dt.datetime(2026, 11, 10, tzinfo=UTC))], ["m2"])
        self.assertEqual([r.message_id for r in self.recs(until=dt.datetime(2026, 11, 10, tzinfo=UTC))], ["m1"])
        row = self.recs()[0].as_row()
        self.assertEqual(list(row), list(usage.USAGE_FIELDS))
        self.assertEqual((row["department"], row["agent"], row["process"]), ("direct", "interactive", ""))


class StageTests(Sandbox):
    def depts(self, o):
        return [(r.message_id, r.department, r.process) for r in self.recs(org=o)]

    def test_the_last_mapped_skill_sets_the_stage_and_clear_resets_it(self):
        o = test_org(skills={"design-task": "design", "review-code": "quality"})
        self.session([
            turn("a", at(0), self.work),
            turn("b", at(1), self.work, skill="design-task"),  # the turn that calls the skill is still before it
            turn("c", at(2), self.work),
            turn("d", at(3), self.work, skill="some-unmapped-skill"),  # not in the org: the stage holds
            turn("e", at(4), self.work),
            typed(at(5), self.work, "review-code"),
            turn("f", at(6), self.work),
            typed(at(7), self.work, "clear"),
            turn("g", at(8), self.work),
        ])
        self.assertEqual(self.depts(o), [
            ("a", "direct", ""), ("b", "direct", ""), ("c", "design", "design-task"), ("d", "design", "design-task"),
            ("e", "design", "design-task"), ("f", "quality", "review-code"), ("g", "direct", "")])

    def test_a_changed_org_rebuckets_history_without_a_rescan(self):
        self.session([turn("a", at(0), self.work, skill="land"), turn("b", at(1), self.work)])
        self.assertEqual(self.depts(test_org())[1][1], "direct")
        with mock.patch.object(usage, "scan", side_effect=AssertionError("no re-scan")):
            self.assertEqual(usage.records(org=test_org(skills={"land": "release"}), rescan=False)[1].department,
                             "release")

    def test_subagents_inherit_the_stage_they_were_spawned_in_unless_their_type_is_declared(self):
        o = test_org(skills={"design-task": "design"}, agents={"code-reviewer": "quality"})
        self.session([turn("a", at(0), self.work, skill="design-task"), turn("b", at(1), self.work),
                      typed(at(5), self.work, "clear"), turn("z", at(6), self.work)])
        subs = self.projects / "p" / "s1" / "subagents"
        write_jsonl(subs / "agent-x1.jsonl", [turn("x", at(2), self.work, side=True), turn("x2", at(7), self.work)])
        (subs / "agent-x1.meta.json").write_text(json.dumps({"agentType": "general-purpose"}))
        write_jsonl(subs / "agent-x2.jsonl", [turn("y", at(3), self.work, side=True)])
        (subs / "agent-x2.meta.json").write_text(json.dumps({"agentType": "code-reviewer"}))
        got = dict((m, (d, p)) for m, d, p in self.depts(o))
        self.assertEqual(got["x"], ("design", "design-task"))
        self.assertEqual(got["x2"], ("design", "design-task"))  # spawned before the /clear: keeps its stage
        self.assertEqual(got["y"], ("quality", "code-reviewer"))
        self.assertEqual(got["z"], ("direct", ""))

    def test_factory_runs_take_their_capability_and_repo_mappings_stay_in_their_repo(self):
        o = test_org(capabilities={"upgrade": "upkeep"})
        o.project_maps["shop"] = {"skills": {"design-task": "design"}}
        run = usage.RUNS / "shop" / "upgrade-20261117-0600"
        shop = self.git_repo("shop", '[repo]\nname = "shop"\n')
        blog = self.git_repo("blog")
        self.session([turn("r", at(0), run, skill="design-task"), turn("r2", at(1), run)], "run")
        self.session([turn("s", at(0), shop, skill="design-task", sid="s2"), turn("s2", at(1), shop, sid="s2")], "s2")
        self.session([turn("b", at(0), blog, skill="design-task", sid="s3"), turn("b2", at(1), blog, sid="s3")], "s3")
        got = {r.message_id: (r.project, r.department, r.agent) for r in self.recs(org=o)}
        self.assertEqual(got["r2"], ("shop", "upkeep", "run:upgrade"))
        self.assertEqual(got["s2"], ("shop", "design", "interactive"))
        self.assertEqual(got["b2"], ("blog", "direct", "interactive"))


class OrgTests(Sandbox):
    def test_the_factorys_own_skills_declare_their_departments(self):
        declared = org.skill_declarations()
        self.assertEqual(declared["factory:upgrade"][0], "upkeep")
        self.assertEqual(declared["factory:skills-review"][0], "upkeep")
        self.assertEqual(declared["fleet"][0], "cos")
        o = org.load()
        self.assertEqual([d.id for d in o.departments],
                         ["cos", "product", "design", "engineering", "quality", "release", "upkeep", "direct"])
        self.assertEqual(o.capability("upgrade"), "upkeep")

    def test_the_user_file_extends_the_org_and_a_repo_only_maps(self):
        o = org.load({"departments": [{"id": "design", "name": "UX"}, {"id": "data", "name": "Data"}],
                      "skills": {"etl": "data"}}, declarations={})
        self.assertEqual((o.name_of("design"), o.skill("etl"), o.department("design").agents), ("UX", "data", ["Plan"]))
        with self.assertRaises(org.OrgError):
            org.load({"skills": {"x": "nowhere"}}, declarations={})
        with self.assertRaises(config.ConfigError) as e:
            config.parse(self.base, '[[org.departments]]\nid = "x"\n', Path("x"))
        self.assertIn("a repo maps its own skills", str(e.exception))
        cfg = config.parse(self.base, '[repo]\nname = "shop"\n[org.skills]\ndesign-task = "design"\n', Path("x"))
        s = cost.Settings(costconfig.CostConfig(), {"shop": cfg.cost})
        self.assertEqual((s.org.skill("design-task", "shop"), s.org.skill("design-task", "blog")), ("design", None))


class PriceTests(unittest.TestCase):
    def test_every_token_kind_fast_us_only_and_web_search(self):
        p = usage.DEFAULT_PRICES
        usd, priced = p.cost("claude-opus-5-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertTrue(priced)
        self.assertAlmostEqual(usd, 4 + 20 + 5 + 8 + 0.2)
        self.assertAlmostEqual(p.cost("claude-opus-5-5", 1_000_000, 0, 0, 0, 0, speed="fast")[0], 8)
        self.assertAlmostEqual(p.cost("claude-opus-5-5", 1_000_000, 0, 0, 0, 0, geo="us")[0], 4.4)
        self.assertAlmostEqual(p.cost("claude-opus-5-5", 0, 0, 0, 0, 0, web_searches=3)[0], 0.03)

    def test_haiku_long_prompts_unknown_models_names_and_overrides(self):
        p = usage.DEFAULT_PRICES
        self.assertAlmostEqual(p.cost("claude-haiku-5-5", 100_000, 0, 0, 0, 0)[0], 0.01)
        self.assertAlmostEqual(p.cost("claude-haiku-5-5", 100_001, 0, 0, 0, 0)[0], 100_001 * 0.5 / 1e6)
        self.assertEqual(p.cost("claude-imaginary-9", 1000, 1000, 0, 0, 0), (0.0, False))
        self.assertEqual(usage.canonical_model("us.anthropic.claude-opus-4-8-20260101[1m]"), "claude-opus-4-8")
        q = p.with_overrides({"claude-opus-5-5": {"input": 1.0}})
        self.assertEqual((q.models["claude-opus-5-5"].input, q.models["claude-opus-5-5"].output), (1.0, 20))
        with self.assertRaises(ValueError):
            p.with_overrides({"claude-new": {"input": 1.0}})


class ConfigTests(Sandbox):
    def test_budgets_speak_the_spending_rules_vocabulary_and_default_hard_where_money_is_guarded(self):
        cfg = config.parse(self.base, textwrap.dedent("""
            [repo]
            name = "shop"
            [[budgets]]
            daily_spend_usd = 50
            [[budgets]]
            department = "engineering"
            monthly_spend_usd = 400
            [[budgets]]
            capability = "upgrade"
            run_spend_usd = 15
            [[budgets]]
            runtime = true
            monthly_spend_usd = 30
            [[budgets]]
            department = "design"
            monthly_call_count = 1000
            hard = true
            """), Path("x"))
        got = [(b.label, b.project, b.hard) for b in cfg.cost.budgets]
        self.assertEqual(got, [("project shop", "shop", True), ("department engineering · project shop", "shop", False),
                               ("project shop · capability upgrade", "shop", True),
                               ("project shop (runtime)", "shop", True),
                               ("department design · project shop", "shop", True)])
        user = costconfig.parse({"budgets": [{"department": "upkeep", "monthly_spend_usd": 5},
                                             {"agent": "interactive", "daily_tokens": 5}, {"task": "*", "daily_spend_usd": 5}]},
                                "u")
        self.assertEqual([b.hard for b in user.budgets], [False, False, False])

    def test_typos_and_impossible_budgets_are_refused(self):
        bad = {
            "[cost]\nprize = 1\n": "unknown key cost.prize",
            "[[budgets]]\nmonthly_spend_usd = 5\ndepartment = 'a'\nagent = 'b'\n": "one of department, agent",
            "[[budgets]]\ndepartment = 'a'\n": "at least one of",
            "[[budgets]]\nmonthly_usd = 5\n": "unknown monthly_usd",
            "[[budgets]]\nrun_spend_usd = 5\n": "set capability",
            "[[budgets]]\nproject = 'other'\nmonthly_spend_usd = 5\n": "only its own spend",
            "[[budgets]]\nmonthly_spend_usd = 5\nwarn = 95\ndanger = 90\n": "warn < danger",
            "[cost]\ntask_pattern = '('\n": "not a valid regex",
            "[efficiency]\nsources = []\n": "unknown key efficiency.sources",
            "[budget]\n": "unknown section",
        }
        for text, msg in bad.items():
            with self.assertRaises(config.ConfigError, msg=text) as e:
                config.parse(self.base, text, Path("x"))
            self.assertIn(msg, str(e.exception))
        self.user("[verify]\ncommands = []\n")
        with self.assertRaises(costconfig.CostConfigError):
            costconfig.load_user()

    def test_task_ids_from_branch_then_worktree_and_never_from_stamps(self):
        pat = re.compile(usage.DEFAULT_TASK_PATTERN)
        cases = {("factory/f-33-costs-budgets", ""): "F-33", ("slavabro/1.106d-impl", ""): "1.106d",
                 ("main", "proj-7-x"): "PROJ-7", ("factory/upgrade-20261109-0600", "upgrade-20261109-0600"): usage.NO_TASK,
                 ("worktree-agent-a1a417940a7da5ede", "agent-a1a417940a7da5ede"): usage.NO_TASK,
                 ("release/v0.2", ""): usage.NO_TASK}
        for (branch, wt), want in cases.items():
            self.assertEqual(usage.task_of(branch, wt, pat), want, branch)


def rec(cost_usd, ts=NOW, project="shop", department="engineering", agent="interactive", task="T-1", tokens=0,
        session="s", capability=None, source="session", branch=""):
    return usage.UsageRecord(ts=ts, project=project, department=department, agent=agent, process="", task=task,
                             model="m", input_tokens=tokens, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0,
                             cost_usd=cost_usd, session_id=session, capability=capability, source=source, branch=branch)


class BudgetTests(unittest.TestCase):
    def test_states_follow_the_product_thresholds(self):
        self.assertEqual([cost.state_of(p, 75, 90) for p in (10, 75, 90, 99.9, 100, 130)],
                         ["within", "approaching", "near", "near", "over", "over"])
        m = cost.meters([rec(249.71)], [costconfig.Budget(monthly_spend_usd=250)], NOW)[0]
        self.assertEqual((m.state, m.pct_text), (cost.NEAR, "99%"))

    def test_scopes_windows_globs_calls_runs_and_runtime(self):
        yesterday = NOW - dt.timedelta(days=1)
        rows = [rec(40, yesterday), rec(30, task="T-2"), rec(5, project="blog", department="design", task="T-9"),
                rec(6, capability="upgrade", agent="run:upgrade", department="upkeep", session="r1", ts=yesterday),
                rec(9, capability="upgrade", agent="run:upgrade", department="upkeep", session="r2"),
                rec(3, source="runtime", department="investment")]
        budgets = [
            costconfig.Budget(department="engineering", project="shop", monthly_spend_usd=60, daily_spend_usd=20),
            costconfig.Budget(task="T-*", monthly_spend_usd=35),
            costconfig.Budget(project="blog", monthly_call_count=1),
            costconfig.Budget(capability="upgrade", run_spend_usd=8, hard=True),
            costconfig.Budget(runtime=True, department="investment", monthly_spend_usd=2),
        ]
        found = {(m.label, m.window, m.unit): m for m in cost.meters(rows, budgets, NOW)}
        self.assertEqual(found[("department engineering · project shop", "this month", "usd")].used, 70)
        self.assertEqual(found[("department engineering · project shop", "today", "usd")].state, cost.OVER)
        self.assertEqual(found[("task T-1", "this month", "usd")].state, cost.OVER)
        self.assertEqual(found[("task T-2", "this month", "usd")].state, cost.APPROACHING)
        self.assertEqual(found[("project blog", "this month", "calls")].state, cost.OVER)
        self.assertEqual(found[("capability upgrade", "latest run", "usd")].used, 9)  # the latest run, not the month
        self.assertEqual(found[("department investment (runtime)", "this month", "usd")].used, 3)

    def test_a_run_is_stopped_only_by_hard_budgets_over_the_limit_that_cover_it(self):
        settings = cost.Settings(costconfig.CostConfig(), org=test_org(capabilities={"upgrade": "upkeep"}))
        probe = cost.run_probe(settings, "shop", "upgrade")
        self.assertEqual((probe.department, probe.agent), ("upkeep", "run:upgrade"))
        mk = lambda **kw: cost.Meter(**{**dict(label="x", window="this month", unit="usd", used=2, limit=1, pct=200,  # noqa: E731
                                            state=cost.OVER, hard=True, warn=75, danger=90, scope={}, runtime=False,
                                            source="f"), **kw})
        self.assertEqual(len(cost.blockers([mk(), mk(scope={"project": "shop", "department": "upkeep"})], probe)), 2)
        self.assertEqual(cost.blockers([mk(hard=False), mk(state=cost.NEAR), mk(scope={"project": "blog"}),
                                        mk(scope={"department": "design"}), mk(scope={"task": "T-1"}),
                                        mk(runtime=True), mk(window="latest run")], probe), [])


class GateTests(Sandbox):
    def seed(self, cwd=None):
        repo = self.git_repo("shop")
        cwd = cwd or usage.RUNS / "shop" / "upgrade-20261117-0600"
        now = dt.datetime.now(UTC).isoformat()
        self.session([turn("m1", now, cwd, inp=1_000_000, out=0)])  # $4
        return repo

    def test_factory_run_refuses_to_start_over_a_hard_limit_and_says_why(self):
        repo = self.seed()
        self.user('[[budgets]]\ncapability = "upgrade"\nmonthly_spend_usd = 1\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(runner, "HOME", self.base / "h"):
            self.assertEqual(runner.run(config.load(repo), "upgrade"), 1)
        self.assertIn("not started — budget ✕ Over limit: capability upgrade, this month — $4.00 of $1.00",
                      out.getvalue())
        self.assertFalse((self.base / "h" / "runs").exists())  # nothing was created
        self.assertEqual(cost.run_blockers(repo, "efficiency"), [])  # another capability: not covered

    def test_department_budgets_steer_and_unrelated_ones_let_it_run(self):
        repo = self.seed()
        self.user('[[budgets]]\ndepartment = "upkeep"\nmonthly_spend_usd = 1\n'
                  '[[budgets]]\nproject = "blog"\nmonthly_spend_usd = 1\n')
        self.assertEqual(cost.run_blockers(repo, "upgrade"), [])
        self.assertEqual(cost.run_blockers(self.git_repo("nobudgets"), "upgrade"), [])

    def test_a_run_over_its_per_run_cap_is_stopped_while_it_runs(self):
        repo = self.git_repo("shop", '[[budgets]]\ncapability = "upgrade"\nrun_spend_usd = 3\n')
        wt = self.base / "wt" / "upgrade-1"
        watch = runner.run_budget_watch(config.load(repo), "upgrade", wt)
        self.assertIsNone(watch())
        write_jsonl(self.projects / usage.encode_project_dir(wt) / "s.jsonl",
                    [turn("m1", at(0), wt, inp=1_000_000, out=0)])
        self.assertIn("spent $4.00 of its $3.00 per-run cap", watch())
        with open(self.base / "run.log", "w+") as log:
            code = runner._sh([sys.executable, "-c", "import time; time.sleep(30)"], self.base, dict(os.environ), log,
                              timeout=60, watch=watch, every=0.1)
        self.assertEqual(code, runner._OVER_BUDGET)
        self.assertIn("stopped — spent $4.00", (self.base / "run.log").read_text())
        self.assertIsNone(runner.run_budget_watch(config.load(self.git_repo("free")), "upgrade", wt))


class LandedTests(Sandbox):
    def test_a_task_lands_when_any_of_its_branches_merged(self):
        rows = [rec(10, task="T-1", branch="t-1-a"), rec(5, task="T-1", branch="t-1-b", department="quality"),
                rec(7, task="T-2", branch="t-2"), rec(2, task=usage.NO_TASK), rec(4, project="blog", task="B-1")]
        status = cost.task_status(rows, {"shop": "/shop"}, merged=lambda root: {"t-1-b"})
        self.assertEqual(status, {("shop", "T-1"): cost.LANDED, ("shop", "T-2"): cost.NOT_LANDED,
                                  ("blog", "B-1"): cost.NOT_CHECKED})
        by = {r.department: r for r in cost.cost_per_landed_task(rows, status)}
        self.assertEqual((by["engineering"].landed, by["engineering"].landed_cost, by["engineering"].per_landed), (1, 10, 10))
        self.assertEqual((by["engineering"].open, by["engineering"].open_cost, by["engineering"].no_task_cost), (1, 7, 2))
        self.assertEqual((by["engineering"].unchecked, by["quality"].landed_cost), (1, 5))

    def test_merged_branches_are_asked_once_and_cached(self):
        calls = []
        gh = lambda root: calls.append(root) or [{"headRefName": "t-1", "number": 3, "mergedAt": "2026-11-01"}]  # noqa: E731
        self.assertEqual(cost.merged_branches("/shop", gh=gh), {"t-1"})
        self.assertEqual(cost.merged_branches("/shop", gh=gh), {"t-1"})
        self.assertEqual(len(calls), 1)
        self.assertIsNone(cost.merged_branches("/blog", gh=lambda root: None))  # gh can't tell: not checked


class RuntimeTests(Sandbox):
    def test_runtime_rows_are_read_and_priced_and_failures_say_only_the_exit_code(self):
        rows = [{"ts": "2026-11-17T09:00:00Z", "model": "claude-opus-5-5", "department": "investment", "agent": "analyst",
                 "input_tokens": 1_000_000}, {"ts": "2026-11-17T09:01:00Z", "model": "m", "cost_usd": 0.5}, "not json"]
        src = self.base / "rows.jsonl"
        src.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n")
        got = usage.runtime_records(f"cat {src} # since {{since}}", "shop", NOW - dt.timedelta(days=1))
        self.assertEqual([(r.department, r.cost_usd, r.source, r.project) for r in got],
                         [("investment", 4.0, "runtime", "shop"), ("", 0.5, "runtime", "shop")])
        with self.assertRaises(usage.RuntimeUsageError) as e:
            usage.runtime_records(f"echo {SECRET} >&2; exit 3", "shop", NOW)
        self.assertEqual(str(e.exception), "shop: the runtime usage command exited 3")


class HookTests(Sandbox):
    def seed(self):
        self.git_repo("shop")
        self.session([turn("m1", dt.datetime.now(UTC).isoformat(), self.work / "shop", inp=1_000_000, out=0)])

    def test_the_hook_is_silent_when_healthy_and_reads_only_the_saved_state(self):
        self.seed()
        self.assertEqual(cost.hook(spawn=False), "")  # no state yet
        self.user("[[budgets]]\nmonthly_spend_usd = 100\n")
        cost.evaluate(cost.load())
        self.assertEqual(cost.hook(spawn=False), "")
        self.user("[[budgets]]\nmonthly_spend_usd = 4.2\n")
        cost.evaluate(cost.load())
        with mock.patch.object(usage, "records", side_effect=AssertionError("the hook must not scan")):
            started = time.monotonic()
            line = cost.hook(spawn=False)
            self.assertLess(time.monotonic() - started, 1)
        self.assertIn("! Near limit: all spend, this month — $4.00 of $4.20 (95%)", line)

    def test_the_hook_refreshes_in_the_background_only_when_stale(self):
        self.seed()
        self.user("[[budgets]]\nmonthly_spend_usd = 100\n")
        cost.evaluate(cost.load())
        with mock.patch.object(cost, "_refresh_in_background") as bg:
            cost.hook()
            bg.assert_not_called()
            cost.hook(now=dt.datetime.now().astimezone() + dt.timedelta(hours=7))
            bg.assert_called_once()


class CommandTests(Sandbox):
    def seed(self):
        shop = self.git_repo("shop", '[repo]\nname = "shop"\n[org.skills]\nbuild-it = "engineering"\n'
                                     f'[efficiency]\nruntime_usage = "cat {self.base}/rt.jsonl"\n')
        (self.base / "rt.jsonl").write_text(json.dumps({
            "ts": dt.datetime.now(UTC).isoformat(), "model": "claude-opus-5-5", "department": "investment",
            "input_tokens": 1000}) + "\n")
        today = dt.datetime.now(UTC).replace(microsecond=0)
        rows = [turn("k", (today - dt.timedelta(days=6, minutes=1)).isoformat(), shop, skill="build-it")]
        for i in range(6):
            ts = (today - dt.timedelta(days=i)).isoformat()
            rows.append(turn(f"m{i}", ts, shop, branch=f"feature/t-{i}", inp=100_000, cw=50_000, cw1h=10_000,
                             cr=400_000, tools=["Bash"]))
        rows.append(turn("mx", today.isoformat(), shop, model="claude-imaginary-9"))
        self.session(rows)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["cost", *argv])
        return code, out.getvalue()

    def test_report_by_every_cut_and_as_json(self):
        self.seed()
        for by in cost.BY:
            code, text = self.run_cli("--by", by, "--since", "7d")
            self.assertEqual(code, 0)
            self.assertIn(f"Spend by {by}, last 7 days", text)
        self.assertIn("! No price for claude-imaginary-9", text)
        data = json.loads(self.run_cli("--by", "department", "--json")[1])
        # the turn that invokes the repo's skill is still Direct; every turn after it is Engineering
        self.assertEqual({r["name"]: r["calls"] for r in data["rows"]}, {"Engineering": 7, "Direct": 1})
        self.assertEqual(data["total"]["calls"], 8)
        self.assertIn("build-it → engineering", self.run_cli("org")[1])

    def test_budgets_exit_one_while_any_is_over(self):
        self.seed()
        self.assertEqual(self.run_cli("budgets")[0], 0)
        self.user("[[budgets]]\nmonthly_spend_usd = 0.01\n")
        code, text = self.run_cli("budgets")
        self.assertEqual(code, 1)
        self.assertIn("✕ Over limit: all spend, this month", text)
        self.assertIn("alert only", text)

    def test_the_page_is_self_contained_and_holds_no_transcript_text(self):
        self.seed()
        self.user('[cost]\nplan_monthly_usd = 200\n[[budgets]]\nmonthly_spend_usd = 0.5\n'
                  '[[budgets]]\ntask = "*"\nmonthly_spend_usd = 100\n')
        out = self.base / "costs.html"
        with mock.patch.object(cost, "_gh_merged", return_value=[{"headRefName": "feature/t-1", "number": 1}]):
            self.assertEqual(self.run_cli("page", "--out", str(out))[0], 0)
        page = out.read_text()
        self.assertNotIn(SECRET, page)
        self.assertNotIn("http", page.replace("http://www.w3.org", ""))  # nothing fetched
        for words in ("Projected this month", "Spending budgets", "Over limit", "Within budget", "more task value",
                      "Engineering took 100% of spend; shop 100%", "1 task landed, at", "Where the spend went",
                      "costliest sessions", "claude-imaginary-9", "No price",
                      "Run: the products spent", "investment", "The cache served", "subscription $200/month"):
            self.assertIn(words, page)
        for state in ("over", "within"):  # a state is a word and a shape, never colour alone
            self.assertRegex(page, rf'data-state="{state}"><svg[^>]*>.*?</svg><span>\w')


class PageUnitTests(unittest.TestCase):
    def test_fold_keeps_the_total_and_names_the_rest(self):
        rows = [(f"g{i}", cost.Totals(cost=10 - i, calls=1)) for i in range(9)]
        folded = costpage.fold(rows, limit=6)
        self.assertEqual(len(folded), 6)
        self.assertEqual((folded[-1]["name"], folded[-1]["other"]), ("Other", 4))
        self.assertAlmostEqual(sum(r["t"].cost for r in folded), sum(t.cost for _, t in rows))
        self.assertEqual(costpage.share_title(folded, "x"), f"g0 drove {round(10 / 54 * 100)}% of spend")


if __name__ == "__main__":
    unittest.main()
