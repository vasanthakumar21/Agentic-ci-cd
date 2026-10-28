"""Tests for the plugin API-version gate (task 15, design §11.4, the 1a gate).

Each plugin declares the plugin-contract version it was built against
(:attr:`ProviderPlugin.plugin_api_version`). The loader refuses to load a plugin
whose declared version is not in :data:`SUPPORTED_PLUGIN_API`, raising a
:class:`PluginLoadError` that names the plugin, its declared version, and the
supported range (consistent with the loader's existing "clear failures" policy).

The gate is a pure predicate with NO vendor/platform knowledge (R3.4): the
behaviour is identical for every plugin name; only the declared version matters.

Covered here:
    * Property 6 — for every version in a bounded range, a plugin loads iff its
      declared ``plugin_api_version`` is in ``SUPPORTED_PLUGIN_API`` (and the
      failure message names the plugin, its version, and the supported range);
    * a compatible-version plugin (default version) loads;
    * an incompatible-version plugin fails with the named-range message;
    * every real plugin on disk still loads (default version is supported).

These tests inject plugins via the loader's in-memory ``registry`` so the gate
is exercised without the filesystem, then separately load the real plugins from
disk to prove existing plugins keep working.
"""

from __future__ import annotations

import pytest

from cicdgen.ir.plugins import AvailablePlugins
from cicdgen.plugins import (
    PLUGIN_API_VERSION,
    SUPPORTED_PLUGIN_API,
    CIVendorPlugin,
    GeneratedFile,
    PluginFamily,
    PluginLoadError,
    StageGraph,
    load_ci_vendor_plugin,
    load_plugin,
    load_target_platform_plugin,
)


# ---------------------------------------------------------------------------
# A tiny, complete CI-vendor plugin whose declared version is tunable
# ---------------------------------------------------------------------------
def _ci_plugin(name: str, version: int | None = None) -> CIVendorPlugin:
    """Build a minimal, valid CI-vendor plugin declaring ``version``.

    When ``version`` is ``None`` the plugin inherits the default
    ``plugin_api_version`` (the core's current :data:`PLUGIN_API_VERSION`).
    """

    class _Plugin(CIVendorPlugin):
        def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
            return (GeneratedFile(path=".ci/pipeline.yml", content="stages: []"),)

    _Plugin.name = name
    if version is not None:
        _Plugin.plugin_api_version = version
    return _Plugin()


def _registry(name: str, plugin: CIVendorPlugin) -> dict:
    return {(PluginFamily.CI_VENDOR, name): plugin}


# ---------------------------------------------------------------------------
# 15.3 — Property 6: the version gate accepts iff version in SUPPORTED set
# ---------------------------------------------------------------------------
# No hypothesis in this project; a property is exercised exhaustively over a
# bounded version range that brackets the supported set on both sides.
_VERSION_RANGE = range(-2, max(SUPPORTED_PLUGIN_API) + 4)


@pytest.mark.parametrize("version", list(_VERSION_RANGE))
def test_property6_version_gate_accepts_iff_supported(version: int) -> None:
    """For any declared version, load succeeds iff it is in SUPPORTED_PLUGIN_API.

    Feature: agentic-cicd-generator, Property 6: Plugin API-version compatibility
    gate — a plugin loads iff ``plugin.plugin_api_version in SUPPORTED_PLUGIN_API``.
    Validates: Requirements R3.4.
    """
    name = "fake-ci"
    plugin = _ci_plugin(name, version)
    registry = _registry(name, plugin)

    if version in SUPPORTED_PLUGIN_API:
        loaded = load_plugin(PluginFamily.CI_VENDOR, name, registry=registry)
        assert loaded is plugin
    else:
        with pytest.raises(PluginLoadError) as excinfo:
            load_plugin(PluginFamily.CI_VENDOR, name, registry=registry)
        message = str(excinfo.value)
        # The failure names the plugin, its declared version, and the range.
        assert name in message
        assert str(version) in message
        for supported in SUPPORTED_PLUGIN_API:
            assert str(supported) in message


# ---------------------------------------------------------------------------
# 15.4 — compatible-version plugin loads
# ---------------------------------------------------------------------------
def test_default_version_plugin_loads() -> None:
    """A plugin inheriting the default (current) version loads."""
    name = "fake-ci"
    plugin = _ci_plugin(name)  # inherits PLUGIN_API_VERSION
    assert plugin.plugin_api_version == PLUGIN_API_VERSION

    loaded = load_ci_vendor_plugin(name, registry=_registry(name, plugin))
    assert loaded is plugin


@pytest.mark.parametrize("version", list(SUPPORTED_PLUGIN_API))
def test_every_supported_version_loads(version: int) -> None:
    """Each explicitly supported version (e.g. v1 and v2) loads."""
    name = "fake-ci"
    plugin = _ci_plugin(name, version)
    loaded = load_ci_vendor_plugin(name, registry=_registry(name, plugin))
    assert loaded is plugin
    assert loaded.plugin_api_version == version


# ---------------------------------------------------------------------------
# 15.4 — incompatible-version plugin fails with the named-range message
# ---------------------------------------------------------------------------
def test_incompatible_version_fails_with_named_range() -> None:
    """An unsupported version is rejected, naming plugin, version, and range."""
    name = "fake-ci"
    bad_version = max(SUPPORTED_PLUGIN_API) + 1
    plugin = _ci_plugin(name, bad_version)

    with pytest.raises(PluginLoadError) as excinfo:
        load_ci_vendor_plugin(name, registry=_registry(name, plugin))

    message = str(excinfo.value)
    assert name in message
    assert "plugin_api_version" in message
    assert str(bad_version) in message
    # The supported range is spelled out so the author knows the target.
    for supported in SUPPORTED_PLUGIN_API:
        assert str(supported) in message


# ---------------------------------------------------------------------------
# 15.4 — every real plugin on disk still loads (default version is supported)
# ---------------------------------------------------------------------------
def test_all_real_plugins_still_load() -> None:
    """Every authored plugin loads: the default version is in the supported set.

    Enumerated dynamically from the filesystem (no vendor/platform literals here,
    R3.4) so this stays correct as plugins are added or removed.
    """
    available = AvailablePlugins.discover()
    assert available.ci, "expected at least one ci plugin on disk"
    assert available.target, "expected at least one target plugin on disk"

    assert PLUGIN_API_VERSION in SUPPORTED_PLUGIN_API  # the default is supported

    for ci_name in sorted(available.ci):
        plugin = load_ci_vendor_plugin(ci_name)
        assert plugin.name == ci_name
        assert plugin.plugin_api_version in SUPPORTED_PLUGIN_API

    for target_name in sorted(available.target):
        plugin = load_target_platform_plugin(target_name)
        assert plugin.name == target_name
        assert plugin.plugin_api_version in SUPPORTED_PLUGIN_API
