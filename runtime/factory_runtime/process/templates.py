"""Placeholders and conditions in a process file, resolved inside the workflow.

- `{{results.<step>.<path>}}`: an earlier step's result; `{{input.<path>}}`: the run's input data;
  `{{item}}` / `{{item.<path>}}`: the current `for_each` item.
- Only a whole string is a placeholder: `"{{input.a}}"` resolves, `"x {{input.a}}"` stays as written. Dicts and lists
  are resolved element by element. A missing path gives None (logged unless `log_missing=False`; `strict=True`
  raises instead).
- A condition substitutes every placeholder in place (a string value as its repr, None as `None`) and evaluates the
  result with `safe_eval_condition`: comparisons, `and`/`or`/`not`, arithmetic and literals, nothing else.
"""

from __future__ import annotations

import ast
import operator
import re
from typing import Any, Callable

from temporalio import workflow

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


def safe_eval_condition(expression: str) -> bool:
    """Evaluate a condition expression without arbitrary code execution: comparisons (==, !=, <, >, <=, >=),
    boolean logic (and, or, not), arithmetic (+, -, *, /, %) and literals. Raises ValueError for anything else."""
    try:
        tree = ast.parse(expression, mode="eval")
        return bool(_safe_eval(tree))
    except (SyntaxError, ValueError) as e:
        raise ValueError(f"Invalid condition expression '{expression}': {e}")


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

    def _miss(msg: str) -> None:
        if strict:
            raise ValueError(msg)
        if log_missing:
            workflow.logger.warning(msg)

    if isinstance(arg, str):
        # {{item}} and {{item.nested.path}} (for_each loops)
        if (arg == "{{item}}" or arg.startswith("{{item.")) and arg.endswith("}}"):
            if loop_item is None:
                _miss("{{item}} placeholder used outside of for_each step")
                return None
            if arg == "{{item}}":
                return loop_item
            path = arg[len("{{item.") : -2]
            value = loop_item
            for part in path.split("."):
                if isinstance(value, dict) and part in value:
                    value = value[part]
                else:
                    _miss(f"Placeholder '{arg}' - property '{part}' not found in item")
                    return None
            return value

        if arg.startswith("{{results.") and arg.endswith("}}"):
            path = arg[len("{{results.") : -2]
            parts = path.split(".")
            step_id = parts[0]
            if step_id not in workflow_results:
                _miss(f"Placeholder '{{{{results.{path}}}}}' - step '{step_id}' not found in workflow results")
                return None
            value = workflow_results[step_id]
            for part in parts[1:]:
                if isinstance(value, dict) and part in value:
                    value = value[part]
                else:
                    _miss(f"Placeholder '{{{{results.{path}}}}}' - property '{part}' not found")
                    return None
            return value
        elif arg.startswith("{{input.") and arg.endswith("}}"):
            path = arg[len("{{input.") : -2]
            value = input_data
            for part in path.split("."):
                if isinstance(value, dict) and part in value:
                    value = value[part]
                else:
                    _miss(f"Placeholder '{{{{input.{path}}}}}' - property '{part}' not found")
                    return None
            return value
        return arg
    elif isinstance(arg, dict):
        return {
            k: resolve_argument(v, workflow_results, input_data, strict, loop_item, log_missing) for k, v in arg.items()
        }
    elif isinstance(arg, list):
        return [resolve_argument(i, workflow_results, input_data, strict, loop_item, log_missing) for i in arg]
    return arg


def evaluate_condition(condition: str, workflow_results: dict[str, Any], input_data: dict[str, Any]) -> bool:
    """Substitute each placeholder in `condition` and evaluate it; an invalid expression is logged and is False.

    A missing path is None, silently: "step skipped" or "key absent" compares with `== None` / `!= None`."""
    resolved = condition
    for placeholder in re.findall(r"\{\{(.*?)\}\}", condition):
        value = resolve_argument(f"{{{{{placeholder}}}}}", workflow_results, input_data, log_missing=False)
        if isinstance(value, str):
            value_str = repr(value)
        elif value is None:
            value_str = "None"
        else:
            value_str = str(value)
        resolved = resolved.replace(f"{{{{{placeholder}}}}}", value_str)

    try:
        return safe_eval_condition(resolved)
    except ValueError as e:
        workflow.logger.warning(f"Condition evaluation failed: {condition} - {e}")
        return False
