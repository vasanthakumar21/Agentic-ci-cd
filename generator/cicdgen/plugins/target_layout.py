"""Target-plugin directory layout — a pure, data-only name->directory resolver.

Target plugins live on disk in a provider-nested tree::

 plugins/target/<provider>/<service>/ e.g. aws/ecs, azure/databricks, gcp/gke

but the IR still names a flat ``target_platform`` enum value (``aws-ecs``,
``databricks``, ``azure-functions``, ``kubernetes``, plus the newer
``<provider>-<service>`` entries). This module is the ONE place that maps an IR
value to its nested home — and it does so with **data, not branching**:

 *:data:`TARGET_ALIASES` is a reviewed lookup table mapping each *legacy flat*
 IR value to its ``(provider, service)`` directory. It holds no behaviour.
 *:func:`resolve_target_dir` is a pure function: a provider-prefixed name
 (``aws-``/``azure-``/``gcp-``) splits on the FIRST ``-``; any other name is
 looked up in :data:`TARGET_ALIASES`; an unknown name raises a clear error.

The loader (``cicdgen/plugins/loader.py``) calls :func:`resolve_target_dir` for
the target family and joins ``root / "target" / provider / service``. The loader
itself stays free of any vendor/platform literal — the only names it handles come
from the IR and this data table.
"""

from __future__ import annotations

#: Provider prefixes whose IR grammar is ``<provider>-<service>`` (the prefix rule).
#: Data only — used to decide whether to split a name or consult the alias table.
_PROVIDER_PREFIXES: tuple[str, ...] = ("aws-", "azure-", "gcp-")

#: Legacy / alias flat IR value -> (provider, service) nested directory.
#:
#: This is reviewed DATA, not control flow. It is the single place the flat
#: ``target_platform`` values are tied to their nested home.
TARGET_ALIASES: dict[str, tuple[str, str]] = {
    "aws-ecs": ("aws", "ecs"),
    "databricks": ("azure", "databricks"),  # flat name for the nested home
    "azure-functions": ("azure", "functions"),
    # Provider-neutral k8s alias -> the generic plugin's provider-neutral home.
    # This frees ``gcp/gke`` for the first-class native ``gcp-gke`` plugin
    # (resolved by the ``gcp-`` prefix rule, NOT by this alias table).
    "kubernetes": ("k8s", "generic"),
}


class UnknownTargetError(ValueError):
    """Raised when a ``target_platform`` name maps to no nested directory."""


def resolve_target_dir(name: str) -> tuple[str, str]:
    """Resolve an IR ``target_platform`` value to its ``(provider, service)`` dir.

 Pure function, no side effects, no vendor/platform branching:

 1. If ``name`` carries a provider prefix (``aws-``, ``azure-``, ``gcp-``),
 split on the FIRST ``-`` into ``(provider, service)``.
 2. Otherwise consult the data-only :data:`TARGET_ALIASES` table.
 3. An unknown name (no prefix, not in the table) raises
:class:`UnknownTargetError` naming the value and the known aliases.

 Args:
 name: the IR ``target_platform`` value (e.g. ``aws-ecs``, ``databricks``,
 ``gcp-cloud-run``).

 Returns:
 ``(provider, service)`` — the nested directory under ``plugins/target/``.

 Raises:
 UnknownTargetError: if ``name`` has no provider prefix and is absent from
:data:`TARGET_ALIASES`.
 """
    for prefix in _PROVIDER_PREFIXES:
        if name.startswith(prefix):
            provider, service = name.split("-", 1)
            return provider, service

    try:
        return TARGET_ALIASES[name]
    except KeyError:
        known = ", ".join(sorted(TARGET_ALIASES)) or "(none)"
        raise UnknownTargetError(
            f"target_platform '{name}' maps to no nested plugin directory: it has no "
            f"provider prefix (one of aws-/azure-/gcp-) and is not a known alias "
            f"[{known}]."
        ) from None


def ir_name_for_dir(provider: str, service: str) -> str:
    """Inverse of :func:`resolve_target_dir`: the IR name a nested dir reports.

 Pure, data-only. Used by discovery (``ir/plugins.py``) to enumerate the
 target plugins by their *reported* IR name rather than by their directory
 segments. A ``(provider, service)`` that is the home of a legacy alias
 reports that alias (e.g. ``k8s/generic`` -> ``kubernetes``); otherwise the name
 follows the ``<provider>-<service>`` grammar (so ``gcp/gke`` -> ``gcp-gke``, the
 first-class native plugin, NOT the legacy ``kubernetes`` alias).
 """
    for alias, dest in TARGET_ALIASES.items():
        if dest == (provider, service):
            return alias
    return f"{provider}-{service}"
