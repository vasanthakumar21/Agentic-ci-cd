"""Azure Functions target-platform plugin.

This package *is* the plugin: it exists on disk at ``plugins/target/azure-functions/``
so the loader discovers it by name (``target_platform: azure-functions`` in the IR)
with no core change. It exposes the concrete :class:`~.plugin.AzureFunctions`
via the preferred ``get_plugin`` factory convention the loader looks for first.

The plugin implements the six stage verbs for the azure-functions column of — ``build`` = build the function package (deps into a build dir, NO image),
``package`` = zip + stage the function app, ``deploy`` = ``func azure functionapp
publish`` / ``az functionapp deployment config-zip``, ``verify`` = a function smoke
test (``az functionapp show`` state + curl) — and supplies the backing Terraform
(Function App, storage account, service plan, user-assigned identity + OIDC
federated credential). Secrets/account specifics are referenced, never valued;
output is deterministic. See :mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import AzureFunctions


def get_plugin() -> AzureFunctions:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return AzureFunctions()


__all__ = ["AzureFunctions", "get_plugin"]
