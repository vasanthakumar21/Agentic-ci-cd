"""The Databricks job spec the databricks ``build`` verb emits.

``deploy`` on databricks is *create/update a job via CLI/API* — there
is NO container image and NO ECS task definition. The ``databricks jobs reset`` /
``create`` CLI takes a JSON job definition; this module supplies that JSON as a
committed support file so the ``deploy`` verb has a concrete spec to apply.

Why a committed job.json
------------------------
The job definition is the databricks analogue of the aws-ecs task definition: it
names the task, the cluster, and the artifact to run. Emitting it as a file (rather
than inlining a huge JSON string in a shell command) keeps the ``deploy`` command
readable and lets a client review/extend the spec in version control.

Secrets by reference / determinism
----------------------------------------------
The spec references the artifact and names by ``settings``-resolved values or by
shell-style ``$REF`` placeholders the CI step substitutes (``envsubst`` /
``sed``) — never a literal workspace URL or token. The JSON is assembled from a
fixed, ordered template so regeneration is byte-identical.
"""

from __future__ import annotations

import json

#: Pinned Databricks runtime for the job cluster (reproducible, non-"latest").
_DEFAULT_SPARK_VERSION = "15.4.x-scala2.12"
_DEFAULT_NODE_TYPE = "i3.xlarge"


def render_job_spec(
    *,
    job_name: str,
    package_name: str,
    entry_point: str,
    artifact_ref: str,
) -> str:
    """Render the ``databricks jobs`` JSON definition as deterministic text.

 Args:
 job_name: the job name ``deploy`` creates/updates (value or ``$REF``).
 package_name: the installed wheel package the task invokes.
 entry_point: the wheel entry point.
 artifact_ref: the uploaded artifact path (DBFS/Volume) or a ``$REF``.

 The spec runs a python wheel task on an ephemeral job cluster — NO image. The
 artifact is installed as a library from ``artifact_ref``. ``json.dumps`` with
 ``sort_keys`` + fixed indent gives byte-stable output.
 """
    spec = {
        "name": job_name,
        "job_clusters": [
            {
                "job_cluster_key": "default",
                "new_cluster": {
                    "spark_version": _DEFAULT_SPARK_VERSION,
                    "node_type_id": _DEFAULT_NODE_TYPE,
                    "num_workers": 2,
                },
            }
        ],
        "tasks": [
            {
                "task_key": "main",
                "job_cluster_key": "default",
                "python_wheel_task": {
                    "package_name": package_name,
                    "entry_point": entry_point,
                },
                "libraries": [{"whl": artifact_ref}],
            }
        ],
    }
    # sort_keys=True + fixed indent => deterministic, review-friendly JSON.
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"
