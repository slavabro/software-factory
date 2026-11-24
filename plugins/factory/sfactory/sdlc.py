"""The SDLC a project runs: processes built from blocks, started by triggers, passing gates.

Every layer is data, and none names a project (reference: docs/factory-sdlc.md; design: docs/designs/sdlc.md):

- the factory catalog, `plugins/factory/sdlc/catalog.toml`: the vocabularies (models, autonomy ladder, ask modes,
  executors, outputs, tools, resources, events), the defaults per block kind, the default gates and the floors;
- factory blocks, `plugins/factory/sdlc/blocks/<id>.toml`, referenced as `factory:<id>`;
- factory templates, `plugins/factory/sdlc/processes/<id>.json`, a project's starting points;
- the project, configured by `.factory.toml` [sdlc]: in its folder (default `.factory/`) its own blocks
  (`blocks/<id>.toml`, referenced as `repo:<id>`), its processes (`processes/<id>.json`) and its gates
  (`gates.json`).

`check` validates a project against all of them and names every problem with its file. This module owns the
factory's meaning (blocks, settings, triggers, outputs, gates); the runtime's process validator owns how steps
execute (`condition` syntax, placeholders), and the engine runs both. Values resolve step, then block defaults,
then the catalog's defaults for the kind, and `resolve` says which one each came from.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config

FACTORY_DIR = Path(__file__).resolve().parents[1] / "sdlc"
KINDS = ("agent", "check", "command", "human", "event")
PARALLEL = "parallel"  # a step that runs its `steps` at once; all must finish
ID = re.compile(r"[a-z][a-z0-9-]*")
EVENT = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+")
REF = re.compile(r"(factory|repo):([a-z][a-z0-9-]*)")
EXIT_TARGETS = ("next", "end")  # besides a step id

# Settings a step of each kind may carry (and its block may default), besides COMMON_KEYS.
KIND_SETTINGS: dict[str, frozenset[str]] = {
    "agent": frozenset({"model", "autonomy", "budget_minutes", "instructions", "tools", "outputs", "fresh_context"}),
    "check": frozenset({"blocking", "on_fail"}),
    "command": frozenset({"env", "on_fail"}),
    "human": frozenset({"asks", "ask_when", "shows", "exits", "timeout_hours"}),
    "event": frozenset({"event"}),
}
COMMON_KEYS = frozenset({"id", "type", "block", "condition", "description"})
PARALLEL_KEYS = frozenset({"id", "type", "steps", "condition", "description"})
BLOCK_KEYS = frozenset({
    "kind", "name", "does", "status", "task", "skill", "run", "run_from", "inputs", "outputs", "emits", "runs_in",
    "needs", "tools", "touches", "defaults",
})
PROCESS_KEYS = frozenset({"name", "description", "template", "runs_in", "triggers", "limits", "stages", "steps"})
LIMIT_KEYS = frozenset({"concurrent", "budget_minutes"})
GATE_KEYS = frozenset({"id", "name", "on", "env", "requires", "asks", "ask_when", "trust_after", "trusted_since"})
GATES_FILE_KEYS = frozenset({"autonomy_cap", "gates"})
FLOOR_KEYS = frozenset({"id", "on", "reason", "paths", "first", "env"})
CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


class SdlcError(ValueError):
    """A factory file (catalog, block, template) is malformed: the factory itself is broken."""


@dataclass
class Catalog:
    models: list[str]
    autonomy: list[str]
    asks: list[str]
    executors: list[str]
    human_outcomes: list[str]
    input_types: list[str]
    trust_after: int
    max_retries: int
    chain_depth: int
    outputs: dict[str, str]  # output -> least autonomy that may return it
    tools: dict[str, str]
    resources: dict[str, str]
    events: dict[str, str]
    defaults: dict[str, dict[str, Any]]
    default_gates: list[dict[str, Any]]
    floors: list[dict[str, Any]]


@dataclass
class Block:
    ref: str  # factory:<id> or repo:<id>
    kind: str
    name: str
    does: str
    path: Path
    status: str = "available"
    task: str = ""  # the backlog item that builds a planned block
    skill: str = ""  # agent: the skill it runs
    run: list[str] = field(default_factory=list)  # check / command: the commands
    run_from: str = ""  # check / command: a .factory.toml setting holding the commands (e.g. "verify")
    inputs: list[str] = field(default_factory=list)
    outputs: dict[str, int] = field(default_factory=dict)  # output -> most per run
    emits: list[str] = field(default_factory=list)
    runs_in: list[str] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)  # .factory.toml settings that must be set
    tools: list[str] = field(default_factory=list)
    touches: str = ""  # a resource its steps touch (their gates apply)
    defaults: dict[str, Any] = field(default_factory=dict)

    @property
    def planned(self) -> bool:
        return self.status == "planned"


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class Project:
    cfg: Config
    catalog: Catalog
    blocks: dict[str, Block]
    processes: dict[str, dict[str, Any]]  # id -> definition
    paths: dict[str, Path]  # process id -> its file
    gates: dict[str, Any]  # the gates file, defaults filled
    report: Report  # problems found while loading

    @property
    def dir(self) -> Path:
        return self.cfg.root / self.cfg.sdlc.dir


# ---------------------------------------------------------------------------------------------------------- loading


def _toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise SdlcError(f"{path}: not valid TOML ({e})") from None


def _strs(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v for v in value)


def load_catalog(directory: Path = FACTORY_DIR) -> Catalog:
    path = directory / "catalog.toml"
    raw = _toml(path)
    lists = ("models", "autonomy", "asks", "executors", "human_outcomes", "input_types")
    tables = ("outputs", "tools", "resources", "events")
    ints = ("trust_after", "max_retries", "chain_depth")
    known = set(lists) | set(tables) | set(ints) | {"defaults", "default_gates", "floors"}
    if unknown := sorted(set(raw) - known):
        raise SdlcError(f"{path}: unknown keys {unknown}")
    for key in lists:
        if not _strs(raw.get(key)) or len(set(raw[key])) != len(raw[key]):
            raise SdlcError(f"{path}: {key} must be a list of distinct names")
    for key in tables:
        if not isinstance(raw.get(key), dict) or not all(isinstance(v, str) for v in raw[key].values()):
            raise SdlcError(f"{path}: [{key}] must be a table of names to text")
    for key in ints:
        if not isinstance(raw.get(key), int) or isinstance(raw[key], bool) or raw[key] < 1:
            raise SdlcError(f"{path}: {key} must be a whole number of at least 1")
    if bad := [o for o, least in raw["outputs"].items() if least not in raw["autonomy"]]:
        raise SdlcError(f"{path}: [outputs] {bad} name an autonomy level not in `autonomy`")
    if bad := [e for e in raw["events"] if not EVENT.fullmatch(e)]:
        raise SdlcError(f"{path}: event names {bad} must look like source.what (lowercase)")
    defaults = raw.get("defaults", {})
    if unknown := sorted(set(defaults) - set(KINDS)):
        raise SdlcError(f"{path}: [defaults] for unknown kinds {unknown}")
    catalog = Catalog(
        models=raw["models"], autonomy=raw["autonomy"], asks=raw["asks"], executors=raw["executors"],
        human_outcomes=raw["human_outcomes"], input_types=raw["input_types"], trust_after=raw["trust_after"],
        max_retries=raw["max_retries"], chain_depth=raw["chain_depth"], outputs=raw["outputs"], tools=raw["tools"],
        resources=raw["resources"], events=raw["events"], defaults=defaults,
        default_gates=raw.get("default_gates", []), floors=raw.get("floors", []),
    )
    problems = []
    for kind, values in defaults.items():
        for key, value in values.items():
            problems += _setting_problems(kind, key, value, catalog, None, None)
    for gate in catalog.default_gates:
        problems += _gate_problems(gate, catalog)
    problems += _floor_problems(catalog)
    if problems:
        raise SdlcError(f"{path}: " + "; ".join(problems))
    return catalog


def load_block(path: Path, ref: str, catalog: Catalog) -> tuple[Block | None, list[str]]:
    """A block file and its problems (a block with problems is still returned when its kind is known)."""
    try:
        raw = _toml(path)
    except SdlcError as e:
        return None, [str(e)]
    problems = [f"unknown key `{k}`" for k in sorted(set(raw) - BLOCK_KEYS)]
    kind = raw.get("kind")
    if kind not in KINDS:
        return None, [f"{path}: `kind` must be one of {', '.join(KINDS)}"]
    for key in ("name", "does"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            problems.append(f"`{key}` is required (text)")
    status = raw.get("status", "available")
    if status not in ("available", "planned"):
        problems.append('`status` is "available" or "planned"')
    if status == "planned" and not isinstance(raw.get("task"), str):
        problems.append("a planned block names the backlog `task` that builds it")
    for key in ("inputs", "emits", "runs_in", "needs", "tools", "run"):
        if key in raw and not (isinstance(raw[key], list) and all(isinstance(v, str) and v for v in raw[key])):
            problems.append(f"`{key}` must be a list of text")
    outputs = raw.get("outputs", {})
    if not isinstance(outputs, dict) or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in outputs.values()):
        problems.append("`outputs` maps each output to the most per run (a whole number)")
        outputs = {}
    problems += [f"unknown output `{o}` (known: {', '.join(catalog.outputs)})" for o in outputs if o not in catalog.outputs]
    problems += [f"unknown executor `{x}`" for x in raw.get("runs_in", []) if x not in catalog.executors]
    if not raw.get("runs_in"):
        problems.append("`runs_in` names where it can run")
    problems += [f"unknown tool `{t}`" for t in raw.get("tools", []) if t not in catalog.tools]
    problems += [f"emits `{e}`: not an event name" for e in raw.get("emits", []) if not EVENT.fullmatch(e)]
    if raw.get("touches") and raw["touches"] not in catalog.resources:
        problems.append(f"touches unknown resource `{raw['touches']}`")
    if kind == "agent" and status == "available" and not raw.get("skill"):
        problems.append("an available agent names the `skill` it runs")
    if kind in ("check", "command") and status == "available" and not (raw.get("run") or raw.get("run_from")):
        problems.append(f"a {kind} has `run` (commands) or `run_from` (a .factory.toml setting)")
    if kind not in ("agent",) and (raw.get("skill") or raw.get("tools")):
        problems.append("only an agent has a `skill` or `tools`")
    defaults = raw.get("defaults", {})
    if not isinstance(defaults, dict):
        problems.append("`defaults` must be a table")
        defaults = {}
    for key, value in defaults.items():
        if key not in KIND_SETTINGS[kind]:
            problems.append(f"defaults.{key}: not a {kind} setting (known: {', '.join(sorted(KIND_SETTINGS[kind]))})")
        else:
            problems += [f"defaults.{p}" for p in _setting_problems(kind, key, value, catalog, None, None)]
    block = Block(
        ref=ref, kind=kind, name=str(raw.get("name", "")), does=str(raw.get("does", "")), path=path, status=status,
        task=str(raw.get("task", "")), skill=str(raw.get("skill", "")), run=list(raw.get("run", [])),
        run_from=str(raw.get("run_from", "")), inputs=list(raw.get("inputs", [])), outputs=dict(outputs),
        emits=list(raw.get("emits", [])), runs_in=list(raw.get("runs_in", [])), needs=list(raw.get("needs", [])),
        tools=list(raw.get("tools", [])), touches=str(raw.get("touches", "")), defaults=dict(defaults),
    )
    return block, [f"{path}: {p}" for p in problems]


def load_blocks(directory: Path, prefix: str, catalog: Catalog) -> tuple[dict[str, Block], list[str]]:
    blocks, problems = {}, []
    for path in sorted(directory.glob("*.toml")) if directory.is_dir() else []:
        if not ID.fullmatch(path.stem):
            problems.append(f"{path}: a block's file name is its id: lowercase letters, digits and dashes")
            continue
        block, found = load_block(path, f"{prefix}:{path.stem}", catalog)
        problems += found
        if block:
            blocks[block.ref] = block
    return blocks, problems


def factory_blocks(catalog: Catalog | None = None) -> dict[str, Block]:
    catalog = catalog or load_catalog()
    blocks, problems = load_blocks(FACTORY_DIR / "blocks", "factory", catalog)
    if problems:
        raise SdlcError("; ".join(problems))
    return blocks


def templates() -> dict[str, Path]:
    return {p.stem: p for p in sorted((FACTORY_DIR / "processes").glob("*.json"))}


def _json(path: Path) -> tuple[Any, list[str]]:
    try:
        return json.loads(path.read_text()), []
    except json.JSONDecodeError as e:
        return None, [f"{path}: not valid JSON ({e.msg}, line {e.lineno})"]


def load(cfg: Config) -> Project:
    """The project's SDLC: the factory's catalog and blocks, the project's blocks, processes and gates."""
    catalog = load_catalog()
    catalog.models = catalog.models + [m for m in cfg.sdlc.extra_models if m not in catalog.models]
    catalog.events = {**catalog.events, **{e: "this repo" for e in cfg.sdlc.extra_events}}
    report = Report()
    report.errors += [f".factory.toml: sdlc.extra_events `{e}` is not an event name" for e in cfg.sdlc.extra_events
                      if not EVENT.fullmatch(e)]
    folder = cfg.root / cfg.sdlc.dir
    blocks = factory_blocks(catalog)
    own, problems = load_blocks(folder / "blocks", "repo", catalog)
    report.errors += problems
    blocks.update(own)
    processes, paths = {}, {}
    for path in sorted((folder / "processes").glob("*.json")) if (folder / "processes").is_dir() else []:
        if not ID.fullmatch(path.stem):
            report.errors.append(f"{path}: a process's file name is its id: lowercase letters, digits and dashes")
            continue
        data, problems = _json(path)
        report.errors += problems
        if data is not None:
            processes[path.stem], paths[path.stem] = data, path
    gates_path = folder / "gates.json"
    gates: dict[str, Any] = {}
    if gates_path.is_file():
        data, problems = _json(gates_path)
        report.errors += problems
        gates = data if isinstance(data, dict) else {}
        if data is not None and not isinstance(data, dict):
            report.errors.append(f"{gates_path}: must be an object")
    gates = {"autonomy_cap": gates.get("autonomy_cap", catalog.autonomy[-1]),
             "gates": gates.get("gates", catalog.default_gates), **{k: v for k, v in gates.items() if k not in ("autonomy_cap", "gates")}}
    return Project(cfg=cfg, catalog=catalog, blocks=blocks, processes=processes, paths=paths, gates=gates, report=report)


# ------------------------------------------------------------------------------------------------------- validation


def _setting_problems(kind: str, key: str, value: Any, catalog: Catalog, block: Block | None,
                      step_ids: list[str] | None) -> list[str]:
    """Problems with one setting's value. `block`: the step's block (caps outputs and tools); `step_ids`: the
    process's earlier and all step ids, for exits and on_fail (None for a block default: not checked)."""
    def one_of(options: list[str]) -> list[str]:
        return [] if value in options else [f"{key}: `{value}` is not one of {', '.join(options)}"]

    def whole(least: int) -> list[str]:
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= least
        return [] if ok else [f"{key}: must be a whole number of at least {least}"]

    if key == "model":
        return one_of(catalog.models)
    if key == "autonomy":
        return one_of(catalog.autonomy)
    if key in ("budget_minutes", "timeout_hours"):
        return whole(1)
    if key in ("fresh_context", "blocking"):
        return [] if isinstance(value, bool) else [f"{key}: must be true or false"]
    if key in ("instructions", "shows", "ask_when", "env"):
        return [] if isinstance(value, str) and value.strip() else [f"{key}: must be text"]
    if key == "asks":
        return one_of(catalog.asks)
    if key == "tools":
        if not _strs(value):
            return [f"{key}: must be a list of tool names"]
        allowed = block.tools if block else list(catalog.tools)
        return [f"tools: `{t}` is not one its block allows ({', '.join(allowed) or 'none'})" for t in value if t not in allowed]
    if key == "outputs":
        if not isinstance(value, dict):
            return ["outputs: maps each output to the most per run"]
        found = []
        for name, most in value.items():
            cap = block.outputs.get(name) if block else None
            if block and cap is None:
                found.append(f"outputs: `{name}` is not one its block produces ({', '.join(block.outputs) or 'none'})")
            elif not isinstance(most, int) or isinstance(most, bool) or most < 0:
                found.append(f"outputs.{name}: must be a whole number")
            elif cap is not None and most > cap:
                found.append(f"outputs.{name}: {most} is more than its block allows ({cap})")
        return found
    if key == "event":
        return [] if isinstance(value, str) and EVENT.fullmatch(value) else [f"event: `{value}` is not an event name (source.what)"]
    if key == "exits":
        if not isinstance(value, dict):
            return ["exits: maps each outcome to the step it leads to"]
        found = [f"exits: `{o}` is not an outcome ({', '.join(catalog.human_outcomes)})" for o in value if o not in catalog.human_outcomes]
        if step_ids is not None:
            found += [f"exits.{o}: `{t}` is not a step of this process (or next, end)" for o, t in value.items()
                      if t not in EXIT_TARGETS and t not in step_ids]
        return found
    if key == "on_fail":
        if not isinstance(value, dict) or set(value) - {"back_to", "retries"} or "back_to" not in value:
            return ["on_fail: has `back_to` (an earlier step) and, optionally, `retries`"]
        found = []
        if "retries" in value and (not isinstance(value["retries"], int) or isinstance(value["retries"], bool)
                                   or not 0 <= value["retries"] <= catalog.max_retries):
            found.append(f"on_fail.retries: 0 to {catalog.max_retries}")
        if step_ids is not None and value["back_to"] not in step_ids:
            found.append(f"on_fail.back_to: `{value.get('back_to')}` is not an earlier step")
        return found
    return []


def cron_problem(cron: Any) -> str | None:
    """None when `cron` is five numeric fields (minute hour day month weekday) of *, n, a-b, */n, a-b/n, lists."""
    if not isinstance(cron, str):
        return "a schedule is a cron line"
    parts = cron.split()
    if len(parts) != 5:
        return f"`{cron}`: a cron line has 5 fields (minute hour day month weekday)"
    for part, (low, high), name in zip(parts, CRON_RANGES, ("minute", "hour", "day", "month", "weekday")):
        for item in part.split(","):
            base, _, step = item.partition("/")
            if step and (not step.isdigit() or int(step) < 1):
                return f"`{cron}`: {name} step `{step}` must be a positive number"
            if base == "*":
                continue
            low_s, _, high_s = base.partition("-")
            if not low_s.isdigit() or (high_s and not high_s.isdigit()):
                return f"`{cron}`: {name} `{item}` must be numbers"
            a, b = int(low_s), int(high_s or low_s)
            if not (low <= a <= b <= high):
                return f"`{cron}`: {name} `{item}` is outside {low}-{high}"
    return None


def emitted(process: dict[str, Any], blocks: dict[str, Block]) -> set[str]:
    """Events a process may emit: its event steps and what its blocks declare."""
    found: set[str] = set()
    for step in _all_steps(process):
        if step.get("type") == "event" and isinstance(step.get("event"), str):
            found.add(step["event"])
        block = blocks.get(step.get("block", ""))
        if block:
            found.update(block.emits)
    return found


def _all_steps(process: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for step in process.get("steps", []) if isinstance(process.get("steps"), list) else []:
        if isinstance(step, dict):
            out.append(step)
            if step.get("type") == PARALLEL and isinstance(step.get("steps"), list):
                out += [s for s in step["steps"] if isinstance(s, dict)]
    return out


def process_problems(process: Any, project: Project, known_events: set[str]) -> list[str]:
    """Every problem with one process definition (the factory's meaning; the runtime checks execution)."""
    if not isinstance(process, dict):
        return ["must be an object"]
    cat, problems = project.catalog, []
    problems += [f"unknown key `{k}` (known: {', '.join(sorted(PROCESS_KEYS))})" for k in sorted(set(process) - PROCESS_KEYS)]
    if not isinstance(process.get("name"), str) or not process["name"].strip():
        problems.append("`name` is required")
    runs_in = process.get("runs_in")
    if not _strs(runs_in) or not runs_in:
        problems.append(f"`runs_in` lists where it may run ({', '.join(cat.executors)})")
        runs_in = []
    problems += [f"runs_in: unknown executor `{x}`" for x in runs_in if x not in cat.executors]
    problems += _trigger_problems(process.get("triggers"), cat, known_events)
    limits = process.get("limits", {})
    if not isinstance(limits, dict):
        problems.append("`limits` must be an object")
    else:
        problems += [f"limits: unknown `{k}`" for k in sorted(set(limits) - LIMIT_KEYS)]
        problems += [f"limits.{k}: must be a whole number of at least 1" for k, v in limits.items()
                     if k in LIMIT_KEYS and (not isinstance(v, int) or isinstance(v, bool) or v < 1)]
    steps = process.get("steps")
    if not isinstance(steps, list) or not steps:
        return problems + ["`steps` must be a non-empty list"]
    all_ids = [s.get("id") for s in _all_steps(process)]
    top_ids = [s.get("id") for s in steps if isinstance(s, dict)]
    problems += [f"step id `{i}` is used twice" for i in sorted({i for i in all_ids if isinstance(i, str) and all_ids.count(i) > 1})]
    earlier: list[str] = []
    for step in steps:
        problems += _step_problems(step, project, runs_in, earlier, top_ids, inside=False)
        if isinstance(step, dict) and isinstance(step.get("id"), str):
            earlier.append(step["id"])
    problems += _stage_problems(process.get("stages"), top_ids)
    return problems


def _trigger_problems(triggers: Any, cat: Catalog, known_events: set[str]) -> list[str]:
    if not isinstance(triggers, list) or not triggers:
        return ["`triggers` lists what starts it: {\"event\": …}, {\"schedule\": …} or {\"manual\": {…}}"]
    found = []
    for n, trigger in enumerate(triggers, 1):
        where = f"triggers[{n}]"
        kinds = set(trigger) & {"event", "schedule", "manual"} if isinstance(trigger, dict) else set()
        if len(kinds) != 1:
            found.append(f"{where}: exactly one of event, schedule, manual")
            continue
        extra = set(trigger) - kinds - ({"condition"} if "event" in kinds else set())
        found += [f"{where}: unknown key `{k}`" for k in sorted(extra)]
        if "event" in trigger:
            event = trigger["event"]
            if event not in known_events:
                found.append(f"{where}: unknown event `{event}` (the catalog's, [sdlc] extra_events, or one a process emits)")
            if "condition" in trigger and (not isinstance(trigger["condition"], str) or not trigger["condition"].strip()):
                found.append(f"{where}: condition must be an expression")
        elif "schedule" in trigger:
            if problem := cron_problem(trigger["schedule"]):
                found.append(f"{where}: {problem}")
        else:
            manual = trigger["manual"]
            inputs = manual.get("inputs", {}) if isinstance(manual, dict) else None
            if not isinstance(inputs, dict) or set(manual) - {"inputs"}:
                found.append(f"{where}: manual is {{\"inputs\": {{name: type}}}}")
                continue
            found += [f"{where}: input `{k}` has unknown type `{v}` ({', '.join(cat.input_types)})"
                      for k, v in inputs.items() if v not in cat.input_types]
    return found


def _stage_problems(stages: Any, top_ids: list[Any]) -> list[str]:
    if not isinstance(stages, list) or not stages:
        return ["`stages` groups the steps: [{\"name\": …, \"steps\": [ids]}]"]
    found, placed, names = [], [], []
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) != {"name", "steps"} or not _strs(stage.get("steps")) \
                or not isinstance(stage.get("name"), str) or not stage["name"].strip():
            found.append("each stage is {\"name\": text, \"steps\": [step ids]}")
            continue
        names.append(stage["name"])
        placed += stage["steps"]
    found += [f"stage `{n}` appears twice" for n in sorted({n for n in names if names.count(n) > 1})]
    found += [f"stages name `{i}`, which is not a top-level step" for i in placed if i not in top_ids]
    found += [f"step `{i}` is in no stage" for i in top_ids if isinstance(i, str) and i not in placed]
    found += [f"step `{i}` is in more than one stage" for i in sorted({i for i in placed if placed.count(i) > 1})]
    order = [i for i in top_ids if i in placed]
    if not found and [i for i in placed] != order:
        found.append("stages list the steps in a different order than `steps`")
    return found


def _step_problems(step: Any, project: Project, runs_in: list[str], earlier: list[str], top_ids: list[Any],
                   inside: bool) -> list[str]:
    if not isinstance(step, dict):
        return ["each step must be an object"]
    sid = step.get("id")
    label = f"step `{sid}`" if isinstance(sid, str) else "a step"
    found = []
    if not isinstance(sid, str) or not ID.fullmatch(sid):
        found.append("needs an `id`: lowercase letters, digits and dashes")
    kind = step.get("type")
    if "condition" in step and (not isinstance(step["condition"], str) or not step["condition"].strip()):
        found.append("condition must be an expression")
    if kind == PARALLEL:
        if inside:
            found.append("a parallel step can't sit inside another")
        found += [f"unknown key `{k}`" for k in sorted(set(step) - PARALLEL_KEYS)]
        inner = step.get("steps")
        if not isinstance(inner, list) or len(inner) < 2:
            found.append("a parallel step runs two or more `steps`")
            inner = []
        problems = [f"{label}: {p}" for p in found]
        for sub in inner:
            problems += _step_problems(sub, project, runs_in, earlier, top_ids, inside=True)
        return problems
    if kind not in KINDS:
        return [f"{label}: `type` is one of {', '.join(KINDS + (PARALLEL,))}"]
    found += [f"unknown key `{k}` (a {kind} step has: {', '.join(sorted(COMMON_KEYS | KIND_SETTINGS[kind]))})"
              for k in sorted(set(step) - COMMON_KEYS - KIND_SETTINGS[kind])]
    ref = step.get("block")
    block = project.blocks.get(ref) if isinstance(ref, str) else None
    if block is None:
        found.append(f"block `{ref}` not found (factory:<id> or repo:<id>)")
    else:
        if block.kind != kind:
            found.append(f"block `{ref}` is a {block.kind}, the step's type is {kind}")
        if block.planned:
            found.append(f"block `{ref}` is planned ({block.task}), not available yet")
        if missing := [x for x in runs_in if x not in block.runs_in]:
            found.append(f"block `{ref}` can't run in {', '.join(missing)} (it runs in {', '.join(block.runs_in)})")
        for need in block.needs:
            if not getattr(project.cfg, need, None):
                found.append(f"block `{ref}` needs `{need}` set in .factory.toml")
    cat = project.catalog
    for key in sorted(set(step) & KIND_SETTINGS.get(kind, frozenset())):
        ids = earlier if key == "on_fail" else [i for i in top_ids if isinstance(i, str)] if key == "exits" else None
        found += _setting_problems(kind, key, step[key], cat, block, ids)
    if block and block.kind == kind and kind in ("agent", "human"):
        values = resolve(step, block, cat)
        if kind == "agent":
            level = values["autonomy"][0]
            cap = project.gates.get("autonomy_cap")
            if level in cat.autonomy and cap in cat.autonomy and cat.autonomy.index(level) > cat.autonomy.index(cap):
                found.append(f"autonomy `{level}` is above the project's cap `{cap}`")
            outputs = values.get("outputs", ({}, ""))[0] or {}
            for name in outputs:
                least = cat.outputs.get(name)
                if least and level in cat.autonomy and cat.autonomy.index(level) < cat.autonomy.index(least):
                    found.append(f"output `{name}` needs autonomy `{least}` or more (the step has `{level}`)")
        if kind == "human" and values["asks"][0] == "when" and not values.get("ask_when", (None,))[0]:
            found.append("asks `when` needs `ask_when` (the rule)")
    if kind == "event" and "event" not in step:
        found.append("an event step names its `event`")
    if kind == "command" and block and block.touches == "deploy" and "env" not in step and "env" not in block.defaults:
        found.append("a deploy step names its `env`")
    return [f"{label}: {p}" for p in found]


def resolve(step: dict[str, Any], block: Block, catalog: Catalog) -> dict[str, tuple[Any, str]]:
    """Each setting of a step's kind with its value and where it came from: step, block, catalog."""
    out: dict[str, tuple[Any, str]] = {}
    base = catalog.defaults.get(block.kind, {})
    for key in sorted(KIND_SETTINGS[block.kind]):
        if key in step:
            out[key] = (step[key], "step")
        elif key in block.defaults:
            out[key] = (block.defaults[key], "block")
        elif key in base:
            out[key] = (base[key], "catalog")
    if block.kind == "agent":
        out.setdefault("outputs", (dict(block.outputs), "block"))
        out.setdefault("tools", (list(block.tools), "block"))
    return out


def _gate_problems(gate: Any, catalog: Catalog) -> list[str]:
    if not isinstance(gate, dict):
        return ["each gate is an object"]
    gid = gate.get("id", "?")
    found = [f"unknown key `{k}`" for k in sorted(set(gate) - GATE_KEYS)]
    if not isinstance(gate.get("id"), str) or not ID.fullmatch(gate["id"]):
        found.append("`id`: lowercase letters, digits and dashes")
    if not isinstance(gate.get("name"), str) or not gate["name"].strip():
        found.append("`name` is required")
    if gate.get("on") not in catalog.resources:
        found.append(f"`on` is one of {', '.join(catalog.resources)}")
    if "env" in gate and (not isinstance(gate["env"], str) or not gate["env"].strip()):
        found.append("`env` must be text")
    if "requires" in gate and not _strs(gate["requires"]):
        found.append("`requires` lists check names")
    asks = gate.get("asks", "always")
    if asks not in catalog.asks:
        found.append(f"`asks` is one of {', '.join(catalog.asks)}")
    if asks == "when" and not (isinstance(gate.get("ask_when"), str) and gate["ask_when"].strip()):
        found.append("asks `when` needs `ask_when`")
    if "trust_after" in gate and (not isinstance(gate["trust_after"], int) or isinstance(gate["trust_after"], bool) or gate["trust_after"] < 1):
        found.append("`trust_after` is a whole number of at least 1")
    if "trusted_since" in gate:
        if asks != "until-trusted":
            found.append("`trusted_since` belongs only to a gate that asks until-trusted")
        try:
            dt.date.fromisoformat(str(gate["trusted_since"]))
        except ValueError:
            found.append("`trusted_since` is a date, YYYY-MM-DD")
    return [f"gate `{gid}`: {p}" for p in found]


def _floor_problems(catalog: Catalog) -> list[str]:
    found, ids = [], []
    for floor in catalog.floors:
        if not isinstance(floor, dict):
            found.append("each floor is a table")
            continue
        fid = floor.get("id")
        ids.append(fid)
        if unknown := sorted(set(floor) - FLOOR_KEYS):
            found.append(f"floor `{fid}`: unknown keys {unknown}")
        if floor.get("on") not in catalog.resources:
            found.append(f"floor `{fid}`: `on` is one of {', '.join(catalog.resources)}")
        if not isinstance(floor.get("reason"), str):
            found.append(f"floor `{fid}`: `reason` is required")
        if "paths" in floor and not _strs(floor["paths"]):
            found.append(f"floor `{fid}`: `paths` lists globs")
    found += [f"floor id `{i}` is used twice" for i in sorted({str(i) for i in ids if ids.count(i) > 1})]
    return found


def gates_problems(project: Project) -> list[str]:
    gates, cat = project.gates, project.catalog
    found = [f"unknown key `{k}`" for k in sorted(set(gates) - GATES_FILE_KEYS)]
    if gates.get("autonomy_cap") not in cat.autonomy:
        found.append(f"autonomy_cap is one of {', '.join(cat.autonomy)}")
    entries = gates.get("gates")
    if not isinstance(entries, list):
        return found + ["`gates` must be a list"]
    ids = [g.get("id") for g in entries if isinstance(g, dict)]
    found += [f"gate id `{i}` is used twice" for i in sorted({str(i) for i in ids if ids.count(i) > 1})]
    floors = {f.get("id") for f in cat.floors}
    found += [f"gate `{i}`: that id is a factory floor, which a project can't change" for i in ids if i in floors]
    for gate in entries:
        found += _gate_problems(gate, cat)
    return found


def event_cycles(project: Project) -> list[list[str]]:
    """Process chains that start each other through events, round to the first (each cycle once)."""
    starts: dict[str, set[str]] = {}
    for pid, proc in project.processes.items():
        for trigger in proc.get("triggers", []) if isinstance(proc.get("triggers"), list) else []:
            if isinstance(trigger, dict) and isinstance(trigger.get("event"), str):
                starts.setdefault(trigger["event"], set()).add(pid)
    edges = {pid: sorted({q for e in emitted(proc, project.blocks) for q in starts.get(e, ())})
             for pid, proc in project.processes.items()}
    cycles, seen = [], set()

    def walk(path: list[str]) -> None:
        for nxt in edges.get(path[-1], []):
            if nxt == path[0]:
                key = frozenset(path)
                if key not in seen:
                    seen.add(key)
                    cycles.append(path + [nxt])
            elif nxt not in path and nxt > path[0]:
                walk(path + [nxt])

    for pid in sorted(edges):
        walk([pid])
    return cycles


def check(cfg: Config) -> Report:
    """Validate the project's blocks, processes and gates. Problems name their file."""
    project = load(cfg)
    report = project.report
    events = set(project.catalog.events)
    for proc in project.processes.values():
        events |= emitted(proc, project.blocks)
    for pid, proc in project.processes.items():
        report.errors += [f"{project.paths[pid]}: {p}" for p in process_problems(proc, project, events)]
    for block in project.blocks.values():
        if block.ref.startswith("repo:") and block.kind == "agent" and block.skill and not any(
                (cfg.root / d / block.skill / "SKILL.md").is_file() for d in cfg.sdlc.skills_dirs):
            report.errors.append(f"{block.path}: skill `{block.skill}` not found in {', '.join(cfg.sdlc.skills_dirs)}")
    for block in project.blocks.values():
        if block.run_from and not getattr(cfg, block.run_from, None):
            report.errors.append(f"{block.path}: run_from `{block.run_from}` is not a .factory.toml setting with commands")
    gates_file = project.dir / "gates.json"
    report.errors += [f"{gates_file}: {p}" for p in gates_problems(project)]
    for cycle in event_cycles(project):
        report.warnings.append(f"processes start each other in a loop: {' → '.join(cycle)}; the engine stops a "
                               f"chain at depth {project.catalog.chain_depth}")
    return report


# ----------------------------------------------------------------------------------------------------------- output


def card(project: Project, ref: str) -> dict[str, Any]:
    """Everything the Desk and the CLI show about a block: what it is, what it needs, and where it is used."""
    block = project.blocks[ref]
    used = [f"{pid}:{s.get('id')}" for pid, proc in project.processes.items() for s in _all_steps(proc)
            if s.get("block") == ref]
    return {
        "ref": block.ref, "kind": block.kind, "name": block.name, "does": block.does,
        "status": block.status if not block.planned else f"planned ({block.task})",
        "source": str(block.path.relative_to(project.cfg.root) if block.path.is_relative_to(project.cfg.root) else block.path),
        "skill": block.skill, "run": block.run or ([f"[{block.run_from}] in .factory.toml"] if block.run_from else []),
        "inputs": block.inputs, "outputs": block.outputs, "emits": block.emits, "runs_in": block.runs_in,
        "needs": block.needs, "tools": block.tools, "touches": block.touches,
        "defaults": {**project.catalog.defaults.get(block.kind, {}), **block.defaults}, "used_by": used,
    }


def _card_text(c: dict[str, Any]) -> str:
    lines = [f"{c['name']}  ({c['kind']}, {c['ref']}, {c['status']})", f"  {c['does']}", ""]
    for label, key in (("Source", "source"), ("Skill", "skill"), ("Runs", "run"), ("Inputs", "inputs"),
                       ("Outputs", "outputs"), ("Emits", "emits"), ("Runs in", "runs_in"), ("Needs", "needs"),
                       ("Tools", "tools"), ("Touches", "touches"), ("Defaults", "defaults"), ("Used by", "used_by")):
        value = c[key]
        if isinstance(value, dict):
            value = ", ".join(f"{k} {v}" for k, v in value.items())
        elif isinstance(value, list):
            value = ", ".join(value)
        lines.append(f"  {label + ':':<10} {value or '—'}")
    return "\n".join(lines)


def write_process(project: Project, pid: str, process: dict[str, Any]) -> Path:
    """The one writer of a process file: validated first, then written whole (temp file, then rename)."""
    if not ID.fullmatch(pid):
        raise SdlcError(f"`{pid}`: a process id is lowercase letters, digits and dashes")
    events = set(project.catalog.events) | emitted(process, project.blocks)
    for proc in project.processes.values():
        events |= emitted(proc, project.blocks)
    if problems := process_problems(process, project, events):
        raise SdlcError(f"{pid}: " + "; ".join(problems))
    folder = project.dir / "processes"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{pid}.json"
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=f".{pid}.", suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(process, indent=2) + "\n")
    os.replace(tmp, target)
    return target


def main(argv: list[str], cfg: Config) -> int:
    ap = argparse.ArgumentParser(prog="factory sdlc", description="the project's SDLC: blocks, processes, gates")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("check", help="validate the project's blocks, processes and gates")
    sub.add_parser("blocks", help="every block this project can use").add_argument("--json", action="store_true")
    bp = sub.add_parser("block", help="one block's card")
    bp.add_argument("ref")
    bp.add_argument("--json", action="store_true")
    sp = sub.add_parser("show", help="a process with each step's settings resolved and their source")
    sp.add_argument("process")
    sp.add_argument("--json", action="store_true")
    sub.add_parser("templates", help="the factory's template processes")
    np = sub.add_parser("new", help="start a process from a template")
    np.add_argument("process")
    np.add_argument("--from", dest="template", required=True)
    args = ap.parse_args(argv)
    project = load(cfg)

    if args.action == "check":
        report = check(cfg)
        for line in report.errors:
            print(f"error: {line}")
        for line in report.warnings:
            print(f"warning: {line}")
        counts = f"{len(project.processes)} processes, {sum(b.ref.startswith('repo:') for b in project.blocks.values())} own blocks"
        if not report.ok:
            print(f"sdlc check FAILED: {len(report.errors)} problems ({counts})", file=sys.stderr)
            return 1
        print(f"sdlc check passed: {counts}")
        return 0
    if args.action == "blocks":
        cards = [card(project, ref) for ref in sorted(project.blocks)]
        if args.json:
            print(json.dumps(cards, indent=2))
        else:
            for c in cards:
                print(f"{c['ref']:<28} {c['kind']:<8} {c['status']:<16} {c['name']}")
        return 0
    if args.action == "block":
        if args.ref not in project.blocks:
            print(f"no block `{args.ref}` (see `factory sdlc blocks`)", file=sys.stderr)
            return 1
        c = card(project, args.ref)
        print(json.dumps(c, indent=2) if args.json else _card_text(c))
        return 0
    if args.action == "show":
        proc = project.processes.get(args.process)
        if proc is None:
            print(f"no process `{args.process}` in {project.dir / 'processes'}", file=sys.stderr)
            return 1
        steps = []
        for step in _all_steps(proc):
            block = project.blocks.get(step.get("block", ""))
            values = resolve(step, block, project.catalog) if block else {}
            steps.append({"id": step.get("id"), "type": step.get("type"), "block": step.get("block"),
                          "settings": {k: {"value": v, "from": src} for k, (v, src) in values.items()}})
        if args.json:
            print(json.dumps({"process": args.process, "name": proc.get("name"), "steps": steps}, indent=2))
        else:
            print(f"{proc.get('name')} ({args.process}), runs in {', '.join(proc.get('runs_in', []))}")
            for s in steps:
                settings = ", ".join(f"{k} {v['value']} ({v['from']})" for k, v in s["settings"].items())
                print(f"  {s['id']:<16} {s['type']:<8} {s['block'] or '':<26} {settings}")
        return 0
    if args.action == "templates":
        for tid, path in templates().items():
            data, _ = _json(path)
            print(f"{tid:<16} {(data or {}).get('name', '')}: {(data or {}).get('description', '')}")
        return 0
    if args.action == "new":
        found = templates()
        if args.template not in found:
            print(f"no template `{args.template}` (see `factory sdlc templates`)", file=sys.stderr)
            return 1
        if args.process in project.processes:
            print(f"process `{args.process}` already exists", file=sys.stderr)
            return 1
        data, problems = _json(found[args.template])
        if problems:
            print(problems[0], file=sys.stderr)
            return 1
        data = {**data, "template": args.template}
        try:
            print(f"wrote {write_process(project, args.process, data)}")
        except SdlcError as e:
            print(e, file=sys.stderr)
            return 1
        return 0
    return 2
