"""AWS EMR target-platform plugin.

This package *is* the plugin: it exists on disk at ``plugins/target/aws/emr/`` so
the loader discovers it by name (``target_platform: aws-emr`` in the IR — resolved
via the ``aws-`` prefix rule to ``aws/emr``) with no core change. It exposes
the concrete :class:`~.plugin.AwsEmr` via the preferred ``get_plugin`` factory
convention the loader looks for first.

aws-emr is a **Spark/Hadoop data-job shape** like aws-glue: there is
NO container image, NO ECR, NO ECS. ``build`` assembles the PySpark job script(s) +
a wheel of extra python libs, ``package`` uploads them to S3 (capturing an immutable
object version as the promotable reference), ``deploy`` starts an EMR Serverless job
run (or submits an EMR step) via the CLI, and ``verify`` polls the job-run/step state
to a terminal SUCCESS. The backing Terraform provisions an EMR Serverless application
+ execution role, a versioned S3 artifact bucket, and a GitHub/Azure OIDC deploy
role. Secrets/account specifics are referenced, never valued; output is
deterministic. See :mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import AwsEmr


def get_plugin() -> AwsEmr:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return AwsEmr()


__all__ = ["AwsEmr", "get_plugin"]
