"""The factory engine: config, versions, manifests, scan (fake registries), deprecations, runner, schedules.
Run: python3 -m unittest discover -s tests"""

import contextlib
import io
import plistlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "factory"))

from sfactory import config, deprecations, guard, manifests, runner, schedule, upgrade, versions  # noqa: E402


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip("\n"))


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def track(self):
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)


class VersionTests(unittest.TestCase):
    def test_releases_and_prereleases_per_ecosystem(self):
        cases = {
            "pip": (["1.2.3", "2.0", "2.0.post1", "2025.8.3"], ["2.0.0rc1", "1.0.0a1", "1.0.0alpha1", "1.0.0.dev3", "1.0b2"]),
            "npm": (["19.3.1", "1.0"], ["6.0.0-insiders.1", "2.0.0-0", "1.0.0-pr.12", "4.0.0-next.3", "5.0.0-beta.1"]),
            "docker": (["16-alpine", "16-bookworm", "3.13-slim", "1.32.1"], ["17-rc-alpine", "3.15.0rc1-slim", "2.0-experimental"]),
        }
        for eco, (good, bad) in cases.items():
            for v in good:
                self.assertTrue(versions.is_release(v, eco), (eco, v))
            for v in bad:
                self.assertFalse(versions.is_release(v, eco), (eco, v))

    def test_plain_release_wins_a_tie_and_calver_is_never_major(self):
        self.assertEqual(versions.newer("5.9.0", ["6.0.0-insiders.1", "6.0.0"], "npm"), ["6.0.0"])
        self.assertEqual(versions.bump("2025.8.3", "2026.10.23"), "minor")
        self.assertEqual(versions.bump("2025.8.3", "2025.10.1"), "patch")

    def test_bump_classes(self):
        self.assertEqual(versions.bump("1.2.3", "1.2.4"), "patch")
        self.assertEqual(versions.bump("1.2.3", "1.4.0"), "minor")
        self.assertEqual(versions.bump("1.2.3", "2.0.0"), "major")
        self.assertEqual(versions.bump("0.13.0", "0.14.0"), "major")  # 0.x minor is breaking
        self.assertEqual(versions.bump("2.0.0", "1.9.0"), "none")

    def test_docker_tags_compete_only_with_the_same_shape(self):
        tags = ["17-alpine", "17", "16.4-alpine", "18-alpine", "17-bookworm", "19-alpine-rc"]
        self.assertEqual(versions.newer("16-alpine", tags, "docker"), ["17-alpine", "18-alpine"])


class ManifestTests(Repo):
    def test_reads_every_ecosystem(self):
        write(self.root, "requirements.txt", "requests==2.31.0\nuvicorn[standard]==0.30.1 ; python_version>'3'\n# c\n-e .\n")
        write(self.root, "pyproject.toml", '[project]\nname="x"\ndependencies=["httpx>=0.27.0", "rich"]\n')
        write(self.root, "web/package.json", '{"dependencies":{"react":"^18.2.0","local":"file:../x"},"devDependencies":{"vite":"~5.0.0"}}')
        write(self.root, "web/package-lock.json", '{"packages":{"node_modules/react":{"version":"18.3.1"}}}')
        write(self.root, "docker-compose.yml", "services:\n  db:\n    image: postgres:16-alpine\n  x:\n    image: ghcr.io/o/x:1.0\n  y:\n    image: app:${TAG}\n")
        write(self.root, "Dockerfile", "FROM python:3.13-slim AS base\n")
        self.track()
        got = {(d.ecosystem, d.name): d.current for d in manifests.read(self.root)}
        self.assertEqual(got, {
            ("pip", "requests"): "2.31.0", ("pip", "uvicorn"): "0.30.1", ("pip", "httpx"): "0.27.0",
            ("npm", "react"): "18.3.1", ("npm", "vite"): "5.0.0",
            ("docker", "postgres"): "16-alpine", ("docker", "python"): "3.13-slim",
        })


class ScanTests(Repo):
    def test_scan_classifies_with_fake_registries(self):
        write(self.root, "requirements.txt", "requests==2.31.0\nflask==3.0.0\nskipme==1.0\n")
        write(self.root, "docker-compose.yml", "services:\n  db:\n    image: postgres:16-alpine\n")
        write(self.root, ".factory.toml", '[upgrade]\nrisky = ["postgres"]\nignore = ["skipme"]\n')
        self.track()
        pages = {
            "https://pypi.org/pypi/requests/json": {"releases": {"2.31.0": [{}], "2.32.3": [{}], "3.0.0rc1": [{}]},
                                                    "info": {"project_urls": {"Changelog": "https://c", "Source": "https://github.com/psf/requests"}}},
            "https://pypi.org/pypi/flask/json": {"releases": {"3.0.0": [{}]}, "info": {"requires_dist": ["requests>=2", "x ; extra == 'dev'"]}},
        }

        def fetch(url):
            if "hub.docker.com" in url:
                return {"results": [{"name": "17-alpine"}, {"name": "16-alpine"}], "next": None}
            return pages[url]

        result = upgrade.scan(config.load(self.root), fetch=fetch)
        by = {c.name: c for c in result.candidates}
        self.assertEqual(set(by), {"requests", "postgres"})
        self.assertEqual((by["requests"].latest, by["requests"].bump, by["requests"].risky), ("2.32.3", "minor", False))
        self.assertIn("https://github.com/psf/requests/releases", by["requests"].notes)
        self.assertEqual(by["requests"].required_by, ["flask"])
        self.assertEqual(by["requests"].locations, ["requirements.txt:1"])
        self.assertTrue(by["postgres"].risky)
        self.assertEqual(result.candidates[-1].name, "postgres")  # risky sorts last
        self.assertEqual(result.up_to_date, 1)

    def test_a_failed_lookup_is_reported_not_fatal(self):
        write(self.root, "requirements.txt", "a==1.0\nb==1.0\n")
        self.track()

        def fetch(url):
            if "/a/" in url:
                raise OSError("boom")
            return {"releases": {"1.0": [{}], "1.1": [{}]}, "info": {}}

        result = upgrade.scan(config.load(self.root), fetch=fetch)
        self.assertEqual([c.name for c in result.candidates], ["b"])
        self.assertTrue(any("boom" in e for e in result.errors))


class DeprecationTests(Repo):
    def test_only_our_files_count(self):
        write(self.root, "app/core.py", "")
        write(self.root, "web/src/a.ts", "")
        root = self.root
        log = "\n".join([
            f"  {root}/app/core.py:12: DeprecationWarning: utcnow() is deprecated",
            "  /x/.venv/lib/python3.13/site-packages/lib.py:3: DeprecationWarning: theirs",
            "  app/core.py:20: FutureWarning: changes soon",
            "npm warn deprecated glob@7.2.3: Glob versions prior to v9 are no longer supported",
            "(node:123) [DEP0040] DeprecationWarning: The `punycode` module is deprecated.",
            "web/src/a.ts:4:7 - warning: 'foo' is deprecated.",
        ])
        found = deprecations.parse(log, root)
        ours = [(d.file, d.line) for d in found if d.kind == "ours"]
        self.assertEqual(ours, [("app/core.py", 12), ("app/core.py", 20), ("web/src/a.ts", 4)])
        self.assertEqual({d.kind for d in found}, {"ours", "dependency", "runtime"})


class ConfigAndTaskTests(Repo):
    def test_unknown_key_or_wrong_type_is_an_error(self):
        write(self.root, ".factory.toml", '[upgrade]\nriksy = ["postgres"]\n')
        with self.assertRaises(config.ConfigError):
            config.load(self.root)
        write(self.root, ".factory.toml", '[upgrade]\nmax_prs = "3"\n')
        with self.assertRaises(config.ConfigError):
            config.load(self.root)

    def test_teams_come_from_their_tables_and_upgrade_from_its_section(self):
        write(self.root, ".factory.toml",
              '[teams.qa]\nprompt = "/factory:qa"\nschedule = "0 2 * * *"\nwake_on = ["ci_red", "merged"]\n'
              '[upgrade]\nschedule = "0 6 * * 1"\n')
        teams = config.load(self.root).teams()
        self.assertEqual(sorted(teams), ["qa", "upgrade"])
        self.assertEqual((teams["qa"].schedule, teams["qa"].wake_on, teams["qa"].budget_minutes), ("0 2 * * *", ["ci_red", "merged"], 90))
        self.assertEqual((teams["upgrade"].prompt, teams["upgrade"].schedule), ("/factory:upgrade", "0 6 * * 1"))
        write(self.root, ".factory.toml", "[upgrade]\nenabled = false\n")
        self.assertEqual(config.load(self.root).teams(), {})

    def test_a_bad_team_is_an_error(self):
        for body in ('[teams.qa]\nschedule = "0 2 * * *"\n',  # no prompt
                     '[teams.qa]\nprompt = "/x"\nwake_on = ["ci_blue"]\n',  # unknown event
                     '[teams.qa]\nprompt = "/x"\nbudget = 5\n',  # unknown key
                     '[teams.qa]\nprompt = "/x"\nbudget_minutes = "5"\n',  # wrong type
                     '[teams.upgrade]\nprompt = "/x"\n',  # owned by [upgrade]
                     '[teams.QA]\nprompt = "/x"\n'):  # bad name
            write(self.root, ".factory.toml", body)
            with self.assertRaises(config.ConfigError, msg=body):
                config.load(self.root)

    def test_default_tools_deny_merge_and_default_branch_push(self):
        argv = runner.agent_argv(config.load(self.root), "/x")
        denied = argv[argv.index("--disallowedTools") + 1]
        allowed = argv[argv.index("--allowedTools") + 1]
        for rule in ("Bash(gh pr merge*)", "Bash(gh api graphql*)", "Bash(git push*--force*)"):
            self.assertIn(rule, denied)
        for blanket in ("Bash(git *)", "Bash(gh *)", "Bash(python3 *)", "Bash(npx *)", "Bash(make *)", "Bash(find *)"):
            self.assertNotIn(blanket, allowed.split(","))

    def test_task_values_are_shell_quoted(self):
        from sfactory import cli

        out = self.root / "out.txt"
        write(self.root, ".factory.toml", f'[tasks]\nnew = "printf %s {{title}} > {out}"\n')
        body = self.root / "b.md"
        body.write_text("x")
        title = 'Upgrade "postgres" $HOME 16 → 18; rm -rf x'
        self.assertEqual(cli.main(["task", "new", "--title", title, "--body-file", str(body), "--repo", str(self.root)]), 0)
        self.assertEqual(out.read_text(), title)


class GuardTests(unittest.TestCase):
    def test_blocks_merges_default_branch_pushes_and_writes(self):
        for cmd in ["gh pr merge 12 --squash", "git push origin main", "git push origin HEAD:main",
                    "make ci && git push -u origin main", "git push --force origin factory/x", "git push origin +factory/x",
                    "gh api -X PUT repos/o/r/pulls/3/merge", "gh api repos/o/r/merges -f base=main", "gh repo delete o/r",
                    "git push", "git push origin --delete factory/x", "git -C . push origin main", "git -c a=b push origin main",
                    "gh -R o/r pr merge 3", "gh api graphql -f query='mutation { mergePullRequest(input:{}) { clientMutationId } }'",
                    "find . -name x -exec rm {} ;", "git push -u origin HEAD"]:
            self.assertIsNotNone(guard.check(cmd, "main"), cmd)

    def test_head_push_only_from_a_factory_branch(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", "-b", "main", d], check=True)
            self.assertIsNotNone(guard.check("git push -u origin HEAD", "main", d))
            subprocess.run(["git", "checkout", "-q", "-b", "factory/upgrade-1-deps"], cwd=d, check=True)
            self.assertIsNone(guard.check("git push -u origin HEAD", "main", d))

    def test_fails_closed_inside_a_run(self):
        import os
        proc = subprocess.run([sys.executable, "-m", "sfactory.guard"], input="not json", text=True, capture_output=True,
                              env={**os.environ, "FACTORY_RUN": "x", "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "plugins" / "factory")})
        self.assertEqual(proc.returncode, 2)

    def test_allows_the_normal_flow(self):
        for cmd in ["git push -u origin factory/upgrade-20261031-deps", "gh pr create --base main --title x --body-file f",
                    "gh api repos/psf/requests/releases --paginate", "git merge origin/main", "git log origin/main..HEAD",
                    "gh pr checks 12 --watch", "git push origin factory/upgrade-1-maintenance-tasks"]:
            self.assertIsNone(guard.check(cmd, "main"), cmd)


class RunnerAndScheduleTests(Repo):
    def test_agent_command_is_configurable(self):
        write(self.root, ".factory.toml", '[agent]\ncommand = ["my-agent", "--task", "{prompt}"]\nextra_allowed_tools = ["Bash(./x *)"]\n')
        cfg = config.load(self.root)
        self.assertEqual(runner.agent_argv(cfg, "/factory:upgrade"), ["my-agent", "--task", "/factory:upgrade"])
        self.assertIn("Bash(./x *)", cfg.agent_allowed_tools)
        self.assertIn("Read", cfg.agent_allowed_tools)

    def test_cron_to_launchd_systemd_and_crontab(self):
        write(self.root, ".factory.toml", '[repo]\nname = "demo"\n[upgrade]\nschedule = "30 6 * * 1,4"\n')
        cfg = config.load(self.root)
        plist = plistlib.loads(schedule.launchd_plist(cfg, "upgrade", cfg.upgrade.schedule))
        self.assertEqual(plist["Label"], "dev.software-factory.demo.upgrade")
        self.assertEqual(plist["StartCalendarInterval"], [{"Minute": 30, "Hour": 6, "Weekday": 1}, {"Minute": 30, "Hour": 6, "Weekday": 4}])
        self.assertEqual(plist["ProgramArguments"][1:], ["run", "upgrade", "--repo", str(cfg.root)])
        _, timer = schedule.systemd_units(cfg, "upgrade", cfg.upgrade.schedule)
        self.assertIn("OnCalendar=Mon,Thu *-*-* 06:30:00", timer)
        self.assertTrue(schedule.crontab_line(cfg, "upgrade", cfg.upgrade.schedule).startswith("30 6 * * 1,4 "))

    def test_bad_cron_is_refused(self):
        for bad in ["* * *", "*/5 * * * *", "0 6 * * 1-5"]:
            with self.assertRaises(ValueError):
                schedule.parse_cron(bad)

    def _origin(self, files: dict[str, str]):
        """A bare origin with one commit on main holding `files`, and self.root cloned from it."""
        origin = self.root / "origin.git"
        seed = self.root / "seed"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        for rel, text in files.items():
            write(seed, rel, text)
        for cmd in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed", "--allow-empty"],
                    ["remote", "add", "origin", str(origin)], ["push", "-q", "origin", "main"]):
            subprocess.run(["git", *cmd], cwd=seed, check=True)
        repo = self.root / "repo"
        subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
        return repo

    def _run(self, repo: Path):
        home = self.root / "home"
        old = runner.HOME
        runner.HOME = home
        try:
            code = runner.run(config.load(repo), "upgrade")
        finally:
            runner.HOME = old
        trees = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout
        return code, trees.count("\n"), home

    def test_run_without_opt_in_does_nothing_and_cleans_up(self):
        repo = self._origin({"README.md": "x\n"})
        code, trees, _ = self._run(repo)
        self.assertEqual((code, trees), (0, 1))

    def test_run_uses_origin_config_runs_agent_and_cleans_up(self):
        cfg = '[worktree]\nsetup = ["touch setup-ran"]\n[agent]\ncommand = ["sh", "-c", "test -f setup-ran && echo agent-ran {prompt}"]\n'
        repo = self._origin({".factory.toml": cfg, ".gitignore": "setup-ran\n"})
        code, trees, home = self._run(repo)
        self.assertEqual((code, trees), (0, 1))  # nothing pushed -> worktree removed
        said = next((home / "logs").rglob("upgrade-*.agent.md")).read_text()
        self.assertIn("agent-ran /factory:upgrade", said)
        self.assertEqual(next((home / "logs").rglob("upgrade-*.report.md")).read_text(), said)  # last message = digest
        self.assertFalse(list((home / "runs").rglob("*.lock")))

    def test_the_report_file_in_the_worktree_is_collected_and_never_tracked(self):
        cfg = ('[agent]\ncommand = ["sh", "-c", "echo digest > $FACTORY_REPORT && test -z \\"$(git status --porcelain)\\" '
               '&& echo clean"]\n')
        repo = self._origin({".factory.toml": cfg})
        code, _, home = self._run(repo)
        self.assertEqual(code, 0)
        self.assertIn("clean", next((home / "logs").rglob("upgrade-*.agent.md")).read_text())
        self.assertEqual(next((home / "logs").rglob("upgrade-*.report.md")).read_text(), "digest\n")

    def test_unpushed_work_is_kept_not_deleted(self):
        cfg = ('[agent]\ncommand = ["sh", "-c", "git checkout -q -b $FACTORY_BRANCH-deps && echo x > f && git add f && '
               'git -c user.name=t -c user.email=t@t commit -qm wip"]\n')
        repo = self._origin({".factory.toml": cfg})
        code, trees, _ = self._run(repo)
        self.assertEqual((code, trees), (0, 2))  # the worktree with the unpushed commit survives

    def test_stale_lock_is_taken_over_live_one_is_not(self):
        lock = self.root / "x.lock"
        lock.write_text("999999")  # no such process
        self.assertTrue(runner.acquire(lock))
        self.assertFalse(runner.acquire(lock))  # held by us, alive
        lock.unlink()

    def test_timeout_kills_the_agent(self):
        log = (self.root / "t.log").open("w")
        self.assertEqual(runner._sh(["sleep", "30"], self.root, {"PATH": "/usr/bin:/bin"}, log, timeout=0.5), runner._TIMED_OUT)

    def test_dry_run_shows_the_default_branch_config_not_the_checkout(self):
        repo = self._origin({".factory.toml": '[worktree]\nsetup = ["echo from-main"]\n'})
        (repo / ".factory.toml").unlink()  # the checkout lags; the run (and its preview) follow origin
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(runner.run(config.load(repo), "upgrade", dry_run=True), 0)
        self.assertIn("setup     echo from-main", out.getvalue())
        self.assertNotIn("not opted in", out.getvalue())

    def test_dry_run_changes_nothing(self):
        cfg = config.load(self.root)
        self.assertEqual(runner.run(cfg, "upgrade", dry_run=True), 0)
        self.assertEqual(subprocess.run(["git", "worktree", "list"], cwd=self.root, capture_output=True, text=True).stdout.count("\n"), 1)


if __name__ == "__main__":
    unittest.main()
