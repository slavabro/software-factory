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
`query_eventually`.

The package never reads a product's config: everything comes in as arguments. `TemporalSettings` covers a local
server and Temporal Cloud (`tls`, `api_key`); anything else `Client.connect` takes goes through `get_client`.
`cached_client` / `set_client` read or put the process's client without connecting (health checks, tests).

## `factory_runtime.process`

Processes written as a list of YAML steps, run by Temporal workflows, and the schedules and on-demand runs that
start them. Design: `docs/designs/process-engine.md`.

| Piece | Use |
|-------|-----|
| `ProcessWorkflowBase` | declare each `@workflow.defn` class on it, with `@workflow.run` calling `run_linear`, `run_conditional` or `run_parallel`; it brings the query `get_step_results` and the update and signal `resume_step` |
| `BUILTIN_STEP_TYPES`, `StepType`, `StepContext` | step types: `activity`, `workflow`, `pause`/`approval`, `for_each`; add your own as `{**BUILTIN_STEP_TYPES, "x": StepType(fn, for_each=True)}` in the class's `step_types` |
| `resolve_argument`, `evaluate_condition`, `safe_eval_condition` | `{{results.…}}`, `{{input.…}}`, `{{item.…}}` placeholders and `condition:` expressions |
| `files`: `load_process`, `process_interpreter`, `discover_processes`, `discover_schedules` | process files under `ProcessSettings.project_root`, by its globs |
| `schedules`: `ScheduleDefinition`, `build_schedule`, `ScheduleManager` | Temporal Schedules from the files: a sync that converges (and deletes `retired_schedules:`), list, runs, pause, resume, trigger |
| `runs`: `start_process_run`, `describe_process_run` | start a process under a workflow id or join its run; how it stands (`RunState`) |
| `settings.ProcessSettings` | root, globs, default interpreter and time zone, cron jitter, overlap, catchup window, runs listed |

A step type is `async def step(wf, step, ctx) -> result`: `wf` is the running workflow (`wf.step_state` is a dict
for per-run state, e.g. defaults fetched once), `step` the YAML dict, `ctx` a `StepContext` with `base_id` (the YAML
id), `unique_id` (`{base_id}_{uuid4}`, `[i]` appended inside `for_each`), `results`, `input`, `item`, `process_def`,
`metadata` and `ctx.resolve(value)`. Keep per-run state in `wf.step_state`, never on a module, class or function: the
sandbox re-imports modules per run.

A step type moved from a product's own engine must keep its command sequence, so read anything derived from
`ctx.process_def` exactly the way the source did: the definition is whatever the file held, so `metadata` may be
absent, None or not a dict, and `(process_def.get("metadata") or {}).get(...)` and `process_def.get("metadata",
{}).get(...)` fail on different inputs. A "tidier" read changes which runs fail and what they send.

The package `factory_runtime.process` imports only workflow-side code; `files`, `schedules`, `runs` and `settings`
are imported by their own module name. A product passes in its own: `ProcessSettings`, `TemporalSettings` (the task
queue), the process loader, the memo its pages read (`schedule_memo`, the memo of an on-demand run), which new
schedules start paused (`initially_paused`), its `ScheduleDefinition` subclass, and the metadata argument of an
on-demand run.

**Known quirks**, kept because running workflows replay against this code; change one only under Worker Versioning
(`tests/test_process_quirks.py` pins most by name; the golden runs in `tests/test_process_golden.py` pin every
step-level one command by command):

- Templates resolve only whole strings (`"x {{input.a}}"` stays as written); conditions substitute each placeholder
  in place, a string as its repr; a missing path gives None.
- Linear and Parallel ignore `condition:`; Hybrid is Conditional under another name; Parallel steps share one
  results dict, so a step cannot read a sibling's result.
- Each executed step draws one `workflow.uuid4()` (its value unused except inside `for_each` ids); a child with no
  `workflow_id` is `{workflow_name}_{uuid4}`.
- `signal_name:` on a pause step is ignored; `current_step` in `get_step_results` is always None; a skipped step is
  absent from the results; schedules start a process with two arguments, on-demand runs with three.
- An unknown step type logs a warning and stores None; a None process definition fails the workflow task (retried
  until terminated), not the run.
- An activity step's `kwargs:` are `execute_activity` options, not activity arguments; more than one `args:` element
  fails the workflow task (the SDK takes one argument); a dict `args:` passes its keys.
- An unknown `parent_close_policy:` on a workflow step logs a warning and keeps the server default.
- `for_each`: an inner step's own `on_error:` is ignored (only the `for_each` step's counts); a scalar `items:` is one
  item, None gives `[]`; an inner type not flagged `for_each` gives None per item.

**Version:** `factory_runtime.__version__`; a release is the factory tag `v<version>`.

**Tests:** `pip install ./runtime`, then `cd runtime && python -m unittest discover -s tests -t .` (CI runs them
against the installed package, from outside the source tree).
