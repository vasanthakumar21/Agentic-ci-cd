"""Databricks target-platform plugin.

This package *is* the plugin: it lives on disk at ``plugins/target/databricks/`` so
the loader discovers it by name (``target_platform: databricks`` in the IR) with no
core change. It exposes the concrete :class:`~.plugin.Databricks` via the
preferred ``get_plugin`` factory convention the loader looks for first.

Databricks is the **different-shape proof**: the shape MOST unlike
aws-ecs. There is NO container image, NO ECR, NO ECS. ``build`` assembles a wheel /
notebooks (``python -m build``), ``package`` uploads to DBFS/Volumes, ``deploy``
creates/updates a Databricks *job* via the CLI/API, and ``verify`` runs the job and
checks its run state. The backing Terraform provisions a ``databricks_job`` + Unity
Catalog schema. Secrets (workspace host/token) are referenced as
``$DATABRICKS_HOST`` / ``$DATABRICKS_TOKEN``, never valued; output is
deterministic. See :mod:`.plugin` for details.
"""

from __future__ import annotations

from .plugin import Databricks


def get_plugin() -> Databricks:
    """Factory the loader calls (convention #1) to obtain the plugin instance."""
    return Databricks()


__all__ = ["Databricks", "get_plugin"]
