"""The Dataproc batch/job spec the gcp-dataproc ``build`` verb emits.

``deploy`` on gcp-dataproc is *submit a Dataproc job/batch via the CLI* — there is NO container image. The ``gcloud dataproc batches submit pyspark``
CLI accepts a batch config JSON; this module supplies that JSON as a committed
support file so the ``deploy`` verb has a concrete spec a client can review/extend.

Why a committed dataproc-batch.json
------------------------------------
The batch config is the Dataproc analogue of the aws-glue job definition: it names
the main PySpark file, any extra python files (the built wheel/libs), and the
runtime version. Emitting it as a file (rather than inlining a large JSON string in
a shell command) keeps the ``deploy`` command readable and version-controllable.

Secrets by reference / determinism
----------------------------------------------
The main-python-file-uri, the extra-python-file-uris and the staging bucket are
``settings``-resolved values or ``$REF`` placeholders the CI step substitutes —
never a literal GCP project id or bucket owned by a specific account. The JSON is
assembled from a fixed, ordered template (``sort_keys`` + fixed indent) so
regeneration is byte-identical.
"""

from __future__ import annotations

import json

#: Pinned Dataproc Serverless runtime version — a reproducible, non-"latest" default
#: so the committed spec does not drift when GCP ships a new runtime.
_DEFAULT_RUNTIME_VERSION = "2.2"


def render_batch_spec(
    *,
    main_python_file_uri: str,
    extra_python_file_uri: str,
    staging_bucket: str,
) -> str:
    """Render the ``gcloud dataproc batches`` PySpark batch config as deterministic text.

 Args:
 main_python_file_uri: GCS URI of the main PySpark script (value or ``$REF``).
 extra_python_file_uri: GCS URI of the extra python libs/wheel archive
 (value or ``$REF``); wired into ``pythonFileUris`` so the batch can import
 the built wheel.
 staging_bucket: the GCS staging bucket the batch uses (value or ``$REF``) —
 never a literal project-owned bucket in the committed template.

 The spec runs a PySpark batch on Dataproc Serverless — NO image. ``json.dumps``
 with ``sort_keys`` + a fixed indent gives byte-stable output.
 """
    spec = {
        "pysparkBatch": {
            "mainPythonFileUri": main_python_file_uri,
            "pythonFileUris": [extra_python_file_uri],
        },
        "runtimeConfig": {
            "version": _DEFAULT_RUNTIME_VERSION,
        },
        "environmentConfig": {
            "executionConfig": {
                "stagingBucket": staging_bucket,
            },
        },
    }
    # sort_keys=True + fixed indent => deterministic, review-friendly JSON.
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"
