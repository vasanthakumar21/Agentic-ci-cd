"""Bitbucket Pipelines CI-vendor plugin.

This package *is* the plugin: it exists on disk at ``plugins/ci/bitbucket/`` so
the loader discovers it by name (``ci_vendor: bitbucket`` in the IR) with no core
change — the whole point of: adding a plugin never touches the
core. It exposes the concrete :class:`~.plugin.Bitbucket` via the preferred
``get_plugin`` factory convention the loader looks for first.

The plugin renders the vendor-neutral :class:`StageGraph` into a Bitbucket
Pipelines file (``bitbucket-pipelines.yml``) — orchestration only (stage order
via an ordered ``- step:`` sequence, manual gates via ``trigger: manual``,
approvals via environment ``deployment:`` permissions, timeouts via ``max-time``,
security scans, cloud OIDC by reference). It holds **no** target-platform
business logic: the shell command for each stage comes from ``stage.runs`` (what
the verbs *do* is the target plugin's job,). See :mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import Bitbucket


def get_plugin() -> Bitbucket:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return Bitbucket()


__all__ = ["Bitbucket", "get_plugin"]
