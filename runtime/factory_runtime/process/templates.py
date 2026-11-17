"""Placeholders and conditions in a process file, resolved inside the workflow.

- `{{results.<step>.<path>}}`: an earlier step's result; `{{input.<path>}}`: the run's input data;
  `{{item}}` / `{{item.<path>}}`: the current `for_each` item. Spaces inside the braces are allowed.
- A string that is exactly one placeholder gives the value itself (a dict stays a dict). A placeholder inside other
  text is filled in: `"Approve {{input.label}}"` gives `"Approve alpha"`, a non-string value as `str(value)`.
  Dicts and lists are resolved element by element. A missing path gives None (logged unless `log_missing=False`;
  `strict=True` raises instead), so inside text it reads `None`. Anything else in braces stays as written (the
  validator rejects it when the file loads).
- A condition fills in each placeholder (a string as its repr, None as `None`) and evaluates the result with
  `safe_eval_condition`: comparisons, `and`/`or`/`not`, arithmetic and literals, nothing else. A placeholder whose
  value is not a single value (a dict, a list) or an expression that can't be evaluated raises `ConditionError`.
"""

from __future__ import annotations

import ast
import operator
import re
from typing import Any, Callable

from temporalio import workflow

# One placeholder: {{ root.path }} (no braces inside)
PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
PLACEHOLDER_ROOTS = ("results", "input", "item")

# Safe operators for condition evaluation (no code execution)
_SAFE_OPERATORS: dict[type[ast.AST], Callable[..., Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.And: lambda a, b: a and b,
    ast.Or: lambda a, b: a or b,
    ast.Not: operator.not_,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

# Values a condition can compare
_CONDITION_SCALARS = (str, int, float, bool, type(None))


class ConditionError(ValueError):
    """A condition that can't be evaluated: a bad expression, or a placeholder that isn't a single value."""


def parse_placeholder(expression: str) -> tuple[str, list[str]] | None:
    """`results.a.b` -> ("results", ["a", "b"]); None for anything that isn't a placeholder the engine fills
    (an unknown root, `{{results}}` or `{{input}}` alone, an empty path part)."""
    parts = expression.split(".")
    root, path = parts[0], parts[1:]
    if root not in PLACEHOLDER_ROOTS or any(not p for p in path):
        return None
    if root in ("results", "input") and not path:
        return None
    return root, path


def _safe_eval(node: ast.AST) -> Any:
    """Evaluate an AST node without executing arbitrary code: comparisons, boolean logic, arithmetic, literals."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    elif isinstance(node, ast.Constant):
        return node.value
    elif isinstance(node, ast.BoolOp):
        op = _SAFE_OPERATORS.get(type(node.op))
        if not op:
            raise ValueError(f"Unsupported boolean operator: {type(node.op).__name__}")
        result = _safe_eval(node.values[0])
        for value in node.values[1:]:
            result = op(result, _safe_eval(value))
        return result
    elif isinstance(node, ast.Compare):
        left = _safe_eval(node.left)
        for op_node, comparator in zip(node.ops, node.comparators):
            op = _SAFE_OPERATORS.get(type(op_node))
            if not op:
                raise ValueError(f"Unsupported comparison operator: {type(op_node).__name__}")
            right = _safe_eval(comparator)
            if not op(left, right):
                return False
            left = right
        return True
    elif isinstance(node, ast.BinOp):
        op = _SAFE_OPERATORS.get(type(node.op))
        if not op:
            raise ValueError(f"Unsupported binary operator: {type(node.op).__name__}")
        return op(_safe_eval(node.left), _safe_eval(node.right))
    elif isinstance(node, ast.UnaryOp):
        op = _SAFE_OPERATORS.get(type(node.op))
        if not op:
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
        return op(_safe_eval(node.operand))
    elif isinstance(node, ast.Name):
        if node.id == "True":
            return True
        elif node.id == "False":
            return False
        elif node.id == "None":
            return None
        raise ValueError(f"Unsupported name reference: {node.id}")
    else:
        raise ValueError(f"Unsupported AST node type: {type(node).__name__}")


def _check_nodes(node: ast.AST) -> None:
    """Raise ValueError for any node `_safe_eval` would refuse, without evaluating (the validator's check)."""
    if isinstance(node, ast.Expression):
        _check_nodes(node.body)
    elif isinstance(node, ast.Constant):
        return
    elif isinstance(node, ast.BoolOp):
        if type(node.op) not in _SAFE_OPERATORS:
            raise ValueError(f"Unsupported boolean operator: {type(node.op).__name__}")
        for value in node.values:
            _check_nodes(value)
    elif isinstance(node, ast.Compare):
        for op_node in node.ops:
            if type(op_node) not in _SAFE_OPERATORS:
                raise ValueError(f"Unsupported comparison operator: {type(op_node).__name__}")
        for child in (node.left, *node.comparators):
            _check_nodes(child)
    elif isinstance(node, ast.BinOp):
        if type(node.op) not in _SAFE_OPERATORS:
            raise ValueError(f"Unsupported binary operator: {type(node.op).__name__}")
        _check_nodes(node.left)
        _check_nodes(node.right)
    elif isinstance(node, ast.UnaryOp):
        if type(node.op) not in _SAFE_OPERATORS:
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
        _check_nodes(node.operand)
    elif isinstance(node, ast.Name):
        if node.id not in ("True", "False", "None"):
            raise ValueError(f"Unsupported name reference: {node.id}")
    else:
        raise ValueError(f"Unsupported AST node type: {type(node).__name__}")


def safe_eval_condition(expression: str) -> bool:
    """Evaluate a condition expression without arbitrary code execution: comparisons (==, !=, <, >, <=, >=),
    boolean logic (and, or, not), arithmetic (+, -, *, /, %) and literals. Raises ValueError for anything else."""
    try:
        tree = ast.parse(expression, mode="eval")
        return bool(_safe_eval(tree))
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError) as e:
        raise ValueError(f"Invalid condition expression '{expression}': {e}")


def condition_syntax_problem(condition: str) -> str | None:
    """Why `condition` can never be evaluated (bad syntax, an unsupported operator or name), or None. Placeholders
    stand in as None, so only the expression around them is checked."""
    expression = PLACEHOLDER.sub("None", condition)
    try:
        _check_nodes(ast.parse(expression, mode="eval"))
    except (SyntaxError, ValueError) as e:
        return f"condition '{condition}' can't be evaluated: {e}"
    return None


_MISSING = object()


def _lookup(
    root: str,
    path: list[str],
    text: str,
    workflow_results: dict[str, Any],
    input_data: dict[str, Any],
    loop_item: Any,
) -> tuple[Any, str | None]:
    """The placeholder's value, or (_MISSING, why)."""
    if root == "item":
        if loop_item is None:
            return _MISSING, "{{item}} placeholder used outside of for_each step"
        value = loop_item
        for part in path:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return _MISSING, f"Placeholder '{text}' - property '{part}' not found in item"
        return value, None
    if root == "results":
        step_id, *rest = path
        if step_id not in workflow_results:
            return _MISSING, f"Placeholder '{text}' - step '{step_id}' not found in workflow results"
        value = workflow_results[step_id]
        for part in rest:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return _MISSING, f"Placeholder '{text}' - property '{part}' not found"
        return value, None
    value = input_data
    for part in path:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return _MISSING, f"Placeholder '{text}' - property '{part}' not found"
    return value, None


def resolve_argument(
    arg: Any,
    workflow_results: dict[str, Any],
    input_data: dict[str, Any],
    strict: bool = False,
    loop_item: Any = None,
    log_missing: bool = True,
) -> Any:
    """Resolve `arg`'s placeholders against the results so far, the input data and the `for_each` item.

    A missing path raises ValueError when `strict`, otherwise gives None, logging a warning unless `log_missing` is
    False (a condition gate treats absence as a legitimate answer)."""

    def value_of(expression: str, text: str) -> Any:
        parsed = parse_placeholder(expression)
        if parsed is None:
            return _MISSING
        value, why = _lookup(*parsed, text, workflow_results, input_data, loop_item)
        if value is _MISSING:
            if strict:
                raise ValueError(why)
            if log_missing:
                workflow.logger.warning(why)
            return None
        return value

    if isinstance(arg, str):
        whole = PLACEHOLDER.fullmatch(arg)
        if whole:
            value = value_of(whole.group(1), arg)
            return arg if value is _MISSING else value

        def fill(match: re.Match[str]) -> str:
            value = value_of(match.group(1), match.group(0))
            if value is _MISSING:
                return match.group(0)
            return value if isinstance(value, str) else str(value)

        return PLACEHOLDER.sub(fill, arg)
    elif isinstance(arg, dict):
        return {
            k: resolve_argument(v, workflow_results, input_data, strict, loop_item, log_missing) for k, v in arg.items()
        }
    elif isinstance(arg, list):
        return [resolve_argument(i, workflow_results, input_data, strict, loop_item, log_missing) for i in arg]
    return arg


def evaluate_condition(condition: str, workflow_results: dict[str, Any], input_data: dict[str, Any]) -> bool:
    """Fill in each placeholder in `condition` and evaluate it.

    A missing path is None, silently: "step skipped" or "key absent" compares with `== None` / `!= None`. Raises
    ConditionError for a placeholder that isn't a single value (str, number, bool, None) and for an expression
    that can't be evaluated."""

    def fill(match: re.Match[str]) -> str:
        text = match.group(0)
        parsed = parse_placeholder(match.group(1))
        if parsed is None:
            raise ConditionError(f"condition '{condition}': {text} is not a placeholder the engine fills")
        value, _ = _lookup(*parsed, text, workflow_results, input_data, None)
        if value is _MISSING or value is None:
            return "None"
        if not isinstance(value, _CONDITION_SCALARS):
            raise ConditionError(
                f"condition '{condition}': {text} is a {type(value).__name__}; a condition compares single values "
                f"(text, numbers, true/false, None), so name a field of it"
            )
        return repr(value) if isinstance(value, str) else str(value)

    resolved = PLACEHOLDER.sub(fill, condition)
    try:
        return safe_eval_condition(resolved)
    except ValueError as e:
        raise ConditionError(f"condition '{condition}' can't be evaluated: {e}") from None
