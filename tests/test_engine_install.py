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
            "SETTINGS": self.tmp / "config" / "engine.toml", "DEPLOY_LOCK": self.tmp / "share" / "deploy.lock",
            "SOURCE": self.tmp / "share" / "source", "DESK_TOKEN": self.tmp / "config" / "desk.token",
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
        before, after = engine.add_repos(["~/b", "~/a"])
        self.assertEqual(before["repos"], ["~/a"])
        self.assertEqual(engine.settings(), after)
        self.assertEqual(after, {"repos": ["~/a", "~/b"], "server_port": 17243, "unattended": False})

    def test_a_repo_is_one_entry_whether_typed_with_a_tilde_or_absolute(self):
        engine.add_repos(["~/Dev/x"])
        engine.add_repos([str(Path.home() / "Dev" / "x")])
        self.assertEqual(engine.settings()["repos"], ["~/Dev/x"])
        engine.remove_repo(str(Path.home() / "Dev" / "x"))
        self.assertEqual(engine.settings()["repos"], [])

    def test_removing_a_repo_keeps_the_rest_and_an_unknown_one_is_refused(self):
        engine.add_repos(["~/a", "~/b"])
        engine.set_setting("unattended", False)
        before, after = engine.remove_repo("~/a")
        self.assertEqual((before["repos"], after), (["~/a", "~/b"], {"repos": ["~/b"], "unattended": False}))
        with self.assertRaises(KeyError):
            engine.remove_repo("~/a")
        self.assertEqual(engine.settings(), {"repos": ["~/b"], "unattended": False})  # a refused change writes nothing

    def test_set_a_key_and_odd_strings_round_trip(self):
        engine.set_setting("desk_port", 9000)
        engine.set_setting("server_host", 'C:\\a "quoted"\nhost')
        self.assertEqual(engine.settings(), {"desk_port": 9000, "server_host": 'C:\\a "quoted"\nhost'})
        with self.assertRaises(TypeError):
            engine.set_setting("ratio", 0.5)  # type: ignore[arg-type]
        self.assertNotIn("ratio", engine.settings())

    def test_a_symlinked_settings_file_is_written_through_the_link(self):
        real = self.tmp / "dotfiles" / "engine.toml"
        real.parent.mkdir()
        real.write_text("unattended = true\n")
        engine.CONFIG_DIR.mkdir(parents=True)
        engine.SETTINGS.symlink_to(real)
        engine.set_setting("unattended", False)
        self.assertTrue(engine.SETTINGS.is_symlink())
        self.assertEqual(real.read_text(), "unattended = false\n")

    def test_the_settings_write_is_atomic_and_keeps_the_file_mode(self):
        engine.add_repos(["~/a"])
        os.chmod(engine.SETTINGS, 0o600)
        with mock.patch.object(engine.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            engine.add_repos(["~/b"])
        self.assertEqual(engine.settings()["repos"], ["~/a"])  # the old file stands whole
        self.assertEqual(sorted(p.name for p in engine.CONFIG_DIR.iterdir()), ["engine.toml", "engine.toml.lock"])
        engine.add_repos(["~/b"])
        self.assertEqual(stat.S_IMODE(engine.SETTINGS.stat().st_mode), 0o600)

    def test_writers_at_once_lose_no_change(self):
        import threading

        threads = [threading.Thread(target=engine.add_repos, args=([f"~/r{i}"],)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(engine.settings()["repos"]), sorted(f"~/r{i}" for i in range(12)))

    def test_paths_under_home_are_kept_in_tilde_form(self):
        self.assertEqual(engine.home_form(Path.home() / "Dev" / "software-factory"), "~/Dev/software-factory")
        self.assertEqual(engine.home_form(Path("/opt/factory")), str(Path("/opt/factory").resolve()))

    def test_upgrade_holds_the_deploy_lock_and_says_who_holds_it(self):
        held = []
        with mock.patch.object(engine, "server_up"), \
             mock.patch.object(engine, "deploy", lambda: held.append(engine.deploy_running()) or []):
            self.assertEqual(engine.main(["upgrade"]), 0)
        self.assertTrue(held[0].startswith(f"upgrade (pid {os.getpid()}) since "))
        self.assertIsNone(engine.deploy_running())  # released when it ends
        self.assertNotIn("source", engine.settings())  # never in engine.toml: older builds would refuse the key

    def test_a_deploy_records_the_source_once_its_build_is_current(self):
        order = []

        def cli(python, *args, capture=False):
            order.append((args[0], engine.recorded_source()))
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(engine, "snapshot", return_value=("b1", self.tmp / "b1")), \
             mock.patch.object(engine, "ensure_venv", return_value=self.tmp / "py"), \
             mock.patch.object(engine, "_service_present", return_value=True), \
             mock.patch.object(engine, "current_build", return_value="b1"), \
             mock.patch.object(engine, "engine_cli", cli), mock.patch.object(engine, "retire", return_value=[]):
            engine.deploy()
        self.assertEqual(order[0], ("set-current", None))  # not yet: the build is not current
        self.assertEqual(engine.recorded_source(), engine.home_form(engine.main_worktree(engine.FACTORY)))

    def test_the_source_is_the_main_worktree_never_a_linked_one(self):
        repo = self.checkout()
        linked = self.tmp / "agent-worktree"
        git("worktree", "add", "-q", "-b", "agent", str(linked), cwd=repo)
        self.assertEqual(engine.main_worktree(linked), repo.resolve())
        engine.record_source(linked)
        self.assertEqual(engine.recorded_source(), engine.home_form(repo))
        self.assertEqual(engine.main_worktree(self.tmp), self.tmp.resolve())  # not a checkout: itself

    def test_a_holder_that_died_is_not_running(self):
        engine.SHARE.mkdir(parents=True)
        dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
        engine.DEPLOY_LOCK.write_text(f"upgrade (pid {dead.stdout.strip()}) since then\n")
        self.assertIsNone(engine.deploy_running())
        with engine.deploy_lock("upgrade"):  # and the lock itself is free
            pass

    def test_start_and_stop_are_refused_while_a_deploy_runs(self):
        with engine.deploy_lock("upgrade"):
            for cmd in ("start", "stop"):
                with mock.patch.object(engine, "server_up") as up, mock.patch.object(engine, "compose") as comp, \
                     self.assertRaises(SystemExit) as caught:
                    engine.main([cmd])
                up.assert_not_called()
                comp.assert_not_called()
                self.assertIn("upgrade (pid", str(caught.exception))

    def test_a_second_deploy_is_refused_while_one_runs(self):
        with engine.deploy_lock("upgrade"):
            with mock.patch.object(engine, "server_up") as up, self.assertRaises(SystemExit) as caught:
                engine.main(["upgrade"])
            up.assert_not_called()
            self.assertIn(f"upgrade (pid {os.getpid()}) since", str(caught.exception))
            # Another process sees it too (flock is per open file, and goes with its holder).
            probe = subprocess.run([sys.executable, "-c", (
                "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR)\n"
                "try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\nexcept BlockingIOError: sys.exit(3)"),
                str(engine.DEPLOY_LOCK)])
            self.assertEqual(probe.returncode, 3)
        with engine.deploy_lock("upgrade"):  # free again
            pass

    def test_the_database_password_is_made_once_and_private(self):
        engine.ensure_env_file()
        first = engine.ENV_FILE.read_text()
        engine.ensure_env_file()
        self.assertEqual(engine.ENV_FILE.read_text(), first)
        self.assertTrue(first.startswith("ENGINE_DB_PASSWORD=") and len(first) > 30)
        self.assertEqual(stat.S_IMODE(engine.ENV_FILE.stat().st_mode), 0o600)

    def test_the_desk_token_is_made_once_and_private(self):
        token = engine.desk_token()
        self.assertEqual(engine.desk_token(), token)
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(engine.DESK_TOKEN.parent, engine.CONFIG_DIR)
        self.assertEqual(stat.S_IMODE(engine.DESK_TOKEN.stat().st_mode), 0o600)
        engine.DESK_TOKEN.chmod(0o644)
        engine.DESK_TOKEN.write_text("")  # emptied by hand: a new token, private again
        fresh = engine.desk_token()
        self.assertNotEqual(fresh, token)
        self.assertEqual(stat.S_IMODE(engine.DESK_TOKEN.stat().st_mode), 0o600)

    def test_the_source_head_is_read_with_local_git(self):
        self.assertIsNone(engine.source_head())  # no deploy recorded one yet
        repo = self.checkout()
        engine.record_source(repo)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
        with mock.patch.object(engine.subprocess, "run", wraps=engine.subprocess.run) as run:
            self.assertEqual(engine.source_head(), (engine.home_form(repo), head, "main"))
        self.assertFalse([c for c in run.call_args_list if "fetch" in c.args[0]])
        git("checkout", "-q", "--detach", cwd=repo)
        self.assertEqual(engine.source_head(), (engine.home_form(repo), head, None))

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

    def test_each_build_records_the_latest_commit_it_was_taken_from(self):
        repo = self.checkout()

        def head() -> str:
            return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()

        build, path = engine.snapshot(repo)
        self.assertEqual(engine.build_commit(build), head())
        (repo / "docs/d.md").write_text("more docs\n")
        git("commit", "-qam", "docs", cwd=repo)
        self.assertEqual(engine.snapshot(repo), (build, path))  # docs only: the same build ...
        self.assertEqual(engine.build_commit(build), head())  # ... now known to match the newer commit
        self.assertEqual(engine.build_id(path), build)  # COMMIT is not an input of the Build ID
        self.assertIsNone(engine.build_commit("no-such-build"))

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
        engine.add_repos([".", "~/Dev/x"])
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
