# Safe deploys for long-running Temporal workflows

Decided 2026-11-17 by the principal: products keep **one worker that always runs the newest code**, guarded by a
**replay check before every deploy**, and change workflow behaviour with **patch markers**. Version pinning (each
version its own worker until its runs finish, as the factory engine does) was weighed and not chosen for products:
more machinery on one machine, a one-way cut-over, and a product's code would first need untangling so most merges
don't count as a new version. It stays possible later. FamilyOffice is the first product.

Why: a workflow re-reads its history whenever a worker picks it up. New code that would have taken different steps
breaks that run. Products have runs that stay open for weeks or over a year (FamilyOffice decisions: 30 days to
decide, up to 365 to execute), so every deploy must keep every open run replaying.

## 1. The replay check (generic, in the runtime)

`factory_runtime.temporal.replay_check` — given a client, the product's workflow classes, its data converter and
sandbox runner:

- lists every **open** run in the namespace, with **no type filter** (`ExecutionStatus = 'Running'`), so a run whose
  type was removed or renamed fails the check instead of failing for ever after the deploy; runs on the worker's
  task queues are checked, open runs on queues the worker doesn't serve are reported (one of a type it registers
  fails);
- streams each history through Temporal's `Replayer`, built from the **same worker definition the product's worker
  uses** (one owner of workflows, runner, converter, interceptors, failure types: the product exposes it once and both
  the worker and the check build from it; the worker refuses a client with another converter, client interceptors or
  plugins the replayer wouldn't have, and worker options that change how workflow code runs);
- checks what open runs will call **next**, which replay can't see: every pending activity's type, every local
  activity the code starts while replaying (recorded by an interceptor on the check's replayer, one in flight at the
  end of the history included; it must be registered on this worker itself, another worker serving the type doesn't
  count), every schedule's workflow type (on another queue: a registered type fails, as a run there would), and the
  handler of every signal and update open runs have received, must exist on the new worker. Queries leave no trace
  in history, so the check can't see which ones clients send: the query handlers of long-lived workflows are pinned
  by the product's own tests (section 4);
- reports runs checked per type and each failure (workflow id, type, run id, the error), nondeterminism apart from
  other failures; no payload data in the report;
- matches each replay to its run by workflow id and reports the run id visibility lists (a reset run's history
  names the run it was reset from); lists the open runs until two listings agree;
- a CLI, `python -m factory_runtime.temporal.replay_check <module:definition>`: exit 0 when everything passes,
  **1** when runs would break or no replayer can be built from the definition (built before connecting): reject the
  release; **2** when it couldn't check (Temporal or visibility unreachable, or visibility kept changing): retry; 64
  on a bad command line. TLS: on with an API key (an empty `$TEMPORAL_API_KEY` is none) unless `--no-tls`.

Replay proves that the commands each open run already recorded still come out the same; it does not check options
or payloads, nor code beyond the end of a history (measured). Those stay covered by golden tests in CI. A change that
alters only a value kept in workflow state still needs a patch marker.

## 2. The check in a product's deploy (FamilyOffice)

FamilyOffice's deployer (`scripts/deployer.py`, run on the prod Mac) today runs `start_prod.sh` (`docker compose
up -d --build`) then the smoke tests, and rolls back on failure. It becomes:

1. validate `.env.prod` (as today), then **build** the release images under a tag of their own
   (`IMAGE_TAG=<sha12>`), never moving `latest`;
2. **stop the worker** (the old code stops advancing runs, so nothing changes between the check and the switch;
   FamilyOffice's runs can wait seconds), wait for visibility to settle;
3. **replay check** in a one-off container from the new image: `compose run --rm --no-deps` (no `init`, so no
   migration runs before the check), named, on the prod network, read only against the live Temporal, with its own
   time budget; a timed-out container is removed;
4. **pass:** retag the checked image as `latest`, `up -d --no-build` (the exact image checked), then the smoke tests,
   as today;
5. **fail (exit 1):** start the old worker again (its container still holds the old image), reject the release, pause
   CD with the report, tell the principal; **couldn't check (exit 2):** start the old worker, retry next tick up to
   the usual attempts. A failed check on a release with a migration never reports "failed after its migration ran":
   nothing ran.

**Rollbacks are checked too.** A restore after failed smoke tests, or `make deploy-rollback`, puts old code back on
runs the new worker may have advanced; the same check runs from the target release's image first. If it fails, the
deployer does not roll back: it pauses and the fix goes forward. A release built before the check existed can't be
checked: the deployer asks the principal.

## 3. Changing workflow behaviour: patch markers

Any change that alters the commands of a workflow with open runs is written with Temporal's patch API:

```python
if workflow.patched("decision-subject-templates"):
    subject = ctx.resolve_inline(step["subject"])   # new behaviour, for runs that reach here after the deploy
else:
    subject = step["subject"]                       # old behaviour, for runs already past this point
```

Lifecycle, one id per change: `patched(id)` while open runs are on its old side → `deprecate_patch(id)` once none
are → the call removed once no open run carries a non-deprecated marker for it. History alone can't tell "passed the
point on old code" from "never reached it", so `factory_runtime.temporal.patches` answers by replay: it replays open
runs recording each patch call's answer at the workflow instance (whatever the import style, sandboxed, passed
through or unsandboxed) and reports per patch id the runs on the old side (`patched` or `deprecate_patch` answered
False while replaying), the runs carrying a non-deprecated marker (read from history), and the rest. **The patch
report is the gate for each step.** The replay check fails a premature step only when the two sides issue different
commands; a patch that changes only a value kept in workflow state (measured: deprecating it with a run on the old
side replays) passes replay either way, and only the report catches it. Golden tests record both sides: the old
recordings keep proving the old branch, a new recording proves the new.

## 4. CI side

- Golden runs (synthetic, in each repo) prove options and payloads per step type, both sides of every live patch.
- The worker and the check build from one definition (section 1), so they can't disagree about registered classes.
- Handler names and signatures of long-lived workflows pinned by a test.

## 5. Build order

1. Runtime: `replay_check` and `patches` with tests against a real local server (a run that replays, one made
   nondeterministic by a code change, one that needs a patch, a patch whose old side is still open); release.
2. FamilyOffice: one worker definition used by the worker and the check; the deployer as in section 2 (build under
   its tag → stop worker → check → retag and up, or restart the old worker), the check in front of every rollback,
   with tests of each path; first run against production as a dry run (report only), then on for real.
3. Then the engine quirks pinned in R2 (process-engine.md §7) are fixed one by one behind patch markers, each its own
   small change with golden recordings of both sides.
