"""`factory engine …`: install, upgrade and run the factory engine (docs/designs/engine.md, sections 1 and 6).

The engine is its own program: a Docker Compose project (Temporal server, Postgres, UI) and a worker on the host.
Each Build ID gets a frozen snapshot of the code and its own virtualenv under
~/.local/share/software-factory/engine/builds/<build>, and its own worker service, so a worker can be restarted at
any time without ever running another build's code (Worker Versioning pins each run to the build it started on).
The Factory Desk (docs/designs/desk.md) is one more service, DESK_LABEL, always on the current build.

    factory engine install [--repo PATH …]   server up, deploy this checkout's build, schedules from config
    factory engine upgrade                    deploy this checkout's build if it is new; retire drained builds
    factory engine start | stop               the containers, the workers and the desk
    factory engine signal | run-now | pause | resume | reconcile | status | spend on|off | repo add|remove PATH
                   | set-current | drained | build-id | desk [--open]
                                              the engine's own commands (engine/engine/cli.py, on engine/ops.py)

engine.toml has one writer, the functions under "settings" below (install, the engine's CLI and the desk call
them); its keys are documented in engine/engine/settings.py. One deploy, start or stop runs at a time (`deploy_lock`).

Standard library only: everything Temporal is in the engine's virtualenv.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

FACTORY = Path(__file__).resolve().parents[3]  # the checkout this command runs from
CONFIG_DIR = Path(os.environ.get("FACTORY_ENGINE_CONFIG_DIR", "~/.config/software-factory")).expanduser()
SHARE = Path(os.environ.get("FACTORY_ENGINE_HOME", "~/.local/share/software-factory/engine")).expanduser()
BUILDS = SHARE / "builds"
CURRENT = SHARE / "current"
SOURCE = SHARE / "source"  # the checkout the current build was deployed from (`~/…`), the one the desk deploys
DEPLOY_LOCK = SHARE / "deploy.lock"  # held by whoever deploys, starts or stops: the desk runs upgrade
ENV_FILE = CONFIG_DIR / "engine.env"
DESK_TOKEN = CONFIG_DIR / "desk.token"  # the Factory Desk's access token (0600); `factory engine desk` prints the sign-in link
# engine.toml: the one owner of its path. FACTORY_ENGINE_CONFIG names the file (the worker and the engine's CLI get
# it from here); without it, the file is in CONFIG_DIR. engine/settings.py reads this name, never its own.
SETTINGS = Path(os.environ.get("FACTORY_ENGINE_CONFIG") or CONFIG_DIR / "engine.toml").expanduser()
COMPOSE = "engine/infra/docker-compose.yml"
LABEL = os.environ.get("FACTORY_ENGINE_LABEL", "com.software-factory.engine")
DESK = "desk"
DESK_LABEL = f"{LABEL}.{DESK}"  # the Factory Desk's service: one label, always on the current build
# Passed on to every service when set, so the services find the files where this command put them.
SERVICE_ENV = {"FACTORY_ENGINE_STATE", "FACTORY_ENGINE_HOME", "FACTORY_ENGINE_CONFIG_DIR", "FACTORY_ENGINE_LABEL",
               "FACTORY_ENGINE_PROJECT", "SSH_AUTH_SOCK"}
PROJECT = os.environ.get("FACTORY_ENGINE_PROJECT", "software-factory")  # Docker Compose project
SNAPSHOT = ("engine", "runtime", "plugins/factory")  # what a build needs at run time
COMMIT = "COMMIT"  # in each build: the commit it was snapshotted from (not an input of the Build ID)
SHUTDOWN_ALLOWANCE = 90  # seconds a stopping worker gets: 10 s graceful + 30 s agent kill grace + margin

# Build ID: the code the worker runs (docs/designs/engine.md, section 6): the engine, the runtime and the factory's
# own package its activities use. engine/worker.py uses this.
VERSIONED = ("engine", "runtime", "plugins/factory/sfactory")
SKIP_PARTS = {"__pycache__", "tests", ".venv", "build", "infra"}
COUNTED = {".py", ".toml", ".lock", ".txt"}


def build_id(root: Path) -> str:
    """sha256 over the source the worker runs and the engine's lockfile (path, then content); 12 hex characters."""
    digest = hashlib.sha256()
    for d in VERSIONED:
        for path in sorted((root / d).rglob("*")):
            rel = path.relative_to(root)
            if not path.is_file() or path.suffix not in COUNTED:
                continue
            if any(p in SKIP_PARTS or p.endswith(".egg-info") for p in rel.parts):
                continue
            digest.update(str(rel).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def state_dir() -> Path:
    """Logs, worktrees, team notebooks, deploy logs (FACTORY_ENGINE_STATE overrides, read at each call); the one
    owner of its default, engine/settings.py reads it here."""
    return Path(os.environ.get("FACTORY_ENGINE_STATE", "~/.local/state/software-factory")).expanduser()


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, **kw)


# ---- settings: the one writer of engine.toml ----------------------------------------------------------------------

def settings(path: Path | None = None) -> dict:
    path = path or SETTINGS
    return tomllib.loads(path.read_text()) if path.is_file() else {}


def home_form(path: Path) -> str:
    """`~/…` for a path under the home directory, else absolute."""
    path = path.expanduser().resolve()
    try:
        return "~/" + str(path.relative_to(Path.home().resolve()))
    except ValueError:
        return str(path)


def repo_entry(repo: str) -> str:
    """How engine.toml keeps a repo: absolute (a relative path would mean a different repo from every directory the
    engine runs in), or `~/…` as typed."""
    return repo if repo.startswith("~") else str(Path(repo).resolve())


def same_repo(a: str, b: str) -> bool:
    return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()


@contextmanager
def _locked(lock: Path, blocking: bool = True) -> Iterator[int]:
    """An exclusive flock on `lock` (made if missing). It goes with the block or the process, so it is never left
    stale. Not `blocking`: BlockingIOError when someone else holds it."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    """Write through a temp file in the same directory, then rename: a reader sees the old file or the new, never a
    half-written one. An existing file keeps its permissions; a symlink is followed, so its target is replaced, not
    the link."""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, path.stat().st_mode & 0o7777 if path.exists() else mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def dump_settings(data: dict) -> str:
    """engine.toml's text: `repos` first, then the other keys in their order. Machine-written, so no comments (the
    keys' reference is engine/engine/settings.py)."""
    keys = (["repos"] if "repos" in data else []) + [k for k in data if k != "repos"]
    return "".join(f"{k} = {repr_toml(data[k])}\n" for k in keys)


def edit_settings(change: Callable[[dict], None], path: Path | None = None) -> tuple[dict, dict]:
    """Apply `change` to engine.toml's values and write the result atomically, under a lock so two writers (the CLI,
    the desk) never lose each other's change. Returns (before, after); an unchanged file is left alone."""
    path = path or SETTINGS
    with _locked(path.with_name(path.name + ".lock")):
        before = settings(path)
        after = tomllib.loads(dump_settings(before))  # a deep copy
        change(after)
        if after != before:
            write_atomic(path, dump_settings(after))
    return before, after


def add_repos(repos: list[str], path: Path | None = None) -> tuple[dict, dict]:
    """Add each repo that isn't there yet (see `repo_entry`); other keys untouched."""
    def change(data: dict) -> None:
        known = data.setdefault("repos", [])
        for repo in map(repo_entry, repos):
            if not any(same_repo(repo, k) for k in known):
                known.append(repo)
    return edit_settings(change, path)


def remove_repo(repo: str, path: Path | None = None) -> tuple[dict, dict]:
    """Drop the entries naming the same directory as `repo` (`~/x` and its absolute form alike); KeyError if none."""
    def change(data: dict) -> None:
        known = data.get("repos", [])
        kept = [k for k in known if not same_repo(k, repo)]
        if len(kept) == len(known):
            raise KeyError(f"{repo} is not one of the engine's repos")
        data["repos"] = kept
    return edit_settings(change, path)


def set_setting(key: str, value: bool | int | str, path: Path | None = None) -> tuple[dict, dict]:
    def change(data: dict) -> None:
        data[key] = value
    return edit_settings(change, path)


def repr_toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(repr_toml(v) for v in value) + "]"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # JSON's string escapes are valid in a TOML basic string
    raise TypeError(f"engine.toml holds no {type(value).__name__} values ({value!r})")


def ensure_env_file() -> None:
    """The database password, made once, readable only by the user (the database is never published)."""
    if ENV_FILE.is_file():
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"ENGINE_DB_PASSWORD={secrets.token_urlsafe(24)}\n")


def read_desk_token() -> str | None:
    """The Factory Desk's token as it is on disk (None when there is none yet); never makes one, so a desk request
    can't. A token file anyone but the user can read is made private again, and the repair is said on stderr."""
    try:
        mode = DESK_TOKEN.stat().st_mode & 0o777
        token = DESK_TOKEN.read_text().strip()
    except FileNotFoundError:
        return None
    if mode != 0o600:
        os.chmod(DESK_TOKEN, 0o600)
        print(f"factory engine: {DESK_TOKEN} was mode {mode:o}; made it 0600 (if others could read it, delete it to "
              "get a new token)", file=sys.stderr)
    return token or None


def desk_token() -> str:
    """The Factory Desk's token, made once (random, readable only by the user). Only the desk's startup and
    `factory engine desk` call this; requests only read it (`read_desk_token`)."""
    token = read_desk_token()
    if token:
        return token
    DESK_TOKEN.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(DESK_TOKEN, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # made meanwhile by another caller (use it), or left empty (fill it)
        token = read_desk_token()
        if token:
            return token
        fd = os.open(DESK_TOKEN, os.O_WRONLY | os.O_TRUNC)
        os.fchmod(fd, 0o600)
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


def compose_file() -> Path:
    """The current build's compose file (frozen with its code); this checkout's before the first build."""
    build = current_build()
    frozen = BUILDS / build / COMPOSE if build else None
    return frozen if frozen and frozen.is_file() else FACTORY / COMPOSE


def compose(*args: str, check: bool = True, file: Path | None = None) -> subprocess.CompletedProcess:
    s = settings()
    env = dict(os.environ, ENGINE_SERVER_PORT=str(s.get("server_port", 7243)), ENGINE_UI_PORT=str(s.get("ui_port", 8243)))
    cmd = ["docker", "compose", "-p", PROJECT, "--env-file", str(ENV_FILE), "-f", str(file or compose_file()), *args]
    proc = _run(cmd, env=env, capture_output=True)
    if check and proc.returncode:
        raise SystemExit(f"factory engine: `{' '.join(cmd[2:])}` failed:\n{(proc.stderr or proc.stdout).strip()[-2000:]}")
    return proc


def server_up(file: Path | None = None) -> None:
    """Containers up, and the one-shot setup (schema, namespace, search attributes) succeeded: the setup waits for
    a healthy server, so its success means the server is serving. (`up --wait` counts a one-shot that exited as a
    failure, so the one-shots are awaited by exit code instead.)"""
    compose("up", "-d", file=file)
    deadline = time.monotonic() + 600  # a first start pulls images and creates the database
    for one_shot in ("temporal-schema", "temporal-setup"):
        while True:
            state = compose("ps", "-a", "--format", "{{.State}} {{.ExitCode}}", one_shot, check=False, file=file).stdout.split()
            if state[:1] == ["exited"]:
                break
            if time.monotonic() > deadline:
                raise SystemExit(f"factory engine: {one_shot} did not finish in 10 minutes")
            time.sleep(2)
        if state[1:2] != ["0"]:
            logs = compose("logs", "--no-color", "--tail", "20", one_shot, check=False, file=file).stdout
            raise SystemExit(f"factory engine: {one_shot} failed:\n{logs}")


def port_taken(port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ---- builds ------------------------------------------------------------------------------------------------------

def snapshot(source: Path = FACTORY) -> tuple[str, Path]:
    """Copy the checkout's committed engine code (HEAD, not uncommitted edits) to a fresh build dir."""
    head = subprocess.run(["git", "-C", str(source), "rev-parse", "--verify", "HEAD^{commit}"], capture_output=True,
                          text=True)
    if head.returncode:
        raise SystemExit(f"factory engine: {source} has no commit to build: {head.stderr.strip()}")
    commit = head.stdout.strip()  # resolved once, so the archive and COMMIT are the same commit
    SHARE.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="staging-", dir=SHARE))
    with tempfile.NamedTemporaryFile(suffix=".tar") as tar:
        archive = subprocess.run(["git", "-C", str(source), "archive", "--format=tar", "-o", tar.name, commit, *SNAPSHOT],
                                 capture_output=True, text=True)
        if archive.returncode:
            shutil.rmtree(staging)
            raise SystemExit(f"factory engine: git archive failed: {archive.stderr}")
        untar = subprocess.run(["tar", "-xf", tar.name, "-C", str(staging)], capture_output=True, text=True)
        if untar.returncode:
            shutil.rmtree(staging)
            raise SystemExit(f"factory engine: unpacking the build failed: {untar.stderr}")
    build = build_id(staging)
    target = BUILDS / build
    if target.exists():
        shutil.rmtree(staging)
    else:
        BUILDS.mkdir(parents=True, exist_ok=True)
        staging.rename(target)
    # The latest commit this build was taken from: a later commit that changes no versioned file is the same build.
    write_atomic(target / COMMIT, commit + "\n")
    return build, target


def main_worktree(checkout: Path) -> Path:
    """The repo's main worktree for any of its worktrees (a linked one, e.g. an agent's under .claude/worktrees, is
    temporary and must never be recorded as the source); the path itself when git can't tell."""
    common = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True)
    if common.returncode:
        return checkout.resolve()
    git_dir = Path(common.stdout.strip())
    return git_dir.parent.resolve() if git_dir.name == ".git" else checkout.resolve()


def record_source(checkout: Path = FACTORY) -> None:
    """SOURCE: the checkout (its main worktree, `~/…`) the current build was deployed from. Kept next to `current`,
    not in engine.toml, so an older build's settings reader never meets a key it doesn't know."""
    write_atomic(SOURCE, home_form(main_worktree(checkout)) + "\n")


def recorded_source() -> str | None:
    return (SOURCE.read_text().strip() or None) if SOURCE.is_file() else None


def source_head() -> tuple[str, str | None, str | None] | None:
    """(the source checkout as recorded, its HEAD commit, its branch), read with local git only (never a fetch);
    None before a deploy recorded one. Commit or branch is None when git can't tell (a moved checkout, a detached
    HEAD)."""
    source = recorded_source()
    if source is None:
        return None
    path = Path(source).expanduser()

    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return (out.stdout.strip() or None) if out.returncode == 0 else None

    return source, git("rev-parse", "--verify", "HEAD^{commit}"), git("symbolic-ref", "--short", "-q", "HEAD")


def build_commit(build: str) -> str | None:
    """The commit a build was last snapshotted from; None for a build made before builds recorded it."""
    path = BUILDS / build / COMMIT
    return (path.read_text().strip() or None) if path.is_file() else None


def ensure_venv(build_dir: Path) -> Path:
    """The build's virtualenv. It counts as installed only once every step succeeded (a `.complete` marker written
    last), so an interrupted install (^C, a sleep, a network error) is rebuilt next time, never used half done."""
    venv = build_dir / ".venv"
    python, done = venv / "bin" / "python", venv / ".complete"
    if done.is_file():
        return python
    shutil.rmtree(venv, ignore_errors=True)
    steps = [
        [sys.executable, "-m", "venv", str(venv)],
        [str(python), "-m", "pip", "install", "--quiet", "-r", "engine/requirements.lock", "-e", "runtime", "-e", "engine"],
    ]
    for step in steps:
        proc = _run(step, cwd=build_dir, capture_output=True)
        if proc.returncode:
            shutil.rmtree(venv, ignore_errors=True)
            raise SystemExit(f"factory engine: building the engine's environment failed:\n{proc.stderr[-2000:]}")
    done.write_text("")
    return python


def engine_cli(python: Path, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ, FACTORY_ENGINE_CONFIG=str(SETTINGS))
    return _run([str(python), "-m", "engine.cli", *args], env=env, capture_output=capture)


def current_build() -> str | None:
    return CURRENT.read_text().strip() if CURRENT.is_file() else None


def installed_builds() -> list[str]:
    """Each build on disk; never the desk (its service has a label of its own, not a build)."""
    if not BUILDS.is_dir():
        return []
    return sorted(p.name for p in BUILDS.iterdir() if p.is_dir() and p.name != DESK)


# ---- services (launchd on macOS, systemd user units on Linux): a worker per build, and the desk ------------------

def service_label(build: str) -> str:
    """A build's worker service. `desk` is not a build: its label is the desk's (DESK_LABEL)."""
    if build == DESK:
        raise ValueError("`desk` is the desk's service, not a build")
    return f"{LABEL}.{build}"


def _plist_for(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _unit_for(label: str) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{label}.service"


def _plist_path(build: str) -> Path:
    return _plist_for(service_label(build))


def _unit_path(build: str) -> Path:
    return _unit_for(service_label(build))


def service_env() -> dict:
    """A service's environment: the agent CLIs, git, gh and caffeinate as the installing shell's PATH has them
    (launchd's own PATH is bare), engine.toml's path, and the overrides of where the engine keeps its files."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "FACTORY_ENGINE_CONFIG": str(SETTINGS),
        **{k: v for k, v in os.environ.items() if k in SERVICE_ENV},
    }


def worker_service(build: str, build_dir: Path, python: Path) -> dict:
    logs = SHARE / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return {
        "Label": service_label(build),
        "ProgramArguments": [str(python), "-m", "engine.worker", "--build-id", build],
        "WorkingDirectory": str(build_dir),
        "EnvironmentVariables": service_env(),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ExitTimeOut": SHUTDOWN_ALLOWANCE,  # the worker's graceful stop plus an agent group's kill grace
        "ProcessType": "Standard",  # Background would throttle the agents and test suites the worker starts
        "StandardOutPath": str(logs / f"worker-{build}.log"),
        "StandardErrorPath": str(logs / f"worker-{build}.log"),
    }


def desk_service(build_dir: Path, python: Path) -> dict:
    """The Factory Desk (`python -m engine.desk`) on the current build: one label whatever the build, the worker's
    environment (so `gh` is found), restarted when it stops, its log in the state dir."""
    log = state_dir() / "desk.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    return {
        "Label": DESK_LABEL,
        "ProgramArguments": [str(python), "-m", "engine.desk"],
        "WorkingDirectory": str(build_dir),
        "EnvironmentVariables": service_env(),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Standard",
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def _sd_quote(value: str) -> str:
    """A systemd unit value: double-quoted, with backslash, quote and % escaped."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def systemd_unit(build: str, spec: dict) -> str:
    # mixed: the stop signal goes to the worker alone, which cancels its sessions and kills their groups itself; only
    # what is left after TimeoutStopSec is killed. (The default would SIGTERM the agents directly.)
    return _systemd_unit(f"software factory engine worker {build}", spec,
                         f"KillMode=mixed\nRestart=always\nRestartSec=30\nTimeoutStopSec={SHUTDOWN_ALLOWANCE}\n")


def desk_unit(spec: dict) -> str:
    # process: a stop or restart signals the desk alone. The upgrade it starts runs in a session of its own but in
    # the desk's control group, and restarts the desk: with mixed (the worker's), systemd would kill it with the desk.
    return _systemd_unit("software factory desk", spec, "KillMode=process\nRestart=always\nRestartSec=10\n")


def _systemd_unit(description: str, spec: dict, policy: str) -> str:
    env = " ".join(_sd_quote(f"{k}={v}") for k, v in spec["EnvironmentVariables"].items())
    log = spec["StandardOutPath"].replace("%", "%%")
    return (
        f"[Unit]\nDescription={description}\n\n[Service]\n"
        f"WorkingDirectory={_sd_quote(spec['WorkingDirectory'])}\nEnvironment={env}\n"
        f"ExecStart={' '.join(_sd_quote(a) for a in spec['ProgramArguments'])}\n{policy}"
        f"StandardOutput=append:{log}\nStandardError=append:{log}\n\n[Install]\nWantedBy=default.target\n"
    )


def _linger() -> None:
    # Without lingering, user services stop at logout; enabling it may need the admin's consent.
    if _run(["loginctl", "enable-linger"], capture_output=True).returncode:
        print("factory engine: run `loginctl enable-linger` (may need sudo) so the engine keeps running after logout")


def start_worker(build: str, build_dir: Path, python: Path) -> str:
    spec = worker_service(build, build_dir, python)
    if sys.platform == "darwin":
        path = _plist_path(build)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(spec))
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{spec['Label']}"], capture_output=True)
        _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
        return f"launchd {spec['Label']}"
    if shutil.which("systemctl"):
        unit = _unit_path(build)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(systemd_unit(build, spec))
        _run(["systemctl", "--user", "daemon-reload"], check=True)
        _run(["systemctl", "--user", "enable", "--now", unit.name], check=True)
        _linger()
        return f"systemd {unit.name}"
    raise SystemExit("factory engine: no launchd or systemd here; run the worker yourself: "
                     + " ".join(spec["ProgramArguments"]))


def stop_worker(build: str) -> None:
    label = service_label(build)  # never the desk's
    if sys.platform == "darwin":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
        _plist_for(label).unlink(missing_ok=True)
    elif shutil.which("systemctl"):
        _run(["systemctl", "--user", "disable", "--now", f"{label}.service"], capture_output=True)
        _unit_for(label).unlink(missing_ok=True)


def start_desk(build: str) -> str | None:
    """(Re)start the desk on `build` when its service is missing or differs (a new build, a new PATH); None when it
    already runs exactly that. The upgrade the desk starts calls this too: it runs in a session of its own, so the
    desk's restart does not stop it."""
    build_dir = BUILDS / build
    spec = desk_service(build_dir, build_dir / ".venv" / "bin" / "python")
    if sys.platform == "darwin":
        path, text = _plist_for(DESK_LABEL), plistlib.dumps(spec)
        if path.is_file() and path.read_bytes() == text and _label_present(DESK_LABEL):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text)
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{DESK_LABEL}"], capture_output=True)
        _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
        return f"launchd {DESK_LABEL} on build {build}"
    if shutil.which("systemctl"):
        unit, text = _unit_for(DESK_LABEL), desk_unit(spec)
        if unit.is_file() and unit.read_text() == text and _label_present(DESK_LABEL):
            return None
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(text)
        _run(["systemctl", "--user", "daemon-reload"], check=True)
        _run(["systemctl", "--user", "enable", unit.name], check=True)
        _run(["systemctl", "--user", "restart", unit.name], check=True)
        _linger()
        return f"systemd {unit.name} on build {build}"
    print("factory engine: no launchd or systemd here; run the desk yourself: " + " ".join(spec["ProgramArguments"]))
    return None


def stop_desk() -> None:
    if sys.platform == "darwin":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{DESK_LABEL}"], capture_output=True)
        _plist_for(DESK_LABEL).unlink(missing_ok=True)
    elif shutil.which("systemctl"):
        _run(["systemctl", "--user", "disable", "--now", f"{DESK_LABEL}.service"], capture_output=True)
        _unit_for(DESK_LABEL).unlink(missing_ok=True)


def _label_present(label: str) -> bool:
    """Loaded (launchd) or running (systemd)."""
    if sys.platform == "darwin":
        return _run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True).returncode == 0
    return _run(["systemctl", "--user", "is-active", "--quiet", f"{label}.service"], capture_output=True).returncode == 0


# ---- commands -----------------------------------------------------------------------------------------------------

def deploy() -> list[str]:
    """This checkout's committed build: snapshot, venv, worker, current; then retire drained builds and reconcile."""
    out = []
    build, build_dir = snapshot()
    python = ensure_venv(build_dir)
    previous = current_build()
    if previous != build or not _service_present(build):
        out.append(f"worker for build {build}: {start_worker(build, build_dir, python)}")
    for attempt in range(60):  # the version exists once its worker has polled
        proc = engine_cli(python, "set-current", "--build-id", build, capture=True)
        if proc.returncode == 0:
            break
        time.sleep(2)
    else:
        raise SystemExit(f"factory engine: build {build} never became current:\n{proc.stderr[-1500:]}")
    write_atomic(CURRENT, build)
    record_source()  # the checkout this build came from, now that it is current
    out.append(f"current build: {build}" + (f" (was {previous})" if previous and previous != build else ""))
    desk = start_desk(build)  # onto the new build before retire, so no build is deleted from under the desk
    if desk:
        out.append(f"desk: {desk}")
    out += retire(python, keep=build)
    reconcile = engine_cli(python, "reconcile", capture=True)
    said = reconcile.stdout.strip() or reconcile.stderr.strip()
    if said:  # an unchanged schedule says nothing
        out.append(said)
    return out


def _service_present(build: str) -> bool:
    if sys.platform == "darwin":
        return _run(["launchctl", "print", f"gui/{os.getuid()}/{service_label(build)}"], capture_output=True).returncode == 0
    return _unit_path(build).is_file()


def retire(python: Path, keep: str) -> list[str]:
    """Stop and delete builds other than `keep` that nothing open is pinned to any more."""
    out = []
    for build in installed_builds():
        if build == keep:
            continue
        if engine_cli(python, "drained", "--build-id", build, capture=True).returncode == 0:
            stop_worker(build)
            shutil.rmtree(BUILDS / build, ignore_errors=True)
            out.append(f"retired build {build} (drained)")
        else:
            out.append(f"build {build} still has runs pinned to it; it keeps running until they finish")
    return out


@contextmanager
def deploy_lock(what: str) -> Iterator[None]:
    """One change to the engine's builds and services at a time on this machine (install, upgrade, start, stop; the
    desk runs upgrade): a second is refused, not queued. The lock goes with its process, so one that crashed never
    blocks the next. While held, the lock file names the holder (`deploy_running` reads it)."""
    with ExitStack() as held:
        try:
            fd = held.enter_context(_locked(DEPLOY_LOCK, blocking=False))
        except BlockingIOError:
            raise SystemExit(f"factory engine: {deploy_running() or 'another engine change'} is running; "
                             "try again when it has finished") from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{what} (pid {os.getpid()}) since {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode())
        try:
            yield
        finally:
            os.ftruncate(fd, 0)  # cleared before the lock is let go, so a reader never sees a holder that left


def deploy_running() -> str | None:
    """Who holds the deploy lock ("upgrade (pid N) since T"), or None. Reads the holder's line without touching the
    lock, so asking never makes a real deploy fail. A line left by a holder that died (its pid gone) counts as none.
    Advisory: the deploy that is started still takes the lock itself, and is refused if it lost a race."""
    try:
        line = DEPLOY_LOCK.read_text().strip()
    except OSError:
        return None
    pid = line.partition("(pid ")[2].partition(")")[0]
    if not pid.isdigit():
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # alive, someone else's
    return line


def install(repos: list[str]) -> int:
    if not shutil.which("docker"):
        raise SystemExit("factory engine: Docker is required (the engine's Temporal runs in Docker)")
    add_repos(repos)
    ensure_env_file()
    s = settings()
    for key, default in (("server_port", 7243), ("ui_port", 8243)):
        port = int(s.get(key, default))
        running = compose("ps", "--status", "running", "--quiet", check=False).stdout.strip()
        if port_taken(port) and not running:
            raise SystemExit(f"factory engine: port {port} ({key}) is taken; set {key} in {SETTINGS}")
    print("factory engine: starting the server (first start pulls images and creates the database)")
    _, build_dir = snapshot()
    server_up(build_dir / COMPOSE)
    for line in deploy():
        print(f"factory engine: {line}")
    print(f"factory engine: UI http://localhost:{s.get('ui_port', 8243)} (namespace factory)")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "install":
        repos = [rest[i + 1] for i, a in enumerate(rest) if a == "--repo" and i + 1 < len(rest)]
        with deploy_lock("install"):
            return install(repos)
    if cmd == "upgrade":
        with deploy_lock("upgrade"):
            server_up()
            for line in deploy():
                print(f"factory engine: {line}")
        return 0
    if cmd == "start":
        with deploy_lock("start"):
            server_up()
            for build in installed_builds():
                if not _service_present(build):  # restarting a running worker would cancel its runs
                    start_worker(build, BUILDS / build, BUILDS / build / ".venv" / "bin" / "python")
            current = current_build()
            if current:
                start_desk(current)  # left alone when it already runs the current build
        print("factory engine: started")
        return 0
    if cmd == "stop":
        with deploy_lock("stop"):
            stop_desk()
            for build in installed_builds():
                stop_worker(build)
            compose("stop")
        print("factory engine: stopped (history kept)")
        return 0
    if cmd == "build-id":
        print(build_id(FACTORY))
        return 0
    build = current_build()
    if build is None:
        raise SystemExit("factory engine: not installed; run `factory engine install --repo PATH`")
    return engine_cli(BUILDS / build / ".venv" / "bin" / "python", *argv).returncode
