"""GitLab CI-vendor plugin.

This package *is* the plugin: it exists on disk at ``plugins/ci/gitlab/`` so the
loader discovers it by name (``ci_vendor: gitlab`` in the IR) with no core change. It exposes the concrete :class:`~.plugin.GitLab` via the preferred
``get_plugin`` factory convention the loader looks for first.

The plugin renders the vendor-neutral :class:`StageGraph` into a GitLab CI file
(``.gitlab-ci.yml``) — orchestration only (stage order via a top-level
``stages:`` list + each job's ``stage:`` key, manual gates via ``when: manual``,
approvals via protected environments, timeouts, scans, cloud OIDC via
``id_tokens:`` + assume-role). It holds **no** target-platform business logic:
the shell command for each stage comes from ``stage.runs``. See
:mod:`.plugin` for the mapping details.
"""

from __future__ import annotations

from .plugin import GitLab


def get_plugin() -> GitLab:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return GitLab()


__all__ = ["GitLab", "get_plugin"]
