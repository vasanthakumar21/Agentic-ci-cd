"""Azure Container Apps target-platform plugin.

This package *is* the plugin: it exists on disk at
``plugins/target/azure/container-apps/`` so the loader discovers it by name
(``target_platform: azure-container-apps`` in the IR) with no core change — the
``azure-`` prefix rule resolves ``azure-container-apps`` to the
``azure/container-apps`` directory, splitting on the FIRST ``-`` so the multi-hyphen
service name ``container-apps`` is preserved. It exposes the concrete
:class:`~.plugin.AzureContainerApps` via the preferred ``get_plugin`` factory
convention the loader looks for first.

The plugin implements the six stage verbs for the azure-container-apps row of — ``build`` = ``docker build`` (emitting a resilient, pinned, apt-mirror-free
Dockerfile,), ``package`` = push to ACR (the immutable image digest is the
promotable reference), ``deploy`` = ``az containerapp update --image`` consuming the
pre-built image, ``verify`` = poll the latest revision's provisioningState/health —
and supplies the backing Terraform (Container App + Container Apps environment + ACR
+ a user-assigned managed identity + a CI federated credential). Secrets/account
specifics are referenced, never valued; output is deterministic. See
:mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import AzureContainerApps


def get_plugin() -> AzureContainerApps:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return AzureContainerApps()


__all__ = ["AzureContainerApps", "get_plugin"]
