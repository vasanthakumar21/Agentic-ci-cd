"""GitHub Actions CI-vendor plugin.

This package *is* the plugin: it exists on disk at ``plugins/ci/github/`` so the
loader discovers it by name (``ci_vendor: github`` in the IR) with no core change. It exposes the concrete :class:`~.plugin.GitHubActions` via the preferred
``get_plugin`` factory convention the loader looks for first.

The plugin renders the vendor-neutral :class:`StageGraph` into a GitHub Actions
workflow (``.github/workflows/ci.yml``) — orchestration only (stage order, gates,
approvals, timeouts, scans, cloud OIDC). It holds **no** target-platform business
logic: the shell command for each stage comes from ``stage.runs`` (what the verbs
*do* is the target plugin's job,). See :mod:`.plugin` for the mapping details.
"""

from __future__ import annotations

from .plugin import GitHubActions


def get_plugin() -> GitHubActions:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return GitHubActions()


__all__ = ["GitHubActions", "get_plugin"]
