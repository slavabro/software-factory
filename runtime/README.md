# factory-runtime

Building blocks a product installs instead of rebuilding. Design and order: `docs/designs/foundation.md`.

```
factory-runtime @ https://github.com/slavabro/software-factory/archive/<commit>.tar.gz#subdirectory=runtime
```

Pin the same commit as the project's `slavabro/software-factory@<commit>` action, and keep a test that checks the
two are equal (FamilyOffice: `tests/standalone/test_software_factory_pin.py`). The archive URL needs no git and no
login, so it installs in slim Docker images too.

## `factory_runtime.temporal`

| Piece | Use |
|-------|-----|
| `TemporalSettings` | server, namespace, task queue, payload limits; pass your own values or `TemporalSettings.from_env()` |
| `get_client(settings)`, `cached_client`, `set_client` | the process-wide client for those settings, with the shared data converter |
| `data_converter(min_bytes, warn_bytes)` | Pydantic JSON + zlib for large payloads; every client, worker and test env uses it |
| `build_workflow_runner(passthrough=…)` | the sandbox runner; the product names its own pure modules to pass through |
| `activity_idempotency_key()`, `DATA_SHAPE_ERRORS` | inside activities: a key stable across retries; errors a retry can't fix |
| `get_run_failure(client, …)` | one line on why a closed run failed |
| `signal_with_start(client, …)` | start a workflow or join its run, delivering a signal atomically |

`factory_runtime.testing`: `make_worker` (the production runner with the strict import policy), `time_skipping_env`,
`query_eventually`. `factory_runtime.testing.golden`: the process engine's golden runs (below).

The package never reads a product's config: everything comes in as arguments. `TemporalSettings` covers a local
server and Temporal Cloud (`tls`, `api_key`); anything else `Client.connect` takes goes through `get_client`.
`cached_client` / `set_client` read or put the process's client without connecting (health checks, tests).

## Safe deploys: the worker definition, the replay check, the patch report

Design: `docs/designs/safe-deploys.md`. A product keeps one worker on the newest code and checks every open run
before each deploy and rollback.

**One definition** (`factory_runtime.temporal.WorkerDefinition`), exposed once at module level; the worker and the
check both build from it, so they can't disagree about what is registered:

```python
DEFINITION = WorkerDefinition(
    task_queues=(settings.task_queue,),
    workflows=(OrderWorkflow, ...),
    activity_names=activity_names(ACTIVITIES),   # names only: the check never builds activities
    workflow_runner=build_workflow_runner(passthrough=...),
    data_converter=data_converter(settings.compress_min_bytes, settings.warn_bytes),
    interceptors=(...), workflow_failure_exception_types=(...),
    settings=settings,                           # where connect() and the check connect by default
)
client = await DEFINITION.connect()                                    # the definition's data converter
worker = DEFINITION.worker(client, activities=build_activities(...))  # raises if the activities differ
```

`external_activity_names` names activity types the workflows call that other workers serve (not local activities:
those run on this worker and must be in `activity_names`). `replayer()` gives the matching `Replayer`. So the two
can't drift, `worker()` refuses a client whose data converter isn't the definition's, a client carrying worker
plugins or worker interceptors that aren't the first of the definition's `interceptors` (`Worker` would add them),
and the `Worker` options that change how workflow code runs (`REPLAY_OPTIONS`: workflows, runners, interceptors,
plugins, failure types); put those in the definition. A test environment's client carries an interceptor of its own:
workers in tests connect with `definition.connect(env.client.service_client.config.target_host)`.

**`python -m factory_runtime.temporal.replay_check MODULE:ATTR [--server HOST:PORT] [--namespace NS]
[--tls | --no-tls] [--json]`** (ATTR: the definition or a function returning it; flags override
`definition.settings`; an API key comes from the settings or a non-empty `$TEMPORAL_API_KEY` and turns TLS on unless
`--no-tls`). It builds the definition's replayer first, then lists every open run in the namespace with no type
filter (until two listings agree) and, for the definition's queues, fails a run whose type isn't registered, replays
each history through the definition's `Replayer` (nondeterminism reported apart from other replay failures), and
checks what runs call next: each pending activity's type, each local activity the code starts while replaying (one
in flight at the end of the history included), each received signal's and update's handler, and each schedule's
workflow type on those queues. Runs and schedules on other queues are counted for information; one of a type the
definition registers fails. Runs are matched by workflow id and reported under the run id visibility lists (a reset
run's history names the run it was reset from). The report gives counts per type and each failure's kind, workflow
id, type, run id and error message, never payloads. Exit **0** pass, **1** runs would break, or the definition
didn't load or no replayer can be built from it: reject the release, **2** couldn't check (Temporal or visibility
unreachable, or visibility kept changing): retry, **64** bad command line. In code: `await check_open_runs(client,
definition)` returns the `ReplayReport`.

Replay proves only that recorded commands come out the same: not options, payloads, nor code past the end of a
history (golden tests cover those), and not the queries a client sends (they leave no history; pin handler names in
a product test).

**`python -m factory_runtime.temporal.patches MODULE:ATTR [same options]`** (`await patch_report(client,
definition)`): replays open runs, recording each patch call's answer at the workflow instance (so any import style,
a module passed through the sandbox and an unsandboxed runner are all seen), and reports per patch id the runs on its
old side (`patched` or `deprecate_patch` answered False while replaying), the runs carrying a non-deprecated marker,
and the rest, with the step the lifecycle allows: keep `patched(id)` → `deprecate_patch(id)` once no run is on the
old side → remove the call once no run carries a non-deprecated marker. **This report is the gate for each step.**
The replay check also fails a premature step, but only when the two sides issue different commands: a patch that
changes only a value in workflow state replays either way. Exit 1 when runs had to be left out (replay failed, type
unregistered, a run of a registered type on a queue the definition doesn't serve).

## `factory_runtime.process`

Processes written as a list of YAML steps, run by Temporal workflows, and the schedules and on-demand runs that
start them. Design: `docs/designs/process-engine.md`.

| Piece | Use |
|-------|-----|
| `ProcessWorkflowBase` | declare each `@workflow.defn` class on it, with `@workflow.run` calling `run_linear` (`run_conditional` is the same loop under its old name) or `run_parallel`; it brings the query `get_step_results`, the update and signal `resume_step`, and `once(key, fetch)` |
| `BUILTIN_STEP_TYPES`, `StepType`, `StepContext` | step types: `activity`, `workflow`, `pause`/`approval`, `for_each`; add your own as `{**BUILTIN_STEP_TYPES, "x": StepType(fn, for_each=True, required=…, optional=…)}` in the class's `step_types` |
| `validate_process`, `process_problems`, `ProcessValidationError` | check a definition: every problem, each naming its step (below) |
| `resolve_argument`, `evaluate_condition`, `safe_eval_condition` | `{{results.…}}`, `{{input.…}}`, `{{item.…}}` placeholders and `condition:` expressions |
| `files`: `load_process`, `process_interpreter`, `discover_processes`, `discover_schedules` | process files under `ProcessSettings.project_root`, by its globs; `load_process(path, settings)` validates against `settings.step_types` (and `settings.interpreters`) |
| `schedules`: `ScheduleDefinition`, `build_schedule`, `ScheduleManager` | Temporal Schedules from the files: a sync that converges (and deletes `retired_schedules:`), list, runs, pause, resume, trigger |
| `runs`: `start_process_run`, `describe_process_run` | start a process under a workflow id or join its run; how it stands (`RunState`) |
| `settings.ProcessSettings` | root, the product's `step_types` (required: the map its classes run, so a file never loads against the built-ins alone), the `interpreters` a file may name (None: unchecked), the parallel ones, globs, default interpreter and time zone, cron jitter, overlap, catchup window, runs listed |

A step type is `async def step(wf, step, ctx) -> result`: `wf` is the running workflow (`wf.step_state` is a dict
for per-run state; `await wf.once(key, fetch)` runs `fetch()` at most once per run, steps asking at the same time
awaiting the same call), `step` the YAML dict, `ctx` a `StepContext` with `base_id` (the YAML id), `unique_id`
(`{base_id}_{uuid4}`, `[i]` appended inside `for_each`; its uuid4 is drawn the first time a step reads it),
`results`, `input`, `item`, `process_def`, `metadata` and `ctx.resolve(value)`. Keep per-run state in
`wf.step_state`, never on a module, class or function: the sandbox re-imports modules per run. Its `StepType` names
the keys a step of it must have (`required`) and may have (`optional`, besides `id`, `type`, `condition`,
`on_error`, `description`; None leaves its keys unchecked), and an optional `check(step) -> [problems]`. A step that
finds its definition wrong raises `definition_error(message)` (a non-retryable `ProcessDefinitionError`).

The package `factory_runtime.process` imports only workflow-side code; `files`, `schedules`, `runs` and `settings`
are imported by their own module name. A product passes in its own: `ProcessSettings`, `TemporalSettings` (the task
queue), the process loader, the memo its pages read (`schedule_memo`, the memo of an on-demand run), which new
schedules start paused (`initially_paused`), its `ScheduleDefinition` subclass, and the metadata argument of an
on-demand run.

**Process files.** A step is `{id, type, ...}` plus `condition:` (skip the step when false; every interpreter
honours it), `on_error: continue` (a failure stores None and the run goes on) and `description:`. Built-in types:

- `activity`: `activity:` (required), `args:` (its one argument: a one-element list gives its element, a dict or
  other value is passed whole, absent or `[]` passes none; more than one element is an error), `timeout:`
  (start_to_close seconds, default 60),
  `heartbeat_timeout_seconds:`, `retry: {max_attempts, initial_interval_seconds, non_retryable_error_types}`
  (default 3 attempts), `options:` (execute_activity options: `summary`, `activity_id`, `task_queue`,
  `schedule_to_close_timeout` and `schedule_to_start_timeout` in seconds; `kwargs:` is the old name and is rejected);
- `workflow`: `workflow_name:` (required), `args:` (its one argument, by the activity's rule; absent or `[]` passes
  `{}`), `workflow_id:` (placeholders filled in; default `{workflow_name}_{uuid4}`, also when it resolves to
  nothing), `task_queue:` (placeholders filled in; default the parent's), `wait_for_result:` (default true; false
  gives `{"child_workflow_id"}`),
  `parent_close_policy:` (`abandon`, `terminate`, `request_cancel`), `execution_timeout:` (seconds);
- `pause` / `approval`: `timeout:` (seconds; then `{"action": "expired", "timeout_seconds"}`); resumed by
  `resume_step(step_id, data)` under its id (`signal_name:` is gone and rejected);
- `for_each`: `items:` (a list; None gives `[]`, a single value is one item) and `step:` (a type flagged
  `for_each`, with no `id`, `condition` or `on_error` of its own: the `for_each` step's `on_error` applies per item).

Placeholders: `{{results.<step>.<path>}}` (an earlier step), `{{input.<path>}}`, `{{item.<path>}}` (inside a
`for_each` inner step). A string that is exactly one placeholder gives the value (a dict stays a dict); a
placeholder inside text is filled in (`"Approve {{input.label}}"`, a non-string as `str(value)`). A missing path is
None. `\{{` is a literal `{{` (`\{{input.x}}` gives the text `{{input.x}}`; in a double-quoted YAML string write
`\\{{`); other text in braces that doesn't start `results`, `input` or `item` (`{{name}}`) is left as written, and
`description:` is never filled in. A condition compares single values (text, numbers, true/false, None): one that
names a whole dict or list, or that the safe evaluator can't evaluate (bad syntax, an unsupported operator or name),
fails the run; a comparison or arithmetic its values can't support (`{{results.f.n}} > 0` when `f` was skipped,
`'5' > 0`, a division by zero) makes the condition false, logged, so the step is skipped. In a parallel process
(`run_parallel`) a step can't read another step's result.

Durations (`timeout:`, `heartbeat_timeout_seconds:`, `execution_timeout:`, the `options:` timeouts) are seconds
above 0: the SDK reads 0 as "no timeout", never what a file means.

**Validation.** `validate_process(definition, step_types, parallel=None, source="", interpreters=None)` raises
`ProcessValidationError` listing every problem, each naming its step: not a mapping with a `steps:` list, an
`interpreter:` not among `interpreters` (when given), a step without a unique `id`, an unknown type, missing or
unknown keys (on a built-in type, a retired key, `kwargs` or `signal_name`, says what replaced it; a product's own
type may use those names), an unknown `on_error`, an empty or unevaluable `condition`, a type's own `check`, a
`for_each` inner step that can't run there, a duration of 0, and a placeholder that names a later step (or, in a
parallel process, any step) or starts like one but isn't one of the three. A malformed value (a list where a name
belongs, keys of mixed types) is a problem, never a crash. `step_types` is required, as in `ProcessSettings`:
`load_process` runs it with the settings; so does every run before its first step, with its class's `step_types`,
failing the run with a non-retryable `ProcessDefinitionError` (a None definition too, and a check that crashes,
e.g. a product's `check` raising). A product's YAML contract test runs it over every process file.

**Failures.** A definition error fails the run at once whatever `on_error` says. Any other exception a step type
raises that isn't a Temporal failure becomes a non-retryable `ProcessStepError` (so a bug fails the run instead of
retrying its workflow task forever); `on_error: continue` applies to it and to activity and child failures.

**Progress.** `get_step_results` gives `{current_step, completed_steps, results}`: `current_step` is the step running
now (in a parallel process the earliest started of those still running), None when none is; `completed_steps` the
steps finished so far, in the order they finished (a skipped step is absent; after a failure, the steps that
finished before it). A `resume_step` signal for a pause step that hasn't started waiting is kept until it does; one
for a pause step that has finished (resumed or expired) is dropped and logged. The update is validated: only a step
waiting now.

**Since v0.8** (greenfield, docs/designs/safe-deploys.md section 0): the quirks the engine kept so running workflows
would replay are fixed (docs/designs/process-engine.md section 7), so a run started before v0.8 may not replay; run
the replay check in report mode before deploying and restart what it lists. Other notes: schedules start a process
with two arguments, on-demand runs with three.

**Since v0.9** (greenfield too): `ProcessSettings.step_types` is required and `load_process(path, settings)` takes the
settings (`validate_process` / `process_problems` take `step_types` with no default); a condition its values can't
support is false instead of failing the run; a child workflow's one-element `args:` list gives its element (it gave
the list) and `workflow_id:` / `task_queue:` are filled in; durations of 0, two-element child `args:` and literal
text in braces are validated as above. A run whose definition the new validator refuses fails at once; the replay
check lists runs that no longer replay.

**Golden runs** (`factory_runtime.testing.golden`): one harness for the runtime's recordings and a product's, so
they normalize the same way. `GoldenHarness(fixture_dir, interpreters, workflows=…, activities=…, workflow_runner=…,
data_converter=…, stable_task_failure_messages=…, prepare_case=…)` runs a synthetic process file (format: the
module's docstring) in the time-skipping server, normalizes its history to commands, and replays saved histories.
The generic stubs are in `factory_runtime.testing.golden_stubs`; a product adds its interpreters, the stubs of its
own step types, its converter and sandbox runner. A workflow task failure is kept as its exception type, plus the
message only when the product names it stable, so a CPython or temporalio upgrade does not change a golden. A
deliberate change re-records: `GOLDEN_RECORD=1` makes a test call `record_case` before comparing (the runtime's:
`cd runtime && GOLDEN_RECORD=1 python -m unittest tests.test_process_golden`); review the diff of `recorded/`.

**Version:** `factory_runtime.__version__`; a release is the factory tag `v<version>`.

**Tests:** `pip install ./runtime`, then `cd runtime && python -m unittest discover -s tests -t .` (CI runs them
against the installed package, from outside the source tree).
