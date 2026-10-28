"""GCP Cloud Run target-platform plugin.

This package *is* the plugin: it exists on disk at ``plugins/target/gcp/cloud-run/``
so the loader discovers it by name (``target_platform: gcp-cloud-run`` in the IR)
with no core change — the ``gcp-`` prefix rule resolves ``gcp-cloud-run`` to the
``gcp/cloud-run`` directory, splitting on the FIRST ``-`` so the multi-hyphen
service name ``cloud-run`` is preserved. It exposes the concrete
:class:`~.plugin.GcpCloudRun` via the preferred ``get_plugin`` factory convention
the loader looks for first.

The plugin implements the six stage verbs for the gcp-cloud-run row of
— ``build`` = ``docker build`` (emitting a resilient, pinned, apt-mirror-free
Dockerfile,), ``package`` = push to Google Artifact Registry (immutable digest
as the promotable reference), ``deploy`` = ``gcloud run deploy`` consuming the
pre-built image, ``verify`` = poll the new revision's Ready condition — and supplies
the backing Terraform (Cloud Run + Artifact Registry + a deploy service account +
Workload Identity Federation pool/provider). Secrets/project specifics are
referenced, never valued; output is deterministic. See :mod:`.plugin`
for details.
"""

from __future__ import annotations

from .plugin import GcpCloudRun


def get_plugin() -> GcpCloudRun:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return GcpCloudRun()


__all__ = ["GcpCloudRun", "get_plugin"]
