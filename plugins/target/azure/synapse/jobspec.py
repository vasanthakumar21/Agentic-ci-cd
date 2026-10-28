"""The Synapse Spark job definition the synapse ``build`` verb emits (23.6,).

``deploy`` on azure-synapse is *create/update a Synapse Spark job definition (or
pipeline) via the ``az synapse`` CLI* — there is NO container image.
The ``az synapse spark-job-definition create/update`` CLI takes a JSON job
definition; this module supplies that JSON as a committed support file so the
``deploy`` verb has a concrete spec to apply.

Why a committed spark-job-definition.json
-----------------------------------------
The job definition is the Synapse analogue of a Databricks job.json: it names the
job, the Spark pool, and the artifact (a wheel / main definition file) to run.
Emitting it as a file (rather than inlining a large JSON string in a shell command)
keeps the ``deploy`` command readable and lets a client review/extend the spec in
version control.

Secrets by reference / determinism
----------------------------------------------
The artifact location and the names are ``settings``-resolved values or shell-style
``$REF`` placeholders the CI step substitutes — never a literal workspace URL,
storage account key, or token. The JSON is assembled from a fixed, ordered template
(``sort_keys`` + fixed indent) so regeneration is byte-identical.
"""

from __future__ import annotations

import json

#: Pinned Synapse Spark runtime for the job (reproducible, non-"latest").
_DEFAULT_SPARK_VERSION = "3.4"

#: Default executor sizing kept as documented, non-secret constants.
_DEFAULT_EXECUTOR_COUNT = 2
_DEFAULT_EXECUTOR_CORES = 4
_DEFAULT_EXECUTOR_MEMORY = "28g"
_DEFAULT_DRIVER_CORES = 4
_DEFAULT_DRIVER_MEMORY = "28g"


def render_job_spec(
    *,
    job_name: str,
    spark_pool: str,
    main_definition_file: str,
    extra_wheel: str,
) -> str:
    """Render the ``az synapse spark-job-definition`` JSON as deterministic text.

 Args:
 job_name: the Spark job definition name ``deploy`` creates/updates (value or
 ``$REF``).
 spark_pool: the Synapse Spark pool the job runs on (value or ``$REF``).
 main_definition_file: the ADLS/abfss URI of the uploaded main PySpark file
 (value or ``$REF``).
 extra_wheel: the ADLS/abfss URI of the uploaded wheel added to pyFiles (value
 or ``$REF``).

 The spec runs a PySpark job on a Synapse Spark pool reading its main file + extra
 wheel from workspace storage (ADLS) — NO image. ``json.dumps`` with ``sort_keys``
 + a fixed indent gives byte-stable output.
 """
    spec = {
        "name": job_name,
        "properties": {
            "targetBigDataPool": {
                "referenceName": spark_pool,
                "type": "BigDataPoolReference",
            },
            "language": "python",
            "sparkVersion": _DEFAULT_SPARK_VERSION,
            "jobProperties": {
                "name": job_name,
                "file": main_definition_file,
                "pyFiles": [extra_wheel],
                "numExecutors": _DEFAULT_EXECUTOR_COUNT,
                "executorCores": _DEFAULT_EXECUTOR_CORES,
                "executorMemory": _DEFAULT_EXECUTOR_MEMORY,
                "driverCores": _DEFAULT_DRIVER_CORES,
                "driverMemory": _DEFAULT_DRIVER_MEMORY,
            },
        },
    }
    # sort_keys=True + fixed indent => deterministic, review-friendly JSON.
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"
