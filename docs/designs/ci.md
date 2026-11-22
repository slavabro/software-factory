# CI as a factory capability (F-26)

Decided 2026-11-22 by the principal: the self-hosted CI runner pool, and the choice between CI designs, move out of
the first product (FamilyOffice, where it runs today) into the factory, generic, configured from `.factory.toml`.
The pool manager runs on the engine (Temporal), like the other background work (F-40). A host administrator may
switch the safety guards off in host config: a deliberate, written choice, not a default.

## One config, no new files

| Where | What | Why there |
|-------|------|-----------|
| `.factory.toml` `[ci]` (repo) | mode, tiers, gate, pool label | what CI is for *this repo*; travels with the code |
| `.factory.toml` `[deploy]` (F-19) | environments, health, rollback, replay check | CD sits beside CI, same file, same vocabulary |
| `engine.toml` `[ci]` (host) | runner count, CPU and memory per runner, guards | what this *machine* can carry; already the host's home for engine settings |

Rules: a key lives in exactly one of the three; the repo never states machine sizing and the host never states
what a repo's tiers are. No `.env.ci-runner`, no second tier list: the workflow file, the local runner
(`factory ci local`) and the gate all read `[ci.tiers]`.

```toml
[ci]
mode = "auto"                  # hosted | self-hosted | auto (hosted first; the pool when hosted jobs are refused)
pool = "factory-ci"            # runs-on label of the pool

[ci.tiers.lint]                # one table per tier; the key is its name
run = "make lint-py"
class = "light"                # light (1 core) | heavy (multi-core) | gate (seconds; own small runner, below)
needs = []
[ci.tiers.database]
run = "pytest -m database"
setup = ["postgres"]           # named, built-in setup steps (postgres, node, python); anything else is a command
class = "light"

[ci.gate]
name = "CI OK"                 # the one check the PR gate (F-02) and branch protection wait for
requires = "all"               # all | [tier names]
```

```toml
# engine.toml, on the host that runs the pool
[ci]
runners = 4                    # idle one-job runners kept registered
cpus = 2                       # per runner
memory = "4g"                  # per runner, no swap
gate_runners = 1               # small runners (1 core, 512m) only for class = "gate" jobs
guards = ["no-prod", "egress"] # default; [] switches both off. Written into the status output when empty
```

## What moves, and what each part becomes

- **Pool manager** (`ci_runner.py`, ~1,100 lines): a workflow on the engine (`engine/`): keeps `runners` JIT runners
  registered, beats the heartbeat, rebuilds the image, runs the `auto` switch and the hourly hosted probe as a
  Temporal Schedule. A crash resumes instead of restarting; status and history come from Temporal, not a journal.
- **Host install** (`install.sh`, firewall, units): `factory ci install` on the host; the firewall and the
  prod-marker check are the two guards, each named in `guards`. With a guard off, the command prints what it no
  longer enforces, and `factory ci status` shows it while it is off.
- **Workflows**: generated from `[ci]` by `factory ci render` and checked in CI (`render --check`), so the tier
  list, the local runner and the gate cannot drift. A repo with its own workflow keeps it and sets `mode` only.
- **Runner image**: the tools its tiers name (`setup`), not a fixed list. One image per host, rebuilt on a version
  change.

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

## Build order

1. `[ci]` reader in `sfactory/config.py` and `factory ci render|check|local` (tiers, gate): no host needed.
2. Pool workflow on the engine and `factory ci install|status|use`, FamilyOffice as first consumer, migrated in
   place: the running pool keeps serving until the engine's pool is online, then the old units are removed.
3. `class = "gate"` runners and the `guards` switch.
4. F-19 reuses `[ci.gate]` for "deploy only when the gate is green".

## Open questions

- Pool on the engine or beside it: if the engine's own CI needs the pool, a pool outage blocks the fix for the
  pool. Proposed: factory CI stays on hosted runners, a different `[ci]` mode than its products.
- Several repos on one pool: one label per repo, or one shared pool with a runner group per repo. Needs a runner
  group per repo for the threat model (untrusted PR code); decide before step 2.
