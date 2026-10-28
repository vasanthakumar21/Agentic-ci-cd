"""GCP GKE target-platform plugin.

This package *is* the plugin: it exists on disk at ``plugins/target/gcp/gke/`` so
the loader discovers it by name (``target_platform: gcp-gke`` in the IR) with no
core change — the ``gcp-`` prefix rule resolves ``gcp-gke`` to the ``gcp/gke``
directory, splitting on the FIRST ``-``. (The legacy generic ``kubernetes``
plugin used to live here; it was relocated to the provider-neutral
``plugins/target/k8s/generic/`` by so this directory is free for the
first-class native GKE plugin — see note.) It exposes the concrete
:class:`~.plugin.GcpGke` via the preferred ``get_plugin`` factory convention the
loader looks for first.

The plugin implements the six stage verbs for the gcp-gke row of —
``build`` = ``docker build`` (emitting a resilient, pinned, apt-mirror-free
Dockerfile,), ``package`` = push to Google Artifact Registry (immutable digest
as the promotable reference), ``deploy`` = ``gcloud container clusters
get-credentials`` then ``kubectl apply`` / ``helm upgrade`` consuming the pre-built
image, ``verify`` = ``kubectl rollout status`` — and supplies the backing Terraform
(a GKE cluster reference + Artifact Registry + a deploy service account + GKE
Workload Identity binding + a Workload Identity Federation pool/provider) alongside
k8s manifests. It is GKE-specific (gcloud get-credentials, Artifact Registry, GKE
Workload Identity), NOT a copy of the generic kubernetes plugin. Secrets/project
specifics are referenced, never valued; output is deterministic. See
:mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import GcpGke


def get_plugin() -> GcpGke:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return GcpGke()


__all__ = ["GcpGke", "get_plugin"]
