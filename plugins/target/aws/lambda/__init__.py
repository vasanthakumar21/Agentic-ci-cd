"""AWS Lambda target-platform plugin.

This package *is* the plugin: it exists on disk at ``plugins/target/aws/lambda/``
so the loader discovers it by name (``target_platform: aws-lambda`` in the IR —
resolved via the ``aws-`` prefix rule to ``aws/lambda``) with no core change. It exposes the concrete :class:`~.plugin.AwsLambda` via the preferred
``get_plugin`` factory convention the loader looks for first.

The plugin implements the six stage verbs for the aws-lambda row of —
``build`` = assemble a resilient, deterministic deployment zip (NO image),
``package`` = upload the zip to S3 and capture its immutable object version as the
promotable reference, ``deploy`` = update the function code + move an alias (or
consume a pre-built reference under promotion), ``verify`` = wait + invoke smoke
test — and supplies the backing Terraform (Lambda, S3 artifact bucket, IAM/OIDC).
Secrets/account specifics are referenced, never valued; output is
deterministic. See :mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import AwsLambda


def get_plugin() -> AwsLambda:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return AwsLambda()


__all__ = ["AwsLambda", "get_plugin"]
