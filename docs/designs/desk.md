# F-25 design: the Factory Desk (management dashboard)

One page on the home machine: the factory's admin panel. The principal sees the factory's health and what it can do,
registers projects, designs each project's SDLC ([sdlc.md](sdlc.md)), and turns things on and off without a
terminal. The day-to-day work (tasks in flight, PRs waiting, agents' questions) is not here: each developer handles
it in their own tool (D-13). Mockup (interactive, sample data marked): https://claude.ai/artifact/DEoZ3SSHqqgNo5y21Ur5Vc

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

Out of scope: a cost view of its own (F-33's page); anything a product's own dashboards show; the day-to-day work
(D-13: F-26 removes the open factory PRs from Projects, and Needs you keeps only admin items). Designing each
project's processes from building blocks, their triggers and events, and its gates (team schedules included, which
leave `.factory.toml`) is [sdlc.md](sdlc.md)'s (D-6, D-10).

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
  and the desk all call this writer. It adds one key, `desk_port` (default 8245), which only a person sets: every
  build refuses keys it doesn't know, so nothing the factory writes by itself adds a key while older builds still run.
  Machine state that isn't a setting (`current`, `source`) lives in the share dir, next to the builds.
- **One action log**: `state/actions.jsonl`, one line per change (time, who: `cli` or `desk`, action, target,
  before → after). Written by `ops`, so the CLI's changes are logged too.
- **One owner for status words and shapes**: the done / interrupted / failed / skipped / paused / over-limit words, their
  shapes and the stylesheet live in one sfactory module, shared with F-33's cost page rather than a second copy.

### The desk itself

- **`engine/engine/desk/`**: a standard-library `ThreadingHTTPServer` that renders HTML on the server, with one
  asyncio loop in a background thread holding the Temporal client (requests use `run_coroutine_threadsafe`). There is
  no JSON API and no JS framework. Changes are HTML forms posted to `/act/<action>` and then redirected
  (post-redirect-get) to a page that reads the result back from the engine. Spend on, run now, deploy and remove a
  repo confirm once, on a server-rendered page (`/confirm`, a GET that changes nothing), so CSP needs no script. No new dependency: the engine's lockfile is part of the Build ID, so a web stack
  would sit in every worker and redeploy them on each bump.
- **Its own service**, `com.software-factory.engine.desk` (launchd / systemd user unit), one label that always runs the
  `current` build. `deploy()` restarts it on the new build right after set-current and before `retire()`, so a build
  is never deleted from under it. `start`, `stop` and `status` include it; the worker loops (`installed_builds`,
  `stop_worker`) skip it. It gets the worker's PATH so `gh` is found, restarts when it crashes, and logs to
  `desk.log` in the state dir. It is restarted only when its service file would change (a new build, a new PATH).
  Its systemd unit uses `KillMode=mixed`, as the worker's. The upgrade the desk starts must outlive the desk's
  restart, so it is never in the desk's reach: on systemd it runs as a transient unit of its own (`systemd-run
  --user --unit factory-deploy-<id> --collect`, outside the desk's control group); under launchd, in a session of
  its own, outside the desk's process group, which is what launchd stops. launchd's `bootout` returns before the
  job is gone, so a restart waits for the label to go (bounded) and retries `bootstrap`; a desk that still does not
  start is said in words and the deploy goes on to retire and reconcile.
  Under systemd without a desktop session, `gh`'s keyring may be locked: the desk then shows CI as unknown with the
  reason.
- **Reads**: Temporal live (cheap and local); `engine.toml`, notebooks and logs from disk; `gh` (CI, PRs) cached per
  repo for 10 minutes. When a `gh` read fails, the page keeps the last value with its age, or shows "CI: unknown"
  plus the reason, and never fails the page.

### Deploy from the desk

`factory engine upgrade` snapshots the checkout it runs from. A frozen build is not a checkout, so the desk can't call
`deploy()` itself. Instead:

- `deploy()` records **`source`** next to `current` in the share dir (`~/.local/share/software-factory/engine/source`)
  once the new build is current: the main worktree of the checkout it ran from (never a linked worktree such as an
  agent's). Every build also records the commit it was made from (a `COMMIT` file written by `snapshot()`).
- The Engine panel shows "running build X from commit A; source checkout `~/Dev/software-factory` at B (branch b)".
  When they differ, it offers **Deploy**. The desk never pulls or switches branches in the checkout: that checkout
  belongs to the principal, and the desk deploys exactly what the CLI would.
- Deploy runs `<source>/home/bin/factory engine upgrade` as a detached process (its own session, stdin closed) with
  its output in `state/deploys/<time>.log`, then redirects to a page that shows that log by its ID (the UTC start
  time), never a path. Because the upgrade restarts the desk itself, the upgrade must not be the desk's child. It
  runs under `sh`, which appends the upgrade's exit status to the log, so a desk started after the restart still
  says whether it worked; `<time>.json` keeps what it was started with and when. Whether it still runs is decided
  by the deploy lock, never by a stored pid (a reboot can hand it to another process): the newest deploy without
  an exit status runs while the lock is held, or in its first minute, before the upgrade has taken the lock.
  Whether the lock is held is asked with a non-blocking shared `flock` that is let go at once (no `flock(1)`,
  which macOS lacks); the deploy lock waits two seconds for such a probe before it refuses. A second deploy while one is running is refused, with a lock file
  shared with the CLI (`install`, `upgrade`, `start` and `stop` all take it). The desk reads who holds it without
  taking it, so asking never fails a real deploy.

### Safety

The desk can spend money and deploy, so:

- It binds to 127.0.0.1 and answers only `Host: localhost:<port>` or `127.0.0.1:<port>`, which defeats DNS
  rebinding.
- **Token.** A random token lives in a 0600 file under `~/.config/software-factory` (a looser mode is set back to
  0600). Only the desk's startup and `factory engine desk` make it; a request never does. `factory engine desk`
  prints the sign-in link with the token (`--open` opens it); `factory engine status` prints only the desk's address
  and whether the desk answers there (it recognises the desk by its `X-Factory-Desk` header, so another program on
  the port is reported as such). The first visit trades the token for an HttpOnly, SameSite=Strict cookie and
  redirects to a URL without it. The cookie holds HMAC(token, "desk-cookie"), never the token. Every request, reads
  included, needs the cookie, because pages show repo paths, summaries and logs. The Cookie header is parsed by
  hand, so another localhost app's malformed cookie can't hide the desk's.
- **Every POST** must also carry a form token and an `Origin` that equals the desk's own exactly: scheme, host and
  port (`http://localhost:<port>` or `http://127.0.0.1:<port>`), because SameSite treats every port on localhost as
  the same site. The desk sends no CORS headers and serves CSP `default-src 'self'`. Its `Referrer-Policy` is
  `same-origin`, not `no-referrer`: under `no-referrer` browsers send `Origin: null` even on a same-origin form
  POST, so every change would be refused; `same-origin` still keeps the Referer (and a `?token=`) from other sites.
- **Untrusted text.** Agent-written text (summaries, failure notes, logs) comes from agents that read untrusted PR
  text, so all of it is escaped. Logs and notebooks are served by run or team ID, never by a client-given path.
- **What it can't promise.** Temporal's own frontend (7243) and UI (8243) take connections from any local process
  without auth. The desk does not make the machine safe from other local users, and the doc says so. Cookies are
  not scoped by port, so every other app served on localhost receives the desk's cookie (the derived value, not
  the token), and SameSite=Strict does not separate one localhost port from another. The sign-in link with
  `?token=` stays in the browser's history.
- **Other devices (F-06)** need real authentication and TLS. That will get its own design; it is not a setting.

## Build order

0. **Foundations:** `ops` (with the CLI moved onto it), the settings writer, the action log, `COMMIT` per build,
   and the shared status words and stylesheet (taken from F-33's page if #26 has landed, else made here and
   reused by it).
1. **Read-only views:** overview, projects, teams, runs and engine, all from the real sources.
2. **Actions:** the spend switch, pause / resume, run now, add / remove repo and deploy, with confirmation and the
   action log.
3. **Service:** `factory engine install` / `upgrade` / `start` / `stop` manage the desk's service; `status` says whether
   it is up.

## Tests

- **The ops layer** runs against the local test server that `test_server.py` already starts. Each action's effect is
  checked through `snapshot()`, the read the CLI uses too.
- **Rendering:** each state shows a word and a shape. A summary containing `<script>` renders escaped. There are no
  whole-page snapshots, which would break on every wording change.
- **Security:** a wrong Host is refused, as are a request without the cookie, a cross-origin POST and a POST without
  the form token.
- **Deploy:** the desk service is restarted onto the new build before `retire()`. A second deploy is refused while
  one runs.
