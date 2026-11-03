"""Turn a repo's cron schedule into the machine's own scheduler.

macOS → a launchd agent; Linux with systemd → a user service + timer; anything else → a crontab line to paste.
The schedule itself lives in the repo's `.factory.toml`, so moving to another machine is `factory schedule install`
there. Every entry runs `factory run <capability> --repo <path>`.
"""

from __future__ import annotations

import itertools
import os
import platform
import plistlib
import shlex
import shutil
import subprocess
from pathlib import Path

from .config import Config

DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def parse_cron(expr: str) -> list[list[int] | None]:
    """Five fields → each a list of ints, or None for `*`. Lists (`1,15`) yes; ranges/steps no (say so)."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"schedule {expr!r}: need 5 cron fields (minute hour day month weekday)")
    out: list[list[int] | None] = []
    for f in fields:
        if f == "*":
            out.append(None)
        elif all(p.isdigit() for p in f.split(",")):
            out.append([int(p) for p in f.split(",")])
        else:
            raise ValueError(f"schedule {expr!r}: use numbers, comma lists or * (no ranges or steps)")
    return out


def label(cfg: Config, capability: str) -> str:
    return f"dev.software-factory.{cfg.name}.{capability}".lower()


def command(cfg: Config, capability: str) -> list[str]:
    exe = shutil.which("factory") or str(Path.home() / ".local" / "bin" / "factory")
    return [exe, "run", capability, "--repo", str(cfg.root)]


def launchd_plist(cfg: Config, capability: str, expr: str) -> bytes:
    minute, hour, dom, month, dow = parse_cron(expr)
    keys = [("Minute", minute), ("Hour", hour), ("Day", dom), ("Month", month), ("Weekday", dow)]
    present = [(k, v) for k, v in keys if v is not None]
    intervals = [dict(zip([k for k, _ in present], combo)) for combo in itertools.product(*[v for _, v in present])]
    log_dir = Path.home() / ".cache" / "software-factory" / "logs" / cfg.name
    return plistlib.dumps({
        "Label": label(cfg, capability),
        "ProgramArguments": command(cfg, capability),
        "StartCalendarInterval": intervals,
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home())},
        "StandardOutPath": str(log_dir / f"{capability}-launchd.log"),
        "StandardErrorPath": str(log_dir / f"{capability}-launchd.log"),
        "WorkingDirectory": str(cfg.root),
    })


def systemd_units(cfg: Config, capability: str, expr: str) -> tuple[str, str]:
    minute, hour, dom, month, dow = parse_cron(expr)

    def part(values, width=2):
        return "*" if values is None else ",".join(str(v).zfill(width) for v in values)

    days = "" if dow is None else ",".join(dict.fromkeys(DOW[d] for d in dow)) + " "
    on_calendar = f"{days}*-{part(month)}-{part(dom)} {part(hour)}:{part(minute)}:00"
    service = (
        f"[Unit]\nDescription=software-factory {capability} for {cfg.name}\n\n"
        f"[Service]\nType=oneshot\nWorkingDirectory={cfg.root}\nEnvironment=PATH={os.environ.get('PATH', '/usr/bin:/bin')}\n"
        f"ExecStart={shlex.join(command(cfg, capability))}\n"
    )
    timer = f"[Unit]\nDescription=software-factory {capability} schedule for {cfg.name}\n\n[Timer]\nOnCalendar={on_calendar}\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"
    return service, timer


def crontab_line(cfg: Config, capability: str, expr: str) -> str:
    parse_cron(expr)
    return f"{expr} {shlex.join(command(cfg, capability))} >/dev/null 2>&1"


def backend() -> str:
    if platform.system() == "Darwin":
        return "launchd"
    if shutil.which("systemctl") and Path.home().joinpath(".config").exists():
        return "systemd"
    return "cron"


def schedule_of(cfg: Config, capability: str) -> str:
    sec = getattr(cfg, capability)
    return "off" if not getattr(sec, "enabled", True) else sec.schedule


def install(cfg: Config, capability: str, print_only: bool = False) -> str:
    expr = schedule_of(cfg, capability)
    if expr == "off":
        return remove(cfg, capability, print_only) + f"\n{capability} is off in .factory.toml"
    kind = backend()
    name = label(cfg, capability)
    if kind == "launchd":
        path = Path.home() / "Library" / "LaunchAgents" / f"{name}.plist"
        body = launchd_plist(cfg, capability, expr)
        if print_only:
            return f"# {path}\n{body.decode()}"
        (Path.home() / ".cache" / "software-factory" / "logs" / cfg.name).mkdir(parents=True, exist_ok=True)
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], check=True)
        return f"launchd: {name} installed ({expr}) → {path}"
    if kind == "systemd":
        service, timer = systemd_units(cfg, capability, expr)
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        if print_only:
            return f"# {unit_dir / (name + '.service')}\n{service}\n# {unit_dir / (name + '.timer')}\n{timer}"
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / f"{name}.service").write_text(service)
        (unit_dir / f"{name}.timer").write_text(timer)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{name}.timer"], check=True)
        return (f"systemd: {name}.timer enabled ({expr}); to run while logged out: "
                "`loginctl enable-linger $USER`")
    return "Add this line with `crontab -e`:\n" + crontab_line(cfg, capability, expr)


def remove(cfg: Config, capability: str, print_only: bool = False) -> str:
    name = label(cfg, capability)
    kind = backend()
    if print_only:
        return f"would remove {kind} entry {name}"
    if kind == "launchd":
        path = Path.home() / "Library" / "LaunchAgents" / f"{name}.plist"
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
        path.unlink(missing_ok=True)
    elif kind == "systemd":
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{name}.timer"], capture_output=True)
        for ext in ("service", "timer"):
            (Path.home() / ".config" / "systemd" / "user" / f"{name}.{ext}").unlink(missing_ok=True)
    else:
        return f"Remove the `{name}` line from `crontab -e` by hand"
    return f"{kind}: {name} removed"
