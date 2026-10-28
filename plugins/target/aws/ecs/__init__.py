"""AWS ECS target-platform plugin.

This package *is* the plugin: it exists on disk at ``plugins/target/aws-ecs/`` so
the loader discovers it by name (``target_platform: aws-ecs`` in the IR) with no
core change. It exposes the concrete :class:`~.plugin.AwsEcs` via the
preferred ``get_plugin`` factory convention the loader looks for first.

The plugin implements the six stage verbs for the aws-ecs column of —
``build`` = ``docker build`` (emitting a resilient, pinned, apt-mirror-free
Dockerfile,), ``package`` = push to ECR, ``deploy`` = register a task def +
update the ECS service, ``verify`` = wait for service stability — and supplies the
backing Terraform (ECR, ECS, IAM/OIDC). Secrets/account specifics are referenced,
never valued; output is deterministic. See :mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import AwsEcs


def get_plugin() -> AwsEcs:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return AwsEcs()


__all__ = ["AwsEcs", "get_plugin"]
