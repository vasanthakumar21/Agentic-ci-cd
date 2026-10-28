"""AWS Glue target-platform plugin.

This package *is* the plugin: it exists on disk at ``plugins/target/aws/glue/`` so
the loader discovers it by name (``target_platform: aws-glue`` in the IR — resolved
via the ``aws-`` prefix rule to ``aws/glue``) with no core change. It exposes
the concrete :class:`~.plugin.AwsGlue` via the preferred ``get_plugin`` factory
convention the loader looks for first.

aws-glue is a **data-job shape** like databricks: there is NO
container image, NO ECR, NO ECS. ``build`` assembles the Glue job script(s) + extra
python libs/wheel, ``package`` uploads them to S3 (capturing an immutable object
version as the promotable reference), ``deploy`` creates/updates the Glue job via
the CLI, and ``verify`` starts a job run and polls its state to SUCCEEDED. The
backing Terraform provisions a Glue job + execution role, a versioned S3 script
bucket, and a GitHub/Azure OIDC deploy role. Secrets/account specifics are
referenced, never valued; output is deterministic. See :mod:`.plugin`
for details.
"""

from __future__ import annotations

from .plugin import AwsGlue


def get_plugin() -> AwsGlue:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return AwsGlue()


__all__ = ["AwsGlue", "get_plugin"]
