"""The AWS Glue job definition the glue ``build`` verb emits.

``deploy`` on aws-glue is *create/update a Glue job via the CLI/API* — there is NO container image and NO ECS task definition. The
``aws glue create-job`` / ``update-job`` CLI takes a JSON job definition; this
module supplies that JSON as a committed support file so the ``deploy`` verb has a
concrete spec to apply.

Why a committed glue-job.json
-----------------------------
The job definition is the glue analogue of the aws-ecs task definition: it names
the job, the Glue version, the worker sizing, and the S3 script location the job
runs. Emitting it as a file (rather than inlining a large JSON string in a shell
command) keeps the ``deploy`` command readable and lets a client review/extend the
spec in version control.

The spec is split into two top-level keys so it can drive BOTH CLI verbs from one
committed file without a second template:

 * ``Command`` / ``DefaultArguments`` / ``GlueVersion`` / ``WorkerType`` /
 ``NumberOfWorkers`` — the ``--job-command`` / ``--default-arguments`` /
 ``--glue-version`` etc. payload the create/update verb applies.
 * the job ``Name`` + the execution ``Role`` are referenced by
 ``settings``-resolved values or shell-style ``$REF`` placeholders.

Secrets by reference / determinism
----------------------------------------------
The script location, the IAM role, and the job name are ``settings``-resolved
values or ``$REF`` placeholders the CI step substitutes — never a literal account
id, ARN, or bucket owned by a specific account. The JSON is assembled from a fixed,
ordered template (``sort_keys`` + fixed indent) so regeneration is byte-identical.
"""

from __future__ import annotations

import json

#: Pinned Glue version for the job — a reproducible, non-"latest" default so the
#: committed spec does not drift when AWS ships a new Glue version.
_DEFAULT_GLUE_VERSION = "4.0"

#: Default worker type / count for a Spark ETL job. Documented, non-secret
#: constants a client overrides via ``settings`` / the job spec in review.
_DEFAULT_WORKER_TYPE = "G.1X"
_DEFAULT_NUMBER_OF_WORKERS = 2

#: Glue's own python-job command name — not account-specific, part of the API.
_GLUE_COMMAND_NAME = "glueetl"


def render_job_spec(
    *,
    job_name: str,
    role_ref: str,
    script_location: str,
    extra_python_libs: str,
) -> str:
    """Render the ``aws glue`` JSON job definition as deterministic text.

 Args:
 job_name: the job name ``deploy`` creates/updates (value or ``$REF``).
 role_ref: the Glue execution role ARN/name (value or ``$REF``) — never a
 literal account-specific ARN in the committed template.
 script_location: the S3 URI of the uploaded job script (value or ``$REF``).
 extra_python_libs: S3 URI(s) of extra python libs/wheels (value or ``$REF``);
 wired into ``--extra-py-files`` so the job can import the built wheel.

 The spec runs a Spark/Python ETL job (``glueetl``) that reads its script and
 extra libs from S3 — NO image. ``json.dumps`` with ``sort_keys`` + a fixed
 indent gives byte-stable output.
 """
    spec = {
        "Name": job_name,
        "Role": role_ref,
        "GlueVersion": _DEFAULT_GLUE_VERSION,
        "WorkerType": _DEFAULT_WORKER_TYPE,
        "NumberOfWorkers": _DEFAULT_NUMBER_OF_WORKERS,
        "Command": {
            "Name": _GLUE_COMMAND_NAME,
            "PythonVersion": "3",
            "ScriptLocation": script_location,
        },
        "DefaultArguments": {
            "--job-language": "python",
            "--extra-py-files": extra_python_libs,
            "--enable-metrics": "true",
            "--enable-continuous-cloudwatch-log": "true",
        },
    }
    # sort_keys=True + fixed indent => deterministic, review-friendly JSON.
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"
