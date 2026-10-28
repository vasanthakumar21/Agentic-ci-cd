"""AWS Step Functions target-platform plugin.

This package *is* the plugin: it exists on disk at ``plugins/target/aws/
stepfunctions/`` so the loader discovers it by name (``target_platform:
aws-stepfunctions`` in the IR — resolved via the ``aws-`` prefix rule to
``aws/stepfunctions``) with no core change. It exposes the concrete
:class:`~.plugin.AwsStepFunctions` via the preferred ``get_plugin`` factory
convention the loader looks for first.

aws-stepfunctions is a **state-machine orchestration shape**: there
is NO container image, NO ECR, NO ECS. ``build`` assembles/validates the Amazon
States Language (ASL) state-machine definition + any task code, ``package`` uploads
the definition/artifacts to S3 (capturing an immutable object version as the
promotable reference), ``deploy`` creates/updates the state machine via the CLI, and
``verify`` starts an execution and polls its status to SUCCEEDED. The backing
Terraform provisions an ``aws_sfn_state_machine`` + IAM role, a versioned S3 bucket,
and a GitHub/Azure OIDC deploy role. Secrets/account specifics are referenced, never
valued; output is deterministic. See :mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import AwsStepFunctions


def get_plugin() -> AwsStepFunctions:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return AwsStepFunctions()


__all__ = ["AwsStepFunctions", "get_plugin"]
