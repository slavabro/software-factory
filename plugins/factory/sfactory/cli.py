"""factory — the software factory's command line.

    factory config                         the repo's resolved .factory.toml
    factory upgrade scan [--json|--md]     what can be upgraded, how far, with release-notes links
    factory upgrade deprecations [FILE]    deprecation warnings from our own code in test/build output (stdin if no FILE)
    factory task ...                       the task system (`factory task --help`); with [tasks] system = "commands"
                                           only `new --title T --body-file F [--priority P]` and `exists QUERY`,
                                           which run the repo's [tasks] new / exists with every value quoted
    factory run CAPABILITY [--dry-run]     run a capability unattended in a fresh worktree (e.g. `upgrade`)
    factory schedule install|remove|show CAPABILITY [--print]

Every command takes --repo PATH (default: the git root of the current directory).
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from . import config as config_mod
from . import deprecations, runner, schedule, tasks, upgrade


def _root(path: Path | None) -> Path:
    base = path or Path.cwd()
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=base, capture_output=True, text=True)
    return Path(out.stdout.strip()) if out.returncode == 0 else base.resolve()


def task(argv: list[str], repo: Path | None) -> int:
    """`factory task …`: the factory's task system, or the repo's own commands when [tasks] system = "commands"."""
    if "--repo" in argv:  # also accepted after the subcommand
        i = argv.index("--repo")
        repo, argv = Path(argv[i + 1]), argv[:i] + argv[i + 2:]
    cfg = config_mod.load(_root(repo))
    if cfg.tasks_system == "factory":
        return tasks.main(argv, cfg.root)
    ap = argparse.ArgumentParser(prog="factory task", description="file or find a task with the repo's [tasks] commands")
    sub = ap.add_subparsers(dest="action", required=True)
    tn = sub.add_parser("new")
    tn.add_argument("--title", required=True)
    tn.add_argument("--body-file", required=True, type=Path)
    tn.add_argument("--priority", default="2")
    sub.add_parser("exists").add_argument("query")
    args = ap.parse_args(argv)
    if args.action == "new":
        values = {"title": args.title, "body_file": str(args.body_file.resolve()), "priority": args.priority}
        template = cfg.new_task
    else:
        values = {"query": args.query}
        template = cfg.task_exists
    cmd = template
    for k, v in values.items():
        cmd = cmd.replace("{" + k + "}", shlex.quote(v))
    return subprocess.run(cmd, shell=True, cwd=cfg.root).returncode


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    lead = 2 if argv[:1] == ["--repo"] else 0
    if argv[lead:lead + 1] == ["task"]:  # its own parser: options after `task` belong to it
        return task(argv[lead + 1:], Path(argv[1]) if lead else None)
    ap = argparse.ArgumentParser(prog="factory", description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", type=Path)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("config")
    up = sub.add_parser("upgrade").add_subparsers(dest="action", required=True)
    sc = up.add_parser("scan")
    sc.add_argument("--json", action="store_true")
    dp = up.add_parser("deprecations")
    dp.add_argument("file", nargs="?", type=Path)
    dp.add_argument("--json", action="store_true")
    dp.add_argument("--base", type=Path, help="directory the tool ran in (default: repo root)")
    sub.add_parser("task", help="the task system: factory task --help")  # parsed by task() above
    rn = sub.add_parser("run")
    rn.add_argument("capability")
    rn.add_argument("--dry-run", action="store_true")
    rn.add_argument("--keep", action="store_true", help="keep the worktree even if nothing was pushed")
    sh = sub.add_parser("schedule")
    sh.add_argument("action", choices=["install", "remove", "show"])
    sh.add_argument("capability")
    sh.add_argument("--print", action="store_true", help="show what would be written; change nothing")
    for p in (sc, dp, rn, sh):
        p.add_argument("--repo", type=Path, default=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    cfg = config_mod.load(_root(args.repo))
    if args.cmd == "config":
        data = asdict(cfg)
        data["root"] = str(cfg.root)
        print(json.dumps(data, indent=2))
        return 0
    if args.cmd == "upgrade" and args.action == "scan":
        result = upgrade.scan(cfg)
        print(json.dumps(result.as_dict(), indent=2) if args.json else upgrade.markdown(result))
        return 0
    if args.cmd == "upgrade" and args.action == "deprecations":
        text = args.file.read_text(errors="replace") if args.file else sys.stdin.read()
        base = (cfg.root / args.base) if args.base and not args.base.is_absolute() else args.base
        found = deprecations.parse(text, cfg.root, base)
        if args.json:
            print(json.dumps([d.as_dict() for d in found], indent=2))
        else:
            for d in found:
                where = f"{d.file}:{d.line} " if d.file else ""
                print(f"{d.kind:<10} {where}{d.category}: {d.message}")
            print(f"{sum(d.kind == 'ours' for d in found)} in our code · {len(found)} total", file=sys.stderr)
        return 1 if any(d.kind == "ours" for d in found) else 0
    if args.cmd == "run":
        return runner.run(cfg, args.capability, dry_run=args.dry_run, keep=args.keep)
    if args.cmd == "schedule":
        if args.action == "show":
            print(f"{args.capability}: {schedule.schedule_of(cfg, args.capability)} via {schedule.backend()} "
                  f"({schedule.label(cfg, args.capability)})")
            return 0
        fn = schedule.install if args.action == "install" else schedule.remove
        print(fn(cfg, args.capability, print_only=args.print))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
