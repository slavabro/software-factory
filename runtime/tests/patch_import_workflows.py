"""Releases of `Order` whose patch changes only a value kept in workflow state (no command differs), written with
`from temporalio.workflow import patched`: the import style that binds the function when the module loads, which the
patch report must see through whether the module is sandboxed, passed through or unsandboxed.

- `ValueOld`: sets `value` to "old" once `go` arrives.
- `ValuePatched`: `patched("value-v2")` picks "new"; a run past the point on `ValueOld` keeps "old".
- `ValueDeprecated`: `deprecate_patch("value-v2")`, then "new": premature while a run is on the old side, and replay
  can't tell (no command differs).
"""

from temporalio import workflow
from temporalio.workflow import deprecate_patch, patched

VALUE_PATCH = "value-v2"


class _Valued:
    def __init__(self) -> None:
        self.go_ahead = False
        self.stage = "waiting"
        self.value = ""

    @workflow.signal
    def go(self) -> None:
        self.go_ahead = True

    @workflow.query
    def current_stage(self) -> str:
        return self.stage


@workflow.defn(name="Order")
class ValueOld(_Valued):
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self.go_ahead)
        self.value = "old"
        self.stage = "reserved"
        await workflow.wait_condition(lambda: False)


@workflow.defn(name="Order")
class ValuePatched(_Valued):
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self.go_ahead)
        self.value = "new" if patched(VALUE_PATCH) else "old"
        self.stage = "reserved"
        await workflow.wait_condition(lambda: False)


@workflow.defn(name="Order")
class ValueDeprecated(_Valued):
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self.go_ahead)
        deprecate_patch(VALUE_PATCH)
        self.value = "new"
        self.stage = "reserved"
        await workflow.wait_condition(lambda: False)
