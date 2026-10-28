"""Dynamic plugin loader — turn a plugin *name* into a concrete plugin instance.

The IR names a ``ci_vendor`` and a ``target_platform`` independently; each names a
directory under::

 plugins/ci/<ci_vendor>/
 plugins/target/<target_platform>/

This module imports the package at that directory and hands back a concrete
:class:`~cicdgen.plugins.base.CIVendorPlugin` /
:class:`~cicdgen.plugins.base.TargetPlatformPlugin` instance — with **NO**
hard-coded vendor/platform knowledge. There is not a single
``if name == "github"`` / ``if name == "aws-ecs"`` branch anywhere in the core;
everything is driven by the name the IR supplies and what the plugin package
exposes.

The plugin-exposure convention
-------------------------------
A plugin package (the directory under ``plugins/<family>/<name>/``, importable
because it contains an ``__init__.py``) exposes its concrete class in one of two
ways, tried in this order:

1. **Factory (preferred).** A module-level ``get_plugin`` callable that returns
 an *instance* of the right base class. This is the explicit, unambiguous
 contract and the one the plugin-authoring guide recommends::

 # plugins/ci/github/__init__.py
 from.plugin import GitHubActions
 def get_plugin -> GitHubActions:
 return GitHubActions

2. **Subclass discovery (fallback).** If there is no ``get_plugin``, the loader
 scans the package's public attributes for exactly one concrete subclass of the
 expected family base class and instantiates it with no arguments. This keeps a
 trivial plugin boilerplate-free, while the factory remains available for
 plugins that need construction arguments.

Either way the loaded plugin's :attr:`~cicdgen.plugins.base.ProviderPlugin.name`
MUST equal the directory / IR name, tying the IR value, the on-disk folder, and
the loaded class together (see ``base.py`` module docstring).

Testability
-----------
The loader is decoupled from the on-disk ``plugins/`` tree so it can be
unit-tested with in-memory plugins:

 * ``plugins_root`` is injectable — point it at a tmp dir of fake plugin
 packages on disk; or
 * ``registry`` is injectable — a mapping of ``(family, name) -> plugin`` (or a
 zero-arg factory) that bypasses the filesystem entirely.

Clear failures
--------------
Every failure mode raises :class:`PluginLoadError` with a message naming the
plugin, its family, and what went wrong: directory missing, package not
importable, no exposed class, the wrong base class, an ambiguous set of
candidates, a ``name`` that disagrees with the directory, or a declared
:attr:`~cicdgen.plugins.base.ProviderPlugin.plugin_api_version` the core does not
support (a plugin built against an incompatible contract fails loudly instead of
silently misbehaving).
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import TypeVar

from cicdgen.plugins.base import (
    SUPPORTED_PLUGIN_API,
    CIVendorPlugin,
    PluginFamily,
    ProviderPlugin,
    TargetPlatformPlugin,
)
from cicdgen.plugins.target_layout import resolve_target_dir

# Repo root =.../agentic-cicd-generator (this file is generator/cicdgen/plugins/loader.py).
_DEFAULT_PLUGINS_ROOT = Path(__file__).resolve().parents[3] / "plugins"

#: The module-level factory a plugin package may expose (convention #1).
FACTORY_ATTR = "get_plugin"

_P = TypeVar("_P", bound=ProviderPlugin)

#: A registry entry is either a ready plugin instance or a zero-arg factory.
RegistryValue = ProviderPlugin | Callable[[], ProviderPlugin]
#: An injectable registry keyed by (family, name) — bypasses the filesystem.
Registry = Mapping[tuple[PluginFamily, str], RegistryValue]


class PluginLoadError(RuntimeError):
    """Raised when a named plugin cannot be loaded into a valid instance.

 Covers: missing directory, un-importable package, no exposed plugin class,
 wrong base class, ambiguous candidates, or a name/directory mismatch.
 """


def load_plugin(
    family: PluginFamily,
    name: str,
    *,
    plugins_root: Path | str | None = None,
    registry: Registry | None = None,
) -> ProviderPlugin:
    """Load the plugin called ``name`` from ``family`` and return an instance.

 No vendor/platform knowledge lives here: the behaviour is identical
 for every ``name``; only the imported package differs.

 Resolution order:

 1. ``registry`` (if given) — an in-memory ``(family, name) -> plugin/factory``
 map, used for tests and for an agent that wants to inject plugins without
 a filesystem.
 2. the filesystem — import ``<plugins_root>/<family>/<name>/`` and obtain its
 class via the exposure convention (``get_plugin`` then subclass scan).

 Args:
 family: which axis to load from (``ci`` or ``target``).
 name: the plugin / directory name (an IR ``ci_vendor`` or
 ``target_platform`` value).
 plugins_root: root containing the ``ci/`` and ``target/`` family folders.
 Defaults to the repo's ``plugins/`` directory.
 registry: optional in-memory override; when it contains ``(family, name)``
 the filesystem is not touched.

 Returns:
 A concrete plugin instance whose base class matches ``family`` and whose
:attr:`~cicdgen.plugins.base.ProviderPlugin.name` equals ``name``.

 Raises:
 PluginLoadError: on any failure, naming the plugin and the cause.
 """
    expected_base = _base_for_family(family)

    if registry is not None and (family, name) in registry:
        plugin = _materialise(registry[(family, name)], family, name)
        return _validate_plugin(plugin, family, name, expected_base)

    root = Path(plugins_root) if plugins_root is not None else _DEFAULT_PLUGINS_ROOT
    module = _import_plugin_package(root, family, name)
    plugin = _instantiate_from_module(module, family, name, expected_base)
    return _validate_plugin(plugin, family, name, expected_base)


def load_ci_vendor_plugin(
    name: str,
    *,
    plugins_root: Path | str | None = None,
    registry: Registry | None = None,
) -> CIVendorPlugin:
    """Load a CI-vendor plugin by name (typed convenience wrapper)."""
    plugin = load_plugin(
        PluginFamily.CI_VENDOR, name, plugins_root=plugins_root, registry=registry
    )
    assert isinstance(plugin, CIVendorPlugin)  # guaranteed by _validate_plugin
    return plugin


def load_target_platform_plugin(
    name: str,
    *,
    plugins_root: Path | str | None = None,
    registry: Registry | None = None,
) -> TargetPlatformPlugin:
    """Load a target-platform plugin by name (typed convenience wrapper)."""
    plugin = load_plugin(
        PluginFamily.TARGET_PLATFORM, name, plugins_root=plugins_root, registry=registry
    )
    assert isinstance(plugin, TargetPlatformPlugin)  # guaranteed by _validate_plugin
    return plugin


# ---------------------------------------------------------------------------
# Internals — none of these contain a vendor/platform literal
# ---------------------------------------------------------------------------
def _base_for_family(family: PluginFamily) -> type[ProviderPlugin]:
    """Map a family to the base class every plugin in it must subclass."""
    mapping: dict[PluginFamily, type[ProviderPlugin]] = {
        PluginFamily.CI_VENDOR: CIVendorPlugin,
        PluginFamily.TARGET_PLATFORM: TargetPlatformPlugin,
    }
    return mapping[family]


def _package_dir_for(root: Path, family: PluginFamily, name: str) -> Path:
    """Locate a plugin package directory for ``name`` under its family folder.

 The CI family is FLAT — ``root / "ci" / name``. The TARGET family is
 provider-NESTED — the pure
:func:`~cicdgen.plugins.target_layout.resolve_target_dir` turns the IR name
 into ``(provider, service)`` and the package lives at
 ``root / "target" / provider / service``.

 No vendor/platform literal lives here: the family folder name comes
 from ``family.value`` and the provider/service come from the data-only alias
 table + prefix rule in :mod:`cicdgen.plugins.target_layout`.
 """
    if family is PluginFamily.TARGET_PLATFORM:
        provider, service = resolve_target_dir(name)
        return root / family.value / provider / service
    return root / family.value / name


def _import_plugin_package(root: Path, family: PluginFamily, name: str) -> ModuleType:
    """Import ``<root>/<family>/<name>/`` as a package, by file location.

 We import by path (not by a dotted name on ``sys.path``) so an injected
 ``plugins_root`` — e.g. a pytest tmp dir — works without the caller mutating
 the import path. A unique synthetic module name keeps multiple roots (real +
 several tmp dirs in one test session) from colliding in ``sys.modules``.
 """
    package_dir = _package_dir_for(root, family, name)
    if not package_dir.is_dir():
        raise PluginLoadError(
            f"{family.value} plugin '{name}' not found: expected a package directory at "
            f"{package_dir}."
        )

    init_file = package_dir / "__init__.py"
    if not init_file.is_file():
        raise PluginLoadError(
            f"{family.value} plugin '{name}' at {package_dir} is not an importable package: "
            f"missing __init__.py."
        )

    # Synthetic, collision-proof module name tied to the absolute location.
    module_name = _synthetic_module_name(package_dir, family, name)
    spec = importlib.util.spec_from_file_location(
        module_name, init_file, submodule_search_locations=[str(package_dir)]
    )
    if spec is None or spec.loader is None:
        raise PluginLoadError(
            f"{family.value} plugin '{name}': could not create an import spec for {init_file}."
        )

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — surface ANY import-time failure clearly
        sys.modules.pop(module_name, None)
        raise PluginLoadError(
            f"{family.value} plugin '{name}' failed to import from {init_file}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    return module


def _synthetic_module_name(package_dir: Path, family: PluginFamily, name: str) -> str:
    """A stable, unique module name for an on-disk plugin package location."""
    digest = abs(hash(str(package_dir.resolve())))
    return f"cicdgen._loaded_plugins.{family.value}.{name}_{digest}"


def _instantiate_from_module(
    module: ModuleType,
    family: PluginFamily,
    name: str,
    expected_base: type[ProviderPlugin],
) -> ProviderPlugin:
    """Obtain an instance from an imported plugin package via the convention.

 Convention #1: a ``get_plugin`` factory. Convention #2 (fallback): exactly
 one concrete subclass of ``expected_base`` exposed on the module.
 """
    factory = getattr(module, FACTORY_ATTR, None)
    if factory is not None:
        if not callable(factory):
            raise PluginLoadError(
                f"{family.value} plugin '{name}': '{FACTORY_ATTR}' exists but is not callable."
            )
        try:
            instance = factory()
        except Exception as exc:  # noqa: BLE001
            raise PluginLoadError(
                f"{family.value} plugin '{name}': {FACTORY_ATTR}() raised "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return _materialise(instance, family, name)

    # Fallback: discover a single concrete subclass defined on the module.
    candidates = _concrete_subclasses(module, expected_base)
    if not candidates:
        raise PluginLoadError(
            f"{family.value} plugin '{name}' exposes no plugin class: define a module-level "
            f"'{FACTORY_ATTR}()' factory returning a {expected_base.__name__} instance, or "
            f"expose exactly one concrete {expected_base.__name__} subclass."
        )
    if len(candidates) > 1:
        names = sorted(c.__name__ for c in candidates)
        raise PluginLoadError(
            f"{family.value} plugin '{name}' is ambiguous: found multiple concrete "
            f"{expected_base.__name__} subclasses {names}. Expose a single '{FACTORY_ATTR}()' "
            f"factory to disambiguate."
        )
    cls = candidates[0]
    try:
        instance = cls()
    except Exception as exc:  # noqa: BLE001
        raise PluginLoadError(
            f"{family.value} plugin '{name}': instantiating {cls.__name__}() raised "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return instance


def _concrete_subclasses(
    module: ModuleType, expected_base: type[ProviderPlugin]
) -> list[type[ProviderPlugin]]:
    """Public, concrete subclasses of ``expected_base`` *defined on* ``module``.

 Only classes whose ``__module__`` is this package module count, so imported
 base classes (e.g. ``CIVendorPlugin`` itself, imported for subclassing) are
 not mistaken for the plugin. Abstract classes are skipped.
 """
    found: list[type[ProviderPlugin]] = []
    for attr_name, obj in vars(module).items():
        if attr_name.startswith("_"):
            continue
        if not inspect.isclass(obj):
            continue
        if not issubclass(obj, expected_base):
            continue
        if obj is expected_base:
            continue
        if inspect.isabstract(obj):
            continue
        if obj.__module__ != module.__name__:
            continue
        found.append(obj)
    return found


def _materialise(
    value: RegistryValue, family: PluginFamily, name: str
) -> ProviderPlugin:
    """Resolve a registry/factory value to an instance (call it if callable)."""
    if isinstance(value, ProviderPlugin):
        return value
    if callable(value):
        try:
            produced = value()
        except Exception as exc:  # noqa: BLE001
            raise PluginLoadError(
                f"{family.value} plugin '{name}': factory raised "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return produced
    raise PluginLoadError(
        f"{family.value} plugin '{name}': registry value is neither a plugin instance "
        f"nor a callable factory (got {type(value).__name__})."
    )


def _validate_plugin(
    plugin: object,
    family: PluginFamily,
    name: str,
    expected_base: type[ProviderPlugin],
) -> ProviderPlugin:
    """Confirm the loaded object is the right base class with a matching name."""
    if not isinstance(plugin, expected_base):
        raise PluginLoadError(
            f"{family.value} plugin '{name}' did not produce a {expected_base.__name__}: "
            f"got {type(plugin).__name__}."
        )
    plugin_name = getattr(plugin, "name", None)
    if plugin_name != name:
        raise PluginLoadError(
            f"{family.value} plugin loaded for '{name}' reports name={plugin_name!r}; "
            f"a plugin's 'name' must equal its directory / IR name."
        )
    if plugin.family is not family:
        raise PluginLoadError(
            f"plugin '{name}' reports family {plugin.family!r} but was loaded as "
            f"{family!r}."
        )
    # A plugin loads iff the contract revision it was built against is one the
    # running core supports. Pure predicate — no vendor/platform knowledge.
    if plugin.plugin_api_version not in SUPPORTED_PLUGIN_API:
        supported = ", ".join(str(v) for v in SUPPORTED_PLUGIN_API)
        raise PluginLoadError(
            f"{family.value} plugin '{name}' declares plugin_api_version="
            f"{plugin.plugin_api_version} but this core supports only "
            f"[{supported}]; rebuild the plugin against a supported contract."
        )
    return plugin
