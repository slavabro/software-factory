# skillcheck rules

`skillcheck --rules` prints this list from the code; this page adds the source for each. Sources were checked
2026-10-31: Claude Code skills (https://code.claude.com/docs/en/skills), sub-agents
(https://code.claude.com/docs/en/sub-agents), memory (https://code.claude.com/docs/en/memory), Agent Skills spec
(https://agentskills.io/specification), skill authoring best practices
(https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices).

## Contents

- [Frontmatter and description](#frontmatter-and-description)
- [Size and progressive disclosure](#size-and-progressive-disclosure)
- [Truth and drift](#truth-and-drift)
- [Not covered here](#not-covered-here)

## Frontmatter and description

| ID | Default | Rule | Source |
|----|---------|------|--------|
| SC001 | error | A `---` frontmatter block that parses. Malformed YAML loads the body with empty metadata, so the description never matches | skills |
| SC002 | error | `description` present: what it does and when to use it | spec |
| SC003 | error | `description` ≤ 1024 chars (the listing truncates description + `when_to_use` at 1,536) | spec, skills |
| SC004 | error | `name`: lowercase `a-z0-9`, single hyphens, ≤ 64 | spec |
| SC005 | warning | `name` matches the directory (the spec requires it; Claude Code defaults to the directory name) | spec |
| SC006 | warning | Only known keys — Claude Code ignores unknown ones silently, so a typo (`allowed_tools`) is a dead setting | skills, sub-agents |
| SC007 | warning | No `<` `>` in a description — they render as `&lt;` `&gt;`; use `[x]` or `argument-hint` | skills |
| SC008 | warning | Third person; the description is injected into the system prompt | best practices |
| SC009 | warning | A model-invocable description says when to use it ("Use when …"); otherwise set `disable-model-invocation: true`, which also drops it from the listing | best practices, skills |
| SC019 | warning | All model-invocable descriptions fit the listing budget (1% of context; least-used drop first) | skills |
| SC022 | error | A subagent has `name` and `description` | sub-agents |

## Size and progressive disclosure

| ID | Default | Rule | Source |
|----|---------|------|--------|
| SC010 | warning | SKILL.md body ≤ 500 lines | skills, best practices |
| SC011 | warning | SKILL.md body ≤ ~5,000 tokens; the whole body stays in context after invoke | spec |
| SC012 | warning | A reference over 100 lines starts with a table of contents | best practices |
| SC013 | warning | References are one level deep: SKILL.md links them; they don't link each other | best practices |
| SC017 | warning | `.claude/commands/*.md` is legacy; use `.claude/skills/<name>/SKILL.md` | skills, plugins |
| SC018 | warning | Each CLAUDE.md ≤ 200 lines | memory |
| SC023 | warning | Every reference file is mentioned by its SKILL.md (else it is never loaded) | — |

## Truth and drift

| ID | Default | Rule |
|----|---------|------|
| SC014 | error | Relative markdown links resolve |
| SC015 | error | A path in backticks (with an extension or a trailing `/`) exists in the repo — the check that catches a skill stranded by a code rename. Placeholders (`my_`, `path/to`, `{x}`, `<x>`, `[x]`, `*`) are skipped; `[paths] allow` globs too |
| SC016 | error | No `/Users/<name>/` or `/home/<name>/` in shared (project) files |
| SC020 | warning | A long line (≥ 140 chars) appearing in two files is a copy that will drift — keep one owner |
| SC021 | per pattern | Repo-specific `[[banned]]` patterns in `.skillcheck.toml`, each with the true replacement |

Suppress one line with `<!-- skillcheck: ignore SC021 -->` on it or the line above.

## Not covered here

- Schema of plugin manifests and marketplace files: `claude plugin validate --strict`.
- Whether a skill triggers and works: `claude plugin eval` (cases under `evals/`, a `tool_used: Skill` grader).
- Whether an instruction is still *true* beyond paths and banned patterns: the skills-review workflow.
