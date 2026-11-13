"""`factory engine …`: install, upgrade and run the factory engine (docs/designs/engine.md, sections 1 and 6).

The engine is its own program: a Docker Compose project (Temporal server, Postgres, UI) and a worker on the host.
Each Build ID gets a frozen snapshot of the code and its own virtualenv under
~/.local/share/software-factory/engine/builds/<build>, and its own worker service, so a worker can be restarted at
any time without ever running another build's code (Worker Versioning pins each run to the build it started on).

    factory engine install [--repo PATH …]   server up, deploy this checkout's build, schedules from config
    factory engine upgrade                    deploy this checkout's build if it is new; retire drained builds
    factory engine start | stop               the containers and the workers
    factory engine signal | run-now | pause | resume | reconcile | status | set-current | drained | build-id
                                              the engine's own commands (engine/engine/cli.py)

Standard library only: everything Temporal is in the engine's virtualenv.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

FACTORY = Path(__file__).resolve().parents[3]  # the checkout this command runs from
CONFIG_DIR = Path(os.environ.get("FACTORY_ENGINE_CONFIG_DIR", "~/.config/software-factory")).expanduser()
SHARE = Path(os.environ.get("FACTORY_ENGINE_HOME", "~/.local/share/software-factory/engine")).expanduser()
BUILDS = SHARE / "builds"
CURRENT = SHARE / "current"
ENV_FILE = CONFIG_DIR / "engine.env"
SETTINGS = CONFIG_DIR / "engine.toml"
COMPOSE = "engine/infra/docker-compose.yml"
LABEL = os.environ.get("FACTORY_ENGINE_LABEL", "com.software-factory.engine")
PROJECT = os.environ.get("FACTORY_ENGINE_PROJECT", "software-factory")  # Docker Compose project
SNAPSHOT = ("engine", "runtime", "plugins/factory")  # what a build needs at run time
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


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, **kw)


def settings() -> dict:
    return tomllib.loads(SETTINGS.read_text()) if SETTINGS.is_file() else {}


def write_settings(repos: list[str]) -> None:
    """engine.toml with `repos` added as absolute paths (a `~/…` path is kept as typed); other keys untouched. A
    relative path would mean a different repo from every directory the engine runs in."""
    current = settings()
    known = list(current.get("repos", []))
    for repo in (r if r.startswith("~") else str(Path(r).resolve()) for r in repos):
        if repo not in known:
            known.append(repo)
    lines = [f"repos = [{', '.join(repr_toml(r) for r in known)}]"]
    for key, value in current.items():
        if key != "repos":
            lines.append(f"{key} = {repr_toml(value)}")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text("\n".join(lines) + "\n")


def repr_toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def ensure_env_file() -> None:
    """The database password, made once, readable only by the user (the database is never published)."""
    if ENV_FILE.is_file():
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"ENGINE_DB_PASSWORD={secrets.token_urlsafe(24)}\n")


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
    SHARE.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="staging-", dir=SHARE))
    with tempfile.NamedTemporaryFile(suffix=".tar") as tar:
        archive = subprocess.run(["git", "-C", str(source), "archive", "--format=tar", "-o", tar.name, "HEAD", *SNAPSHOT],
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
    return build, target


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
    return sorted(p.name for p in BUILDS.iterdir() if p.is_dir()) if BUILDS.is_dir() else []


# ---- worker services (launchd on macOS, systemd user units on Linux) --------------------------------------------

def service_label(build: str) -> str:
    return f"{LABEL}.{build}"


def _plist_path(build: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{service_label(build)}.plist"


def _unit_path(build: str) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{service_label(build)}.service"


def worker_service(build: str, build_dir: Path, python: Path) -> dict:
    logs = SHARE / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return {
        "Label": service_label(build),
        "ProgramArguments": [str(python), "-m", "engine.worker", "--build-id", build],
        "WorkingDirectory": str(build_dir),
        # The agent CLIs, git, gh and caffeinate as the installing shell sees them; launchd's PATH is bare.
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "FACTORY_ENGINE_CONFIG": str(SETTINGS),
            **{k: v for k, v in os.environ.items() if k in {"FACTORY_ENGINE_STATE", "SSH_AUTH_SOCK"}},
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ExitTimeOut": SHUTDOWN_ALLOWANCE,  # the worker's graceful stop plus an agent group's kill grace
        "ProcessType": "Standard",  # Background would throttle the agents and test suites the worker starts
        "StandardOutPath": str(logs / f"worker-{build}.log"),
        "StandardErrorPath": str(logs / f"worker-{build}.log"),
    }


def _sd_quote(value: str) -> str:
    """A systemd unit value: double-quoted, with backslash, quote and % escaped."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def systemd_unit(build: str, spec: dict) -> str:
    env = " ".join(_sd_quote(f"{k}={v}") for k, v in spec["EnvironmentVariables"].items())
    log = spec["StandardOutPath"].replace("%", "%%")
    return (
        f"[Unit]\nDescription=software factory engine worker {build}\n\n[Service]\n"
        f"WorkingDirectory={_sd_quote(spec['WorkingDirectory'])}\nEnvironment={env}\n"
        f"ExecStart={' '.join(_sd_quote(a) for a in spec['ProgramArguments'])}\n"
        # mixed: the stop signal goes to the worker alone, which cancels its sessions and kills their groups itself;
        # only what is left after TimeoutStopSec is killed. (The default would SIGTERM the agents directly.)
        f"KillMode=mixed\nRestart=always\nRestartSec=30\nTimeoutStopSec={SHUTDOWN_ALLOWANCE}\n"
        f"StandardOutput=append:{log}\nStandardError=append:{log}\n\n[Install]\nWantedBy=default.target\n"
    )


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
        env = " ".join(f'"{k}={v}"' for k, v in spec["EnvironmentVariables"].items())
        unit.write_text(systemd_unit(build, spec))
        _run(["systemctl", "--user", "daemon-reload"], check=True)
        _run(["systemctl", "--user", "enable", "--now", unit.name], check=True)
        # Without lingering, user services stop at logout; enabling it may need the admin's consent.
        if _run(["loginctl", "enable-linger"], capture_output=True).returncode:
            print("factory engine: run `loginctl enable-linger` (may need sudo) so the worker keeps running after logout")
        return f"systemd {unit.name}"
    raise SystemExit("factory engine: no launchd or systemd here; run the worker yourself: "
                     + " ".join(spec["ProgramArguments"]))


def stop_worker(build: str) -> None:
    if sys.platform == "darwin":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{service_label(build)}"], capture_output=True)
        _plist_path(build).unlink(missing_ok=True)
    elif shutil.which("systemctl"):
        _run(["systemctl", "--user", "disable", "--now", f"{service_label(build)}.service"], capture_output=True)
        _unit_path(build).unlink(missing_ok=True)


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
    tmp = CURRENT.with_suffix(".tmp")
    tmp.write_text(build)
    tmp.replace(CURRENT)
    out.append(f"current build: {build}" + (f" (was {previous})" if previous and previous != build else ""))
    out += retire(python, keep=build)
    reconcile = engine_cli(python, "reconcile", capture=True)
    out.append(reconcile.stdout.strip() or reconcile.stderr.strip())
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


def install(repos: list[str]) -> int:
    if not shutil.which("docker"):
        raise SystemExit("factory engine: Docker is required (the engine's Temporal runs in Docker)")
    write_settings(repos)
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
        return install(repos)
    if cmd == "upgrade":
        server_up()
        for line in deploy():
            print(f"factory engine: {line}")
        return 0
    if cmd == "start":
        server_up()
        for build in installed_builds():
            if not _service_present(build):  # restarting a running worker would cancel its runs
                start_worker(build, BUILDS / build, BUILDS / build / ".venv" / "bin" / "python")
        print("factory engine: started")
        return 0
    if cmd == "stop":
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
