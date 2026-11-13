# F-25 design: the Factory Desk (management dashboard)

One page on the home machine where the principal sees what the factory is doing for every project and turns things
on and off, without a terminal. Mockup (interactive, sample data marked): https://claude.ai/artifact/DEoZ3SSHqqgNo5y21Ur5Vc

## What it shows and does

| Area | Shows | Actions |
|------|-------|---------|
| Header | engine health in words (running / stopped / degraded) and the current build; link to Temporal's UI | — |
| Spend switch | whether unattended agent runs are on | turn on / off (`engine.toml` `unattended`; read by every run, so it takes effect at once) |
| Needs you | paused teams waiting for their first run, teams paused after three failures (with the note), failed runs, later the decision queue (F-13) | run once now, resume schedule, open the run |
| Projects | each repo in `engine.toml`: factory version it pins, main's CI, teams and their state, open factory PRs | open its teams; add a repo; remove a repo |
| Teams (per project) | each team: what it does, schedule in the engine's time zone, next and last run, failures in a row, active or paused | pause / resume (the Schedule, with a note), run now |
| Recent runs | outcome (done, interrupted, failed, skipped) with a word and a shape, never colour alone; who woke it; the summary; links to history, log and notebook | — |
| Engine | server, database, workers per build (current, draining), time zone, projects | deploy the latest build (`factory engine upgrade`) |

Out of scope at first: editing a team's schedule or prompt (that is the repo's `.factory.toml`, changed by PR), and
anything a product's own dashboards already show.

## How it is built

- **Part of the engine.** `engine/engine/desk/`: a small web app run by its own service next to the worker (launchd /
  systemd), from the same frozen build, so it always matches the engine it controls. Address
  `http://localhost:8245` (a setting), bound to 127.0.0.1 only.
- **One owner per action.** Every button calls the same functions the CLI uses (`engine.cli` commands, `schedules`,
  `wake`, `settings`); the desk adds no second way to pause, run or deploy. Reads come from Temporal (schedules,
  visibility on `FactoryRepo` / `FactoryTeam`, run results), `engine.toml`, the state directory (notebooks, logs) and
  `gh` (PRs, CI), cached for a minute.
- **Stack:** the engine's Python with FastAPI for a JSON API and server-rendered HTML with a few lines of plain JS
  for the switches (no build step, no Node). One stylesheet; status always as word plus shape (red–green safe);
  works at phone width.
- **Safety:** localhost only; every change is a POST with a per-session token; actions that spend money or deploy
  (spend switch on, run now, deploy) ask once to confirm; every action is logged with time and what changed.
- **Several laptops (F-06):** the desk runs where the engine runs; reaching it from another device (e.g. over a
  private network) is a later setting, not a change in the app.

## Build order

1. Read-only views (overview, projects, teams, runs, engine) from the real sources.
2. Actions: spend switch, pause / resume, run now, add / remove repo, deploy, with confirmation and the action log.
3. `factory engine install` and `upgrade` start the desk service with the worker; `factory engine status` prints its
   address.

Tests: API tests against a real local Temporal (as the engine's server tests do), each action's effect checked
through the same reads the CLI uses; an HTML snapshot per view.
