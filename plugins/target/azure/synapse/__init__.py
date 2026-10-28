"""Azure Synapse target-platform plugin.

This package *is* the plugin: it exists on disk at ``plugins/target/azure/synapse/``
so the loader discovers it by name (``target_platform: azure-synapse`` in the IR —
resolved via the ``azure-`` prefix rule to ``azure/synapse``) with no core change. It exposes the concrete :class:`~.plugin.AzureSynapse` via the preferred
``get_plugin`` factory convention the loader looks for first.

azure-synapse is a **data-job shape** (like databricks / aws-glue): NO container
image, NO ACR, NO ECS. The plugin implements the six stage verbs for the
azure-synapse row of — ``build`` = assemble notebooks / Spark job defs
+ a wheel (``python -m build``), ``package`` = upload to the workspace ADLS (the
immutable blob version is the promotable reference), ``deploy`` = create/update a
Synapse Spark job definition via ``az synapse`` (consuming the pre-built ref under
promotion), ``verify`` = start + poll the Synapse job run to Succeeded — and supplies
the backing Terraform (an existing-workspace reference, a Spark pool, an ADLS data
lake, role assignments, and a CI federated credential). Secrets/account specifics are
referenced, never valued; output is deterministic. See :mod:`.plugin` for
details.
"""

from __future__ import annotations

from .plugin import AzureSynapse


def get_plugin() -> AzureSynapse:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return AzureSynapse()


__all__ = ["AzureSynapse", "get_plugin"]
