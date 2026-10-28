"""AWS Batch target-platform plugin.

This package *is* the plugin: it exists on disk at ``plugins/target/aws/batch/`` so
the loader discovers it by name (``target_platform: aws-batch`` in the IR — resolved
via the ``aws-`` prefix rule to ``aws/batch``) with no core change. It exposes
the concrete :class:`~.plugin.AwsBatch` via the preferred ``get_plugin`` factory
convention the loader looks for first.

aws-batch is a **container-based compute shape** like aws-ecs:
``build`` is ``docker build`` (with a resilient Dockerfile), ``package`` pushes the
image to ECR (capturing an immutable digest as the promotable reference), ``deploy``
registers/updates an AWS Batch job definition pointing at the image, and ``verify``
submits a job and polls its status to SUCCEEDED. The backing Terraform provisions a
Batch compute environment + job queue + job definition + IAM + ECR + a GitHub/Azure
OIDC deploy role. Secrets/account specifics are referenced, never valued; output
is deterministic. See :mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import AwsBatch


def get_plugin() -> AwsBatch:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return AwsBatch()


__all__ = ["AwsBatch", "get_plugin"]
