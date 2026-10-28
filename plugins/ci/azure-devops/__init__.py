"""Azure DevOps CI-vendor plugin.

This package *is* the plugin: it exists on disk at ``plugins/ci/azure-devops/``
so the loader discovers it by name (``ci_vendor: azure-devops`` in the IR) with
no core change. It exposes the concrete :class:`~.plugin.AzureDevOps` via
the preferred ``get_plugin`` factory convention the loader looks for first.

The plugin renders the vendor-neutral :class:`StageGraph` into an Azure Pipelines
file (``azure-pipelines.yml``) — orchestration only (stage order via
``dependsOn``, manual gates / approvals via environments, timeouts, scans, cloud
OIDC via workload identity federation). It holds **no** target-platform business
logic: the shell command for each stage comes from ``stage.runs``. See
:mod:`.plugin` for the mapping details.
"""

from __future__ import annotations

from .plugin import AzureDevOps


def get_plugin() -> AzureDevOps:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return AzureDevOps()


__all__ = ["AzureDevOps", "get_plugin"]
