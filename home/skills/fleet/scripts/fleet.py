#!/usr/bin/env python3
"""Fleet: the Orca control tower's eyes and hands. Stdlib only.

  fleet.py [report] [--changes] [--json] [--no-ack]   ranked fleet report
  fleet.py watch [--interval 15] [--pr-every 4]        block until something needs attention
  fleet.py spawn --name N --slug S (--prompt T | --prompt-file F) [--agent claude] [--repo PATH]
  fleet.py name HANDLE "label"                         label a terminal ("" clears)
  fleet.py keep ["text" | --rm N]                      durable notes/grants that survive /clear
  fleet.py reset                                       after this turn: /clear, then /fleet resume
  fleet.py done-reset                                  (called by /fleet resume) zero the event counter
  fleet.py gate PR [--repo PATH] [--json]              release verdict: MERGE | ASK | FIX | WAIT
  fleet.py land PR [--repo PATH] [--approved]          re-gate, then merge (ASK needs --approved)
  fleet.py cleanup PR [--repo PATH]                    after a merge: close its agent, remove worktree + branch

State lives in $TMPDIR/orca-fleet/, so the tower's conversation can be cleared at any time.
Agents come from `orca worktree ps --json` (hook-reported state) joined with
`orca terminal list --json`. Agents on non-default branches carry PR + CI state via `gh`.
Worktrees the tower spawned get their Orca card (status, comment, unread dot) kept in sync,
so Orca's sidebar is a live dashboard that costs no tokens.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "orca-fleet"
TITLE_GLYPHS = re.compile(r"^(?:[✳✦⏲◇✋▲⠀-⣿◐-◓]+|[.*]\s)\s*")
PERMISSION_HINT = re.compile(r"Do you want to|Allow .*\?|wants to (run|edit|write|fetch)|Approve|\(y/n\)", re.I)
RULE_LINE = re.compile(r"^[\s─╌━═│╭╮╰╯]*$")
DEFAULT_BRANCHES = {"main", "master"}
RESET_AFTER_EVENTS = int(os.environ.get("FLEET_RESET_AFTER", "25"))

# Lower rank = shown first. Words and symbols carry meaning; no colour anywhere.
RANK = {"blocked": 0, "waiting": 1, "stuck": 2, "interrupted": 3, "done": 4, "working": 5, "unknown": 6}
LABEL = {
    "blocked": "!! NEEDS APPROVAL",
    "waiting": "?  QUESTION FOR YOU",
    "stuck": "~  SILENT (stuck?)",
    "interrupted": "x  INTERRUPTED",
    "done": "✓  DONE",
    "working": "…  working",
    "unknown": "·  no state",
}
ATTENTION = {"blocked", "waiting", "stuck", "interrupted", "done"}
NEEDS_USER = {"blocked", "waiting", "stuck", "interrupted"}

AUTOMERGE = os.environ.get("TOWER_AUTOMERGE", "1") != "0"
GATE_CHECK = os.environ.get("FLEET_GATE_CHECK", "CI OK")  # the one aggregate check that means "CI passed"
HOLD_LABEL = "hold"
NUDGE_MINUTES = int(os.environ.get("FLEET_NUDGE_MINUTES", "30"))
DEPLOY_DIR = Path(os.environ.get("FLEET_DEPLOY_DIR", str(Path.home() / "FamilyOffice-prod")))
# A PR touching any of these is never merged by the tower alone: schema, deploy machinery, secrets, CI.
ASK_PATHS = re.compile(r"(^|/)(migrations|alembic)/|^infra/|^\.github/workflows/|(^|/)\.env|"
                       r"^scripts/(deployer|start_prod|stop_prod|smoke_prod|init_prod)\b|^Makefile$|"
                       r"(^|/)\.git/hooks/")
# Files CI skips (the repo's paths-ignore): a PR made only of these has no checks by design.
DOCS_ONLY = re.compile(r"^docs/|^\.claude/|(^|/)(CLAUDE|README)\.md$|^[^/]+\.md$|^tests/TESTING_GUIDE\.md$")
DOCS_SETTLE_MINUTES = 5  # a docs-only PR with no checks must be this old, so CI isn't simply late

LANDING_PROTOCOL = ""  # intentionally empty: the repo's CLAUDE.md already defines landing; agents work on their own


# ---------- small utilities ----------

def jload(name: str, default):
    try:
        return json.loads((STATE_DIR / name).read_text())
    except (OSError, ValueError):
        return default


def jsave(name: str, value) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / f".{name}.tmp"
    tmp.write_text(json.dumps(value, indent=1))
    tmp.replace(STATE_DIR / name)


def short(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def ago(ms: int | None, now_ms: int) -> str:
    if not ms:
        return "?"
    secs = max(0, (now_ms - ms) // 1000)
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def clean_title(title: str | None) -> str:
    return TITLE_GLYPHS.sub("", title or "").strip()


# ---------- orca + gh ----------

def orca_bin() -> str:
    explicit = os.environ.get("ORCA_CLI_COMMAND")
    if explicit:
        return explicit
    found = shutil.which("orca")
    if found:
        return found
    return str(Path(os.environ.get("ORCA_CLI_BIN_DIR", "/Applications/Orca.app/Contents/Resources/bin")) / "orca")


def orca(*args: str, timeout: int = 30) -> dict:
    proc = subprocess.run([orca_bin(), *args, "--json"], capture_output=True, text=True, timeout=timeout)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"orca {' '.join(args[:3])} failed: {(proc.stderr or proc.stdout).strip()[:400]}")
    if not data.get("ok", False):
        raise SystemExit(f"orca {' '.join(args[:3])} error: {json.dumps(data.get('error', data))[:400]}")
    return data["result"]


def gh_json(cwd: str | None, *args: str):
    if not shutil.which("gh") or not cwd or not Path(cwd).is_dir():
        return None
    try:
        proc = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=20)
        return json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def ci_summary(checks: list[dict]) -> tuple[str, str]:
    """(state, words) where state is passed | failed | running | none."""
    if not checks:
        return "none", "no CI checks"
    failed = [c.get("name") or c.get("context") or "?" for c in checks
              if (c.get("conclusion") or c.get("state") or "").upper()
              in {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}]
    pending = [c for c in checks
               if (c.get("status") or "").upper() not in {"", "COMPLETED"}
               or (c.get("state") or "").upper() in {"PENDING", "EXPECTED"}]
    if failed:
        return "failed", f"CI failed: {', '.join(failed)}"
    if pending:
        return "running", f"CI running ({len(checks) - len(pending)}/{len(checks)} finished)"
    return "passed", f"CI passed ({len(checks)}/{len(checks)})"


def pr_for(branch: str, cwd: str | None) -> dict | None:
    data = gh_json(cwd, "pr", "view", branch, "--json",
                   "number,url,state,isDraft,mergeable,reviewDecision,statusCheckRollup")
    if not data:
        return None
    ci_state, ci_words = ci_summary(data.get("statusCheckRollup") or [])
    return {
        "number": data["number"], "url": data["url"], "state": data["state"].lower(),
        "draft": data.get("isDraft", False), "mergeable": (data.get("mergeable") or "").lower(),
        "review": (data.get("reviewDecision") or "").lower(), "ci": ci_state, "ciWords": ci_words,
    }


def latest_ci_run(runs: list[dict]) -> dict | None:
    """Newest run that is the repo's own CI, whatever its workflow is called: Dependabot's update jobs
    (event "dynamic") run on the default branch too, and are not CI."""
    return next((r for r in runs if r.get("event") != "dynamic"), None)


def default_branch_ci(cwd: str | None) -> dict | None:
    repo = gh_json(cwd, "repo", "view", "--json", "nameWithOwner,defaultBranchRef")
    if not repo:
        return None
    branch = repo["defaultBranchRef"]["name"]
    runs = gh_json(cwd, "run", "list", "--branch", branch, "--limit", "20",
                   "--json", "conclusion,status,displayTitle,url,event")
    r = latest_ci_run(runs or [])
    if not r:
        return {"repo": repo["nameWithOwner"], "branch": branch, "outcome": "none", "words": "no CI runs"}
    outcome = r["conclusion"] or r["status"]
    return {"repo": repo["nameWithOwner"], "branch": branch, "outcome": outcome,
            "words": f"{outcome} · {short(r['displayTitle'], 70)}", "url": r["url"]}


def screen_excerpt(handle: str, lines: int = 14) -> list[str]:
    """Bottom of the rendered screen, minus rule lines: where prompts and menus live."""
    try:
        tail = orca("terminal", "read", "--terminal", handle, "--screen")["terminal"]["tail"]
    except SystemExit:
        return []
    kept = [ln.strip().strip("│┃|").strip() for ln in tail if ln.strip() and not RULE_LINE.match(ln)]
    return [short(ln, 160) for ln in kept[-lines:]]


# ---------- snapshot ----------

def snapshot(stuck_minutes: int = 20, with_screens: bool = True, with_prs: bool = True) -> dict:
    now_ms = int(time.time() * 1000)
    self_pane = os.environ.get("ORCA_PANE_KEY")
    self_handle = os.environ.get("ORCA_TERMINAL_HANDLE")
    names = jload("names.json", {})
    spawned = jload("spawned.json", {})

    terminals = orca("terminal", "list")["terminals"]
    by_pane = {f"{t['tabId']}:{t['leafId']}": t for t in terminals}
    worktrees = orca("worktree", "ps")["worktrees"]

    agents, seen_panes, prs, repos = [], set(), {}, {}
    for wt in worktrees:
        wt_id = wt.get("worktreeId")
        wt_name = wt.get("displayName") or Path(wt.get("path", "")).name
        branch = (wt.get("branch") or "").removeprefix("refs/heads/")
        repos.setdefault(wt.get("repo") or wt_name, wt.get("path"))
        if with_prs and wt.get("agents") and branch and branch not in DEFAULT_BRANCHES and branch not in prs:
            prs[branch] = pr_for(branch, wt.get("path"))
        for a in wt.get("agents", []):
            pane = a.get("paneKey")
            term = by_pane.get(pane)
            if pane == self_pane or term is None:  # skip self and hook records of closed terminals
                continue
            seen_panes.add(pane)
            state = a.get("state") or "unknown"
            if a.get("interrupted"):
                state = "interrupted"
            if state == "working" and now_ms - (term.get("lastOutputAt") or now_ms) > stuck_minutes * 60_000:
                state = "stuck"
            screen = screen_excerpt(term["handle"]) if with_screens and state in {"waiting", "blocked"} else []
            # Claude Code reports permission prompts as `waiting`; the screen tells them apart.
            if state == "waiting" and any(PERMISSION_HINT.search(ln) for ln in screen):
                state = "blocked"
            agents.append({
                "name": names.get(term["handle"]) or (spawned.get(wt_id) or {}).get("name")
                        or clean_title(term.get("title")) or wt_name,
                "handle": term["handle"], "pane": pane,
                "agent": a.get("agentType") or term.get("agentIdentity") or "agent",
                "state": state, "for": ago(a.get("stateStartedAt"), now_ms),
                "stateStartedAt": a.get("stateStartedAt"), "quiet": ago(term.get("lastOutputAt"), now_ms),
                "worktree": wt_name, "worktreeId": wt_id, "branch": branch, "path": wt.get("path"),
                "spawned": wt_id in spawned,
                "pr": prs.get(branch) if with_prs else None,
                "card": {"status": wt.get("workspaceStatus"), "comment": wt.get("comment") or "",
                         "unread": wt.get("unread", False)},
                "prompt": short(a.get("prompt"), 140), "last": short(a.get("lastAssistantMessage"), 320),
                "tool": a.get("toolName") or "", "toolInput": short(a.get("toolInput"), 200),
                "screen": screen,
            })

    others = []
    for t in terminals:
        pane = f"{t['tabId']}:{t['leafId']}"
        if pane in seen_panes or pane == self_pane or t["handle"] == self_handle:
            continue
        others.append({
            "name": names.get(t["handle"]) or clean_title(t.get("title")) or "terminal",
            "handle": t["handle"], "agent": t.get("agentIdentity") or "shell",
            "quiet": ago(t.get("lastOutputAt"), now_ms), "worktree": Path(t.get("worktreePath", "")).name,
            "preview": short((t.get("preview") or "").splitlines()[-1] if t.get("preview") else "", 100),
        })

    agents.sort(key=lambda a: (RANK.get(a["state"], 9), -(a["stateStartedAt"] or 0)))
    mains = [ci for ci in (default_branch_ci(p) for p in repos.values()) if ci] if with_prs else []
    live_spawned = {a["worktreeId"] for a in agents if a["spawned"]}
    return {"agents": agents, "others": others, "mainCi": mains, "self": self_handle, "takenAt": now_ms,
            "spawnedTotal": len(spawned), "spawnedLive": len(live_spawned)}


# ---------- Orca card sync (spawned worktrees only) ----------

def card_for(a: dict) -> dict:
    pr = a.get("pr")
    if pr and pr["state"] == "merged":
        return {"status": "completed", "unread": False, "comment": f"merged #{pr['number']}"}
    if pr and pr["state"] == "open":
        if pr["ci"] == "failed":
            return {"status": "in-review", "unread": True, "comment": short(f"PR #{pr['number']} · {pr['ciWords']}", 90)}
        if pr["mergeable"] == "conflicting":
            return {"status": "in-review", "unread": True, "comment": f"PR #{pr['number']} · merge conflict"}
        if pr["ci"] == "passed":
            return {"status": "in-review", "unread": True, "comment": f"PR #{pr['number']} · CI passed · ready to merge"}
        return {"status": "in-review", "unread": False, "comment": f"PR #{pr['number']} · {pr['ciWords']}"}
    detail = {
        "blocked": f"needs approval: {a['tool']} {a['toolInput']}",
        "waiting": "question for you",
        "stuck": f"silent {a['quiet']}",
        "interrupted": "interrupted",
        "done": f"done: {a['last']}",
        "working": f"working: {a['tool']}" if a["tool"] else "working",
    }.get(a["state"], a["state"])
    return {"status": "in-progress", "unread": a["state"] in NEEDS_USER, "comment": short(detail, 90)}


def sync_cards(snap: dict) -> None:
    last = jload("cards.json", {})
    changed = False
    for a in snap["agents"]:
        if not a["spawned"]:
            continue
        if a.get("pr") is None and (last.get(a["worktreeId"]) or {}).get("status") in {"in-review", "completed"}:
            continue  # PRs weren't looked up this poll; keep the PR-derived card
        want = card_for(a)
        if last.get(a["worktreeId"]) == want:
            continue
        args = ["worktree", "set", "--worktree", f"id:{a['worktreeId']}", "--workspace-status", want["status"],
                "--comment", want["comment"], "--unread" if want["unread"] else "--read"]
        if a.get("pr"):
            args += ["--pr", str(a["pr"]["number"])]
        try:
            orca(*args)
            last[a["worktreeId"]] = want
            changed = True
        except SystemExit:
            pass
    if changed:
        jsave("cards.json", last)


# ---------- acknowledgement + change tracking ----------

def event_key(a: dict) -> str:
    return f"{a['pane']}|{a['state']}|{a['stateStartedAt']}"


def pr_event_key(a: dict) -> str | None:
    pr = a.get("pr")
    if not pr or pr["state"] != "open" or pr["ci"] not in {"passed", "failed"}:
        return None
    return f"pr|{pr['url']}|{pr['ci']}"


def fingerprint(a: dict) -> str:
    pr = a.get("pr") or {}
    return f"{a['state']}|{a['stateStartedAt']}|{pr.get('state')}|{pr.get('ci')}|{pr.get('mergeable')}"


def ack(snap: dict) -> None:
    acked = set(jload("acked.json", []))
    acked |= {event_key(a) for a in snap["agents"] if a["state"] in ATTENTION}
    acked |= {k for k in map(pr_event_key, snap["agents"]) if k}
    jsave("acked.json", sorted(acked)[-800:])


# ---------- rendering ----------

def render_agent(a: dict) -> list[str]:
    where = a["worktree"] if a["worktree"] == a["branch"] else f"{a['worktree']} ({a['branch']})"
    star = "★ " if a["spawned"] else ""
    out = [f"{LABEL.get(a['state'], a['state'])}  {star}[{a['name']}]  {a['agent']} · {where} · "
           f"{a['state']} {a['for']} · quiet {a['quiet']}",
           f"   handle: {a['handle']}"]
    if a["prompt"]:
        out.append(f"   asked:  {a['prompt']}")
    if a["state"] in {"blocked", "working", "stuck"} and a["tool"]:
        out.append(f"   tool:   {a['tool']} {a['toolInput']}")
    if a["last"] and not a["screen"]:
        out.append(f"   last:   {a['last']}")
    if a.get("pr"):
        pr = a["pr"]
        flags = " · ".join(x for x in [
            pr["state"], "draft" if pr["draft"] else "", pr["ciWords"],
            f"mergeable {pr['mergeable']}" if pr["mergeable"] not in {"", "unknown"} else "",
            f"review {pr['review']}" if pr["review"] else ""] if x)
        out.append(f"   pr:     #{pr['number']} {flags} · {pr['url']}")
    elif a["branch"] and a["branch"] not in DEFAULT_BRANCHES and a["state"] == "done":
        out.append("   pr:     none yet")
    if a["screen"]:
        out.append("   screen:")
        out.extend(f"   │ {ln}" for ln in a["screen"])
    return out


def render(snap: dict, changes_only: bool) -> str:
    agents, others = snap["agents"], snap["others"]
    counts: dict[str, int] = {}
    for a in agents:
        counts[a["state"]] = counts.get(a["state"], 0) + 1
    head = ", ".join(f"{n} {s}" for s, n in sorted(counts.items(), key=lambda kv: RANK.get(kv[0], 9)))
    lines = [f"FLEET  {len(agents)} agent(s): {head or 'none'}"
             + (f"  ·  tower-started: {snap['spawnedLive']} live / {snap['spawnedTotal']} total"
                if snap["spawnedTotal"] else "")
             + (f"  ·  {len(others)} other terminal(s)" if others and not changes_only else "")]
    for ci in snap.get("mainCi", []):
        if not changes_only or ci.get("outcome") not in {"success", "none"}:
            lines.append(f"   {ci['repo']} {ci['branch']} CI: {ci['words']}")
    keep = jload("keep.json", [])
    if keep and not changes_only:
        lines.append("   keep: " + " | ".join(f"{i}) {k}" for i, k in enumerate(keep, 1)))

    last = jload("last_report.json", {})
    shown = [a for a in agents if not changes_only or last.get(a["pane"]) != fingerprint(a)]
    if changes_only:
        gone = set(last) - {a["pane"] for a in agents}
        if gone:
            lines.append(f"   {len(gone)} agent(s) closed since last report")
        if not shown and not gone:
            lines.append("   no changes since last report")
    for a in shown:
        lines.append("")
        lines.extend(render_agent(a))
    if others and not changes_only:
        lines.append("")
        lines.append("OTHER TERMINALS")
        for o in others:
            lines.append(f"   [{o['name']}] {o['agent']} · {o['worktree']} · quiet {o['quiet']} · "
                         f"{o['handle']} · {o['preview']}")
    jsave("last_report.json", {a["pane"]: fingerprint(a) for a in agents})
    return "\n".join(lines)


# ---------- release manager ----------

def git(cwd: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=60)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def main_checkout(cwd: str) -> str:
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return str(Path(common).parent) if common else cwd


def gate(number: int, repo: str) -> dict:
    """The tower's merge decision, as rules not judgment.

    verdict MERGE: every rule holds, merge now. ASK: mergeable but a human must OK it.
    FIX: an agent has work to do (`fix` says what). WAIT: CI or the agent is still going.
    """
    pr = gh_json(repo, "pr", "view", str(number), "--json",
                 "number,title,url,state,isDraft,mergeable,mergeStateStatus,headRefName,headRefOid,"
                 "baseRefName,body,labels,files,statusCheckRollup,updatedAt")
    if not pr:
        return {"pr": number, "verdict": "WAIT", "reasons": ["gh could not read the PR"]}
    out = {"pr": number, "title": pr["title"], "url": pr["url"], "branch": pr["headRefName"],
           "reasons": [], "ask": [], "fix": None}
    if pr["state"] != "OPEN":
        return {**out, "verdict": "WAIT", "reasons": [f"PR is {pr['state'].lower()}"]}
    files = [f["path"] for f in pr.get("files") or []]
    docs_only = bool(files) and all(DOCS_ONLY.search(f) for f in files)
    checks = pr.get("statusCheckRollup") or []
    ci_state, ci_words = ci_summary(checks)
    gate_runs = [c for c in checks if (c.get("name") or c.get("context")) == GATE_CHECK]
    gate_ok = any((c.get("conclusion") or c.get("state") or "").upper() == "SUCCESS" for c in gate_runs)

    if pr.get("isDraft"):
        out["reasons"].append("draft")
    if any(lbl["name"] == HOLD_LABEL for lbl in pr.get("labels") or []):
        out["ask"].append(f"labelled '{HOLD_LABEL}'")

    # CI
    if ci_state == "failed":
        return {**out, "verdict": "FIX", "fix": f"{ci_words}. Fix on this branch and push.",
                "reasons": [ci_words]}
    if ci_state == "running":
        return {**out, "verdict": "WAIT", "reasons": [ci_words]}
    if not gate_ok:
        # A docs-only PR skips the CI OK workflow but may carry other checks (e.g. Skills); those all passed above.
        if not docs_only:
            if checks:
                return {**out, "verdict": "WAIT", "reasons": [f"no green '{GATE_CHECK}' check yet ({ci_words})"]}
            return {**out, "verdict": "WAIT", "reasons": ["no CI checks on a code PR yet"]}
        if not checks:
            age_min = (time.time() - calendar.timegm(time.strptime(pr["updatedAt"], "%Y-%m-%dT%H:%M:%SZ"))) / 60
            if age_min < DOCS_SETTLE_MINUTES:
                return {**out, "verdict": "WAIT", "reasons": ["docs-only PR, giving CI a moment to appear"]}
    if not docs_only and "## Review gate" not in (pr.get("body") or ""):
        return {**out, "verdict": "FIX", "reasons": ["PR body has no '## Review gate' block"],
                "fix": "Run /review-code --gate, fix or rebut findings >= 75, and put its '## Review gate' block "
                       "in the PR body."}

    # Mergeability and freshness against the base branch
    if (pr.get("mergeable") or "").upper() == "CONFLICTING":
        return {**out, "verdict": "FIX", "reasons": ["merge conflict with " + pr["baseRefName"]],
                "fix": f"Merge origin/{pr['baseRefName']} into your branch, resolve the conflicts, "
                       "re-run the standalone tests, push."}
    if (pr.get("mergeable") or "").upper() != "MERGEABLE":
        return {**out, "verdict": "WAIT", "reasons": ["GitHub is still computing mergeability"]}
    main = main_checkout(repo)
    git(main, "fetch", "--quiet", "origin", pr["baseRefName"], pr["headRefName"])
    behind = git(main, "rev-list", "--count", f"{pr['headRefOid']}..origin/{pr['baseRefName']}",
                 "--", ".", ":(exclude)docs")  # generated docs commits never make a PR stale
    if behind and behind != "0" and not docs_only:
        return {**out, "verdict": "FIX", "reasons": [f"{behind} commit(s) behind {pr['baseRefName']}"],
                "fix": f"Merge origin/{pr['baseRefName']} into your branch (it is {behind} commit(s) behind), "
                       "re-run the standalone tests, push."}

    # The base branch itself must be healthy, and prod must be taking deploys.
    base_ci = default_branch_ci(repo)
    if base_ci and base_ci.get("outcome") in {"failure", "cancelled", "timed_out"}:
        out["reasons"].append(f"{pr['baseRefName']} CI is red; land nothing on top of it")
    paused = deploy_state().get("paused_reason") if deploys_from(repo) else None
    if paused:
        out["ask"].append(f"prod deploys are paused ({paused})")

    # The agent that owns the branch must be finished, with everything pushed.
    owner = next((a for a in snapshot(with_screens=False, with_prs=False)["agents"]
                  if a["branch"] == pr["headRefName"]), None)
    if owner:
        if owner["state"] != "done":
            out["reasons"].append(f"its agent is {owner['state']}")
        elif owner["last"].rstrip().endswith("?"):
            out["ask"].append("its agent's last message is a question")
        wt_path = next((w.get("path") for w in orca("worktree", "ps")["worktrees"]
                        if w.get("worktreeId") == owner["worktreeId"]), None)
        if wt_path:
            if git(wt_path, "status", "--porcelain"):
                out["reasons"].append("its worktree has uncommitted changes")
            elif git(wt_path, "rev-parse", "HEAD") != pr["headRefOid"]:
                out["reasons"].append("its worktree has commits that aren't pushed")

    risky = sorted({f for f in files if ASK_PATHS.search(f)})
    if risky:
        out["ask"].append("touches " + ", ".join(risky[:4]) + (" …" if len(risky) > 4 else ""))
    out["docsOnly"] = docs_only
    out["ci"] = "docs-only, no CI" if docs_only and not checks else ci_words
    if out["reasons"]:
        return {**out, "verdict": "WAIT"}
    if out["ask"] or not AUTOMERGE:
        if not AUTOMERGE:
            out["ask"].append("auto-merge is off (TOWER_AUTOMERGE=0)")
        return {**out, "verdict": "ASK"}
    return {**out, "verdict": "MERGE"}


def render_gate(g: dict) -> str:
    lines = [f"GATE #{g['pr']} {g['verdict']}  {g.get('title', '')}"]
    if g.get("ci"):
        lines.append(f"   ci:    {g['ci']}")
    for r in g.get("reasons", []):
        lines.append(f"   wait:  {r}")
    for r in g.get("ask", []):
        lines.append(f"   ask:   {r}")
    if g.get("fix"):
        lines.append(f"   fix:   {g['fix']}")
    if g.get("url"):
        lines.append(f"   {g['url']}")
    return "\n".join(lines)


def cmd_gate(args) -> int:
    g = gate(args.pr, args.repo or os.getcwd())
    print(json.dumps(g, indent=1) if args.json else render_gate(g))
    return 0


def cmd_land(args) -> int:
    repo = args.repo or os.getcwd()
    g = gate(args.pr, repo)
    if not (g["verdict"] == "MERGE" or (g["verdict"] == "ASK" and args.approved)):
        print(render_gate(g))
        print(f"NOT MERGED: verdict {g['verdict']}" + (" (pass --approved after the user's OK)"
                                                        if g["verdict"] == "ASK" else ""))
        return 1
    proc = subprocess.run(["gh", "pr", "merge", str(args.pr), "--merge", "--delete-branch"], cwd=repo,
                          capture_output=True, text=True, timeout=120)
    merged = gh_json(repo, "pr", "view", str(args.pr), "--json", "state,mergeCommit")
    if not merged or merged["state"] != "MERGED":
        print(f"MERGE FAILED #{args.pr}: {(proc.stderr or proc.stdout).strip()[:400]}")
        return 1
    sha = (merged.get("mergeCommit") or {}).get("oid", "")
    landed = jload("landed.json", {})
    landed[sha] = {"pr": args.pr, "title": g.get("title"), "branch": g.get("branch"), "repo": repo,
                   "at": int(time.time() * 1000), "auto": g["verdict"] == "MERGE"}
    jsave("landed.json", landed)
    print(f"MERGED #{args.pr} as {sha[:7]}  {g.get('title', '')}")
    return 0


def cmd_cleanup(args) -> int:
    """Close the merged PR's agent and remove its worktree (Orca deletes the local branch too)."""
    repo = args.repo or os.getcwd()
    pr = gh_json(repo, "pr", "view", str(args.pr), "--json", "state,headRefName")
    if not pr or pr["state"] != "MERGED":
        print(f"NOT CLEANED: PR #{args.pr} is {pr and pr['state'].lower()}")
        return 1
    branch = pr["headRefName"]
    wt = next((w for w in orca("worktree", "ps")["worktrees"]
               if (w.get("branch") or "").removeprefix("refs/heads/") == branch and not w.get("isMainWorktree")),
              None)
    if not wt:
        git(main_checkout(repo), "branch", "-D", branch)
        print(f"no worktree on {branch}; local branch removed if it existed")
        return 0
    if git(wt["path"], "status", "--porcelain"):
        print(f"NOT CLEANED: {wt['path']} has uncommitted changes")
        return 1
    me = os.environ.get("ORCA_WORKTREE_ID")
    if wt["worktreeId"] == me:
        print("NOT CLEANED: that is the tower's own worktree")
        return 1
    for t in orca("terminal", "list", "--worktree", f"id:{wt['worktreeId']}")["terminals"]:
        try:
            orca("terminal", "close", "--terminal", t["handle"])
        except SystemExit:
            pass
    orca("worktree", "rm", "--worktree", f"id:{wt['worktreeId']}", timeout=120)
    git(main_checkout(repo), "branch", "-D", branch)
    spawned = jload("spawned.json", {})
    if spawned.pop(wt["worktreeId"], None):
        jsave("spawned.json", spawned)
    print(f"CLEANED #{args.pr}: closed its agent, removed {wt['path']} and branch {branch}")
    return 0


# ---------- watchdog: prod deploys + base-branch CI ----------

def deploy_state() -> dict:
    try:
        return json.loads((DEPLOY_DIR / ".deploy" / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def deploys_from(repo: str) -> bool:
    """Whether the watched prod clone deploys this repo (same origin)."""
    return bool(git(repo, "remote", "get-url", "origin")) and \
        git(repo, "remote", "get-url", "origin") == git(str(DEPLOY_DIR), "remote", "get-url", "origin")


def deploy_events() -> list[str]:
    """New lines in the deployer's history since the last look (read-only; never runs prod commands)."""
    path = DEPLOY_DIR / ".deploy" / "history.jsonl"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    seen = jload("deploy_seen.json", {"count": None})
    jsave("deploy_seen.json", {"count": len(lines)})
    if seen["count"] is None or seen["count"] > len(lines):
        return []  # first look, or the file was rotated: start from here
    landed = jload("landed.json", {})
    events = []
    for raw in lines[seen["count"]:]:
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        pr = next((v for k, v in landed.items() if k.startswith(e.get("sha", "")[:7])), None)
        what = f"#{pr['pr']} {short(pr['title'], 60)}" if pr else e.get("sha", "")[:7]
        events.append(f"FLEET-EVENT DEPLOY-{e.get('outcome', '?').upper()} {what} · {short(e.get('detail'), 160)}")
    return events


def main_ci_events(snap: dict) -> list[str]:
    acked = set(jload("acked.json", []))
    out, keys = [], []
    for ci in snap.get("mainCi", []):
        if ci.get("outcome") in {"failure", "cancelled", "timed_out"}:
            key = f"main|{ci.get('url')}"
            if key not in acked:
                keys.append(key)
                out.append(f"FLEET-EVENT MAIN-CI-FAILED {ci['repo']} {ci['branch']} · {ci['words']} · {ci.get('url')}")
    if keys:
        jsave("acked.json", sorted(acked | set(keys))[-800:])
    return out


def nudge_events(snap: dict) -> list[str]:
    acked = set(jload("acked.json", []))
    now_ms, out, keys = int(time.time() * 1000), [], []
    for a in snap["agents"]:
        started = a.get("stateStartedAt") or now_ms
        if a["state"] in NEEDS_USER and now_ms - started > NUDGE_MINUTES * 60_000:
            key = f"nudge|{event_key(a)}"
            if key not in acked:
                keys.append(key)
                out.append(f"FLEET-EVENT NUDGE [{a['name']}] {a['state']} for {a['for']}")
    if keys:
        jsave("acked.json", sorted(acked | set(keys))[-800:])
    return out


def ready_events(snap: dict) -> list[str]:
    """PRs whose gate says MERGE or ASK; checked only for done agents with a quiet PR."""
    acked = set(jload("acked.json", []))
    out, keys = [], []
    for a in snap["agents"]:
        pr = a.get("pr")
        if not pr or pr["state"] != "open" or a["state"] != "done" or pr["ci"] not in {"passed", "none"}:
            continue
        g = gate(pr["number"], a.get("path") or os.getcwd())
        if g["verdict"] not in {"MERGE", "ASK"}:
            continue
        key = f"ready|{pr['url']}|{g['verdict']}|{a['stateStartedAt']}"
        if key not in acked:
            keys.append(key)
            out.append(f"FLEET-EVENT PR-{'READY' if g['verdict'] == 'MERGE' else 'NEEDS-OK'} [{a['name']}] "
                       f"#{pr['number']} {short(g.get('title'), 70)}" + (f" · {'; '.join(g['ask'])}" if g["ask"] else ""))
    if keys:
        jsave("acked.json", sorted(acked | set(keys))[-800:])
    return out


# ---------- commands ----------

def cmd_report(args) -> int:
    snap = snapshot(args.stuck_minutes)
    sync_cards(snap)
    if not args.no_ack:
        ack(snap)
    print(json.dumps(snap, indent=1) if args.json else render(snap, args.changes))
    return 0


def _claim_watch_slot() -> None:
    """One watcher at a time: the newest wins and stops the previous one."""
    pidfile = STATE_DIR / "watch.pid"
    try:
        old = int(pidfile.read_text())
        if old != os.getpid():
            os.kill(old, signal.SIGTERM)
    except (OSError, ValueError):
        pass
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))

    def superseded(*_):
        print("FLEET-WATCH SUPERSEDED")
        sys.exit(0)

    signal.signal(signal.SIGTERM, superseded)


def cmd_watch(args) -> int:
    _claim_watch_slot()
    deadline = time.time() + args.max_minutes * 60 if args.max_minutes else None
    polls = 0
    while deadline is None or time.time() < deadline:
        with_prs = polls % max(1, args.pr_every) == 0
        polls += 1
        try:
            snap = snapshot(args.stuck_minutes, with_screens=True, with_prs=with_prs)
            sync_cards(snap)
        except (SystemExit, subprocess.SubprocessError) as exc:
            print(f"FLEET-WATCH ERROR {exc}")
            return 2
        acked = set(jload("acked.json", []))
        fresh = [a for a in snap["agents"] if a["state"] in ATTENTION and event_key(a) not in acked]
        fresh_prs = {k: a for a in snap["agents"] if (k := pr_event_key(a)) and k not in acked}
        extra = deploy_events() + nudge_events(snap)
        if with_prs:
            extra += main_ci_events(snap) + ready_events(snap)
        if fresh or fresh_prs or extra:
            jsave("acked.json", sorted(set(jload("acked.json", [])) | {event_key(a) for a in fresh}
                                       | set(fresh_prs))[-800:])
            for line in extra:
                print(line)
            for a in fresh:
                print(f"FLEET-EVENT {a['state'].upper()} [{a['name']}] {a['handle']} · "
                      f"{short(a['last'] or a['toolInput'], 160)}")
            for a in fresh_prs.values():
                pr = a["pr"]
                print(f"FLEET-EVENT PR-CI-{pr['ci'].upper()} [{a['name']}] #{pr['number']} "
                      f"{pr['ciWords']} · {pr['url']}")
            session = jload("session.json", {"events": 0})
            session["events"] = session.get("events", 0) + 1
            jsave("session.json", session)
            if session["events"] >= RESET_AFTER_EVENTS:
                print(f"FLEET-HINT reset-due events={session['events']}")
            return 0
        time.sleep(args.interval)
    print("FLEET-WATCH TIMEOUT")
    return 0


def cmd_spawn(args) -> int:
    prompt = Path(args.prompt_file).read_text() if args.prompt_file else args.prompt
    if not prompt:
        raise SystemExit("spawn needs --prompt or --prompt-file")
    repo_path = Path(args.repo).resolve() if args.repo else None
    root = repo_path or Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                                            text=True).stdout.strip() or ".")
    extras = root / ".claude" / "fleet-brief.md"
    if extras.is_file():
        prompt += "\n\n" + extras.read_text().strip()
    prompt += LANDING_PROTOCOL

    create = ["worktree", "create", "--name", args.slug, "--agent", args.agent, "--prompt", prompt,
              "--comment", short(f"starting: {args.name}", 90)]
    if repo_path:
        create += ["--repo", f"path:{repo_path}"]
    parent = os.environ.get("ORCA_WORKTREE_ID")
    tower_root = parent.split("::", 1)[1] if parent and "::" in parent else None
    same_repo = bool(tower_root) and (not repo_path or Path(tower_root).resolve() == repo_path)
    lineage = ["--parent-worktree", f"worktree:{parent}"] if same_repo else ["--no-parent"]
    try:
        res = orca(*create, *lineage, timeout=180)
    except SystemExit:
        if not same_repo:
            raise
        res = orca(*create, "--no-parent", timeout=180)  # lineage is cosmetic; never fail a spawn over it
    wt = res["worktree"]
    handle = (res.get("startupTerminal") or {}).get("handle") or res.get("agentTerminalHandle")
    if not handle:
        terms = orca("terminal", "list", "--worktree", f"id:{wt['id']}")["terminals"]
        handle = terms[0]["handle"] if terms else None

    spawned = jload("spawned.json", {})
    spawned[wt["id"]] = {"name": args.name, "handle": handle, "slug": args.slug, "agent": args.agent,
                         "path": wt.get("path"), "branch": (wt.get("branch") or "").removeprefix("refs/heads/"),
                         "createdAt": int(time.time() * 1000)}
    jsave("spawned.json", spawned)
    if handle:
        names = jload("names.json", {})
        names[handle] = args.name
        jsave("names.json", names)
    try:
        orca("worktree", "set", "--worktree", f"id:{wt['id']}", "--display-name", args.name,
             "--workspace-status", "in-progress")
    except SystemExit:
        pass
    print(json.dumps({"name": args.name, "handle": handle, "worktreeId": wt["id"], "path": wt.get("path"),
                      "branch": spawned[wt["id"]]["branch"], "lineage": "child of tower" if same_repo else "none",
                      "warnings": res.get("warnings") or []}, indent=1))
    return 0


def cmd_name(args) -> int:
    names = jload("names.json", {})
    if args.label:
        names[args.handle] = args.label
    else:
        names.pop(args.handle, None)
    jsave("names.json", names)
    print(f"named {args.handle} -> {args.label or '(cleared)'}")
    return 0


def cmd_keep(args) -> int:
    keep = jload("keep.json", [])
    if args.rm:
        if 0 < args.rm <= len(keep):
            print(f"removed: {keep.pop(args.rm - 1)}")
    elif args.text:
        keep.append(args.text)
    jsave("keep.json", keep)
    print("\n".join(f"{i}) {k}" for i, k in enumerate(keep, 1)) or "(nothing kept)")
    return 0


def cmd_reset(_args) -> int:
    handle = os.environ.get("ORCA_TERMINAL_HANDLE")
    if not handle:
        raise SystemExit("reset must run inside the tower's Orca terminal")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(STATE_DIR / "reset.log", "a")
    subprocess.Popen([sys.executable, __file__, "_reset-worker", handle], stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, start_new_session=True)
    print("reset scheduled: when this turn ends the tower runs /clear, then /fleet resume")
    return 0


def agent_state(handle: str) -> str | None:
    """Hook-reported state of the agent in this terminal. Unlike `tui-idle`, it stays
    `working` through long tool calls, so it's the only safe signal that a turn ended."""
    try:
        t = orca("terminal", "show", "--terminal", handle)["terminal"]
        pane = f"{t['tabId']}:{t['leafId']}"
        for wt in orca("worktree", "ps")["worktrees"]:
            for a in wt.get("agents", []):
                if a.get("paneKey") == pane:
                    return a.get("state")
    except (SystemExit, KeyError):
        pass
    return None


def wait_turn_ended(handle: str, timeout_s: int, settle_s: int = 4) -> bool:
    """True once the agent has been out of `working` for settle_s seconds in a row."""
    deadline, calm_since = time.time() + timeout_s, None
    while time.time() < deadline:
        state = agent_state(handle)
        if state and state != "working":
            calm_since = calm_since or time.time()
            if time.time() - calm_since >= settle_s:
                return True
        else:
            calm_since = None
        time.sleep(1)
    return False


def cmd_reset_worker(args) -> int:
    stamp = lambda: time.strftime("%H:%M:%S")  # noqa: E731
    time.sleep(3)  # let the turn that scheduled the reset finish
    if not wait_turn_ended(args.handle, 900):
        print(f"{stamp()} reset aborted: tower turn never ended")
        return 1
    orca("terminal", "send", "--terminal", args.handle, "--text", "/clear", "--enter")
    time.sleep(4)
    orca("terminal", "send", "--terminal", args.handle, "--text", args.then, "--enter")
    print(f"{stamp()} reset done")
    return 0


def cmd_done_reset(_args) -> int:
    jsave("session.json", {"events": 0, "resetAt": int(time.time() * 1000)})
    print("event counter reset")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    # Back-compat with the first version's flags (`--watch ...`, `--name HANDLE LABEL`).
    if argv[:1] == ["--watch"]:
        argv = ["watch", *argv[1:]]
    elif argv[:1] == ["--name"]:
        argv = ["name", *argv[1:]]
    if not argv or argv[0].startswith("-"):
        argv = ["report", *argv]
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("report")
    r.add_argument("--changes", action="store_true", help="only agents that changed since the last report")
    r.add_argument("--json", action="store_true")
    r.add_argument("--no-ack", action="store_true")
    r.add_argument("--stuck-minutes", type=int, default=20)
    r.set_defaults(fn=cmd_report)

    w = sub.add_parser("watch")
    w.add_argument("--interval", type=int, default=15)
    w.add_argument("--pr-every", type=int, default=4, help="check PRs every N polls")
    w.add_argument("--max-minutes", type=int, default=0, help="0 = run until an event")
    w.add_argument("--stuck-minutes", type=int, default=20)
    w.set_defaults(fn=cmd_watch)

    s = sub.add_parser("spawn")
    s.add_argument("--name", required=True, help='fleet label, e.g. "1.98 · design"')
    s.add_argument("--slug", required=True, help="worktree/branch name, e.g. 198-design")
    s.add_argument("--prompt")
    s.add_argument("--prompt-file")
    s.add_argument("--agent", default="claude")
    s.add_argument("--repo", help="repo path when spawning for another project")
    s.set_defaults(fn=cmd_spawn)

    n = sub.add_parser("name")
    n.add_argument("handle")
    n.add_argument("label")
    n.set_defaults(fn=cmd_name)

    k = sub.add_parser("keep")
    k.add_argument("text", nargs="?")
    k.add_argument("--rm", type=int)
    k.set_defaults(fn=cmd_keep)

    g = sub.add_parser("gate")
    g.add_argument("pr", type=int)
    g.add_argument("--repo", help="any checkout of the PR's repo (default: cwd)")
    g.add_argument("--json", action="store_true")
    g.set_defaults(fn=cmd_gate)

    la = sub.add_parser("land")
    la.add_argument("pr", type=int)
    la.add_argument("--repo")
    la.add_argument("--approved", action="store_true", help="the user OK'd an ASK verdict")
    la.set_defaults(fn=cmd_land)

    c = sub.add_parser("cleanup")
    c.add_argument("pr", type=int)
    c.add_argument("--repo")
    c.set_defaults(fn=cmd_cleanup)

    sub.add_parser("reset").set_defaults(fn=cmd_reset)
    rw = sub.add_parser("_reset-worker")
    rw.add_argument("handle")
    rw.add_argument("then", nargs="?", default="/fleet resume")
    rw.set_defaults(fn=cmd_reset_worker)
    sub.add_parser("done-reset").set_defaults(fn=cmd_done_reset)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
