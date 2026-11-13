"""`factory engine` install helpers (sfactory/engine.py): standard library only, no Docker or Temporal needed."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "factory"))

from sfactory import engine  # noqa: E402


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class EngineInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        patches = {
            "CONFIG_DIR": self.tmp / "config", "SHARE": self.tmp / "share", "BUILDS": self.tmp / "share" / "builds",
            "CURRENT": self.tmp / "share" / "current", "ENV_FILE": self.tmp / "config" / "engine.env",
            "SETTINGS": self.tmp / "config" / "engine.toml",
        }
        for name, value in patches.items():
            p = mock.patch.object(engine, name, value)
            p.start()
            self.addCleanup(p.stop)

    def checkout(self) -> Path:
        repo = self.tmp / "factory"
        for rel, text in (("engine/engine/a.py", "x = 1\n"), ("engine/requirements.lock", "temporalio==1\n"),
                          ("runtime/factory_runtime/b.py", "y = 2\n"), ("plugins/factory/sfactory/c.py", "z = 3\n"),
                          ("docs/d.md", "docs\n")):
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text)
        git("init", "-q", "-b", "main", str(repo), cwd=self.tmp)
        for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
            git("config", key, value, cwd=repo)
        git("add", ".", cwd=repo)
        git("commit", "-qm", "init", cwd=repo)
        return repo

    def test_settings_keep_other_keys_and_add_repos_once(self):
        engine.CONFIG_DIR.mkdir(parents=True)
        engine.SETTINGS.write_text('repos = ["~/a"]\nserver_port = 17243\nunattended = false\n')
        engine.write_settings(["~/b", "~/a"])
        self.assertEqual(engine.settings(), {"repos": ["~/a", "~/b"], "server_port": 17243, "unattended": False})

    def test_the_database_password_is_made_once_and_private(self):
        engine.ensure_env_file()
        first = engine.ENV_FILE.read_text()
        engine.ensure_env_file()
        self.assertEqual(engine.ENV_FILE.read_text(), first)
        self.assertTrue(first.startswith("ENGINE_DB_PASSWORD=") and len(first) > 30)
        self.assertEqual(stat.S_IMODE(engine.ENV_FILE.stat().st_mode), 0o600)

    def test_a_snapshot_is_the_committed_code_named_by_its_build_id(self):
        repo = self.checkout()
        (repo / "engine/engine/a.py").write_text("x = 'uncommitted'\n")  # not in HEAD: not in the build
        build, path = engine.snapshot(repo)
        self.assertEqual((path / "engine/engine/a.py").read_text(), "x = 1\n")
        self.assertTrue((path / "plugins/factory/sfactory/c.py").is_file())
        self.assertFalse((path / "docs").exists())
        self.assertEqual(build, engine.build_id(path))
        self.assertEqual(engine.snapshot(repo), (build, path))  # same commit, same build, nothing new
        self.assertEqual(engine.installed_builds(), [build])

    def test_a_docs_change_is_the_same_build_and_a_runner_change_is_not(self):
        repo = self.checkout()
        first = engine.build_id(repo)
        (repo / "docs/d.md").write_text("more docs\n")
        self.assertEqual(engine.build_id(repo), first)
        (repo / "plugins/factory/sfactory/c.py").write_text("z = 4\n")
        self.assertNotEqual(engine.build_id(repo), first)

    def test_the_worker_service_runs_its_own_build_and_may_stop_gracefully(self):
        spec = engine.worker_service("abc123", Path("/b/abc123"), Path("/b/abc123/.venv/bin/python"))
        self.assertEqual(spec["ProgramArguments"], ["/b/abc123/.venv/bin/python", "-m", "engine.worker", "--build-id", "abc123"])
        self.assertEqual(spec["WorkingDirectory"], "/b/abc123")
        self.assertTrue(spec["KeepAlive"])
        self.assertGreaterEqual(spec["ExitTimeOut"], 10 + 30)
        self.assertIn("PATH", spec["EnvironmentVariables"])

    def test_repos_are_stored_absolute_unless_typed_with_a_tilde(self):
        engine.write_settings([".", "~/Dev/x"])
        self.assertEqual(engine.settings()["repos"], [str(Path(".").resolve()), "~/Dev/x"])

    def test_an_interrupted_environment_build_is_redone_not_used(self):
        build = self.tmp / "b"
        build.mkdir()
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[1:3] == ["-m", "venv"]:
                (build / ".venv" / "bin").mkdir(parents=True)
                (build / ".venv" / "bin" / "python").write_text("")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 1, "", "network down")  # pip fails

        with mock.patch.object(engine, "_run", run), self.assertRaises(SystemExit):
            engine.ensure_venv(build)
        self.assertFalse((build / ".venv").exists())
        with mock.patch.object(engine, "_run", lambda cmd, **kw: (run(cmd) if "venv" in cmd else
                                                                   subprocess.CompletedProcess(cmd, 0, "", ""))):
            python = engine.ensure_venv(build)
        self.assertTrue((build / ".venv" / ".complete").is_file())
        self.assertEqual(python, build / ".venv" / "bin" / "python")

    def test_the_systemd_unit_lets_the_worker_stop_its_agents_and_quotes_paths(self):
        spec = engine.worker_service("abc", Path("/b/my builds/abc"), Path("/b/my builds/abc/.venv/bin/python"))
        unit = engine.systemd_unit("abc", spec)
        self.assertIn("KillMode=mixed", unit)
        self.assertIn('ExecStart="/b/my builds/abc/.venv/bin/python" "-m" "engine.worker"', unit)
        self.assertIn('WorkingDirectory="/b/my builds/abc"', unit)
        self.assertIn(f"TimeoutStopSec={engine.SHUTDOWN_ALLOWANCE}", unit)

    def test_start_leaves_running_workers_alone(self):
        engine.BUILDS.mkdir(parents=True)
        (engine.BUILDS / "abc").mkdir()
        with mock.patch.object(engine, "server_up"), mock.patch.object(engine, "_service_present", return_value=True), \
             mock.patch.object(engine, "start_worker") as start:
            engine.main(["start"])
        start.assert_not_called()

    def test_engine_commands_need_an_install(self):
        with self.assertRaises(SystemExit) as caught:
            engine.main(["status"])
        self.assertIn("not installed", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
