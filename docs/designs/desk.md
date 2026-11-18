# F-25 design: the Factory Desk (management dashboard)

One page on the home machine where the principal sees what the factory is doing for every project and turns things
on and off, without a terminal. Mockup (interactive, sample data marked): https://claude.ai/artifact/DEoZ3SSHqqgNo5y21Ur5Vc

Not real time: pages read fresh on load and the principal reloads; nothing polls or streams.

## What it shows and does

| Area | Shows | Actions |
|------|-------|---------|
| Header | engine health in words (running / stopped / degraded) and the current build; link to Temporal's UI | — |
| Spend | the spend switch (`engine.toml` `unattended`) and, once F-33 lands, budgets near or over their limit | turn the switch on / off |
| Needs you | teams paused waiting for their first run; teams paused after failures in a row (with the note); budgets over their limit (F-33); later the decision queue (F-13) | run once now, resume, open the run |
| Projects | each repo in `engine.toml`: the factory commit it pins, main's CI, its teams and their state, open factory PRs | open its teams; add a repo; remove a repo |
| Teams (per project) | each team: what it does, schedule in the engine's time zone, next and last run, failures in a row, active or paused | pause / resume (with a note), run now |
| Recent runs | outcome (done, interrupted, failed, skipped) as a word and a shape, never colour alone; who woke it; the summary; links to history, log and notebook | — |
| Engine | server, workers per build (current, draining), time zone, the running build's commit next to the source checkout's HEAD | deploy the checkout's HEAD |
| Costs | — (F-33 owns it) | link to `factory cost page` |

Semantics the page states where the button is:

- **Spend switch off** stops runs that have not started; a session already running finishes. **Run now** while the
  switch is off asks first, because the run would only stop at once.
- **Needs you** comes from current state (a schedule's pause note, the team's failures-in-a-row counter in
  `state/teams/<repo>/<team>`), not from history, so it empties when things are fixed.
- **Remove a repo** deletes its schedules (reconcile) and lists the runs still open; they finish, nothing new starts.
  **Add a repo** requires a `.factory.toml` on its default branch, then reconciles (new schedules start paused).

Out of scope: editing a team's schedule or prompt (the repo's `.factory.toml`, changed by PR); a cost view of its own
(F-33's page); anything a product's own dashboards show.

## How it is built

### Foundations first (build order step 0)

The review of this design found that the actions have no callable owner yet. They are built first and the CLI moves
onto them, so the desk and the CLI share one owner per action:

- **`engine/engine/ops.py`**: the engine's operations as functions that return dataclasses and print nothing:
  `snapshot() -> EngineSnapshot` (schedules, open and recent team runs with outcomes, builds, settings),
  `pause(ref, note)`, `resume(ref, note)`, `run_now(ref)`, `set_unattended(on)`, `add_repo(path)`,
  `remove_repo(path)`. `engine.cli` keeps only argument parsing and formatting. Team lists come from schedule IDs and
  each repo's already-fetched `origin/<default>` ref; a read never runs `git fetch` (only an action does).
- **One settings writer** in `sfactory/engine.py`. It sits on the stdlib side so install can use it too. It can add or
  remove a repo and set a key, and writes atomically (temp file, then rename). It does not keep comments: the file is
  machine-written, and the reference lives in `engine/engine/settings.py`. `write_settings` goes, and install, the CLI
  and the desk all call this writer. It adds two keys: `desk_port` (default 8245) and `source` (see Deploy below).
- **One action log**: `state/actions.jsonl`, one line per change (time, who: `cli` or `desk`, action, target,
  before → after). Written by `ops`, so the CLI's changes are logged too.
- **One owner for status words and shapes**: the done / interrupted / failed / skipped / paused / over-limit words, their
  shapes and the stylesheet live in one sfactory module, shared with F-33's cost page rather than a second copy.

### The desk itself

- **`engine/engine/desk/`**: a standard-library `ThreadingHTTPServer` that renders HTML on the server, with one
  asyncio loop in a background thread holding the Temporal client (requests use `run_coroutine_threadsafe`). There is
  no JSON API and no JS framework. Changes are HTML forms posted and then redirected (post-redirect-get), and the
  money-spending ones confirm once. No new dependency: the engine's lockfile is part of the Build ID, so a web stack
  would sit in every worker and redeploy them on each bump.
- **Its own service**, `com.software-factory.engine.desk` (launchd / systemd user unit), one label that always runs the
  `current` build. `deploy()` restarts it on the new build right after set-current and before `retire()`, so a build
  is never deleted from under it. `start`, `stop` and `status` include it; the worker loops (`installed_builds`,
  `stop_worker`) skip it. It gets the worker's PATH so `gh` is found.
- **Reads**: Temporal live (cheap and local); `engine.toml`, notebooks and logs from disk; `gh` (CI, PRs) cached per
  repo for 10 minutes. When a `gh` read fails, the page keeps the last value with its age, or shows "CI: unknown"
  plus the reason, and never fails the page.

### Deploy from the desk

`factory engine upgrade` snapshots the checkout it runs from. A frozen build is not a checkout, so the desk can't call
`deploy()` itself. Instead:

- Install and upgrade record **`source`** in `engine.toml`: the checkout they ran from. Every build also records the
  commit it was made from (a `COMMIT` file written by `snapshot()`).
- The Engine panel shows "running build X from commit A; source checkout `~/Dev/software-factory` at B (branch b)".
  When they differ, it offers **Deploy**. The desk never pulls or switches branches in the checkout: that checkout
  belongs to the principal, and the desk deploys exactly what the CLI would.
- Deploy runs `<source>/home/bin/factory engine upgrade` as a detached process (its own session) with its output in
  `state/deploys/<time>.log`, then redirects to a page that shows that log. Because the upgrade restarts the desk
  itself, the upgrade must not be the desk's child. A second deploy while one is running is refused, with a lock file
  shared with the CLI.

### Safety

The desk can spend money and deploy, so:

- It binds to 127.0.0.1 and answers only `Host: localhost:<port>` or `127.0.0.1:<port>`, which defeats DNS
  rebinding.
- **Token.** A random token lives in a 0600 file under `~/.config/software-factory`. `factory engine status` prints
  the desk's URL with the token. The first visit trades it for an HttpOnly, SameSite=Strict cookie and redirects to
  a URL without it. Every request, reads included, needs the cookie, because pages show repo paths, summaries and
  logs.
- **Every POST** must also carry a matching `Origin` and a form token. The desk sends no CORS headers and serves CSP
  `default-src 'self'`.
- **Untrusted text.** Agent-written text (summaries, failure notes, logs) comes from agents that read untrusted PR
  text, so all of it is escaped. Logs and notebooks are served by run or team ID, never by a client-given path.
- **What it can't promise.** Temporal's own frontend (7243) and UI (8243) take connections from any local process
  without auth. The desk does not make the machine safe from other local users, and the doc says so.
- **Other devices (F-06)** need real authentication and TLS. That will get its own design; it is not a setting.

## Build order

0. **Foundations:** `ops` (with the CLI moved onto it), the settings writer, the action log, `COMMIT` per build,
   and the shared status words and stylesheet (taken from F-33's page if #26 has landed, else made here and
   reused by it).
1. **Read-only views:** overview, projects, teams, runs and engine, all from the real sources.
2. **Actions:** the spend switch, pause / resume, run now, add / remove repo and deploy, with confirmation and the
   action log.
3. **Service:** `factory engine install` / `upgrade` / `start` / `stop` manage the desk's service; `status` prints its
   URL.

## Tests

- **The ops layer** runs against the local test server that `test_server.py` already starts. Each action's effect is
  checked through `snapshot()`, the read the CLI uses too.
- **Rendering:** each state shows a word and a shape. A summary containing `<script>` renders escaped. There are no
  whole-page snapshots, which would break on every wording change.
- **Security:** a wrong Host is refused, as are a request without the cookie, a cross-origin POST and a POST without
  the form token.
- **Deploy:** the desk service is restarted onto the new build before `retire()`. A second deploy is refused while
  one runs.
