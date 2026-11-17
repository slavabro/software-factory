"""One worker definition that a product's worker and its replay check both build from.

A product exposes its `WorkerDefinition` once, at module level (`myproduct.worker:DEFINITION`): the workflow classes,
the sandbox runner, the data converter, the interceptors, the workflow failure exception types, the task queues it
serves and the names of the activities it registers. The worker builds its `Worker` with `definition.worker(...)`;
the deploy check (`factory_runtime.temporal.replay_check`) builds its `Replayer` with `definition.replayer()`. So the
two can't disagree about what is registered or how workflow code runs: `worker()` refuses a client whose data
converter isn't the definition's (connect with `definition.connect()`), client interceptors or plugins that would
add to the worker what the replayer doesn't have, and `Worker` options that change how workflow code runs.

Activities are carried by name, not as callables: a product's activities often need clients and connections built at
worker start, which the check (run in a one-off container against the live server) must not build. `worker()` checks
that the activities it is handed register exactly `activity_names`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.converter import DataConverter
from temporalio.worker import Interceptor, Plugin, Replayer, Worker, WorkflowRunner

from factory_runtime.temporal.converter import data_converter as default_data_converter
from factory_runtime.temporal.sandbox import build_workflow_runner
from factory_runtime.temporal.settings import TemporalSettings

# In `activity_names`: a dynamic activity is registered, so every activity type is served.
DYNAMIC = "*"

# `Worker` options that change how workflow code runs; they belong in the definition, which the replayer shares.
REPLAY_OPTIONS = frozenset(
    {
        "workflows",
        "workflow_runner",
        "unsandboxed_workflow_runner",
        "interceptors",
        "workflow_failure_exception_types",
        "plugins",
    }
)


def activity_names(activities: Iterable[Callable[..., Any]]) -> frozenset[str]:
    """The activity type names `activities` register (`DYNAMIC` for a dynamic activity)."""
    names = set()
    for fn in activities:
        name = activity._Definition.must_from_callable(fn).name
        names.add(DYNAMIC if name is None else name)
    return frozenset(names)


@dataclass(frozen=True)
class Handlers:
    """The message handler names a workflow class defines; `dynamic_*` when it has a dynamic handler of that kind."""

    signals: frozenset[str]
    updates: frozenset[str]
    queries: frozenset[str]
    dynamic_signal: bool
    dynamic_update: bool
    dynamic_query: bool


@dataclass(frozen=True)
class WorkerDefinition:
    """What a worker registers and how it runs workflows: everything `Worker` and `Replayer` must agree on.

    `task_queues`: the queues workers of this definition poll (one `Worker` each). `activity_names`: the activity
    types those workers register (`activity_names(...)` builds it from the callables); local activities must be among
    them. `external_activity_names`: activity types the workflows call that other workers serve; the check accepts
    them for (remote) activities, the worker doesn't register them. `settings`: where `connect()` and the check
    connect when nothing says otherwise (its `task_queue` is not used here)."""

    task_queues: tuple[str, ...]
    workflows: tuple[type, ...]
    activity_names: frozenset[str] = frozenset()
    external_activity_names: frozenset[str] = frozenset()
    workflow_runner: WorkflowRunner = field(default_factory=build_workflow_runner)
    data_converter: DataConverter = field(default_factory=default_data_converter)
    interceptors: tuple[Interceptor, ...] = ()
    workflow_failure_exception_types: tuple[type[BaseException], ...] = ()
    settings: TemporalSettings | None = None

    def __post_init__(self) -> None:
        if not self.task_queues or not all(self.task_queues):
            raise ValueError("WorkerDefinition needs at least one task queue")
        if not self.workflows:
            raise ValueError("WorkerDefinition needs at least one workflow")
        for cls in self.workflows:
            workflow._Definition.must_from_class(cls)  # raises for a class that isn't a workflow

    # -- what is registered --------------------------------------------------------------------------------------

    def workflow_classes(self) -> dict[str | None, type]:
        """Registered workflow type names to their classes (None: the dynamic workflow)."""
        return {workflow._Definition.must_from_class(cls).name: cls for cls in self.workflows}

    def workflow_class(self, workflow_type: str) -> type | None:
        """The class that runs `workflow_type` on this worker (the dynamic workflow if no class names it), or None."""
        classes = self.workflow_classes()
        return classes.get(workflow_type) or classes.get(None)

    def serves_activity(self, activity_type: str) -> bool:
        """Whether an activity of this type gets served: here or by another worker (`external_activity_names`)."""
        names = self.activity_names | self.external_activity_names
        return activity_type in names or DYNAMIC in names

    def runs_local_activity(self, activity_type: str) -> bool:
        """Whether this worker runs a local activity of this type: only its own registered activities do."""
        return activity_type in self.activity_names or DYNAMIC in self.activity_names

    @staticmethod
    def handlers(cls: type) -> Handlers:
        d = workflow._Definition.must_from_class(cls)
        return Handlers(
            signals=frozenset(n for n in d.signals if n is not None),
            updates=frozenset(n for n in d.updates if n is not None),
            queries=frozenset(n for n in d.queries if n is not None),
            dynamic_signal=None in d.signals,
            dynamic_update=None in d.updates,
            dynamic_query=None in d.queries,
        )

    # -- building ------------------------------------------------------------------------------------------------

    async def connect(self, server: str | None = None, *, namespace: str | None = None, **options: Any) -> Client:
        """A client with the definition's data converter, at `server` / `namespace` (default: `settings`, else
        `localhost:7233` / `default`). `options` go to `Client.connect` (`tls`, `api_key`, `identity`, ...)."""
        settings = self.settings
        return await Client.connect(
            server or (settings.server_url if settings is not None else "localhost:7233"),
            namespace=namespace or (settings.namespace if settings is not None else "default"),
            data_converter=self.data_converter,
            **options,
        )

    def worker(
        self,
        client: Client,
        *,
        activities: Sequence[Callable[..., Any]] = (),
        task_queue: str | None = None,
        **options: Any,
    ) -> Worker:
        """The `Worker` for one of the definition's queues (the only one when `task_queue` is omitted), registering
        exactly `activity_names`. `options` go to `Worker` (executors, tuning, identity); options that change how
        workflow code runs (`REPLAY_OPTIONS`) are refused, as is a client that would add to the worker what the
        replayer doesn't have."""
        if task_queue is None:
            if len(self.task_queues) != 1:
                raise ValueError(f"name the task queue: this definition serves {list(self.task_queues)}")
            task_queue = self.task_queues[0]
        if task_queue not in self.task_queues:
            raise ValueError(f"task queue {task_queue!r} is not one of {list(self.task_queues)}")
        given = activity_names(activities)
        if given != self.activity_names:
            raise ValueError(
                "the activities handed to the worker differ from the definition's activity_names: "
                f"missing {sorted(self.activity_names - given)}, extra {sorted(given - self.activity_names)}"
            )
        if refused := sorted(REPLAY_OPTIONS & set(options)):
            raise ValueError(f"{refused} change how workflow code runs: put them in the WorkerDefinition")
        return Worker(
            client,
            task_queue=task_queue,
            workflows=list(self.workflows),
            activities=list(activities),
            workflow_runner=self.workflow_runner,
            interceptors=self._interceptors_beyond(client),
            workflow_failure_exception_types=list(self.workflow_failure_exception_types),
            **options,
        )

    def _interceptors_beyond(self, client: Client) -> list[Interceptor]:
        """The interceptors to hand `Worker` on top of the client's: `Worker` puts the client's worker interceptors
        first, so they must be the start of the definition's (and the client's converter must be the definition's,
        and it may carry no worker plugin)."""
        if client.data_converter is not self.data_converter:
            raise ValueError(
                "the client's data converter is not the definition's: connect with definition.connect() "
                "(or Client.connect(..., data_converter=definition.data_converter))"
            )
        config = client.config()
        if plugins := [p for p in config.get("plugins", []) if isinstance(p, Plugin)]:
            raise ValueError(f"the client carries worker plugins {plugins}, which the replayer wouldn't have")
        from_client = [i for i in config.get("interceptors", []) if isinstance(i, Interceptor)]
        n = len(from_client)
        if from_client != list(self.interceptors[:n]):
            raise ValueError(
                "the client carries worker interceptors that aren't the first of the definition's interceptors, "
                "so the worker would run with interceptors the replayer doesn't have (or in another order)"
            )
        return list(self.interceptors[n:])

    def replayer(
        self, *, namespace: str | None = None, extra_interceptors: Sequence[Interceptor] = (), **options: Any
    ) -> Replayer:
        """A `Replayer` with the worker's workflows, runner, converter, interceptors and failure types (the checks add
        their recording interceptor last, `extra_interceptors`)."""
        if namespace is None:
            namespace = self.settings.namespace if self.settings is not None else "default"
        return Replayer(
            workflows=list(self.workflows),
            workflow_runner=self.workflow_runner,
            namespace=namespace,
            data_converter=self.data_converter,
            interceptors=[*self.interceptors, *extra_interceptors],
            workflow_failure_exception_types=list(self.workflow_failure_exception_types),
            **options,
        )
