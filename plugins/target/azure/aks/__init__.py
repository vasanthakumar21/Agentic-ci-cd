"""Azure AKS target-platform plugin.

This package *is* the plugin: it exists on disk at ``plugins/target/azure/aks/`` so
the loader discovers it by name (``target_platform: azure-aks`` in the IR — resolved
via the ``azure-`` prefix rule to ``azure/aks``) with no core change. It
exposes the concrete :class:`~.plugin.AzureAks` via the preferred ``get_plugin``
factory convention the loader looks for first.

The plugin implements the six stage verbs for the azure-aks row of —
``build`` = ``docker build`` (emitting a resilient, pinned, apt-mirror-free
Dockerfile,), ``package`` = push to ACR (the immutable image digest is the
promotable reference), ``deploy`` = ``az aks get-credentials`` then ``kubectl apply``
/ ``helm upgrade`` to the AKS cluster (consuming the pre-built image digest under
promotion), ``verify`` = ``kubectl rollout status`` — and supplies the backing
Terraform (AKS references, ACR, a user-assigned managed identity, AKS workload
identity binding, and a CI federated credential) plus k8s manifests (a ServiceAccount
annotated for Azure Workload Identity, a Deployment, and a Service). Secrets/account
specifics are referenced, never valued; output is deterministic. See
:mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import AzureAks


def get_plugin() -> AzureAks:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return AzureAks()


__all__ = ["AzureAks", "get_plugin"]
