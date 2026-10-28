"""GCP Dataproc target-platform plugin.

This package *is* the plugin: it exists on disk at ``plugins/target/gcp/dataproc/``
so the loader discovers it by name (``target_platform: gcp-dataproc`` in the IR —
resolved via the ``gcp-`` prefix rule to ``gcp/dataproc``) with no core change. It exposes the concrete :class:`~.plugin.GcpDataproc` via the preferred
``get_plugin`` factory convention the loader looks for first.

gcp-dataproc is a **data-job shape** like aws-glue/databricks: there
is NO container image, NO Artifact Registry, NO Cloud Run. ``build`` assembles the
PySpark job script(s) + extra python libs/wheel, ``package`` uploads them to a GCS
staging bucket (capturing an immutable object generation as the promotable
reference), ``deploy`` submits a Dataproc Serverless PySpark batch via the CLI, and
``verify`` polls the batch state to SUCCEEDED. The backing Terraform provisions a
Dataproc batch + runtime SA, a versioned GCS staging bucket, a deploy service
account, and a Workload Identity Federation pool/provider for keyless CI. Secrets/
project specifics are referenced, never valued; output is deterministic. See :mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import GcpDataproc


def get_plugin() -> GcpDataproc:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return GcpDataproc()


__all__ = ["GcpDataproc", "get_plugin"]
