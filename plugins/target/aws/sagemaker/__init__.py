"""AWS SageMaker target-platform plugin.

This package *is* the plugin: it exists on disk at ``plugins/target/aws/sagemaker/``
so the loader discovers it by name (``target_platform: aws-sagemaker`` in the IR —
resolved via the ``aws-`` prefix rule to ``aws/sagemaker``) with no core change. It exposes the concrete :class:`~.plugin.AwsSageMaker` via the preferred
``get_plugin`` factory convention the loader looks for first.

aws-sagemaker is a **container-based ML shape** (bring-your-own-container) like
aws-ecs: ``build`` is ``docker build`` (a training/inference image
with a resilient Dockerfile), ``package`` pushes to ECR (capturing an immutable
digest as the promotable reference), ``deploy`` creates/updates a SageMaker Model +
EndpointConfig + Endpoint via the CLI, and ``verify`` polls the endpoint status to
InService. The backing Terraform provisions the SageMaker execution role +
model/endpoint config + ECR + S3 (model artifacts) + a GitHub/Azure OIDC deploy
role. Secrets/account specifics are referenced, never valued; output is
deterministic. See :mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import AwsSageMaker


def get_plugin() -> AwsSageMaker:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return AwsSageMaker()


__all__ = ["AwsSageMaker", "get_plugin"]
