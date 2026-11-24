"""The deterministic half of the efficiency scout: where agent sessions and an app's own model calls spend tokens on
repeated or unnecessary work, with the tokens and dollars at stake. The judgement half (sample the worst, propose
fixes, file them) is the `/factory:efficiency` skill. Spec: docs/tasks/F-32-efficiency-scout.md.

Sources: the agent client's session transcripts (`[efficiency] projects_dir`, Claude Code's JSONL by default) —
interactive sessions in every worktree and the factory's own unattended runs — and the app's runtime usage, which a
repo exposes as a command printing JSONL (`[efficiency] runtime_usage`, schema in RUNTIME_FIELDS).

Privacy: transcripts hold private data. Nothing here keeps prompt text, file contents or tool output: a session is
reduced to metrics, ids, timestamps, file paths, tool names and command names (`command_head`) while it is read,
and every finding is built from those alone.

Project and department: every finding carries the project (the repo, worktrees folded in) and the department
that owns the work, resolved per session as `department_of` says, so fixes are grouped by who owns them.

Usage and pricing: `usage.py` (F-31) owns per-message usage records and the price table. This module reads the
richer event stream it needs (tool calls, their inputs and result sizes, errors, prompts) itself; once usage.py
lands, `Turn` should come from its records and `price` from its table, so the two never disagree on a dollar.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shlex
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from . import runner
from .config import Config, Efficiency

# Where unattended runs keep their worktrees: `factory run` (runner.HOME) and the engine (engine/engine/settings.py).
ENGINE_STATE = Path(os.environ.get("FACTORY_ENGINE_STATE", "~/.local/state/software-factory")).expanduser()

DETECTORS = (
    "repeat-read", "big-read", "oversized-result", "repeat-call", "retry-unchanged", "polling", "cache-rebuild",
    "long-context", "subagent-overspawn", "subagent-reread", "base-context", "duplicate-effort",
    "permission-prompts", "user-correction",
    "runtime-repeat-prompt", "runtime-expensive-model", "runtime-retry", "runtime-unused",
)
# Detectors that count events rather than dollars: reported by count, never dropped by min_usd.
COUNTED = {"permission-prompts", "user-correction"}

# Departments are functions; the project is a separate dimension. Work with no declared owner is the principal's own.
DEPARTMENTS = ("Chief of Staff", "Product", "Design", "Engineering", "Quality", "Release", "Upkeep", "Direct")
DIRECT = "Direct"
UNASSIGNED = "unassigned"  # a runtime record that names no department

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SPAWN_TOOLS = {"Agent", "Task"}
POLL_TOOLS = {"BashOutput", "TaskOutput"}
ASK_TOOLS = {"AskUserQuestion", "ExitPlanMode"}  # asking the user is their purpose, not a permission prompt

# A typed prompt that corrects the agent. Only the category name is kept, never the text.
DEFAULT_CORRECTIONS = {
    "no": r"^(no|nope|nah)\b",
    "stop": r"^(stop|wait|hold on|don'?t|do not)\b",
    "repeat": r"\b(i said|i told you|as i said|i already (said|told|asked)|i asked you)\b",
    "wrong": r"\b(that'?s (not|wrong)|this is wrong|not what i (asked|meant|wanted)|you (didn'?t|forgot|missed|ignored))\b",
    "revert": r"\b(revert|undo|roll ?back|put it back)\b",
    "why": r"^why (did|do|are|is|would|didn'?t|not|does|should)\b",
}

# $ per million tokens: (input, output, 5-minute cache write, 1-hour cache write, cache read), first match wins.
# TODO(F-33, PR #26): usage.py owns the price table. These rows mirror its values for the models in use so both
# report the same dollar; when #26 lands, `price` reads usage.py's table and this list goes.
Price = tuple[float, float, float, float, float]
PRICES: list[tuple[str, Price]] = [
    (r"<synthetic>", (0, 0, 0, 0, 0)),
    (r"opus-5-5", (4, 20, 5, 8, 0.20)),
    (r"opus-4(-1)?(-\d{8})?$|opus-4-1|claude-3-opus", (15, 75, 18.75, 30, 1.50)),
    (r"opus", (5, 25, 6.25, 10, 0.50)),
    (r"sonnet-5-5", (2, 10, 2.5, 4, 0.10)),
    (r"sonnet-5", (2, 10, 2.5, 4, 0.20)),
    (r"sonnet", (3, 15, 3.75, 6, 0.30)),
    (r"haiku-5", (0.10, 0.50, 0.125, 0.20, 0.01)),
    (r"haiku-4", (1, 5, 1.25, 2, 0.10)),
    (r"haiku", (0.8, 4, 1.0, 1.6, 0.08)),
]
DEFAULT_PRICE: Price = (3, 15, 3.75, 6, 0.30)
CHEAPER_TIER_SAVING = 0.6  # share of cost a move one tier down saves (top tier to mid tier at list prices)

# Runtime usage, one JSON object per line. Required: ts, model. Everything else defaults as shown. v2 added
# `department` and `process`; a v1 line (without them) is still read.
RUNTIME_SCHEMA = 2
RUNTIME_FIELDS = {
    "v": "schema version, 2 (optional)",
    "ts": "ISO 8601 time of the call",
    "kind": "llm_call | agent_job (default llm_call)",
    "agent": "who made the call (agent, department or service name)",
    "task": "what it was for (job type, caller, workflow type)",
    "department": "the app's own department that owns the agent (from its org declaration); v2",
    "process": "the process or workflow the call ran in, if any; v2",
    "run_id": "groups the calls of one job, session or workflow run (optional)",
    "model": "the model that answered",
    "input_tokens": "int, default 0",
    "output_tokens": "int, default 0",
    "cache_write_tokens": "int, default 0",
    "cache_read_tokens": "int, default 0",
    "cost_usd": "float or null (null = priced from the tokens)",
    "duration_ms": "int or null",
    "prompt_hash": "hash of the normalised request (system + messages + tools); never the text",
    "outcome": "ok | error | retry | expired | cancelled (default ok)",
    "attempt": "int, 1 for the first try (default 1)",
    "output_used": "true | false | null: whether anything consumed the result (null = unknown)",
}


# ---------------------------------------------------------------------------------------------------- reading


@dataclass
class Turn:
    """One model request: its usage, priced."""

    ts: float
    model: str
    mid: str = ""  # message id: a resumed or forked session copies earlier turns into its own file
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    n_tools: int = 0

    @property
    def context(self) -> int:
        return self.input + self.cache_write + self.cache_read

    @property
    def usd(self) -> float:
        pin, pout, pw5, pw1h, pread = price(self.model)
        cw5 = self.cache_write - self.cache_write_1h
        return (self.input * pin + cw5 * pw5 + self.cache_write_1h * pw1h + self.cache_read * pread
                + self.output * pout) / 1e6


@dataclass
class Call:
    """One tool call, reduced to what is safe to keep: no input values except paths and the command head."""

    id: str
    name: str
    ts: float
    turn: int
    key: str  # hash of the normalised input: equal keys = identical calls
    target: str = ""  # file path, command head, skill or subagent type
    span: str = ""  # a Read's offset:limit ("" = whole file)
    sleep: float = 0.0  # seconds of `sleep` in a shell command
    result_tokens: int = 0
    error: bool = False
    prompted: bool = False  # the user had to approve it
    rejected: bool = False


@dataclass
class Prompt:
    ts: float
    turn: int  # turns before it
    kind: str  # typed | interrupt
    correction: str = ""  # category name from DEFAULT_CORRECTIONS (or pattern-N), never text


@dataclass
class Session:
    id: str
    path: str
    cwd: str = ""
    branch: str = ""
    is_subagent: bool = False
    parent_id: str = ""
    agent_type: str = ""
    turns: list[Turn] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    prompts: list[Prompt] = field(default_factory=list)
    compactions: list[int] = field(default_factory=list)  # turn index each compaction happened before
    skills: Counter = field(default_factory=Counter)
    skill_order: list[str] = field(default_factory=list)  # skills and commands invoked, in order
    # Filled in after reading:
    repo: str = ""  # display name
    repo_root: str = ""  # main checkout, when known
    worktree: str = ""
    origin: str = "session"  # session | factory-run
    capability: str = ""
    children_usd: float = 0.0
    department: str = DIRECT

    @property
    def start(self) -> float:
        return self.turns[0].ts if self.turns else 0.0

    @property
    def end(self) -> float:
        return self.turns[-1].ts if self.turns else 0.0

    @property
    def usd(self) -> float:
        return sum(t.usd for t in self.turns)

    def turns_after(self, i: int) -> int:
        """Requests after turn i that still carry what turn i added (until the next compaction)."""
        nxt = next((c for c in self.compactions if c > i), len(self.turns))
        return max(nxt - i - 1, 0)

    def compacted_between(self, a: int, b: int) -> bool:
        return any(a < c <= b for c in self.compactions)


def price(model: str) -> Price:
    for pattern, row in PRICES:
        if re.search(pattern, model or ""):
            return row
    return DEFAULT_PRICE


def _ts(value) -> float:
    if not value:
        return 0.0
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _tokens(text_len: int) -> int:
    return text_len // 4  # the same estimate skillcheck uses (bytes / 4)


TWO_WORD = {"git", "gh", "npm", "pnpm", "yarn", "make", "docker", "uv", "cargo", "go", "kubectl", "factory", "brew",
            "pip", "pip3", "poetry", "bun", "npx", "claude", "orca", "terraform", "helm", "skillcheck"}
SKIP_SEGMENTS = {"cd", "pushd", "popd", "export", "source", ".", "set", "unset", "do", "then", "else", "done", "fi",
                 "if", "while", "until", "for", "(", "{", "}", ")"}
PREFIX_WORDS = {"time", "timeout", "nohup", "env", "sudo", "exec", "command"}
SAFE_WORD = re.compile(r"^[\w./:@+-]{1,40}$")


def command_head(command: str) -> str:
    """The program (and subcommand) a shell command runs, e.g. `git status`, `python3 -m unittest`: a name, never
    its arguments, so no data a command carries ends up in a finding."""
    for segment in re.split(r"&&|\|\||;|\||\n", command or ""):
        try:
            words = shlex.split(segment, comments=True)
        except ValueError:
            words = segment.split()
        while words and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]) or words[0] in PREFIX_WORDS
                         or words[0].startswith("-") or re.fullmatch(r"[\d.]+[smh]?", words[0])):
            words.pop(0)  # VAR=value, `timeout 30`, `env -i`: what runs comes after
        if not words or words[0] in SKIP_SEGMENTS:
            continue
        head = [Path(words[0]).name]
        rest = words[1:]
        if head[0].startswith("python") and rest[:1] == ["-m"] and len(rest) > 1:
            head += ["-m", rest[1]]
        elif head[0].startswith("python") and rest and not rest[0].startswith("-"):
            head.append(Path(rest[0]).name)
        elif head[0] in TWO_WORD:
            for w in rest[:2]:
                if w.startswith("-") or not SAFE_WORD.match(w):
                    break
                head.append(w)
                if head[0] not in ("gh", "orca", "factory", "docker", "kubectl"):
                    break
        return " ".join(w for w in head if SAFE_WORD.match(w)) or "?"
    return ""


def _key(name: str, inp: dict) -> str:
    norm = {k: v for k, v in inp.items() if k not in ("description", "run_in_background", "timeout")}
    if isinstance(norm.get("command"), str):
        norm["command"] = " ".join(norm["command"].split())
    return hashlib.sha1((name + json.dumps(norm, sort_keys=True, default=str)).encode()).hexdigest()[:12]


def _call(block: dict, ts: float, turn: int) -> Call:
    name, inp = block.get("name", "?"), block.get("input") or {}
    call = Call(id=block.get("id", ""), name=name, ts=ts, turn=turn, key=_key(name, inp))
    if name in ("Read", *EDIT_TOOLS):
        call.target = str(inp.get("file_path") or inp.get("notebook_path") or "")
        if name == "Read" and (inp.get("offset") or inp.get("limit")):
            call.span = f"{inp.get('offset', '')}:{inp.get('limit', '')}"
    elif name in ("Grep", "Glob"):
        call.target = str(inp.get("path") or "")
    elif name == "Bash":
        cmd = str(inp.get("command") or "")
        call.target = command_head(cmd)
        call.sleep = sum(float(s) for s in re.findall(r"(?:^|[\s;&|(])sleep\s+(\d+(?:\.\d+)?)", cmd))
    elif name in SPAWN_TOOLS:
        call.target = str(inp.get("subagent_type") or "general-purpose")
    elif name == "Skill":
        call.target = str(inp.get("skill") or "")
    if call.target and not SAFE_WORD.match(call.target.replace(" ", "_")) and name not in ("Read", "Grep", "Glob", *EDIT_TOOLS):
        call.target = "?"
    return call


def _result_len(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for b in content:
            if isinstance(b, dict):
                total += len(b.get("text") or "") if b.get("type") != "image" else 6000  # ~1.5k tokens an image
        return total
    return 0


def _correction(text: str, patterns: list[tuple[str, re.Pattern]]) -> str:
    head = text[:400].lower()
    return next((name for name, rx in patterns if rx.search(head)), "")


def _prompt_kind(record: dict, text: str) -> str:
    origin = record.get("origin") or {}
    if record.get("isMeta") or origin.get("kind") not in (None, "human") or record.get("promptSource") == "system":
        return ""
    s = text.lstrip()
    if s.startswith("[Request interrupted"):
        return "interrupt"
    if not s or s.startswith(("<", "[", "This session is being continued", "Caveat:")):
        return ""
    return "typed"


def read_session(path: str, corrections: list[tuple[str, str]] | None = None) -> Session | None:
    """One transcript, reduced (see the module docstring). None if it holds no model request."""
    p = Path(path)
    patterns = [(n, re.compile(rx, re.I)) for n, rx in (corrections or list(DEFAULT_CORRECTIONS.items()))]
    s = Session(id=p.stem, path=path)
    if p.parent.name == "subagents":
        s.is_subagent, s.parent_id = True, p.parent.parent.name
        meta = p.with_suffix(".meta.json")
        if meta.is_file():
            try:
                info = json.loads(meta.read_text())
                s.agent_type = str(info.get("agentType") or "")
            except (OSError, ValueError):
                pass
    by_msg: dict[str, int] = {}
    calls: dict[str, Call] = {}
    try:
        f = open(p, errors="replace")
    except OSError:
        return None
    with f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            kind = r.get("type")
            if kind not in ("assistant", "user", "system"):
                continue
            if r.get("isSidechain") and not s.is_subagent:
                continue  # an old-format subagent inside its parent's file: its cost is not split out in v0.1
            if not s.cwd and r.get("cwd"):
                s.cwd = r["cwd"]
            if r.get("gitBranch") and r["gitBranch"] != "HEAD":
                s.branch = r["gitBranch"]
            ts = _ts(r.get("timestamp"))
            msg = r.get("message") if isinstance(r.get("message"), dict) else {}
            if kind == "assistant":
                mid = msg.get("id") or r.get("uuid") or str(len(s.turns))
                usage = msg.get("usage") or {}
                if mid not in by_msg:
                    by_msg[mid] = len(s.turns)
                    s.turns.append(Turn(ts=ts, model=str(msg.get("model") or ""), mid=str(mid)))
                t = s.turns[by_msg[mid]]
                t.input = max(t.input, int(usage.get("input_tokens") or 0))
                t.output = max(t.output, int(usage.get("output_tokens") or 0))
                t.cache_write = max(t.cache_write, int(usage.get("cache_creation_input_tokens") or 0))
                t.cache_write_1h = max(t.cache_write_1h, int((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0))
                t.cache_read = max(t.cache_read, int(usage.get("cache_read_input_tokens") or 0))
                for b in msg.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        c = _call(b, ts, by_msg[mid])
                        t.n_tools += 1
                        s.calls.append(c)
                        calls[c.id] = c
                        if c.name == "Skill" and c.target:
                            s.skills[c.target] += 1
                            s.skill_order.append(c.target)
            elif kind == "user":
                content = msg.get("content")
                blocks = content if isinstance(content, list) else []
                results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
                if results:
                    decision = r.get("permissionDecision") or {}
                    tur = r.get("toolUseResult")
                    for b in results:
                        c = calls.get(b.get("tool_use_id", ""))
                        if c is None:
                            continue
                        c.result_tokens = _tokens(_result_len(b.get("content")))
                        c.error = bool(b.get("is_error")) or (isinstance(tur, str) and tur.startswith("Error"))
                        src = str(decision.get("source") or "")
                        c.prompted = src.startswith("user")
                        c.rejected = decision.get("decision") == "reject" and src.startswith("user")
                    continue
                text = content if isinstance(content, str) else " ".join(
                    str(b.get("text") or "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
                m = re.search(r"<command-name>/?([\w:.-]+)</command-name>", text[:500])
                if m:
                    s.skills[m.group(1)] += 1
                    s.skill_order.append(m.group(1))
                if text.lstrip().startswith("This session is being continued"):
                    s.compactions.append(len(s.turns))
                pk = _prompt_kind(r, text)
                if pk:
                    s.prompts.append(Prompt(ts=ts, turn=len(s.turns), kind=pk,
                                            correction=_correction(text, patterns) if pk == "typed" else ""))
            elif r.get("subtype") == "compact_boundary":
                s.compactions.append(len(s.turns))
    return s if s.turns else None


# ---------------------------------------------------------------------------------------------------- where


_GIT_CACHE: dict[str, tuple[str, str]] = {}


def _git_roots(cwd: str) -> tuple[str, str]:
    """(main checkout, worktree root) of a directory, or ("", "") when it is gone or not a repo."""
    if cwd not in _GIT_CACHE:
        roots = ("", "")
        if cwd and Path(cwd).is_dir():
            out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel"],
                                 cwd=cwd, capture_output=True, text=True)
            lines = out.stdout.split()
            if out.returncode == 0 and len(lines) == 2:
                common = Path(lines[0])
                roots = (str(common.parent if common.name == ".git" else common), lines[1])
        _GIT_CACHE[cwd] = roots
    return _GIT_CACHE[cwd]


def run_dirs() -> list[Path]:
    return [runner.HOME / "runs", ENGINE_STATE / "worktrees"]


def place(s: Session) -> None:
    """Fill in repo, worktree, origin and capability."""
    cwd = Path(s.cwd) if s.cwd else None
    for base in run_dirs():
        if cwd and (cwd == base or base in cwd.parents):
            parts = cwd.relative_to(base).parts
            s.origin = "factory-run"
            s.repo = parts[0] if parts else ""
            if len(parts) > 1:
                s.capability = re.sub(r"-\d{8}-\d{4}$", "", parts[1])
            break
    root, top = _git_roots(s.cwd)
    s.repo_root, s.worktree = root, top or s.cwd
    if not s.repo:
        s.repo = Path(root).name if root else (cwd.name if cwd else "?")


def adopt(sessions: list[Session]) -> None:
    """A worktree removed since its session ran can't name its repo; its siblings can. When the worktrees that
    still exist next to it (same parent directory) all belong to one repo, the removed one did too."""
    by_parent: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for s in sessions:
        if s.repo_root and s.worktree:
            by_parent[str(Path(s.worktree).parent)].add((s.repo_root, s.repo))
    for s in sessions:
        if not s.repo_root and s.cwd and s.origin == "session":
            owners = by_parent.get(str(Path(s.cwd).parent), set())
            if len(owners) == 1:
                s.repo_root, s.repo = next(iter(owners))


class Org:
    """Who owns what: the department each skill, command and subagent type declares.

    A declaration is a `department:` line in the file's front matter, top level or under `metadata:` (the one place
    skill front matter allows extra keys), e.g. the factory's own skills declare `metadata: department: Upkeep`.
    Files read: the factory plugin's skills (as `factory:<name>`), each project's `.claude/skills`, `.claude/commands`
    and `.claude/agents`, and the user's `~/.claude/skills` and `~/.claude/agents`.

    TODO(F-33, PR #26): #26 settles the one declaration mechanism (org/org_structure.yaml, agents declaring
    `department:`) and the `[org]` config. When it lands, this class reads that instead of front matter, and the
    session-to-department rule moves there too, so the costs page and this scout never disagree on an owner.
    """

    LINE = re.compile(r"^\s*department:\s*[\"']?([^\"'#\n]+?)[\"']?\s*(#.*)?$", re.M)

    def __init__(self, roots: list[str]):
        self.owners: dict[str, str] = {}
        factory_skills = Path(__file__).resolve().parents[1] / "skills"
        self._scan(factory_skills.glob("*/SKILL.md"), prefix="factory:")
        home = Path.home() / ".claude"
        for base in [home, *(Path(r) / ".claude" for r in roots if r)]:
            self._scan(base.glob("skills/*/SKILL.md"))
            self._scan(base.glob("commands/*.md"))
            self._scan(base.glob("agents/*.md"))

    def _scan(self, files, prefix: str = "") -> None:
        for f in files:
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            if not text.startswith("---"):
                continue
            block = text[3:text.find("\n---", 3)] if "\n---" in text[3:] else ""
            m = self.LINE.search(block)
            if m:
                name = f.parent.name if f.name == "SKILL.md" else f.stem
                self.owners.setdefault(prefix + name, normalise_department(m.group(1)))

    def of(self, name: str) -> str:
        """The declared department of a skill, command or subagent type; "" when none is declared."""
        name = name.lstrip("/")
        return self.owners.get(name) or self.owners.get(name.split(":")[-1], "")


def normalise_department(value: str) -> str:
    v = " ".join(value.replace("-", " ").replace("_", " ").split())
    return next((d for d in DEPARTMENTS if d.lower() == v.lower()), v)


def capability_skill(capability: str) -> str:
    """The skill a factory capability runs (`factory run` prompts; an engine team named like its skill)."""
    prompt = runner.PROMPTS.get(capability, f"/factory:{capability}")
    return prompt.split()[0].lstrip("/")


def department_of(s: Session, org: Org, parent: Session | None = None) -> str:
    """The owner of a session's work, first that applies: the last skill or command invoked in it that declares a
    department; for a subagent, its declared type, then whoever owns its parent; for a factory run, its
    capability's skill; otherwise Direct (the principal's own work)."""
    for skill in reversed(s.skill_order):
        if org.of(skill):
            return org.of(skill)
    if s.is_subagent and org.of(s.agent_type):
        return org.of(s.agent_type)
    if parent is not None:
        return parent.department
    if s.capability and org.of(capability_skill(s.capability)):
        return org.of(capability_skill(s.capability))
    return DIRECT


HOME_DIR = str(Path.home())


def rel(s: Session, path: str) -> str:
    """A file path relative to its worktree, so the same file matches across worktrees; else with `~` for home."""
    if s.worktree and path.startswith(s.worktree.rstrip("/") + "/"):
        return path[len(s.worktree.rstrip("/")) + 1:]
    return "~" + path[len(HOME_DIR):] if path.startswith(HOME_DIR + "/") else path


def short_id(session: str) -> str:
    return session.removeprefix("agent-")[:8]


# ---------------------------------------------------------------------------------------------------- findings


@dataclass
class Finding:
    detector: str
    project: str  # the repo, its worktrees folded in
    department: str  # who owns the work (department_of); runtime: the app's own department
    session: str  # "" for a finding across sessions
    usd: float  # at stake: an upper bound on what avoiding it saves; findings can overlap
    tokens: int
    count: int
    what: str  # a sentence from metrics, paths, tool and command names only
    source: str = "session"  # session | subagent | factory-run | runtime
    when: str = ""
    sessions: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)  # the top paths / commands / categories behind it


def _when(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else ""


def _source(s: Session) -> str:
    return "factory-run" if s.origin == "factory-run" else ("subagent" if s.is_subagent else "session")


def call_cost(s: Session, c: Call) -> tuple[int, float]:
    """What a call costs: its share of the request that made it, plus its result carried by every later request."""
    t = s.turns[c.turn]
    share = 1 / max(t.n_tools, 1)
    later = s.turns_after(c.turn)
    _, _, write, _, read = price(t.model)
    tokens = int((t.context + t.output) * share) + c.result_tokens * (1 + later)
    return tokens, t.usd * share + c.result_tokens * (write + read * later) / 1e6


def _sum(s: Session, calls: list[Call]) -> tuple[int, float]:
    tok = usd = 0.0
    for c in calls:
        a, b = call_cost(s, c)
        tok, usd = tok + a, usd + b
    return int(tok), usd


def _top(counter: Counter, n: int = 5, unit: str = "") -> list[str]:
    return [f"{k} ({_tok(v)} {unit})" if unit else f"{k} ×{v}" for k, v in counter.most_common(n)]


class Scout:
    def __init__(self, eff: Efficiency):
        self.eff = eff
        self.findings: list[Finding] = []

    def lim(self, name: str) -> float:
        return self.eff.limit(name)

    def on(self, name: str) -> bool:
        return name not in self.eff.disabled

    def ignored(self, path: str) -> bool:
        return any(fnmatch(path, g) for g in self.eff.ignore)

    def add(self, s: Session | None, detector: str, calls_or_cost, count: int, what: str, items=None, repo="",
            department=DIRECT):
        if not self.on(detector):
            return
        tokens, usd = calls_or_cost
        if detector not in COUNTED and usd < self.lim("min_usd"):
            return
        self.findings.append(Finding(
            detector, s.repo if s else repo, s.department if s else department, s.id if s else "", round(usd, 2),
            int(tokens), count, what,
            _source(s) if s else "session", _when(s.start) if s else "", items=list(items or []),
        ))

    # -- per session

    def session(self, s: Session) -> None:
        self.reads(s)
        self.results(s)
        self.repeats(s)
        self.polling(s)
        self.cache(s)
        self.people(s)

    def reads(self, s: Session) -> None:
        last: dict[str, tuple[int, str]] = {}
        count: Counter = Counter()
        redundant: dict[str, list[Call]] = defaultdict(list)
        big: list[Call] = []
        for c in s.calls:
            if not c.target or self.ignored(c.target):
                continue
            if c.name in EDIT_TOOLS:
                last.pop(c.target, None)
            elif c.name == "Read" and not c.error:
                count[c.target] += 1
                prev = last.get(c.target)
                if prev and prev[1] == c.span and not s.compacted_between(prev[0], c.turn):
                    redundant[c.target].append(c)
                last[c.target] = (c.turn, c.span)
                if not c.span and c.result_tokens >= self.lim("big_read_tokens"):
                    big.append(c)
        waste = [c for p, cs in redundant.items() if count[p] >= self.lim("repeat_read_min") for c in cs]
        if waste:
            files = Counter({rel(s, p): count[p] for p, cs in redundant.items() if count[p] >= self.lim("repeat_read_min")})
            self.add(s, "repeat-read", _sum(s, waste), len(waste),
                     f"{len(waste)} re-reads of unchanged files ({len(files)} files)", _top(files))
        if big:
            files = Counter({rel(s, c.target): c.result_tokens for c in big})
            self.add(s, "big-read", _sum(s, big), len(big),
                     f"{len(big)} whole-file reads over {int(self.lim('big_read_tokens'))} tokens", _top(files, unit="tokens"))

    def results(self, s: Session) -> None:
        big = [c for c in s.calls if c.name != "Read" and c.result_tokens >= self.lim("oversized_tokens")]
        if big:
            what = Counter(f"{c.name}:{c.target}" if c.target else c.name for c in big)
            self.add(s, "oversized-result", _sum(s, big), len(big),
                     f"{len(big)} tool results over {int(self.lim('oversized_tokens'))} tokens, "
                     f"largest {max(c.result_tokens for c in big)}", _top(what))

    def _is_poll(self, c: Call) -> bool:
        return c.sleep > 0 or c.name in POLL_TOOLS or (c.name == "Bash" and c.target in self.eff.poll_commands)

    def repeats(self, s: Session) -> None:
        groups: dict[str, list[Call]] = defaultdict(list)
        for c in s.calls:
            if c.name != "Read" and not self._is_poll(c):
                groups[c.key].append(c)
        extra = [c for cs in groups.values() if len(cs) >= self.lim("repeat_call_min") for c in cs[1:]]
        if extra:
            what = Counter(f"{c.name}:{c.target}" if c.target else c.name for c in extra)
            self.add(s, "repeat-call", _sum(s, extra), len(extra), f"{len(extra)} repeats of identical tool calls",
                     _top(what))
        window = int(self.lim("retry_window"))
        retried: list[Call] = []
        seen: set[str] = set()
        for i, c in enumerate(s.calls):
            if not c.error or c.id in seen:
                continue
            for d in s.calls[i + 1:i + 1 + window]:
                if d.key == c.key and d.error:
                    retried.append(d)
                    seen.add(d.id)
                    break
        if retried:
            what = Counter(f"{c.name}:{c.target}" if c.target else c.name for c in retried)
            self.add(s, "retry-unchanged", _sum(s, retried), len(retried),
                     f"{len(retried)} failed calls retried unchanged and failing again", _top(what))

    def polling(self, s: Session) -> None:
        polls = [c for c in s.calls if self._is_poll(c)]
        if len(polls) >= self.lim("poll_min"):
            slept = sum(c.sleep for c in polls)
            what = Counter("sleep" if c.sleep else (c.target or c.name) for c in polls)
            self.add(s, "polling", _sum(s, polls[1:]), len(polls),
                     f"{len(polls)} sleeps and status checks ({int(slept // 60)} min asleep)", _top(what))

    def cache(self, s: Session) -> None:
        rebuilds: Counter = Counter()
        usd = tokens = 0.0
        comp = set(s.compactions)
        for i in range(1, len(s.turns)):
            t, prev = s.turns[i], s.turns[i - 1]
            if i in comp or t.cache_write < self.lim("cache_rebuild_tokens"):
                continue
            if t.ts - prev.ts >= self.lim("cache_gap_seconds"):
                cause = "pause"
            elif t.model != prev.model:
                cause = "model switch"
            elif t.cache_write > 0.5 * t.context:
                cause = "context change"
            else:
                continue
            rebuilds[cause] += 1
            _, _, write, _, read = price(t.model)
            tokens += t.cache_write
            usd += t.cache_write * (write - read) / 1e6
        if rebuilds:
            n = sum(rebuilds.values())
            self.add(s, "cache-rebuild", (tokens, usd), n, f"{n} prompt-cache rebuilds mid-session", _top(rebuilds))
        limit = self.lim("long_context_tokens")
        over = [t for t in s.turns if t.context > limit]
        if over:
            usd = sum((t.context - limit) * price(t.model)[4] / 1e6 for t in over)
            self.add(s, "long-context", (sum(t.context - limit for t in over), usd), len(over),
                     f"{len(over)} requests carried over {int(limit // 1000)}k tokens of context "
                     f"(peak {max(t.context for t in over) // 1000}k, {len(s.compactions)} compactions)")

    def people(self, s: Session) -> None:
        prompted = [c for c in s.calls if c.prompted and c.name not in ASK_TOOLS]
        if len(prompted) >= self.lim("permission_prompts"):
            rejected = sum(c.rejected for c in prompted)
            what = Counter(f"{c.name}:{c.target}" if c.target else c.name for c in prompted)
            self.add(s, "permission-prompts", (0, 0.0), len(prompted),
                     f"{len(prompted)} tool calls waited for approval ({rejected} rejected)", _top(what))
        corrections = [p for p in s.prompts if p.correction or p.kind == "interrupt"]
        if corrections:
            starts = [p.turn for p in s.prompts]
            usd = tokens = 0.0
            for p in corrections:
                before = max((x for x in starts if x < p.turn), default=0)
                for t in s.turns[before:p.turn]:
                    usd, tokens = usd + t.usd, tokens + t.context + t.output
            what = Counter(p.correction or "interrupt" for p in corrections)
            items = _top(what) + [f"at {_when(p.ts)} ({p.correction or 'interrupt'})" for p in corrections[:5]]
            self.add(s, "user-correction", (tokens, usd), len(corrections),
                     f"{len(corrections)} corrections or interrupts of {sum(p.kind == 'typed' for p in s.prompts)} prompts",
                     items)

    # -- across sessions

    def subagents(self, sessions: list[Session]) -> None:
        by_id = {s.id: s for s in sessions if not s.is_subagent}
        kids: dict[str, list[Session]] = defaultdict(list)
        for s in sessions:
            if s.is_subagent and s.parent_id in by_id:
                kids[s.parent_id].append(s)
        for pid, children in kids.items():
            parent = by_id[pid]
            parent.children_usd = sum(c.usd for c in children)
            if len(children) >= self.lim("max_subagents"):
                base = [(c.turns[0].context, c.turns[0].usd) for c in children]
                self.add(parent, "subagent-overspawn", (sum(b[0] for b in base), sum(b[1] for b in base)), len(children),
                         f"{len(children)} subagents (${parent.children_usd:.2f} in all); each re-loads the base context",
                         _top(Counter(c.agent_type or "?" for c in children)))
            for child in children:
                parent_reads = {c.target for c in parent.calls if c.name == "Read" and c.ts <= child.start}
                again = [c for c in child.calls if c.name == "Read" and c.target in parent_reads and not self.ignored(c.target)]
                if again:
                    self.add(child, "subagent-reread", _sum(child, again), len(again),
                             f"{len(again)} reads of files its parent session had already read",
                             _top(Counter(rel(child, c.target) for c in again)))

    def base_context(self, sessions: list[Session]) -> None:
        by_repo: dict[str, list[Session]] = defaultdict(list)
        for s in sessions:
            if not s.is_subagent and s.origin == "session":
                by_repo[s.repo].append(s)
        limit = self.lim("base_context_tokens")
        for repo, group in by_repo.items():
            owner = Counter(s.department for s in group).most_common(1)[0][0]  # loaded for every session: the main user
            base = statistics.median(s.turns[0].context for s in group)
            if base <= limit:
                continue
            extra = base - limit
            usd = sum(extra * price(s.turns[0].model)[4] * len(s.turns) / 1e6 for s in group)
            skills = sum((s.skills for s in group), Counter())
            self.add(None, "base-context", (int(extra * sum(len(s.turns) for s in group)), usd), len(group),
                     f"sessions start at {int(base) // 1000}k tokens of context (budget {int(limit) // 1000}k), "
                     f"carried by every request; skills and commands used over {len(group)} sessions listed",
                     _top(skills, 8) or ["no skill invoked"], repo=repo, department=owner)

    def duplicates(self, sessions: list[Session]) -> None:
        main = [s for s in sessions if not s.is_subagent]
        by_repo: dict[str, list[Session]] = defaultdict(list)
        for s in main:
            by_repo[s.repo].append(s)
        kids: dict[str, list[Session]] = defaultdict(list)  # a session's subagents edit on its behalf
        for s in sessions:
            if s.is_subagent:
                kids[s.parent_id].append(s)
        need, ratio = self.lim("duplicate_shared_files"), self.lim("duplicate_overlap")
        for repo, group in by_repo.items():
            edits = {s.id: {rel(x, c.target) for x in [s, *kids[s.id]] for c in x.calls
                            if c.name in EDIT_TOOLS and c.target and not self.ignored(c.target)} for s in group}
            freq = Counter(f for files in edits.values() for f in files)
            hubs = {f for f, n in freq.items() if len(group) >= 5 and n > 0.3 * len(group)}  # edited by nearly every task
            edits = {k: v - hubs for k, v in edits.items()}
            parent = {s.id: s.id for s in group}

            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            for i, a in enumerate(group):
                for b in group[i + 1:]:
                    if a.worktree == b.worktree:
                        continue  # one worktree: the same task carried on, not done twice
                    shared = edits[a.id] & edits[b.id]
                    small = min(len(edits[a.id]), len(edits[b.id])) or 1
                    same_branch = a.branch and a.branch == b.branch and a.branch not in ("main", "master", "HEAD")
                    if (len(shared) >= need and len(shared) / small >= ratio) or same_branch:
                        parent[find(a.id)] = find(b.id)
            clusters: dict[str, list[Session]] = defaultdict(list)
            for s in group:
                clusters[find(s.id)].append(s)
            for cluster in clusters.values():
                if len(cluster) < 2:
                    continue
                costs = sorted((s.usd + s.children_usd for s in cluster), reverse=True)
                shared = set.intersection(*(edits[s.id] for s in cluster)) or set.union(*(edits[s.id] for s in cluster))
                branches = sorted({s.branch for s in cluster if s.branch})
                f_before = len(self.findings)
                owner = max(cluster, key=lambda s: s.usd + s.children_usd).department
                self.add(None, "duplicate-effort", (0, sum(costs[1:])), len(cluster),
                         f"{len(cluster)} sessions in {len({s.worktree for s in cluster})} worktrees worked the same "
                         f"files or branch ({', '.join(branches[:3]) or 'no branch'})",
                         sorted(shared)[:5], repo=repo, department=owner)
                if len(self.findings) > f_before:
                    self.findings[-1].sessions = [s.id for s in cluster]
                    self.findings[-1].when = _when(min(s.start for s in cluster))

    # -- runtime

    def runtime(self, records: list[dict], repo: str) -> None:
        by_hash: dict[str, list[dict]] = defaultdict(list)
        by_job: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for r in records:
            if r.get("prompt_hash"):
                by_hash[r["prompt_hash"]].append(r)
            by_job[(r["department"], r["agent"], r["task"])].append(r)
        repeats = [rs for rs in by_hash.values() if len(rs) >= self.lim("runtime_repeat_min")]
        for rs in repeats:
            extra = rs[1:]
            self.add(None, "runtime-repeat-prompt", (sum(_rt_tokens(r) for r in extra), sum(r["cost_usd"] for r in extra)),
                     len(rs), f"the same prompt sent {len(rs)} times by {rs[0]['agent']} / {rs[0]['task']} "
                     f"({sum(r['cache_read_tokens'] for r in rs)} tokens read from cache)", [rs[0]["model"]], repo=repo,
                     department=rs[0]["department"])
        for (dept, agent, task), rs in by_job.items():
            processes = sorted({r["process"] for r in rs if r["process"]})
            label = f"{agent} / {task}" + (f" (process {', '.join(processes[:2])})" if processes else "")
            failed = [r for r in rs if r["outcome"] in ("error", "retry", "expired") or r["attempt"] > 1]
            if failed:
                self.add(None, "runtime-retry", (sum(_rt_tokens(r) for r in failed), sum(r["cost_usd"] for r in failed)),
                         len(failed), f"{len(failed)} of {len(rs)} calls by {label} failed or were retries",
                         _top(Counter(r["outcome"] for r in failed)), repo=repo, department=dept)
            unused = [r for r in rs if r["output_used"] is False]
            if unused:
                self.add(None, "runtime-unused", (sum(_rt_tokens(r) for r in unused), sum(r["cost_usd"] for r in unused)),
                         len(unused), f"{len(unused)} of {len(rs)} results of {label} were never used", repo=repo,
                         department=dept)
            top = [r for r in rs if any(fnmatch(r["model"], g) for g in self.eff.expensive_models)]
            if len(top) >= 5 and statistics.median(r["output_tokens"] for r in top) <= self.lim("runtime_trivial_output_tokens"):
                cost = sum(r["cost_usd"] for r in top)
                self.add(None, "runtime-expensive-model", (sum(_rt_tokens(r) for r in top), cost * CHEAPER_TIER_SAVING),
                         len(top), f"{label} runs a top-tier model for short answers "
                         f"(median {int(statistics.median(r['output_tokens'] for r in top))} output tokens, ${cost:.2f})",
                         sorted({r["model"] for r in top})[:3], repo=repo, department=dept)


def _rt_tokens(r: dict) -> int:
    return r["input_tokens"] + r["output_tokens"] + r["cache_write_tokens"] + r["cache_read_tokens"]


# ---------------------------------------------------------------------------------------------------- runtime


def parse_runtime(text: str) -> tuple[list[dict], list[str]]:
    """Runtime usage JSONL → normalised records, and the lines that could not be used."""
    out, errors = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            if not isinstance(r, dict) or not r.get("ts") or not r.get("model"):
                raise ValueError("ts and model are required")
            if int(r.get("v") or 1) > RUNTIME_SCHEMA:
                raise ValueError(f"schema v{r['v']} is newer than this reader (v{RUNTIME_SCHEMA})")
            rec = {
                "ts": _ts(r["ts"]), "kind": str(r.get("kind") or "llm_call"), "agent": str(r.get("agent") or "?"),
                "task": str(r.get("task") or "?"), "run_id": str(r.get("run_id") or ""), "model": str(r["model"]),
                "department": str(r.get("department") or UNASSIGNED), "process": str(r.get("process") or ""),
                "prompt_hash": str(r.get("prompt_hash") or ""), "outcome": str(r.get("outcome") or "ok"),
                "attempt": int(r.get("attempt") or 1), "output_used": r.get("output_used"),
            }
            for k in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens"):
                rec[k] = int(r.get(k) or 0)
            if r.get("cost_usd") is None:
                pin, pout, write, _, read = price(rec["model"])
                rec["cost_usd"] = (rec["input_tokens"] * pin + rec["cache_write_tokens"] * write
                                   + rec["cache_read_tokens"] * read + rec["output_tokens"] * pout) / 1e6
            else:
                rec["cost_usd"] = float(r["cost_usd"])
            out.append(rec)
        except (ValueError, TypeError) as exc:
            errors.append(f"runtime usage line {n}: {exc}")
    return out, errors


def read_runtime(cfg: Config, since: float) -> tuple[list[dict], list[str]]:
    cmd = cfg.efficiency.runtime_usage
    if not cmd:
        return [], []
    iso = dt.datetime.fromtimestamp(since, dt.timezone.utc).isoformat(timespec="seconds")
    try:
        out = subprocess.run(cmd.replace("{since}", shlex.quote(iso)), shell=True, cwd=cfg.root, capture_output=True,
                             text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return [], ["runtime_usage timed out after 600 s"]
    if out.returncode:
        return [], [f"runtime_usage exited {out.returncode}"]  # its stderr may hold private data: not echoed
    records, errors = parse_runtime(out.stdout)
    return [r for r in records if r["ts"] >= since], errors[:5]


# ---------------------------------------------------------------------------------------------------- scan


def parse_since(value: str) -> float:
    """`7d`, `24h`, `2w`, `90m` or an ISO date → epoch seconds."""
    m = re.fullmatch(r"(\d+)([mhdw])", value.strip())
    if m:
        unit = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return time.time() - int(m.group(1)) * unit
    stamp = _ts(value if "T" in value or "+" in value else value + "T00:00:00+00:00")
    if not stamp:
        raise ValueError(f"--since: {value!r} is not like 7d, 24h, 2w or 2026-11-01")
    return stamp


def transcripts(projects_dir: Path, since: float) -> list[Path]:
    files = list(projects_dir.glob("*/*.jsonl")) + list(projects_dir.glob("*/*/subagents/*.jsonl"))
    return sorted(p for p in files if p.stat().st_mtime >= since)


@dataclass
class Report:
    since: str
    scope: str
    sessions: int
    subagents: int
    factory_runs: int
    usd: float
    tokens: int
    cache_read_share: float
    findings: list[Finding]
    projects: list[dict]
    departments: list[dict]
    runtime_calls: int = 0
    runtime_usd: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _read_all(paths: list[Path], corrections, workers: int) -> list[Session]:
    args = [(str(p), corrections) for p in paths]
    if workers <= 1 or len(paths) < 8:
        found = [read_session(*a) for a in args]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            found = list(pool.map(_read_one, args, chunksize=4))
    return [s for s in found if s]


def _read_one(args) -> Session | None:
    return read_session(*args)


def main_root(cfg: Config) -> str:
    """The repo's main checkout: a run or a session may sit in one of its worktrees."""
    return _git_roots(str(cfg.root))[0] or str(cfg.root)


def repo_name(cfg: Config) -> str:
    """[repo] name when set, else the main checkout's directory name (a worktree's name says nothing)."""
    return cfg.name if cfg.name != cfg.root.name else Path(main_root(cfg)).name


def _wanted(cfg: Config) -> tuple[set[str], set[str]]:
    main = main_root(cfg)
    roots, names = {str(cfg.root), main}, {cfg.name, cfg.root.name, Path(main).name}
    for r in cfg.efficiency.repos:
        p = Path(r).expanduser()
        main = _git_roots(str(p))[0] or str(p.resolve())
        roots.add(main)
        names.add(Path(main).name)
    return roots, names


def scan(cfg: Config, since: str | None = None, all_repos: bool | None = None, projects_dir: Path | None = None,
         workers: int | None = None, runtime: bool = True) -> Report:
    eff = cfg.efficiency
    window = since or eff.since
    cutoff = parse_since(window)
    all_repos = eff.scope == "all" if all_repos is None else all_repos
    pdir = projects_dir or Path(eff.projects_dir).expanduser()
    corrections = ([(f"pattern-{i + 1}", rx) for i, rx in enumerate(eff.corrections)] if eff.corrections else None)
    paths = transcripts(pdir, cutoff) if pdir.is_dir() else []
    sessions = _read_all(paths, corrections, workers if workers is not None else min(8, os.cpu_count() or 1))
    for s in sessions:
        place(s)
    adopt(sessions)
    org = Org(sorted({s.repo_root for s in sessions if s.repo_root}))
    # A subagent belongs to its parent's place (its own cwd can be anywhere).
    parents = {s.id: s for s in sessions if not s.is_subagent}
    for s in sessions:
        p = parents.get(s.parent_id) if s.is_subagent else None
        if p:
            s.repo, s.repo_root, s.origin, s.capability = p.repo, p.repo_root, p.origin, p.capability
            s.worktree = s.worktree or p.worktree
    for s in sorted(sessions, key=lambda s: s.is_subagent):  # parents first: a subagent may inherit
        s.department = department_of(s, org, parents.get(s.parent_id) if s.is_subagent else None)
    roots, names = _wanted(cfg)
    keep = []
    for s in sessions:
        if s.end < cutoff or any(fnmatch(s.cwd, g) for g in eff.ignore):
            continue
        if ("runs" if s.origin == "factory-run" else "sessions") not in eff.sources:
            continue
        if not all_repos and not (s.repo_root in roots or (not s.repo_root and s.repo in names)):
            continue
        keep.append(s)

    scout = Scout(eff)
    for s in keep:
        scout.session(s)
    scout.subagents(keep)
    scout.base_context(keep)
    scout.duplicates(keep)
    errors: list[str] = []
    records: list[dict] = []
    if runtime and "runtime" in eff.sources:
        records, errors = read_runtime(cfg, cutoff)
        scout.runtime(records, cfg.name)

    # Spend counts each model request once (by message id, across transcripts) and only inside the window, as
    # usage.py does; the detectors read whole sessions, since a pattern does not stop at the window's edge.
    seen: set[str] = set()
    counted: dict[str, list[Turn]] = {}
    for s in sorted(keep, key=lambda s: s.start):
        counted[s.id + s.path] = [t for t in s.turns if t.ts >= cutoff and not (t.mid in seen or seen.add(t.mid))]
    turns = [t for ts in counted.values() for t in ts]
    read = sum(t.cache_read for t in turns)
    all_in = sum(t.context for t in turns) or 1
    projects = []
    for repo in sorted({s.repo for s in keep}):
        group = [s for s in keep if s.repo == repo]
        rt = [t for s in group for t in counted[s.id + s.path]]
        projects.append({
            "project": repo, "sessions": sum(not s.is_subagent for s in group),
            "usd": round(sum(t.usd for t in rt), 2),
            "subagent_usd": round(sum(t.usd for s in group if s.is_subagent for t in counted[s.id + s.path]), 2),
            "factory_run_usd": round(sum(t.usd for s in group if s.origin == "factory-run"
                                         for t in counted[s.id + s.path]), 2),
            "cache_read_share": round(sum(t.cache_read for t in rt) / (sum(t.context for t in rt) or 1), 3),
        })
    projects.sort(key=lambda r: -r["usd"])
    findings = sorted(scout.findings, key=lambda f: (-f.usd, -f.count))
    departments = []
    for dept in sorted({s.department for s in keep} | {f.department for f in findings}):
        mine = [f for f in findings if f.department == dept]
        departments.append({
            "department": dept,
            "usd": round(sum(t.usd for s in keep if s.department == dept for t in counted[s.id + s.path]), 2),
            "findings": len(mine), "at_stake_usd": round(sum(f.usd for f in mine if f.detector not in COUNTED), 2),
            "detectors": [f"{d} ${v:,.2f}" for d, v in Counter(
                {d: sum(f.usd for f in mine if f.detector == d) for d in {f.detector for f in mine}}).most_common(4)],
        })
    departments.sort(key=lambda d: -d["at_stake_usd"])
    return Report(
        since=_when(cutoff), scope="all repos" if all_repos else repo_name(cfg),
        sessions=sum(not s.is_subagent for s in keep), subagents=sum(s.is_subagent for s in keep),
        factory_runs=sum(s.origin == "factory-run" and not s.is_subagent for s in keep),
        usd=round(sum(t.usd for t in turns), 2), tokens=sum(t.context + t.output for t in turns),
        cache_read_share=round(read / all_in, 3), findings=findings, projects=projects, departments=departments,
        runtime_calls=len(records), runtime_usd=round(sum(r["cost_usd"] for r in records), 2), errors=errors,
    )


# ---------------------------------------------------------------------------------------------------- output


PER_DETECTOR = 3


def _tok(n: float) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else (f"{n / 1e3:.0f}k" if n >= 1e3 else str(int(n)))


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def markdown(report: Report, top: int = 20) -> str:
    out = [
        f"# Efficiency scan: {report.scope}, since {report.since} UTC",
        "",
        f"{report.sessions} sessions ({report.factory_runs} factory runs) and {report.subagents} subagents · "
        f"${report.usd:,.2f} estimated · {_tok(report.tokens)} tokens · cache reads {report.cache_read_share:.0%} of input",
    ]
    if report.runtime_calls:
        out.append(f"Runtime: {report.runtime_calls} model calls, ${report.runtime_usd:,.2f}")
    per: Counter = Counter()
    priced = []
    for f in report.findings:  # sorted by $: keep the biggest few of each detector so one pattern can't fill the table
        if f.detector not in COUNTED and per[f.detector] < PER_DETECTOR:
            per[f.detector] += 1
            priced.append(f)
    out += ["", "## Top findings", "",
            f"At most {PER_DETECTOR} per detector; the full list is `--json`. At stake = what the pattern cost (an upper "
            "bound on the saving; findings can overlap).", "",
            "| # | Detector | At stake | Tokens | Project | Department | Session | When | What | Top items |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for i, f in enumerate(priced[:top], 1):
        sess = short_id(f.session) if f.session else (", ".join(short_id(x) for x in f.sessions[:3]) or "all")
        out.append(f"| {i} | {f.detector} | ${f.usd:,.2f} | {_tok(f.tokens)} | {f.project} | {f.department} | {sess} ({f.source}) | "
                   f"{f.when} | {_cell(f.what)} | {_cell('; '.join(f.items[:4]))} |")
    if not priced:
        out.append("| — | nothing above the thresholds | | | | | | | | |")
    out += ["", "## By department", "",
            "Fixes go to the department that owns the work: its skills, its agents, its runs.", "",
            "| Department | Spend | Findings | At stake | Biggest patterns ($) |", "|---|---|---|---|---|"]
    for d in report.departments:
        out.append(f"| {d['department']} | ${d['usd']:,.2f} | {d['findings']} | ${d['at_stake_usd']:,.2f} | "
                   f"{_cell('; '.join(d['detectors']))} |")
    totals: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for f in report.findings:
        totals[f.detector][0] += f.count
        totals[f.detector][1] += f.usd
    out += ["", "## By detector", "", "| Detector | Findings | Events | At stake |", "|---|---|---|---|"]
    for name in DETECTORS:
        n = sum(f.detector == name for f in report.findings)
        if n:
            out.append(f"| {name} | {n} | {int(totals[name][0])} | ${totals[name][1]:,.2f} |")
    corr = [f for f in report.findings if f.detector == "user-correction"]
    if corr:
        out += ["", "## Corrections (learning-loop candidates)", "",
                "| Session | Project | Department | When | Corrections | Work before them | Categories |",
                "|---|---|---|---|---|---|---|"]
        for f in sorted(corr, key=lambda f: -f.count)[:top]:
            out.append(f"| {short_id(f.session)} | {f.project} | {f.department} | {f.when} | {f.count} | ${f.usd:,.2f} | "
                       f"{_cell('; '.join(i for i in f.items if '×' in i))} |")
    perm = [f for f in report.findings if f.detector == "permission-prompts"]
    if perm:
        heads: Counter = Counter()
        for f in perm:
            for item in f.items:
                name, _, n = item.rpartition(" ×")
                heads[name] += int(n or 0)
        out += ["", "## Permission prompts", "",
                f"{sum(f.count for f in perm)} approvals in {len(perm)} sessions; most asked: "
                + ", ".join(_top(heads, 8))]
    out += ["", "## By project", "", "| Project | Sessions | Cost | Subagents | Factory runs | Cache read share |",
            "|---|---|---|---|---|---|"]
    for r in report.projects:
        out.append(f"| {r['project']} | {r['sessions']} | ${r['usd']:,.2f} | ${r['subagent_usd']:,.2f} | "
                   f"${r['factory_run_usd']:,.2f} | {r['cache_read_share']:.0%} |")
    if report.errors:
        out += ["", "Problems: " + "; ".join(report.errors)]
    return "\n".join(out)


def find_transcript(projects_dir: Path, session: str) -> Path | None:
    hits = sorted(projects_dir.glob(f"*/{session}*.jsonl")) + sorted(projects_dir.glob(f"*/*/subagents/{session}*.jsonl"))
    if not hits and not session.startswith("agent-"):
        hits = sorted(projects_dir.glob(f"*/*/subagents/agent-{session}*.jsonl"))
    return hits[0] if hits else None


def timeline(s: Session) -> list[dict]:
    """A session's tool calls, safe to show: names, paths, command heads, sizes and costs."""
    rows = []
    for c in s.calls:
        tokens, usd = call_cost(s, c)
        rows.append({"turn": c.turn, "when": _when(c.ts), "tool": c.name, "target": rel(s, c.target),
                     "span": c.span, "result_tokens": c.result_tokens, "error": c.error, "prompted": c.prompted,
                     "sleep": c.sleep, "usd": round(usd, 4), "key": c.key})
    return rows


def show_text(s: Session) -> str:
    head = (f"session {s.id} · {s.repo} · {s.department} · {s.origin}{' ' + s.capability if s.capability else ''} · branch {s.branch or '?'}"
            f" · {len(s.turns)} requests · ${s.usd:,.2f} · {len(s.compactions)} compactions · "
            f"{sum(p.kind == 'typed' for p in s.prompts)} prompts")
    lines = [head, f"{'turn':>5}  {'when':16}  {'tool':12}  {'result':>7}  {'$':>7}  flags  target"]
    for r in timeline(s):
        flags = ("E" if r["error"] else "-") + ("P" if r["prompted"] else "-") + ("S" if r["sleep"] else "-")
        lines.append(f"{r['turn']:>5}  {r['when']:16}  {r['tool'][:12]:12}  {_tok(r['result_tokens']):>7}  "
                     f"{r['usd']:>7.3f}  {flags:5}  {r['target']}{' [' + r['span'] + ']' if r['span'] else ''}")
    lines.append("flags: E = error, P = waited for approval, S = slept")
    return "\n".join(lines)
