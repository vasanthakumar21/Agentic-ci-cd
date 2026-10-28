"""Jenkins CI-vendor plugin.

This package *is* the plugin: it exists on disk at ``plugins/ci/jenkins/`` so the
loader discovers it by name (``ci_vendor: jenkins`` in the IR) with no core change — adding this plugin touches nothing in the core. It exposes the concrete
:class:`~.plugin.Jenkins` via the preferred ``get_plugin`` factory convention the
loader looks for first.

The plugin renders the vendor-neutral :class:`StageGraph` into a single declarative
``Jenkinsfile`` (a Groovy pipeline) — orchestration only (stage order, manual gates,
approvals, timeouts, scans, cloud OIDC). It holds **no** target-platform business
logic: the shell command for each stage comes from ``stage.runs`` (what the verbs
*do* is the target plugin's job,). See :mod:`.plugin` for the mapping details.

Unlike the GitHub / Azure DevOps plugins, a Jenkinsfile is **Groovy, not YAML**, so
this plugin does *not* use a YAML emitter — it builds the declarative pipeline text
with a small, deterministic Groovy string builder (fixed indentation, stable
ordering, no timestamps).
"""

from __future__ import annotations

from .plugin import Jenkins


def get_plugin() -> Jenkins:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return Jenkins()


__all__ = ["Jenkins", "get_plugin"]
