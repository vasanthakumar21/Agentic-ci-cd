"""Kubernetes target-platform plugin.

This package *is* the plugin: it lives on disk at ``plugins/target/kubernetes/`` so
the loader discovers it by name (``target_platform: kubernetes`` in the IR) with no
core change. It exposes the concrete :class:`~.plugin.Kubernetes` via the
preferred ``get_plugin`` factory convention the loader looks for first.

Kubernetes is image-based like aws-ecs — ``build`` = ``docker build`` (emitting a
resilient, pinned, apt-mirror-free Dockerfile,), ``package`` = push to a
generic container registry, ``deploy`` = ``kubectl apply`` / ``helm upgrade``,
``verify`` = ``kubectl rollout status`` — but the registry/deploy are generic (NOT
ECR / ECS). It supplies the backing IaC as k8s manifests (Deployment + Service) and
an equivalent kubernetes-provider Terraform module. Secrets/environment specifics
are referenced, never valued; output is deterministic. See
:mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import Kubernetes


def get_plugin() -> Kubernetes:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return Kubernetes()


__all__ = ["Kubernetes", "get_plugin"]
