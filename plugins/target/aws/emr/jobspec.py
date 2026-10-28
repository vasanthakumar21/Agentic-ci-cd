"""The EMR Serverless job-driver config the emr ``build`` verb emits.

``deploy`` on aws-emr is *start an EMR Serverless job run (or submit an EMR step)
via the CLI/API* — there is NO container image and NO ECS task
definition. The ``aws emr-serverless start-job-run`` CLI takes a JSON
``--job-driver`` payload; this module supplies that JSON as a committed support file
so the ``deploy`` verb has a concrete spec to apply.

Why a committed emr-job.json
-----------------------------
The job driver is the EMR analogue of the glue job definition: it names the PySpark
entry-point script on S3 and the spark-submit parameters the run uses. Emitting it
as a file (rather than inlining a large JSON string in a shell command) keeps the
``deploy`` command readable and lets a client review/extend the spec in version
control.

Secrets by reference / determinism
----------------------------------------------
The script location and the extra-py-files bundle are ``settings``-resolved values
or shell-style ``$REF`` placeholders the CI step substitutes — never a literal
account id, ARN, or bucket owned by a specific account. The JSON is assembled from a
fixed, ordered template (``sort_keys`` + fixed indent) so regeneration is
byte-identical.
"""

from __future__ import annotations

import json


def render_job_spec(
    *,
    entry_point: str,
    extra_python_libs: str,
) -> str:
    """Render the EMR Serverless ``--job-driver`` JSON as deterministic text.

 Args:
 entry_point: the S3 URI of the uploaded PySpark entry-point script (value or
 ``$REF``) — never a literal account-specific bucket in the committed
 template.
 extra_python_libs: S3 URI of the extra python libs/wheel archive (value or
 ``$REF``); wired into ``--py-files`` so the job can import the built wheel.

 The spec runs a Spark job (``sparkSubmit``) that reads its entry point and extra
 libs from S3 — NO image. ``json.dumps`` with ``sort_keys`` + a fixed indent gives
 byte-stable output.
 """
    spec = {
        "sparkSubmit": {
            "entryPoint": entry_point,
            "sparkSubmitParameters": (
                f"--py-files {extra_python_libs} "
                "--conf spark.executor.cores=2 "
                "--conf spark.executor.memory=4g "
                "--conf spark.driver.cores=2 "
                "--conf spark.driver.memory=4g"
            ),
        }
    }
    # sort_keys=True + fixed indent => deterministic, review-friendly JSON.
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"
