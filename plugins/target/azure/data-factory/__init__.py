"""Azure Data Factory target-platform plugin.

This package *is* the plugin: it exists on disk at
``plugins/target/azure/data-factory/`` so the loader discovers it by name
(``target_platform: azure-data-factory`` in the IR) with no core change — the
``azure-`` prefix rule resolves ``azure-data-factory`` to the ``azure/data-factory``
directory, splitting on the FIRST ``-`` so the multi-hyphen service name
``data-factory`` is preserved. It exposes the concrete
:class:`~.plugin.AzureDataFactory` via the preferred ``get_plugin`` factory
convention the loader looks for first.

azure-data-factory is a **data-job shape** (like databricks / aws-glue): NO container
image, NO ACR, NO ECS. The plugin implements the six stage verbs for the
azure-data-factory row of — ``build`` = assemble/validate the ADF
pipeline + linked-service + dataset JSON, ``package`` = upload the definitions to
storage (the immutable blob version is the promotable reference), ``deploy`` =
``az datafactory pipeline create`` (upsert the pipeline, consuming the pre-built ref
under promotion), ``verify`` = ``az datafactory pipeline create-run`` + poll the run
to Succeeded — and supplies the backing Terraform (the Data Factory, a pipeline, role
assignments, an ADLS data lake, and a CI federated credential). Secrets/account
specifics are referenced, never valued; output is deterministic. See
:mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import AzureDataFactory


def get_plugin() -> AzureDataFactory:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return AzureDataFactory()


__all__ = ["AzureDataFactory", "get_plugin"]
