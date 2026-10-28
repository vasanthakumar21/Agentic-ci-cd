"""Plugin-existence validation — a separate layer from IR shape validation.

The IR names a ``ci_vendor`` and a ``target_platform`` independently. A valid IR
is not generatable unless the matching provider plugins exist on disk:

 plugins/ci/<ci_vendor>/
 plugins/target/<target_platform>/

This module confirms those directories exist and produces a clear error naming
the missing plugin. It is kept out of the Pydantic models on purpose:

 * IR *shape* can be validated (and unit-tested) independently of the plugins.
 * the available plugin set is discovered *dynamically* from the filesystem,
 not hard-coded, so adding a plugin never touches the core.
 * the discovery root is injectable, and an explicit known-plugin set can be
 supplied, so tests can exercise the check deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Repo root =.../agentic-cicd-generator (this file is generator/cicdgen/ir/plugins.py).
_DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[3]


class PluginNotFoundError(ValueError):
    """Raised when a named ci_vendor or target_platform plugin does not exist."""


@dataclass(frozen=True)
class AvailablePlugins:
    """The set of plugins known to be available, by family.

 ``ci`` and ``target`` are sets of plugin names (directory names). Build one
 by scanning the filesystem with :meth:`discover`, or construct directly in
 tests to inject a deterministic known set.
 """

    ci: frozenset[str] = field(default_factory=frozenset)
    target: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def discover(cls, repo_root: Path | str | None = None) -> AvailablePlugins:
        """Scan ``plugins/ci/`` and ``plugins/target/`` for plugin directories.

 A plugin is any sub-directory of the family folder. Missing family
 folders simply yield an empty set rather than an error — the per-axis
 check below is what turns "no such plugin" into a precise message.
 """
        root = Path(repo_root) if repo_root is not None else _DEFAULT_REPO_ROOT
        return cls(
            ci=_scan_family(root / "plugins" / "ci"),
            target=_scan_target_family(root / "plugins" / "target"),
        )


def _scan_family(family_dir: Path) -> frozenset[str]:
    """FLAT scan — each sub-directory is a plugin named by the directory (ci/)."""
    if not family_dir.is_dir():
        return frozenset()
    return frozenset(p.name for p in family_dir.iterdir() if p.is_dir())


def _scan_target_family(target_dir: Path) -> frozenset[str]:
    """NESTED scan — target plugins live under ``<provider>/<service>/``.

 Each ``provider/service`` leaf is enumerated by its *reported* IR name, so
 ``AvailablePlugins.target`` speaks the flat ``target_platform`` values
 (``aws-ecs``, ``databricks``, …) the rest of the core and the IR use. The
 dir→name mapping is the data-only inverse in
:mod:`cicdgen.plugins.target_layout` (imported lazily to avoid an import cycle
 between ``cicdgen.ir`` and ``cicdgen.plugins``).
 """
    if not target_dir.is_dir():
        return frozenset()

    # Local import keeps cicdgen.ir importable without eagerly pulling in the
    # plugins package (the mapping is data, resolved only when scanning).
    from cicdgen.plugins.target_layout import ir_name_for_dir

    names: set[str] = set()
    for provider_dir in target_dir.iterdir():
        if not provider_dir.is_dir():
            continue
        for service_dir in provider_dir.iterdir():
            if not service_dir.is_dir():
                continue
            names.add(ir_name_for_dir(provider_dir.name, service_dir.name))
    return frozenset(names)


def check_plugins_exist(
    ci_vendor: str,
    target_platform: str,
    available: AvailablePlugins | None = None,
    *,
    repo_root: Path | str | None = None,
) -> None:
    """Confirm both named plugins exist; raise naming the missing one.

 Args:
 ci_vendor: the IR's ``ci_vendor`` (a plugin name under ``plugins/ci/``).
 target_platform: the IR's ``target_platform`` (under ``plugins/target/``).
 available: an explicit known-plugin set. When ``None``, plugins are
 discovered from the filesystem (optionally under ``repo_root``).
 repo_root: discovery root; only used when ``available`` is ``None``.

 Raises:
 PluginNotFoundError: if either plugin directory is absent, with a
 message naming the missing plugin, its family, and where it was
 looked for.
 """
    if available is None:
        available = AvailablePlugins.discover(repo_root)

    if ci_vendor not in available.ci:
        raise PluginNotFoundError(
            f"ci_vendor '{ci_vendor}' has no plugin. Expected a directory at "
            f"plugins/ci/{ci_vendor}/. Available ci plugins: "
            f"{sorted(available.ci) or '(none authored yet)'}."
        )

    if target_platform not in available.target:
        raise PluginNotFoundError(
            f"target_platform '{target_platform}' has no plugin. Expected a directory at "
            f"plugins/target/{target_platform}/. Available target plugins: "
            f"{sorted(available.target) or '(none authored yet)'}."
        )
