"""Placeholders and conditions in a process file, resolved inside the workflow.

- `{{results.<step>.<path>}}`: an earlier step's result; `{{input.<path>}}`: the run's input data;
  `{{item}}` / `{{item.<path>}}`: the current `for_each` item. Spaces inside the braces are allowed.
- A string that is exactly one placeholder gives the value itself (a dict stays a dict). A placeholder inside other
  text is filled in: `"Approve {{input.label}}"` gives `"Approve alpha"`, a non-string value as `str(value)`.
  Dicts and lists are resolved element by element. A missing path gives None (logged unless `log_missing=False`;
  `strict=True` raises instead), so inside text it reads `None`. Anything else in braces stays as written (the
  validator rejects it when the file loads if it starts like a placeholder: `results`, `input`, `item`, or a
  near miss of one followed by a path, such as `inputs.x` or `Result.a`).
- Escapes, read the same way by the runtime and the validator (`split_escapes`): a run of backslashes right before
  `{{` stands for half as many backslashes, and an odd one makes that `{{` literal. So `\\{{input.x}}` gives the text
  `{{input.x}}`, `\\\\{{input.x}}` a backslash and the value, `\\\\\\{{input.x}}` a backslash and the text
  `{{input.x}}`. A backslash anywhere else is kept as written. (Write them in a plain or single-quoted YAML string;
  in a double-quoted one each backslash is doubled.)
- A condition fills in each placeholder and evaluates the result with the safe evaluator: comparisons,
  `and`/`or`/`not` (short-circuiting, as in Python), arithmetic and literals, nothing else. A placeholder whose value
  is not a single value (a dict, a list) or an expression that can't be evaluated (bad syntax, an unsupported
  operator or name) raises `ConditionError`. A comparison that can't be made with the values it got (`None > 0` when
  a step was skipped, `'5' > 0`, a division by zero inside it, a non-finite number) is false on its own, logged, so
  `{{input.n}} == None or {{input.n}} > 0` is true when `n` is missing; arithmetic that fails outside any comparison
  makes the whole condition false.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from typing import Any, Callable, Collection

from temporalio import workflow

# One placeholder, `{{ root.path }}` (no braces inside), in a part of text `split_escapes` gives.
PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
# A run of backslashes right before `{{`: half as many backslashes, and an odd run makes that `{{` literal.
_ESCAPE = re.compile(r"(\\+)\{\{")
PLACEHOLDER_ROOTS = ("results", "input", "item")


def split_escapes(text: str) -> list[str]:
    """`text` cut at each literal (escaped) `{{`, with backslash runs before `{{` halved: placeholders are found in
    each part, and the parts joined with `{{` again. The one escape rule, for the runtime and the validator."""
    parts: list[str] = []
    current: list[str] = []
    position = 0
    for match in _ESCAPE.finditer(text):
        run = len(match.group(1))
        current.append(text[position : match.start()] + "\\" * (run // 2))
        if run % 2:
            parts.append("".join(current))
            current = []
        else:
            current.append("{{")
        position = match.end()
    current.append(text[position:])
    parts.append("".join(current))
    return parts


def find_placeholders(text: str) -> list[re.Match[str]]:
    """Every text in double braces that isn't escaped (by the rule of `split_escapes`)."""
    return [match for part in split_escapes(text) for match in PLACEHOLDER.finditer(part)]


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


class _Values:
    """The placeholders' values a condition was filled in with (by name), and why comparisons were false."""

    def __init__(self) -> None:
        self.by_name: dict[str, Any] = {}
        self.notes: list[str] = []

    def add(self, value: Any) -> str:
        name = f"_p{len(self.by_name)}"
        self.by_name[name] = value
        return name

    def describe(self, node: ast.AST) -> str:
        """`node` as text, each placeholder's name replaced by its value."""
        return re.sub(r"\b_p\d+\b", lambda m: repr(self.by_name.get(m.group(0))), ast.unparse(node))


def _safe_eval(node: ast.AST, values: _Values) -> Any:
    """Evaluate an AST node without executing arbitrary code: comparisons, boolean logic (short-circuiting),
    arithmetic, literals and the placeholders' values. A comparison that can't be made is false."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body, values)
    elif isinstance(node, ast.Constant):
        return node.value
    elif isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):
            raise ValueError(f"Unsupported boolean operator: {type(node.op).__name__}")
        result: Any = None
        for value in node.values:
            result = _safe_eval(value, values)
            if bool(result) is isinstance(node.op, ast.Or):  # `or` stops at the first true, `and` at the first false
                return result
        return result
    elif isinstance(node, ast.Compare):
        try:
            return _compare(node, values)
        except (TypeError, ArithmeticError) as e:
            values.notes.append(f"{values.describe(node)} can't be compared ({type(e).__name__}: {e}), so it is false")
            return False
    elif isinstance(node, ast.BinOp):
        op = _SAFE_OPERATORS.get(type(node.op))
        if not op:
            raise ValueError(f"Unsupported binary operator: {type(node.op).__name__}")
        return op(_safe_eval(node.left, values), _safe_eval(node.right, values))
    elif isinstance(node, ast.UnaryOp):
        op = _SAFE_OPERATORS.get(type(node.op))
        if not op:
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
        return op(_safe_eval(node.operand, values))
    elif isinstance(node, ast.Name):
        if node.id == "True":
            return True
        elif node.id == "False":
            return False
        elif node.id == "None":
            return None
        elif node.id in values.by_name:
            value = values.by_name[node.id]
            if isinstance(value, float) and not math.isfinite(value):
                raise TypeError(f"{value} is not a finite number")
            return value
        raise ValueError(f"Unsupported name reference: {node.id}")
    else:
        raise ValueError(f"Unsupported AST node type: {type(node).__name__}")


def _compare(node: ast.Compare, values: _Values) -> bool:
    """A comparison, chained as in Python (`1 < x < 3`): each operand evaluated only when it is reached."""
    left = _safe_eval(node.left, values)
    for op_node, comparator in zip(node.ops, node.comparators):
        op = _SAFE_OPERATORS.get(type(op_node))
        if not op:
            raise ValueError(f"Unsupported comparison operator: {type(op_node).__name__}")
        right = _safe_eval(comparator, values)
        if not op(left, right):
            return False
        left = right
    return True


def _check_nodes(node: ast.AST, names: Collection[str] = ()) -> None:
    """Raise ValueError for any node `_safe_eval` would refuse, without evaluating (the validator's check). `names`:
    the placeholders' names, when filled in."""
    if isinstance(node, ast.Expression):
        _check_nodes(node.body, names)
    elif isinstance(node, ast.Constant):
        return
    elif isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):
            raise ValueError(f"Unsupported boolean operator: {type(node.op).__name__}")
        for value in node.values:
            _check_nodes(value, names)
    elif isinstance(node, ast.Compare):
        for op_node in node.ops:
            if type(op_node) not in _SAFE_OPERATORS:
                raise ValueError(f"Unsupported comparison operator: {type(op_node).__name__}")
        for child in (node.left, *node.comparators):
            _check_nodes(child, names)
    elif isinstance(node, ast.BinOp):
        if type(node.op) not in _SAFE_OPERATORS:
            raise ValueError(f"Unsupported binary operator: {type(node.op).__name__}")
        _check_nodes(node.left, names)
        _check_nodes(node.right, names)
    elif isinstance(node, ast.UnaryOp):
        if type(node.op) not in _SAFE_OPERATORS:
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
        _check_nodes(node.operand, names)
    elif isinstance(node, ast.Name):
        if node.id not in ("True", "False", "None") and node.id not in names:
            raise ValueError(f"Unsupported name reference: {node.id}")
    else:
        raise ValueError(f"Unsupported AST node type: {type(node).__name__}")


def safe_eval_condition(expression: str) -> bool:
    """Evaluate a condition expression without arbitrary code execution: comparisons (==, !=, <, >, <=, >=),
    boolean logic (and, or, not; short-circuiting), arithmetic (+, -, *, /, %) and literals. A comparison that
    can't be made is false; raises ValueError for anything else that can't be evaluated."""
    try:
        tree = ast.parse(expression, mode="eval")
        _check_nodes(tree)
        return bool(_safe_eval(tree, _Values()))
    except (SyntaxError, ValueError, TypeError, ArithmeticError) as e:
        raise ValueError(f"Invalid condition expression '{expression}': {e}")


def condition_syntax_problem(condition: str) -> str | None:
    """Why `condition` can never be evaluated (bad syntax, an unsupported operator or name), or None. Placeholders
    stand in as None, so only the expression around them is checked."""
    expression = "{{".join(PLACEHOLDER.sub("None", part) for part in split_escapes(condition))
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
        parts = split_escapes(arg)
        whole = PLACEHOLDER.fullmatch(arg) if parts == [arg] else None
        if whole:
            value = value_of(whole.group(1), arg)
            return arg if value is _MISSING else value

        def fill(match: re.Match[str]) -> str:
            value = value_of(match.group(1), match.group(0))
            if value is _MISSING:
                return match.group(0)
            return value if isinstance(value, str) else str(value)

        # Split on the escapes first, so a filled-in value that happens to contain `\{{` is left as it is.
        return "{{".join(PLACEHOLDER.sub(fill, part) for part in parts)
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
    that can't be evaluated (bad syntax, an unsupported operator or name). `and` / `or` short-circuit as in Python.
    A comparison the values can't support (`None > 0`, `'5' > 0`, `1 / 0 > 1`, a non-finite number) is false on its
    own, and arithmetic that fails outside any comparison makes the whole condition false, with a warning: a value
    that is missing or of the wrong kind skips the step instead of failing the run, and a guard such as
    `{{input.n}} == None or {{input.n}} > 0` works."""
    values = _Values()

    def fill(match: re.Match[str]) -> str:
        text = match.group(0)
        parsed = parse_placeholder(match.group(1))
        if parsed is None:
            raise ConditionError(f"condition '{condition}': {text} is not a placeholder the engine fills")
        value, _ = _lookup(*parsed, text, workflow_results, input_data, None)
        if value is _MISSING:
            value = None
        if not isinstance(value, _CONDITION_SCALARS):
            raise ConditionError(
                f"condition '{condition}': {text} is a {type(value).__name__}; a condition compares single values "
                f"(text, numbers, true/false, None), so name a field of it"
            )
        return values.add(value)

    resolved = "{{".join(PLACEHOLDER.sub(fill, part) for part in split_escapes(condition))
    try:
        tree = ast.parse(resolved, mode="eval")
        _check_nodes(tree, values.by_name)
    except (SyntaxError, ValueError) as e:
        raise ConditionError(f"condition '{condition}' can't be evaluated: {e}") from None
    try:
        result = bool(_safe_eval(tree, values))
    except (TypeError, ArithmeticError) as e:
        values.notes.append(f"{values.describe(tree)} can't be evaluated ({type(e).__name__}: {e})")
        result = False
    if values.notes:
        workflow.logger.warning(f"Condition '{condition}' is {result}: " + "; ".join(values.notes))
    return result
