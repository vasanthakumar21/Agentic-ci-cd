"""AWS EKS target-platform plugin.

This package *is* the plugin: it exists on disk at ``plugins/target/aws/eks/`` so
the loader discovers it by name (``target_platform: aws-eks`` in the IR — resolved
via the ``aws-`` prefix rule to ``aws/eks``) with no core change. It exposes
the concrete :class:`~.plugin.AwsEks` via the preferred ``get_plugin`` factory
convention the loader looks for first.

The plugin implements the six stage verbs for the aws-eks row of —
``build`` = ``docker build`` (emitting a resilient, pinned, apt-mirror-free
Dockerfile,), ``package`` = push to ECR (the immutable image digest is the
promotable reference), ``deploy`` = ``kubectl apply`` / ``helm upgrade`` to the EKS
cluster (consuming the pre-built image digest under promotion), ``verify`` =
``kubectl rollout status`` — and supplies the backing Terraform (EKS references,
ECR, IRSA/OIDC) plus k8s manifests (an IRSA-annotated ServiceAccount, a Deployment,
and a Service). Secrets/account specifics are referenced, never valued; output
is deterministic. See :mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import AwsEks


def get_plugin() -> AwsEks:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return AwsEks()


__all__ = ["AwsEks", "get_plugin"]
