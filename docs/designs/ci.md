# CI as a factory capability (F-26)

Decided 2026-11-22 by the principal: the self-hosted CI runner pool, and the choice between CI designs, move out of
the first product (FamilyOffice, where it runs today) into the factory, generic, configured from `.factory.toml`.
The pool manager runs on the engine (Temporal), like the other background work (F-40). A host administrator may
switch the safety guards off in host config: a deliberate, written choice, not a default.

## One config, no new files

| Where | What | Why there |
|-------|------|-----------|
| `.factory.toml` `[ci]` (repo) | default mode, pool label, tiers, gate, hosted labels | what CI is for *this repo*; travels with the code |
| `.factory.toml` `[deploy]` (F-19) | environments, health, rollback, replay check | CD sits beside CI, same file, same vocabulary |
| `engine.toml` `[ci]` (host) | runners, CPU and memory per runner, guards, prod markers | what this *machine* can carry and risk; the host's home for engine settings |
| GitHub repo variables (state, not config) | `FACTORY_CI_MODE` (the live override), `FACTORY_CI_ROUTE`, `FACTORY_CI_HEARTBEAT_AT` | GitHub reads `runs-on` with no runner, so the live decision must sit where GitHub can read it; the file only holds the default |

Rules: a key lives in exactly one of the two files; the repo never states machine sizing or guards, and the host
never states what a repo's tiers are (each is an error in the wrong file, below). No `.env.ci-runner`, no second tier
list: the generated workflow, `factory ci local` and the gate all read `[ci.tiers]`. One parser owns both tables:
`sfactory/ci.py` (stdlib: `Ci`, `Tier`, `Gate`, `HostCi`, validation); `config.py` exposes `cfg.ci`, the engine's
settings call the same `HostCi` loader.

## Engine topology

One engine per host: a Temporal server (Postgres-backed) with its own namespaces, one namespace per project. The
host that runs a pool (Linux, systemd, Docker, no prod: the `no-prod` guard) runs its own engine and the `factory-ci`
worker; a prod host runs its own, for its own work. A product's application Temporal is never shared with the
factory's (engine.md section 1). Each host keeps working when another is down: the pool needs no other machine, and
the route variables let `auto` fall back to hosted when the pool host is gone. The Factory Desk (F-25) lists every
host's engine as a remote (name, address, health) and reads and acts on it through that engine's own `ops` layer; a
host the Desk cannot reach shows as unreachable and nothing else changes.

A CI-only engine needs the Temporal server, its database and one worker. Options: the single `temporal` binary with a
SQLite file (one process, no Docker for the engine itself, though the host has Docker anyway for runners), or the
full Docker Compose project the home machine uses (server, Postgres, UI, namespace setup). Recommended: the same
Compose project with the worker and Desk service left off. One install path to test and upgrade (`factory engine
install` on every host), real history retention, and the UI for a stuck runner. The single binary is the fallback
for a box that cannot run Compose.

## Credentials: a GitHub App, no personal token

GitHub cannot create personal access tokens by API, and the old installer copied an admin's own `gh` login to the
service user (an account-wide credential). Instead the factory uses a **GitHub App**:

- **Registered once** by the owner through the app manifest flow: `factory ci app create` serves a one-shot local
  page that posts the manifest (name, the permissions below, no webhook, not public) to GitHub; GitHub redirects back
  with a code and the command exchanges it (`POST /app-manifests/{code}/conversions`) for the app id and private key.
  The owner approves once in the browser. Nothing else is typed.
- **Installed per repository** by the owner on GitHub (`factory ci app install` opens the page), selecting only the
  repos that use CI. A repo without the installation has no pool.
- **Short-lived tokens, minted on demand.** For each call the factory signs a JWT with the key and asks for an
  installation access token scoped to that one repository and to the permissions the call needs (valid one hour,
  cached in memory only, never written to disk or a job container). The signing uses `openssl dgst -sha256 -sign`
  (standard library has no RSA; `openssl` is on every supported host).
- **Required repository permissions** (checked against GitHub's "Permissions required for GitHub Apps" table):

| Need | Permission | Access |
|------|-----------|--------|
| JIT runner registration, list and delete runners | Administration | write |
| List and re-run workflow runs, list jobs, cancel, dispatch the probe | Actions | write |
| Billing-refusal annotations on a check run | Checks | read |
| `FACTORY_CI_*` repository variables | Variables | write |
| (always granted) | Metadata | read |

  Nothing on Contents, Pull requests or Secrets. Administration is the broad one (it also covers repo settings), so
  the app is private, owned by the principal and installed only on repos they own.
- **The private key lives on the pool host**, in the service user's home, mode 0600, so the pool and the route
  activities work with no Desk and no other machine. Other hosts that only read status (`factory ci status`, `use`)
  mint through their own key copy or ask the pool host; the key never goes to the Desk.
- **The Desk shows per project**: installation present or missing, granted permissions against the required list (a
  missing or extra one named in words), last token mint time and the call that used it, with links to GitHub to
  install, change permissions or revoke. Permissions can only be changed on GitHub; the Desk never changes them.
- `factory ci install` replaces the `gh` login hand-off: it checks the key file and the installation, and fails with
  the one command to run (`factory ci app create|install`) when either is missing.

## The `[ci]` schema

### Repo: `.factory.toml`

```toml
[ci]
mode = "hosted"                # hosted | self-hosted | auto. The default; `factory ci use` overrides it live
pool = "<repo.name>-ci"        # runs-on label of the pool; derived from [repo] name when absent
hosted_light = "ubuntu-slim"   # runs-on of hosted class light and gate jobs
hosted_heavy = "ubuntu-latest" # runs-on of hosted class heavy jobs
workflow = "generated"         # generated | own: "own" = the repo keeps its workflow; render/check skip it
reuse = ""                     # optional command run first (gate class); exit 0 = the tree is already proven, tiers skip

[ci.tiers.lint]                # one table per tier; the key is the name (lowercase letters, digits, dashes)
run = "make lint-py"           # required: a command, or a list of commands run in order, stop at the first failure
class = "light"                # light | heavy | gate. light = 1 core; heavy = multi-core; gate = seconds, small runner
needs = []                     # tier names that must pass first (a DAG; the rest run in parallel)
setup = []                     # "postgres[:16]", "node[:22]", "python[:3.12]" or a shell command (see below)
env = {}                       # extra environment, strings only (a dummy URL, TESTING=true)
workdir = "."                  # repo-relative directory the commands run in
timeout_minutes = 14           # per tier; hosted slim kills a job at 15

[ci.gate]
name = "CI OK"                 # the one check the PR gate (F-02) and branch protection wait for
requires = "all"               # all | [tier names]
```

| Key | Type | Default | Validation (error text starts `<file>: ci.…`) |
|-----|------|---------|-----------------------------------------------|
| `mode` | enum | `hosted` | `ci.mode must be one of hosted, self-hosted, auto` |
| `pool` | label | `<repo>-ci` | `ci.pool: letters, digits, '.', '_' or '-', at most 41 characters, not `self-hosted`/`linux`/`x64`/`arm64`` |
| `hosted_light`, `hosted_heavy` | string | `ubuntu-slim`, `ubuntu-latest` | non-empty |
| `workflow` | enum | `generated` | `ci.workflow must be generated or own` |
| `reuse` | string | `""` | only with `workflow = "generated"` |
| `tiers.<name>` | table | none; at least one tier when `[ci]` is present | name pattern; `ci.tiers.<n>: run is required` |
| `tiers.<n>.class` | enum | `light` | `must be one of light, heavy, gate` |
| `tiers.<n>.needs` | list | `[]` | `ci.tiers.<n>.needs: unknown tier <x>`; `needs cycle: a -> b -> a`; a tier never needs itself |
| `tiers.<n>.setup` | list | `[]` | a class `gate` tier has none: `gate tiers run on the small runner and install nothing` |
| `tiers.<n>.env` | table of strings | `{}` | every value a string; a key matching `*_TOKEN|*_SECRET|*_PASSWORD|*_KEY` is refused: secrets never go in this file |
| `tiers.<n>.workdir` | path | `.` | relative, no `..` |
| `tiers.<n>.timeout_minutes` | int | `14` | 1 to 360 |
| `gate.name` | string | `CI OK` | non-empty; not equal to a tier's job name |
| `gate.requires` | `"all"` or list | `all` | `ci.gate.requires: unknown tier <x>` |
| `guards`, `runners`, `cpus`, `memory`, `gate_runners`, … | | | `ci.<key> belongs to the host's engine.toml, not the repo` |

Caching: `setup` keeps dependency caching where the repo already gets it. On hosted, `python` renders to setup-python
plus setup-uv with the cache keyed on the repo's lock or requirements files (found by glob from the manifests the
upgrade run already detects), `node` to setup-node with the package-manager cache. On the pool, runners are
ephemeral, so the cache is the host's: a read-write uv and npm cache directory mounted into every runner (shared
across pools of that host, content-addressed so repos cannot poison each other's builds beyond their own
resolved hashes), and the image carries the base toolchain. A repo's own composite action stays usable as a plain
`setup` command or in a `workflow = "own"` repo.

Setup steps: only `postgres`, `node`, `python` (optionally `:version`) are named; anything else is a shell command.
A bare version defaults from the repo's own pin (`.python-version`, `.node-version` or `.nvmrc`; none = refused, no
guessing). `sfactory/ci_setup.py` is the one table: per name, the pinned GitHub action (hosted) and the shell form
(pool image and `local`); a test asserts every name has both. The F-01 upgrade run bumps the pins.

### Host: `engine.toml`

```toml
[ci]
runners = 2                    # idle one-job runners kept registered per pool; 0 = pool paused
cpus = "2"                     # per runner (docker --cpus)
memory = "4g"                  # per runner, no swap; \d+[mg]
pids = 4096                    # per runner, at least 256
gate_runners = 1               # small runners for class = "gate" jobs only; 0 = gate jobs share the main runners
gate_cpus = "1"
gate_memory = "512m"
guards = ["no-prod", "egress"] # the default; [] switches both off; a subset keeps the rest
prod_markers = [".env.prod", ".deploy"]  # files whose presence beside a repo clone marks a prod host (guard no-prod)
host = ""                      # runner-name part; default the hostname

[ci.pools."my-repo"]           # optional, per repo name: the same sizing keys, overriding the defaults above
runners = 4
```

Pools to run are not listed: every repo in `engine.toml` `repos` whose `[ci] mode` is `self-hosted` or `auto` gets
one (repo-scoped runners, label `[ci] pool`). Errors: `ci.guards: unknown guard <x> (known: egress, no-prod)`;
`ci.memory: expected a size like 4g or 512m`; `ci.pools.<n>: no repo of that name in repos`; `ci.mode belongs to the
repo's .factory.toml`; a total over the machine (`sum(runners*cpus)` above the core count) is a warning in `status`,
not an error: sizing is the admin's call.

**Releasing it.** engine.toml is machine-written (no comments) and every installed build refuses keys it doesn't
know, so step 0 of the build order ships the `[ci]` reader and its writer round-trip test in an engine build; only
once no older build is installed does the admin add the table by hand.

### Commands

| Command | Reads | Does |
|---------|-------|------|
| `factory ci check [--host]` | repo `[ci]`; `--host` adds `engine.toml` | validates both files (above), then proves the generated workflow files equal `render`'s output; exit 1 with the diff. Runs in the repo's `[verify]` so drift fails CI |
| `factory ci render` | repo `[ci]` | writes `.github/workflows/factory-ci.yml` (router, optional reuse, one job per tier, the gate job named `gate.name`) and `factory-ci-probe.yml`; self-contained (the router is an inline step, nothing from the factory is needed in the repo). A fork's PR never routes to the pool; `workflow = "own"` skips it |
| `factory ci local [--tiers a,b] [--no-docker]` | repo `[ci]` (+ host sizing if present) | the tiers in `needs` order in a throwaway container (working tree streamed in, own Postgres and ports), or directly with `--no-docker`; every tier runs even after one fails, a tier whose `needs` failed is shown `skipped`; summary with words (`ok`, `FAILED`, `skipped`), exit 1 on any failure; labelled a confidence check, not the gate |
| `sudo factory ci install --repo PATH [--user NAME]` | both | host install: preflight, guards, service user, firewall unit, the CI worker unit, app key and installation check (above), image build; then the engine starts the pool. Idempotent, re-run updates |
| `factory ci app create\|install\|status` | GitHub | the app registration, installation and permission check above; `status` prints granted vs required permissions |
| `factory ci status` | both + GitHub + the engine | mode (default and live override), route, heartbeat age, per-pool runners up/busy, queue, peak CPU and memory per runner next to the caps, and a line `guards OFF: egress, no-prod` for every disabled guard; if the engine is unreachable it says so and prints the GitHub facts |
| `factory ci use hosted\|self-hosted\|auto\|default` | GitHub | sets `FACTORY_CI_MODE` (`default` deletes it, so the file's `mode` applies) and re-runs runs stuck on the pool; `factory ci rescue` does only the re-run. Changes nothing in the repo |

## What moves, and what each part becomes

- **Pool manager** (`ci_runner.py serve`, `Pool`, `AutoSwitch`, `Docker`) becomes workflows on the engine, below.
- **Host install** (`install.sh`, firewall, units) becomes `factory ci install`; the firewall and the prod-marker
  check are the two guards named in `guards`. With a guard off the command prints what it no longer enforces.
- **Workflows**: `render` replaces `runner.yml`, `test.yml`'s tier plumbing and `ci-probe.yml`.
- **Runner image**: one per host, built from the union of the served repos' named `setup` steps plus a fixed base
  (git, make, python3, curl, jq, gh, zstd); rebuilt weekly and on a runner release (as today).

### The pool on the engine

A separate task queue `factory-ci` and a second worker process (`python -m engine.ci_worker`, own Worker Versioning
deployment `factory-ci`, system unit under the pool's service user), same Temporal server and namespace. Why not
the main worker: the pool needs `docker` (root-equivalent) and a `gh` login with runner rights; the main worker
runs agent sessions on an admin's account, and a pool fault or restart must not touch team runs. Workflows follow the
engine's house rules (models in `engine.models`, activities by name with `result_type`, bounded retries).

| Workflow | Lifetime | Does |
|----------|----------|------|
| `PoolWorkflow` `ci/pool/<repo>` | about an hour, then continue-as-new (so it never pins an old build) | runs `preflight` (guards), `adopt` (running containers of this pool), keeps `runners` (and `gate_runners`) `RunnerWorkflow` children alive, starting a replacement the moment one ends: no polling; backs off 30 s when a runner died young (<30 s: nothing worked), and starts none while a guard fails |
| `RunnerWorkflow` | one job | `start_runner` (JIT config, `docker run`) then a heartbeating `wait_runner` (docker wait; cancel stops the container) then `finish_runner` (unregister if idle, record peak CPU and memory as the result) |
| `PoolBeat` (Schedule, 60 s) | one activity | if GitHub sees this host's runners online, set `FACTORY_CI_HEARTBEAT_AT`; no online runner, no beat, so `auto` never routes to a dead pool |
| `RouteTick` (Schedule, 60 s) | one activity | the `auto` switch: refused-on-billing runs move CI to the pool and are re-run; stalled pool jobs are re-run hosted |
| `ProbeTick` (Schedule, 1 h) | one activity | dispatch the probe workflow while routed to the pool; a green run moves CI back |
| `ImageTick` (Schedule, 6 h) | one activity | rebuild when the runner release changed or the image is older than 7 days |

A worker crash resumes where it was; containers outlive the worker, and `adopt` counts them instead of starting
duplicates. Status comes from Temporal (workflow queries, run results), not a journal. All routing facts stay in
the repo variables so they work with no engine and no runner.

## Factory Desk panel (F-25)

One CI page per project on the Desk, built on F-25's `ops` layer and action log; there is no second settings writer
and the Desk holds no credential. `ops` gains read functions that return dataclasses (the same ones `factory ci
status` prints) and `use`/`rescue` actions; every action goes through it with who = desk and lands in
`state/actions.jsonl`. It shows, in words and shapes, never colour alone:

- mode (file default and live override) and route, heartbeat age;
- pool runners up and busy per host, queue depth;
- peak CPU and memory per runner next to the caps;
- `guards OFF: egress, no-prod` as its own line whenever any guard is off;
- app status: installation present, granted against required permissions, last token use, links to GitHub;
- actions with confirmation pages like the other Desk actions: `use hosted|self-hosted|auto|default`, `rescue`.

Sizing, guards and tiers are read-only on the page: they are files (`engine.toml`, `.factory.toml`).

## Findings from the first product that shape this

1. **Gate jobs cost a runner cycle.** The 10-second jobs (pick runner, reuse proof, CI OK) each take a full
   ephemeral runner and queue behind 6-minute tiers (a CI OK job waited 282 s). Hence `class = "gate"` with its own
   small runners.
2. **Most jobs use one core.** Sampled: 3 runners at 2 CPUs averaged 96 % of one core and 0.7 GB of 4 GB. Sizing
   is a host setting and measured, so `factory ci status` reports peak use per runner next to the caps.
3. **The first install fought its own guard.** A staging clone beside the pool tripped the prod-marker check.
   The guard names the file and the one command to override it (`guards`), instead of a hard stop only.
4. **Landing must not run under a timeout.** A killed land command can leave a merged PR looking open. Out of
   scope here (F-02) but recorded.
5. **Stopping the old manager kills busy runners** (`tidy` stops every container of the host). The migration waits
   for idle runners instead of stopping the unit blind.

## Open questions, resolved

**Pool for the factory's own CI: hosted only.** The factory repo sets `mode = "hosted"` and never `auto`, so a pool
or engine outage can never block the fix for it. Beyond that: the heartbeat makes every product's `auto` fall back to
hosted when the pool dies, and `factory ci local` is the last resort when neither runs. A product whose CI needs
the engine is covered the same way. Recommended; the principal decides only if factory CI must also run free.

**Several repos on one pool: one pool per repo, no shared group.** Runners register at repository level (JIT), so a
runner can only ever serve its repo, and untrusted PR code from repo A cannot land on a runner repo B's jobs use.
Runner groups need an organization and add a trust boundary the single principal does not have yet. Cost: idle
runners per repo (`runners` is small, `pools.<repo>` overrides it) and one image per host shared across pools. Revisit
when an organization owns the repos.

## Build order

Each task is one PR through the gate; sizes S under a day, M one to two, L up to three.

| # | Task | Size | Tests |
|---|------|------|-------|
| 0 | `sfactory/ci.py` host `HostCi` reader, `engine.toml` `KEYS` + writer round-trip, release an engine build | S | table-driven parse and every error; per-repo override; mode in the host file refused; settings writer keeps `[ci]`; an old build still refuses the key |
| 1 | Repo `[ci]` reader in `ci.py` and `config.py`, `factory config` shows it, `docs/factory-toml.md` section | S | every row of the schema table valid and invalid; DAG cycle; defaults; wrong-file keys; secret-looking env keys |
| 2 | `ci_setup.py` + `factory ci render` / `check` | M | golden workflow per scenario (all/list gate, three classes, hosted labels, own workflow, reuse); render is a fixed point; `check` fails on an edited file; fork guard present; no factory path referenced |
| 3 | `factory ci local` | M | `needs` order and skip; every tier runs after a failure; exit codes; `--no-docker`; fake executor, no Docker in CI |
| 4 | Port the GitHub client and route logic (`pick`, `AutoSwitch`, `rescue`, billing detection, `use`) to stdlib `sfactory/ci_github.py` + `ci_route.py` | M | the reference's route and switch tests moved over unchanged; fake GitHub |
| 5 | Pool activities: docker, JIT, adopt, beat, image, guards, egress probe | M | fake docker and GitHub; guard on/off; adopt counts existing; image rebuild rules |
| 6 | Workflows, schedules, `ci_worker`, Worker Versioning | M | time-skipping: fills to N, replaces on exit, backs off after a young death, no fill with a failing guard, continue-as-new at the hour, cancel stops containers; replay test |
| 7 | `factory ci install`, `status`, `use`, units, firewall script, `--no-guards` text | M | preflight and unit rendering as pure functions; status text with guards off; a real-host smoke documented in the ci-runner skill (not in CI) |
| 8 | `gate_runners` and peak-use report | S | gate jobs routed to the gate label; status shows peak next to caps |
| 9 | `/factory:ci` skill (setup, repair, switch), generalised from FamilyOffice's `ci-runner` skill | S | skillcheck strict |
| 10 | FamilyOffice migration (below), then F-19 reuses `[ci.gate]` | S here, M there | the runbook's checks |

1 to 3 need no host. 4 to 7 are the pool. 8 follows measured use. Steps 0 and 1 come first and unblock `[verify]`.

## FamilyOffice migrates in place

The old pool keeps serving until the new one carries its load; every step is reversible until the last.

1. Engine build with the `[ci]` reader (task 0); add `[ci]` to its `engine.toml`, values copied from `.env.ci-runner`.
2. FamilyOffice PR: `[ci] mode = "auto"`, `pool = "familyoffice-ci"` (the same label), tiers copied from `test.yml`
   and `run_tiers.sh`; `factory ci render` output is added under the new file names but **not yet enabled**
   (`workflow = "own"` until step 4), and `factory ci check` runs in its verify.
3. `sudo factory ci install` beside the old units. One label, two managers: the new runners carry the same pool
   label (`familyoffice-ci`) but a different name prefix and a different container label key, so the old manager
   neither counts nor stops them and the new one does not count the old (adopt matches its own key only). Jobs go to
   whichever runner is idle; capacity is briefly old plus new. Set `FACTORY_CI_MODE` to the current `CI_RUNNER`
   value with `factory ci use`; the new route and heartbeat variables are written by the new pool, the old ones by
   the old, so each generation's workflows read their own. The app (above) is created and installed first; the old
   manager keeps its `gh` login until step 5.
4. Before this step, record one full Tests run before and after in the PR: wall time of the whole run and of each
   tier, on hosted and on the pool, old workflow against generated (`gh run view --json`). **Do not flip
   `workflow = "generated"` if the generated run is slower** than the old one by more than the noise between two old
   runs; fix `setup` (caches, preinstalled postgres on the pool image) until it is not, or keep `own`. FamilyOffice
   uses its own setup actions (`setup-python-deps`: setup-python, uv with a cache keyed on `requirements.txt`;
   `start-postgres`); those are the baseline the generated `python` and `postgres` setup must match.
   The PR flips `workflow = "generated"` and deletes `runner.yml`, the tier plumbing in `test.yml`, `ci-probe.yml`,
   `run_tiers.sh`, and replaces `tests/standalone/test_ci_runner_workflows.py` (it pins today's hand-written router, jobs and probe) with `factory ci check` in the verify: the generated output is the pin, and its golden tests live in the factory. Until this step the test stays as it is and passes, because the old workflows are untouched. The gate name stays `CI OK`: branch protection is untouched. Revert this PR to roll back.
5. When `factory ci status` shows the new pool's runners online and a week of green gates, wait for the old runners
   to be idle, stop and remove the old units (`familyoffice-ci-runner`, `-firewall`; the new firewall unit has
   the same rules under generic names), delete `CI_RUNNER*` variables, `.env.ci-runner` and the old `gh` copy in the service user's home.

## What in `ci_runner.py` is FamilyOffice-specific

| Generic (moves as is, renamed) | FamilyOffice-specific (becomes config or is dropped) |
|--------------------------------|------------------------------------------------------|
| `Mode`, `route`, `pick` logic | `DEFAULT_POOL = "familyoffice-ci"`, `SERVICE`, `NETWORK`, `BRIDGE`, `IMAGE_REPO`, `LABEL_HOST` names: derived from the pool label |
| `GitHub` (runners, JIT config, variables, runs, re-run, cancel), `latest_runner_release` | `ENV_FILE` `.env.ci-runner` and `check_env.parse_env`: replaced by `engine.toml [ci]` |
| `AutoSwitch`, billing detection, `rescue`, heartbeat age | `PROD_COMPOSE` `infra/docker-compose.prod.yml` and `.env.prod` / `.deploy` markers: `prod_markers`, later `[deploy]` |
| `Pool` fill loop, `Docker`, `ensure_image` (weekly, release + sha) | `TESTS_WORKFLOW` and `node_major()` reading `test.yml`: the `setup` pins |
| `egress_check`, `parse_probe`, `prod_guard` shape | `PROBE_WORKFLOW = "ci-probe.yml"`: generated name |
| `cmd_status`, `cmd_use`, `cmd_local` streaming | `TIERS` tuple and `run_tiers.sh` (make, pytest markers, `seed_db.py`, npm, uv from `setup-python-deps`): `[ci.tiers]` |
| hosted labels `ubuntu-slim` / `ubuntu-latest` | postgres-16 and Node baked in the Dockerfile; `ci_reuse.py` (becomes the `reuse` command); `FAMILYOFFICE-CI` firewall chains, `install.sh` user and clone path, the Makefile targets |

Linux with systemd and Docker is the only supported pool host; macOS gets `factory ci local` only.
