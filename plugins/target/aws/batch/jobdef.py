"""The AWS Batch job definition the batch ``build`` verb emits.

``deploy`` on aws-batch is *register/update an AWS Batch job definition via the
CLI/API* pointing at the pushed ECR image. The ``aws batch
register-job-definition`` CLI takes a JSON job definition; this module supplies that
JSON as a committed support file so the ``deploy`` verb has a concrete definition to
apply.

Why a committed batch-job-definition.json
------------------------------------------
The job definition is the Batch analogue of an ECS task definition: it names the
container image, the vCPU/memory, the execution role, and the log configuration.
Emitting it as a file keeps the ``deploy`` command readable and lets a client
review/extend the definition in version control. The ``deploy`` verb rewrites the
``image`` field to the deployable reference at run time, so the committed value is
only a by-reference placeholder.

Secrets by reference / determinism
----------------------------------------------
The image, the execution role, and names are ``settings``-resolved values or
``$REF`` placeholders — never a literal account id, ARN, or repo owned by a specific
account. ``json.dumps`` with ``sort_keys`` + a fixed indent gives byte-stable output.
"""

from __future__ import annotations

import json


def render_job_definition(
    *,
    job_definition_name: str,
    image_ref: str,
    execution_role_ref: str,
    job_vcpu: str = "1",
    job_memory: str = "2048",
) -> str:
    """Render the ``aws batch register-job-definition`` JSON as deterministic text.

 Args:
 job_definition_name: the job-definition name ``deploy`` registers (value or
 ``$REF``).
 image_ref: the container image reference (value or ``$REF``) — the deploy
 verb rewrites this to the immutable digest at run time.
 execution_role_ref: the Batch/ECS execution role (value or ``$REF``).
 job_vcpu: vCPU per job container (non-secret default).
 job_memory: memory (MiB) per job container (non-secret default).
 """
    spec = {
        "jobDefinitionName": job_definition_name,
        "type": "container",
        "platformCapabilities": ["FARGATE"],
        "containerProperties": {
            "image": image_ref,
            "executionRoleArn": execution_role_ref,
            "resourceRequirements": [
                {"type": "VCPU", "value": job_vcpu},
                {"type": "MEMORY", "value": job_memory},
            ],
            "networkConfiguration": {"assignPublicIp": "ENABLED"},
        },
    }
    # sort_keys=True + fixed indent => deterministic, review-friendly JSON.
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"
