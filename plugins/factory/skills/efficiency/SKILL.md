---
name: efficiency
description: Finds where agent sessions, factory runs and an app's own model calls waste tokens or repeat work — scans the transcripts and runtime usage, samples the worst sessions, and turns each pattern into a concrete fix (skill line, hook, CLAUDE.md change, script, model switch, cache) filed as a task or a small factory PR. Use when asked to review token spend, session efficiency or agent waste, or when a scheduled factory efficiency run starts.
argument-hint: "[--since 7d] [--all] [--report-only]"
---

# Efficiency

You find waste and turn it into fixes. Everything repo-specific comes from `.factory.toml` (`factory config`,
section `[efficiency]`); the detectors, their thresholds and the cost model are in `sfactory/efficiency.py` and
the spec `docs/tasks/F-32-efficiency-scout.md` in the software-factory repo. An unattended run sets `$FACTORY_BRANCH`
and `$FACTORY_REPORT` (`.factory/report.md`, git-ignored). Arguments: `$ARGUMENTS` (`--report-only` = steps 1–3 and the report, nothing filed).

**Privacy.** Transcripts hold private data. Work from `factory efficiency scan` and `factory efficiency show`, which
carry only metrics, ids, paths, tool and command names. Never open a transcript JSONL, and never put prompt text,
file contents or tool output in a task, PR, commit or report.

## 1. Scan

`factory efficiency scan --json` (add `--since`, or `--all` when `[efficiency] scope = "all"` or asked). Each finding
has `detector`, `usd` (at stake: an upper bound, findings overlap), `tokens`, `count`, `session`, `items`. Read the
`errors` too: a failing `runtime_usage` command is itself a finding for that repo.

## 2. Group into patterns

A fix removes a pattern, not one session's symptom. Group findings by cause across sessions:
same file re-read everywhere, the same command polled, the same skill loaded and never used, the same job on a
top-tier model. Rank patterns by total `usd`, then by how often they recur. Keep at most `[efficiency] max_tasks`.

## 3. Sample the worst

For the top sessions of each pattern run `factory efficiency show <session>` (calls with targets, sizes, cost,
E/P/S flags). Confirm the cause from the sequence — what came before the repeat, what the big result was, what the
user corrected after. For `base-context` run `skillcheck --strict <repo>` and compare skill sizes with the skills the
finding lists as used. For `user-correction`, the timestamp and category point at the turn; the fix is the rule that
would have prevented it (the learning loop).

## 4. Propose one fix per pattern

| Pattern | Typical fix |
|---|---|
| repeat-read, subagent-reread | pass the facts in the subagent brief; a skill line "read once, keep the range" |
| big-read, oversized-result | a skill/CLAUDE.md line to read ranges or `head`; a script that prints a summary; a quieter test/log flag |
| repeat-call, retry-unchanged | a skill line ("after a failure, change something or stop"); a hook that blocks the identical retry |
| polling | `gh pr checks --watch` / `gh run watch` or one wait script instead of sleep loops; an event wake (engine) |
| cache-rebuild, long-context | compact or hand off earlier; split long sessions by task; avoid model switches mid-session |
| subagent-overspawn | batch small lookups into one agent; do the 2-file read inline |
| base-context | trim or split the CLAUDE.md / skills that load every session (`/factory:skills-review`) |
| duplicate-effort | one task, one worktree: check the task system before starting; the tower's needs-you queue |
| permission-prompts | allowlist the safe read-only commands (`[agent] extra_allowed_tools` or the repo's settings) |
| runtime-* | a prompt cache, a cheaper model tier for the job, a retry policy, dropping the unused job |

State the fix exactly (the line, the hook, the config key), where it goes, the evidence (findings, sessions, $) and
the expected saving.

## 5. File

- A fix in the factory itself (a skill, hook, check or default here): a small PR on `$FACTORY_BRANCH-<slug>`
  through `[land]`, like any change.
- Anything else: a task. First `factory task exists "<title>"`; if it exists, skip it. Otherwise write the body to a
  file and `factory task new --title "<title>" --body-file <file> --priority 2`. When the task system is files in
  the repo, commit them on `$FACTORY_BRANCH-tasks` and land that branch, or they are lost when the run ends.
- Never merge, never push the default branch.

## 6. Report

Write the digest to `$FACTORY_REPORT` (never `git add` it) and print it last: the window and totals, the top
patterns with $ at stake and the fix filed for each (link), corrections worth a rule, and what was left for next run.
Plain words; no colour-only signals.
