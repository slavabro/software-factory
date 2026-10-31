"""The factory engine: config, versions, manifests, scan (fake registries), deprecations, runner, schedules.
Run: python3 -m unittest discover -s tests"""

import plistlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "factory"))

from sfactory import config, deprecations, manifests, runner, schedule, upgrade, versions  # noqa: E402


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
    def test_releases_and_prereleases(self):
        for v in ["1.2.3", "16-alpine", "16-bookworm", "3.13-slim", "2.0"]:
            self.assertTrue(versions.is_release(v), v)
        for v in ["2.0.0rc1", "1.0.0a1", "5.0.0-beta.1", "1.0.0.dev3", "4.0.0-next.3"]:
            self.assertFalse(versions.is_release(v), v)

    def test_bump_classes(self):
        self.assertEqual(versions.bump("1.2.3", "1.2.4"), "patch")
        self.assertEqual(versions.bump("1.2.3", "1.4.0"), "minor")
        self.assertEqual(versions.bump("1.2.3", "2.0.0"), "major")
        self.assertEqual(versions.bump("0.13.0", "0.14.0"), "major")  # 0.x minor is breaking
        self.assertEqual(versions.bump("2.0.0", "1.9.0"), "none")

    def test_docker_tags_compete_only_with_the_same_shape(self):
        tags = ["17-alpine", "17", "16.4-alpine", "18-alpine", "17-bookworm", "19-alpine-rc"]
        self.assertEqual(versions.newer("16-alpine", tags, same_shape=True), ["17-alpine", "18-alpine"])


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
            "https://pypi.org/pypi/flask/json": {"releases": {"3.0.0": [{}]}, "info": {}},
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

    def test_dry_run_changes_nothing(self):
        cfg = config.load(self.root)
        self.assertEqual(runner.run(cfg, "upgrade", dry_run=True), 0)
        self.assertEqual(subprocess.run(["git", "worktree", "list"], cwd=self.root, capture_output=True, text=True).stdout.count("\n"), 1)


if __name__ == "__main__":
    unittest.main()
