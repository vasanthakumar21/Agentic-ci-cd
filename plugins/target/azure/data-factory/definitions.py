"""The ADF pipeline + linked-service + dataset JSON the data-factory ``build`` emits.

, — ``build`` on azure-data-factory *renders/validates the ADF
pipeline + linked-service + dataset JSON (ARM/JSON definitions)* and ``deploy`` upserts
them via ``az datafactory`` — there is NO container image. This module supplies those
JSON definitions as committed support files so the ``build``/``deploy`` verbs have
concrete specs to validate and apply.

Why committed ADF JSON definitions
-----------------------------------
The pipeline / linked-service / dataset JSON are the ADF analogues of a task
definition: emitting them as files (rather than inlining large JSON strings in a
shell command) keeps the verbs readable and lets a client review/extend the specs in
version control.

Secrets by reference / determinism
----------------------------------------------
Linked services reference their storage account by ``settings``-resolved values or
shell-style ``$REF`` placeholders the CI step substitutes — never a literal
connection string, account key, or token (the linked service uses a managed-identity
/ AAD reference, not an embedded credential). The JSON is assembled from a fixed,
ordered template (``sort_keys`` + fixed indent) so regeneration is byte-identical.
"""

from __future__ import annotations

import json


def render_linked_service(*, name: str, storage_account: str) -> str:
    """Render the ADLS/Blob linked-service JSON (managed-identity auth, no key).

 Args:
 name: the linked-service name.
 storage_account: the backing storage account (value or ``$REF``) — wired by
 its blob endpoint URL, NOT by a connection string or key. ADF uses
 its own managed identity to authenticate.

 ``json.dumps`` with ``sort_keys`` + a fixed indent gives byte-stable output.
 """
    spec = {
        "name": name,
        "properties": {
            "type": "AzureBlobFS",
            "typeProperties": {
                # Endpoint URL only — ADF authenticates with its managed identity, so
                # no connection string / key is committed.
                "url": f"https://{storage_account}.dfs.core.windows.net",
            },
        },
    }
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def render_dataset(*, name: str, linked_service_name: str, filesystem: str) -> str:
    """Render a parameterised ADLS dataset JSON bound to ``linked_service_name``.

 Args:
 name: the dataset name.
 linked_service_name: the linked service the dataset reads/writes through.
 filesystem: the ADLS filesystem/container (value or ``$REF``).

 Deterministic JSON.
 """
    spec = {
        "name": name,
        "properties": {
            "type": "Parquet",
            "linkedServiceName": {
                "referenceName": linked_service_name,
                "type": "LinkedServiceReference",
            },
            "typeProperties": {
                "location": {
                    "type": "AzureBlobFSLocation",
                    "fileSystem": filesystem,
                },
            },
        },
    }
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def render_pipeline(*, name: str, dataset_name: str) -> str:
    """Render the ADF pipeline JSON (a copy activity over ``dataset_name``).

 Args:
 name: the pipeline name ``deploy`` upserts / ``verify`` triggers.
 dataset_name: the dataset the pipeline's copy activity reads/writes.

 The pipeline is a minimal, reviewable default (one copy activity) a client
 extends. Deterministic JSON.
 """
    spec = {
        "name": name,
        "properties": {
            "activities": [
                {
                    "name": "Copy",
                    "type": "Copy",
                    "inputs": [
                        {
                            "referenceName": dataset_name,
                            "type": "DatasetReference",
                        }
                    ],
                    "outputs": [
                        {
                            "referenceName": dataset_name,
                            "type": "DatasetReference",
                        }
                    ],
                    "typeProperties": {
                        "source": {"type": "ParquetSource"},
                        "sink": {"type": "ParquetSink"},
                    },
                }
            ],
        },
    }
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"
