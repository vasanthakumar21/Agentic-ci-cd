"""GCP Dataflow target-platform plugin.

This package *is* the plugin: it exists on disk at ``plugins/target/gcp/dataflow/``
so the loader discovers it by name (``target_platform: gcp-dataflow`` in the IR —
resolved via the ``gcp-`` prefix rule to ``gcp/dataflow``) with no core change. It exposes the concrete :class:`~.plugin.GcpDataflow` via the preferred
``get_plugin`` factory convention the loader looks for first.

gcp-dataflow is a **data-job shape** like aws-glue/databricks: there
is NO required container image, NO Artifact Registry, NO Cloud Run. ``build``
assembles the Beam pipeline + stages a (classic) template, ``package`` stages the
template + artifacts to a GCS bucket (capturing an immutable object generation as
the promotable reference), ``deploy`` launches a Dataflow job from the staged
template via the CLI, and ``verify`` polls the job state to JOB_STATE_DONE. The
backing Terraform provisions a GCS bucket, a Dataflow worker SA, a deploy service
account, and a Workload Identity Federation pool/provider for keyless CI. Secrets/
project specifics are referenced, never valued; output is deterministic. See :mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import GcpDataflow


def get_plugin() -> GcpDataflow:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return GcpDataflow()


__all__ = ["GcpDataflow", "get_plugin"]
