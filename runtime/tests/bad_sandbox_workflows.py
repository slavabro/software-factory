"""A release whose workflow module can't load in the sandbox (it reads the clock at import): the definition
loads on the host, but no replayer (and no worker) can be built from it."""

from datetime import datetime

from temporalio import workflow

from factory_runtime.temporal import WorkerDefinition

LOADED_AT = datetime.now()


@workflow.defn(name="Broken")
class Broken:
    @workflow.run
    async def run(self) -> None:
        pass


DEFINITION = WorkerDefinition(task_queues=("replay-check",), workflows=(Broken,))
