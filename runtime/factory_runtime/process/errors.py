"""How the process engine reports a process file that can't run.

- `ProcessValidationError` (a ValueError): raised where a process file is loaded or checked (`validate_process`),
  with every problem found, each naming its step.
- `definition_error(message)`: the failure a run ends with when its definition is wrong (a None definition, an
  invalid step, a condition that can't be evaluated). It is a non-retryable `ApplicationError` of type
  `ProcessDefinitionError`, so the run fails at once with that message instead of retrying its workflow task until
  someone terminates it, and `on_error: continue` does not swallow it.
"""

from __future__ import annotations

from temporalio.exceptions import ApplicationError

PROCESS_DEFINITION_ERROR = "ProcessDefinitionError"


class ProcessValidationError(ValueError):
    """A process definition that can't run; `problems` lists each one, naming its step."""

    def __init__(self, source: str, problems: list[str]) -> None:
        self.source = source
        self.problems = problems
        where = f"{source}: " if source else ""
        super().__init__(f"{where}invalid process definition: " + "; ".join(problems))


def definition_error(message: str) -> ApplicationError:
    """The non-retryable failure that ends a run whose definition is wrong."""
    return ApplicationError(message, type=PROCESS_DEFINITION_ERROR, non_retryable=True)


def is_definition_error(error: BaseException) -> bool:
    return isinstance(error, ApplicationError) and error.type == PROCESS_DEFINITION_ERROR
