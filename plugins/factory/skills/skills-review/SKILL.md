---
name: skills-review
description: Audits every skill, command, subagent and CLAUDE.md file a repo and the user load — runs skillcheck, verifies each claim against the code, scores trigger quality and token cost — then writes a verdict report and applies the approved fixes. Use when asked to review, audit, clean up or optimise skills, or when skillcheck reports drift.
argument-hint: "[repo path] [--apply]"
---

# Skills review

The mechanical half is `skillcheck` (rules: [references/rules.md](references/rules.md)). This skill is the half that
needs judgement: is each instruction still true, does each description trigger at the right time, does each file
earn its tokens. Arguments: `$ARGUMENTS` (default: the current repo).

## 1. Inventory and lint

1. List what loads: `<repo>/.claude/skills/*/SKILL.md`, legacy `<repo>/.claude/commands/*.md`, `.claude/agents/*.md`,
   every `CLAUDE.md`, `~/.claude/skills/*` (follow symlinks), installed plugins (`claude plugin list`). Skip
   vendor-managed skills (`~/.claude/skills/synced/`, Anthropic or Orca stubs) except to note overlaps.
2. Run `skillcheck --strict <repo>` and `skillcheck --scope user ~/.claude/skills`, and `claude plugin validate --strict`
   on each skills dir. Their findings are facts; don't re-derive them.
3. Measure cost per skill: SKILL.md and references in tokens (bytes / 4), who loads it (model-invocable listing, other
   skills that read it, review lenses). Note any workflow that loads many skills into one context.

## 2. Verify against reality (fan out)

For groups of 2–3 skills, launch one `general-purpose` subagent each, in one message. Each brief: the skill paths, the
ground truth (root and directory CLAUDE.md files, docs/architecture, the code), and the report shape below. It must
grep or ls every path, symbol, setting and command a skill names — nothing from memory — and report:

- stale or dead references (skill file:line → what is true now, with the evidence path)
- contradictions with CLAUDE.md or directory rules (both sides)
- copies of facts that have an owner elsewhere, and whether the copy drifted
- generic textbook content with no project-specific value
- trigger fit of the description (too broad fires on unrelated requests; too narrow never fires)
- concrete edits

Check the commands / workflow skills yourself: they are short and their cross-references (who calls whom, which
command reads which file, what each says the next step is) are where pipelines break.

## 3. Report

Write `docs/research/skills-review.md` (or the path the repo uses for research):
- a summary table: skill → verdict (**keep / fix / merge / remove**) → the biggest problem
- a token-cost table
- per skill: findings with file:line evidence, then the proposed edits
- missing skills: procedures repeated across sessions or buried inside another skill
- side findings outside skills

The governing principle to recommend: **a skill points at the owner of a fact (a directory CLAUDE.md, a module, a
config file); it never restates it.** It keeps only judgement — what to check, what to ask.

## 4. Ask, then apply

Ask the user with AskUserQuestion which groups of fixes to apply. Ask separately only about real policy choices the
review surfaced (e.g. attribution rules, whether a workflow should auto-chain). Then apply:
- parallel subagents (forks) on disjoint skill groups; you take the workflow commands and cross-file references
- migrate `.claude/commands/*.md` to `.claude/skills/<name>/SKILL.md` and fix every path that pointed at them
- add each recurring drift pattern to the repo's `.skillcheck.toml` `[[banned]]` list with the true replacement
- finish with `skillcheck --strict` at 0 findings, then land through the repo's PR procedure

User-level skills are edited only when the user says so; propose them otherwise.
