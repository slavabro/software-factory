"""The workflow sandbox runner: the one place that decides which modules workflows share with the host process.

Workflow code runs in a sandbox that re-imports modules in isolation. A module that reads settings or the
environment at import or first use breaks there (`RestrictedWorkflowAccessError: Cannot access os.environ`), and a
re-import per workflow load is slow. The product names its pure modules (models, settings it has already loaded on
the host) and they pass through as one host copy; the import-notification policy then flags only real leaks.

Workers and tests must build their runner here, or tests pass or fail for reasons production never sees.
"""

from __future__ import annotations

from typing import Iterable

from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions
from temporalio.workflow import SandboxImportNotificationPolicy

# Needed by every product that types workflow inputs with Pydantic, and not on the SDK's default list.
BASE_PASSTHROUGH_MODULES: tuple[str, ...] = ("annotated_types",)

# Test-time policy: report both ways settings or I/O leak into workflows, modules imported after the workflow module
# loaded and modules passed through without being listed. (RAISE is not usable: workflow classes defined in test
# modules trip it on the test module itself.)
STRICT_IMPORT_POLICY = (
    SandboxImportNotificationPolicy.WARN_ON_UNINTENTIONAL_PASSTHROUGH
    | SandboxImportNotificationPolicy.WARN_ON_DYNAMIC_IMPORT
)


def build_workflow_runner(
    passthrough: Iterable[str] = (), *, import_policy: SandboxImportNotificationPolicy | None = None
) -> SandboxedWorkflowRunner:
    """The sandboxed runner with `BASE_PASSTHROUGH_MODULES` plus the product's `passthrough` modules.

    A module passed through must already be initialised on the host (e.g. settings loaded) before the worker starts.
    Tests pass `import_policy=STRICT_IMPORT_POLICY`."""
    restrictions = SandboxRestrictions.default.with_passthrough_modules(*BASE_PASSTHROUGH_MODULES, *passthrough)
    if import_policy is not None:
        restrictions = restrictions.with_import_notification_policy(import_policy)
    return SandboxedWorkflowRunner(restrictions=restrictions)
