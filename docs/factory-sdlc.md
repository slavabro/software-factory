# SDLC reference: blocks, processes, gates

How a project's SDLC is written down (design: [designs/sdlc.md](designs/sdlc.md)). Everything is data and every
layer is configurable; none of it names a project. `factory sdlc check` validates a project against all of it, and
the factory's tests validate the factory's own layers.

| Layer | Where | Referenced as | Changed by |
|-------|-------|---------------|-----------|
| Catalog | `plugins/factory/sdlc/catalog.toml` | — | a PR to the factory |
| Factory blocks | `plugins/factory/sdlc/blocks/<id>.toml` | `factory:<id>` | a PR to the factory |
| Templates | `plugins/factory/sdlc/processes/<id>.json` | `factory sdlc new P --from <id>` | a PR to the factory |
| Project settings | `.factory.toml` `[sdlc]` | — | a PR to the repo |
| Project blocks | `<dir>/blocks/<id>.toml` | `repo:<id>` | a PR to the repo |
| Project processes | `<dir>/processes/<id>.json` | the file name | the Desk's publish (a PR) or `factory sdlc new` |
| Project gates | `<dir>/gates.json` | — | the Desk's publish (a PR) |

`<dir>` is `[sdlc] dir`, `.factory` by default. The engine and CI read these files from the default branch, so a
PR can't loosen the rules that judge it (D-14).

## Commands

```sh
factory sdlc check                       # validate the project's blocks, processes and gates; exit 1 on a problem
factory sdlc blocks [--json]             # every block the project can use, with its status
factory sdlc block REF [--json]          # one block's card: what it does, inputs, outputs, events, needs, where used
factory sdlc show PROCESS [--json]       # a process with each step's settings resolved, and where each came from
factory sdlc templates                   # the factory's template processes
factory sdlc new PROCESS --from TEMPLATE # copy a template into <dir>/processes/ (validated; never overwrites)
```

## `.factory.toml` [sdlc]

```toml
[sdlc]
dir = ".factory"                 # the project's folder: processes/, gates.json, blocks/
skills_dirs = [".claude/skills"] # where a repo agent's `skill` lives (<dir>/<skill>/SKILL.md)
extra_models = []                # model aliases beyond the catalog's
extra_events = []                # event names this repo's sources deliver, beyond the catalog's
```

## The catalog

The vocabularies every other layer uses:

| Key | Holds |
|-----|-------|
| `models` | model tier aliases an agent may name (F-41 maps them to real models) |
| `autonomy` | the autonomy ladder, least to most (`read`, `edit`, `pr`); an agent never merges |
| `asks` | how a human step or gate asks: `always`, `until-trusted`, `when` (with `ask_when`), `never` |
| `executors` | where a process may run: `session`, `engine`, `ci` |
| `human_outcomes` | a human step's exits |
| `input_types` | what a click asks for |
| `trust_after`, `max_retries`, `chain_depth` | defaults and limits |
| `[outputs]` | each output an agent may return, and the least autonomy that may return it |
| `[tools]`, `[resources]`, `[events]` | the tools agents may use, what gates guard, the events the factory delivers |
| `[defaults.<kind>]` | settings a step takes when neither it nor its block sets them |
| `[[default_gates]]` | a project's gates until its `gates.json` says otherwise |
| `[[floors]]` | always-ask rules no project can change (D-11): `id`, `on`, `reason`, `paths` (globs, `{dir}` = the project's folder), `first` |

## Blocks

A block is one kind of thing a step can do. Its file name is its id.

```toml
kind = "agent"                   # agent | check | command | human | event
name = "Upgrade agent"
does = "One paragraph in plain words."
status = "available"             # or "planned", with task = "F-…"
skill = "upgrade"                # agent: the skill it runs
run = ["skillcheck . --strict"]  # check / command: its commands …
run_from = "verify"              # … or the .factory.toml setting that holds them
inputs = ["the repo's manifests"]
outputs = { pr = 3, task = 20 }  # what it may return, and the most per run
emits = ["upgrade.risky_found"]
runs_in = ["engine", "session"]
needs = ["verify"]               # .factory.toml settings that must be set
tools = ["github-read", "github-write"]
touches = "deploy"               # a resource; its steps pass that resource's gates
[defaults]                       # the kind's settings (below)
model = "sonnet"
autonomy = "pr"
```

## Processes

```json
{
  "name": "Upgrade",
  "description": "…",
  "template": "upgrade",
  "runs_in": ["engine", "session"],
  "triggers": [{"schedule": "0 6 * * 1"}, {"event": "pr.merged", "condition": "…"}, {"manual": {"inputs": {"task": "task"}}}],
  "limits": {"concurrent": 1, "budget_minutes": 120},
  "stages": [{"name": "Upgrade", "steps": ["upgrade"]}],
  "steps": [{"id": "upgrade", "type": "agent", "block": "factory:upgrade", "model": "opus"}]
}
```

- **Triggers:** exactly one of `event` (a catalog event, an `[sdlc] extra_events` name, or one a process emits),
  `schedule` (five numeric cron fields, in the engine's time zone) or `manual` (with typed `inputs`).
- **Stages** list every top-level step once, in the order of `steps`.
- **Steps:** each has `id`, `type` (the block's kind, or `parallel`), `block` and, optionally, `condition` (the
  runtime's expression syntax) and `description`. A `parallel` step has two or more `steps`, which run at once.
- **Settings per kind:**

| Kind | Settings |
|------|----------|
| agent | `model`, `autonomy` (at most the project's cap; enough for each output), `budget_minutes`, `instructions`, `tools` (a subset of its block's), `outputs` (a subset of its block's, at most its caps), `fresh_context` |
| check | `blocking`, `on_fail` (`back_to` an earlier step, `retries` up to the catalog's `max_retries`) |
| command | `env` (required when its block touches `deploy`), `on_fail` |
| human | `asks`, `ask_when`, `shows`, `exits` (outcome to a step id, `next` or `end`), `timeout_hours` |
| event | `event` (required) |

A step's value wins over its block's default, which wins over the catalog's `[defaults.<kind>]`. `factory sdlc show`
says which one each value came from.

Processes that start each other through events in a loop are reported as a warning; the engine stops such a chain
at the catalog's `chain_depth`.

## Gates

```json
{
  "autonomy_cap": "pr",
  "gates": [
    {"id": "merge", "name": "Merge", "on": "merge", "requires": ["factory / gate"], "asks": "until-trusted",
     "trust_after": 10, "trusted_since": "2026-12-01"},
    {"id": "production", "name": "Production deploy", "on": "deploy", "env": "production", "asks": "always"}
  ]
}
```

Without the file, the catalog's `default_gates` apply and the cap is the top of the ladder. `trusted_since` belongs
only to a gate that asks `until-trusted` (D-7). A gate can't take a floor's id.

## Who checks what

`factory sdlc check` owns the factory's meaning: blocks, settings, triggers, outputs, autonomy and gates. The
runtime's process validator (`factory_runtime.process`) owns how steps execute: `condition` syntax and
placeholders. The engine runs both before it runs a process.
