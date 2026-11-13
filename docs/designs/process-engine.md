# R2 design: the process engine in the shared runtime

Phase 1, slice R2 (F-42; build order in [foundation.md](foundation.md)). FamilyOffice's YAML process engine —
processes written as a list of steps, run by Temporal "interpreter" workflows, plus the schedules that start them —
moves into `runtime/` as `factory_runtime.process`, so every product describes its background processes in YAML
instead of writing a workflow per process. FamilyOffice becomes its first user; its own processes, decisions and
UI vocabulary stay in FamilyOffice.

Source: FamilyOffice `workflows/interpreters/` (≈1,200 lines), `workflows/schedulers/` (≈730),
`common/utilities/yaml_utils.py`, `common/temporal/process_runs.py`, `common/models/schedule_models.py`
(map of 2026-11-13, FamilyOffice main `47494f97`).

## 1. The constraint: running workflows must keep replaying

FamilyOffice's worker has no Worker Versioning, and its processes stay open for a long time: a pause step waits up
to 24 hours, a decision step waits on its child for up to 30 days. Production has 3,610 process runs on record, 8 of
them open (7 Linear, 1 Conditional; Parallel and Hybrid have never run). When the worker restarts on the new code,
every open run replays its history against it. So this slice is a **move, not a rewrite**:

- **Same names:** workflow types `LinearProcessWorkflow`, `ConditionalProcessWorkflow`, `ParallelProcessWorkflow`,
  `HybridProcessWorkflow`; query `get_step_results`; update and signal `resume_step`; the activities and child
  workflows a step schedules keep their names.
- **Same command sequence** for every input: the same activities, timers, children and `workflow.uuid4()` calls, in
  the same order, with the same options. The current quirks stay exactly as they are (listed in section 7) and
  change later, deliberately, under Worker Versioning.
- **Same payload shapes:** inputs, results, query and update answers.

**Proof, before FamilyOffice switches** (reviewed 2026-11-13: Temporal's replayer checks the kind, order and type of
commands and child ids, but not options or payloads, so replay alone is not enough):

1. **Golden runs, compared exactly.** Before any code moves, today's FamilyOffice classes run a set of synthetic
   process files in the time-skipping test server with stub activities and children: every step type and option
   (retry, heartbeat, timeouts, `parent_close_policy`, `execution_timeout`, `wait_for_result`, `workflow_id`,
   `on_error`, pause timeout, resume by update and by signal, `for_each` over activity, workflow and decision,
   decision with and without defaults and `topic_key`), on all four interpreters. Their histories are saved as
   fixtures. The new code must replay them and produce **the same normalized commands**: activity and child types,
   ids, inputs, timeouts, retry and close policies, timers, query and update answers, and the result.
2. **Production replay, relative to today.** All production histories of the four types, exported read-only, are
   replayed against today's FamilyOffice (the baseline: 3,569 of 3,611 pass; the 42 that fail are closed runs from
   before the decision step required `subject`) and against FamilyOffice on the runtime. Every history that passes
   today must pass, and all open runs (8, all passing today) must pass. The production histories hold real data:
   they stay in the session's scratch space, never in a repository or CI.
3. FamilyOffice's whole test suite, and the engine's own tests.

## 2. What moves and what stays

| Moves to `factory_runtime.process` (generic) | Stays in FamilyOffice (its domain) |
|---|---|
| The interpreter base: step execution for `activity`, `workflow`, `pause`/`approval`, `for_each`; templates; safe conditions; retry policy builder; `get_step_results`, `resume_step` | The `decision` step (DecisionWorkflow, origins, topic supersede, outcome vocabulary, defaults activity and settings) — plugged in as a step type (section 3) |
| Run loops: linear, conditional, parallel (and hybrid = conditional, kept for its name) | The four `@workflow.defn` classes, declared in FamilyOffice with its step types (section 3) |
| Loading a process file, choosing its interpreter, discovering process and schedule files (paths and globs are arguments) | `org/<dept>/processes` layout, department fallback, grounding registration, fuzzy name lookup |
| Schedules: `ScheduleDefinition` model, building a Temporal Schedule from a process or a Python workflow, convergent sync with signature diff, retired ids, pause/resume/trigger/list/runs | Its UI models (`ScheduleCreateRequest`, `ScheduleInfo`, …), cadence wording, the run-status vocabulary of its frontend, `rerunnable`, LLM-backed schedules starting paused (a hook, below) |
| On-demand runs: start-or-join a process run, describe how it stands (generic states) | Memo keys and `mode` values its Workflows page and support desk read (passed in by FamilyOffice) |

## 3. Step types are pluggable; workflow classes are the product's

A product adds step types without touching the engine, and without global state (Temporal's sandbox re-imports
modules per run, so a registry filled at host import time would not be there):

```python
# runtime: factory_runtime/process/base.py
class ProcessWorkflowBase:                      # not a @workflow.defn; holds handlers and step execution
    step_types: ClassVar[Mapping[str, type[StepType]]] = BUILTIN_STEP_TYPES
    @workflow.query
    def get_step_results(self) -> dict: ...
    @workflow.update
    async def resume_step(self, step_id: str, data: Any) -> dict: ...
    async def run_linear(self, process_def, input_data, metadata) -> dict: ...   # the run loops
    async def run_conditional(self, ...): ...
    async def run_parallel(self, ...): ...

# product: FamilyOffice workflows/interpreters/__init__.py
STEP_TYPES = {**BUILTIN_STEP_TYPES, "decision": DecisionStep}

@workflow.defn(name="LinearProcessWorkflow")
class LinearProcessWorkflow(ProcessWorkflowBase):
    step_types = STEP_TYPES
    @workflow.run
    async def run(self, process_def: dict, input_data: dict | None = None, metadata: dict | None = None) -> dict:
        return await self.run_linear(process_def, input_data, metadata)
```

- A step type is a plain async function, `async def step(wf, step, ctx) -> Any`, plus a flag whether it may run
  inside `for_each`. `ctx` carries both ids (`base_id`, the YAML id, which pause and results use; `unique_id`, the
  uuid-suffixed id, `{base}_{uuid}[i]` inside `for_each`, which decision ids use), the results so far, input data,
  the current `for_each` item, and the process definition and metadata. Per-run state a step type needs (the
  decision defaults, fetched at most once per run) lives in `wf.step_state`, a dict the base creates per run —
  never on a function, class or module.
- FamilyOffice's `decision_step` is today's decision code moved as is: same activities and options
  (`resolve_process_defaults_activity` with `result_type=dict`, `supersede_topic_decision_activity` with its model
  argument, best effort), same child (`DecisionWorkflow`, `model_dump(mode="json")`, `REQUEST_CANCEL`), same ids.
- `for_each` calls the step function directly per iteration, and only types flagged for it (activity, workflow,
  decision) run there; anything else, `pause`, `approval` and a nested `for_each` included, logs a warning and gives
  None, as today. An inner `on_error` is ignored with a warning; inner results are not stored separately.
- The base keeps today's run-loop structure exactly: `_begin_process` before the None check, one `uuid4` per
  executed step (Conditional only after its condition passed; Parallel inside each gathered coroutine before its
  first await), the same `on_error` handling and exception types (nothing wrapped: an `ActivityError` leaving `run`
  fails the workflow, a wrapped one would fail the task instead), no `failure_exception_types`.
- The built-in map is a `MappingProxyType`, so no one changes it in place; a product's classes get their own map.
  (A registry filled by imports would also work in the sandbox, but it is implicit global state that depends on
  import order.)
- No ready-made `@workflow.defn` classes in the runtime yet: a product that registered them next to its own would
  hit duplicate names. They come when the factory engine needs processes (F-12).
- Handlers (`get_step_results`, `resume_step` update + signal) live on the base: the SDK inherits handlers, while
  `@workflow.run` is declared on each concrete class (verified by a test).

## 4. Configuration is passed in

`ProcessSettings` (frozen dataclass, like `TemporalSettings`): project root, process glob, workflow-schedule glob,
default interpreter, default timezone, cron jitter, default overlap policy, catchup window, runs-list limit. The task
queue comes from `TemporalSettings` (one owner). FamilyOffice builds it from its settings on every call (its tests
patch settings), and the runtime never imports product config. Workflow code never sees it.

What a product passes in, all plain arguments:
- `initially_paused(config, process_def) -> bool` for new schedules (FamilyOffice: LLM-backed while unattended LLM
  use is off).
- `schedule_memo(config, process_def) -> dict`, and the memo and `mode` per on-demand run, so its Workflows page and
  support desk read exactly what they read today (including today's values: absolute `process_path`, `interpreter`,
  `"unknown"`/`"Unknown"` defaults on schedules, None on on-demand runs).
- The schedule model class (FamilyOffice adds `rerunnable` by subclassing `ScheduleDefinition`); discovery applies
  the default time zone at load time.
- The process loader, so a product's tests that patch its own loader keep working.

**Schedules already in Temporal** are compared by a signature of their decoded args, memo, workflow name, task
queue, jitter, time zone, overlap and catchup. A one-time mass update would be harmless (paused state is kept, no
run starts), but a signature that never compares equal would update every schedule on every sync. FamilyOffice
gets a test that builds every discovered schedule with the old and the new code, round-trips it through the data
converter, and asserts equal and stable signatures; everything placed in args or memo is plain JSON.

## 5. Package layout

```
runtime/factory_runtime/process/
├── base.py          ProcessWorkflowBase: handlers, step dispatch, run loops
├── steps.py         StepType + activity, workflow, pause/approval, for_each; retry policy builder
├── templates.py     whole-string {{results|input|item}} resolution, condition substitution, safe_eval_condition
├── files.py         load_process(path, root) (PyYAML, a new runtime dependency), process_interpreter, discovery
├── schedules.py     ScheduleDefinition, build_schedule, ScheduleManager (sync, list, runs, pause, resume, trigger)
├── runs.py          start_process_run, describe_process_run (generic RunState), signal-with-start stays in temporal/
└── settings.py      ProcessSettings
```

Workflow-side modules (`base`, `steps`, `templates`) import only `temporalio` and the standard library, and the
package's `__init__` imports none of the client-side modules (`files`, `schedules`, `runs`), so the sandbox and the
strict import policy see only workflow code.

## 6. FamilyOffice after the switch

- `workflows/interpreters/` keeps its four module files, each with its `@workflow.defn` class (a few lines: the
  worker, the API's startup check and the theses API import them there, the last by typed reference), plus
  `decision_step` and its defaults activity, and its CLAUDE.md pointing at the runtime's docs. The worker keeps
  registering `resolve_process_defaults_activity` and `supersede_topic_decision_activity`.
- `workflows/schedulers/`, `yaml_utils.py`, `process_runs.py` become thin bindings: build `ProcessSettings`, pass the
  hooks, map the runtime's generic states to its own vocabulary, keep grounding and fuzzy lookup.
- Callers keep their imports (the bindings keep every public name), so the API, triggers (children by name, two
  arguments), support desk, chat operations (three arguments) and tests change only where they reach into internals.
  `start_process_run` keeps the third argument exactly `{"department", "process_name"}`.
- `tests/`: engine-level tests move to the runtime (≈150 functions: templates, conditions, executors, for_each,
  policies, schedules, runs); FamilyOffice keeps its process end-to-end tests, YAML contract tests and decision tests.

## 7. Behaviour kept exactly (to be revisited under Worker Versioning, not here)

- Templates resolve only whole strings; conditions substitute each placeholder (string values as repr).
- Linear and Parallel ignore `condition`; Hybrid runs exactly like Conditional; Parallel steps share one results
  dict.
- One `workflow.uuid4()` per executed step (its value unused) and default child ids `{name}_{uuid4}`.
- `signal_name` on pause steps is ignored; `current_step` in the query is always None; skipped steps are absent from
  results; schedules start processes with two arguments, on-demand runs with three.
- An unknown step type logs a warning and stores None; a missing template path gives None.
- YAML `kwargs:` on an activity step are passed as `execute_activity` options, not activity arguments; more than one
  `args:` element fails the workflow task (the SDK takes one `arg`).

Each is listed in the runtime's README as a known quirk, with a test pinning it, so changing one is a visible,
deliberate decision.

## 8. Build order

1. **Golden runs (FamilyOffice, first):** record the synthetic golden histories and normalized commands from today's
   code, as fixtures in FamilyOffice's tests; generic-step goldens also become the runtime's replay fixtures.
2. **Runtime:** `factory_runtime.process` with the moved code and its tests (ported engine tests, the golden
   comparison for built-in steps); release v0.4.
3. **FamilyOffice binding** (section 6), pinned to v0.4: the golden comparison, the schedule signature test, the
   production replay against the baseline, the whole suite; through its gate; deploy; watch the open runs finish.

## 9. Later, not in this slice

- Worker Versioning for FamilyOffice's worker, after which the quirks in section 7 can be fixed as normal changes.
- The factory engine using processes for its own multi-step work (F-12).
- Triggers, decisions, agent jobs: their own slices (sections of foundation.md).
