"""The runtime's golden runs: the four interpreters with the built-in step types only, bound to the harness of
`factory_runtime.testing.golden` (which holds the process-file format, the stubs, normalization and replay)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from temporalio import workflow

from factory_runtime.process import BUILTIN_STEP_TYPES, ProcessWorkflowBase
from factory_runtime.testing.golden import GoldenHarness


@workflow.defn(name="LinearProcessWorkflow")
class LinearProcessWorkflow(ProcessWorkflowBase):
    step_types = BUILTIN_STEP_TYPES

    @workflow.run
    async def run(
        self,
        process_def: Dict[str, Any],
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self.run_linear(process_def, input_data, metadata)


@workflow.defn(name="ConditionalProcessWorkflow")
class ConditionalProcessWorkflow(ProcessWorkflowBase):
    step_types = BUILTIN_STEP_TYPES

    @workflow.run
    async def run(
        self,
        process_def: Dict[str, Any],
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self.run_conditional(process_def, input_data, metadata)


@workflow.defn(name="ParallelProcessWorkflow")
class ParallelProcessWorkflow(ProcessWorkflowBase):
    step_types = BUILTIN_STEP_TYPES

    @workflow.run
    async def run(
        self,
        process_def: Dict[str, Any],
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self.run_parallel(process_def, input_data, metadata)


@workflow.defn(name="HybridProcessWorkflow")
class HybridProcessWorkflow(ProcessWorkflowBase):
    step_types = BUILTIN_STEP_TYPES

    @workflow.run
    async def run(
        self,
        process_def: Dict[str, Any],
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self.run_conditional(process_def, input_data, metadata)


HARNESS = GoldenHarness(
    fixture_dir=Path(__file__).resolve().parent / "fixtures" / "process_golden",
    interpreters=(LinearProcessWorkflow, ConditionalProcessWorkflow, ParallelProcessWorkflow, HybridProcessWorkflow),
)
