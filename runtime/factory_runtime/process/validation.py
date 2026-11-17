"""Checking a process definition before it runs: `validate_process` where a file loads (`files.load_process` calls
it) and in a product's tests, and the same checks at the start of every run (`ProcessWorkflowBase`), so a definition
that can't run fails its run at once with every problem named, instead of part-way through.

What is checked, each problem naming its step:

- the definition is a mapping with a `steps:` list; each step a mapping with a unique string `id` and a known `type`;
- the keys of each step: every type's `required` keys are there, and (for a type that lists its `optional` keys)
  nothing else but `COMMON_STEP_KEYS`; a retired key (`kwargs`, `signal_name`) says what replaced it;
- `on_error` is `continue`; `condition` is a non-empty expression the safe evaluator accepts;
- each type's own `check` (activity: one argument, known options; workflow: a known parent_close_policy; ...);
- `for_each`: its `step:` is a type that may run inside one, with that type's keys (no `id`, `condition` or
  `on_error` of its own: the `for_each` step's apply);
- placeholders: `{{results.<step>...}}` names an earlier step (in a parallel process, none: its steps run at once,
  so a step can't read a sibling's result), `{{item...}}` appears only inside a `for_each` inner step, and anything
  else in double braces is `{{input....}}`.
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping

from factory_runtime.process.errors import ProcessValidationError
from factory_runtime.process.steps import (
    BUILTIN_STEP_TYPES,
    COMMON_STEP_KEYS,
    INNER_STEP_KEYS,
    ON_ERROR_VALUES,
    RETIRED_KEYS,
    StepType,
)
from factory_runtime.process.templates import PLACEHOLDER, condition_syntax_problem, parse_placeholder

# Workflow types that run a process's steps at once (`run_parallel`): `validate_process` reads the file's
# `interpreter:` against these unless told.
PARALLEL_INTERPRETERS: tuple[str, ...] = ("ParallelProcessWorkflow",)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _key_problems(step: Mapping[str, Any], spec: StepType, common: frozenset[str]) -> list[str]:
    problems = []
    for key in sorted(set(step) & set(RETIRED_KEYS)):
        problems.append(RETIRED_KEYS[key])
    missing = sorted(spec.required - set(step))
    if missing:
        problems.append(f"missing {', '.join(f'`{k}:`' for k in missing)}")
    if spec.optional is not None:
        allowed = common | spec.required | spec.optional
        unknown = sorted(set(step) - allowed - set(RETIRED_KEYS))
        if unknown:
            problems.append(f"unknown keys {unknown} (allowed: {sorted(allowed)})")
    if spec.check is not None and not missing:
        problems.extend(spec.check(dict(step)))
    return problems


def _reference_problems(
    value: Any, own_id: Any, earlier: list[str], all_ids: set[str], *, parallel: bool, in_loop: bool
) -> list[str]:
    problems: list[str] = []
    for text in _strings(value):
        for match in PLACEHOLDER.finditer(text):
            placeholder = match.group(0)
            parsed = parse_placeholder(match.group(1))
            if parsed is None:
                problems.append(f"{placeholder} is not a placeholder: use {{{{results.<step>...}}}}, "
                                "{{input....}} or, inside for_each, {{item...}}")
                continue
            root, path = parsed
            if root == "item" and not in_loop:
                problems.append(f"{placeholder} is used outside a for_each inner step")
            elif root == "results":
                target = path[0]
                if parallel:
                    problems.append(
                        f"{placeholder}: a parallel process runs its steps at once, so a step can't read another "
                        "step's result"
                    )
                elif target == own_id:
                    problems.append(f"{placeholder}: a step can't read its own result")
                elif target not in earlier:
                    where = "runs later" if target in all_ids else "is not a step of this process"
                    problems.append(f"{placeholder}: step '{target}' {where}")
    return problems


def process_problems(
    process_def: Any, step_types: Mapping[str, StepType] = BUILTIN_STEP_TYPES, *, parallel: bool = False
) -> list[str]:
    """Every problem with `process_def` (empty when it can run), each naming its step."""
    if process_def is None:
        return ["Process definition is None"]
    if not isinstance(process_def, Mapping):
        return [f"the process definition is a {type(process_def).__name__}, not a mapping"]
    steps = process_def.get("steps")
    if not isinstance(steps, list):
        return ["`steps:` must be a list of steps"]

    problems: list[str] = []
    all_ids = {s["id"] for s in steps if isinstance(s, Mapping) and isinstance(s.get("id"), str)}
    earlier: list[str] = []
    seen: set[str] = set()
    known = ", ".join(sorted(step_types))

    for position, step in enumerate(steps, start=1):
        if not isinstance(step, Mapping):
            problems.append(f"step {position}: must be a mapping, not {type(step).__name__}")
            continue
        step_id = step.get("id")
        label = f"step '{step_id}'" if isinstance(step_id, str) and step_id else f"step {position}"
        found: list[str] = []

        if not isinstance(step_id, str) or not step_id:
            found.append("needs an `id:` (text)")
        elif step_id in seen:
            found.append("its id is used by an earlier step")
        else:
            seen.add(step_id)

        step_type = step.get("type")
        spec = step_types.get(step_type) if isinstance(step_type, str) else None
        if spec is None:
            found.append(f"unknown type '{step_type}' (known: {known})")
        else:
            found.extend(_key_problems(step, spec, COMMON_STEP_KEYS))

        on_error = step.get("on_error")
        if on_error is not None and on_error not in ON_ERROR_VALUES:
            found.append(f"unknown `on_error: {on_error}` (known: {', '.join(ON_ERROR_VALUES)})")

        if "condition" in step:
            condition = step["condition"]
            if not isinstance(condition, str) or not condition.strip():
                found.append("`condition:` must be a non-empty expression")
            else:
                syntax = condition_syntax_problem(condition)
                if syntax:
                    found.append(syntax)

        inner = step.get("step") if step_type == "for_each" else None
        if step_type == "for_each" and "step" in step:
            found.extend(_inner_problems(inner, step_types))

        refs = {k: v for k, v in step.items() if k not in ("id", "type", "step")}
        found.extend(_reference_problems(refs, step_id, earlier, all_ids, parallel=parallel, in_loop=False))
        if isinstance(inner, Mapping):
            found.extend(_reference_problems(inner, step_id, earlier, all_ids, parallel=parallel, in_loop=True))

        problems.extend(f"{label}: {p}" for p in dict.fromkeys(found))
        if isinstance(step_id, str):
            earlier.append(step_id)
    return problems


def _inner_problems(inner: Any, step_types: Mapping[str, StepType]) -> list[str]:
    if not isinstance(inner, Mapping):
        return ["`step:` must be a step (a mapping with a `type:`)"]
    inner_type = inner.get("type")
    spec = step_types.get(inner_type) if isinstance(inner_type, str) else None
    if spec is None or not spec.for_each:
        can = ", ".join(sorted(name for name, t in step_types.items() if t.for_each))
        return [f"for_each can't run an inner step of type '{inner_type}' (it can run: {can})"]
    own = {"id", "condition", "on_error"} & set(inner)
    found = [f"`{key}:` belongs on the for_each step" for key in sorted(own)]
    found.extend(_key_problems({k: v for k, v in inner.items() if k not in own}, spec, INNER_STEP_KEYS))
    return [f"inner step: {p}" for p in found]


def validate_process(
    process_def: Any,
    step_types: Mapping[str, StepType] = BUILTIN_STEP_TYPES,
    *,
    parallel: bool | None = None,
    source: str = "",
) -> None:
    """Raise ProcessValidationError listing every problem with `process_def` (`source`, e.g. its path, leads the
    message). `parallel`: whether its steps run at once; None reads the definition's `interpreter:` against
    PARALLEL_INTERPRETERS. A product passes its own `step_types` (its classes' map)."""
    if parallel is None:
        parallel = isinstance(process_def, Mapping) and process_def.get("interpreter") in PARALLEL_INTERPRETERS
    problems = process_problems(process_def, step_types, parallel=parallel)
    if problems:
        raise ProcessValidationError(source, problems)
