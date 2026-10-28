"""Unit tests for the core dispatch layer — loader + orchestrator (task 3.3).

Proves the core loads the two named plugins and orchestrates them with NO
hard-coded vendor/platform knowledge (R3.4, R4.1, R4.3):

Loader (``load_plugin`` / family wrappers):
    * loads a plugin from a *fake* on-disk package via the ``get_plugin()``
      factory convention;
    * loads a plugin via the subclass-discovery fallback (no factory);
    * loads a plugin from an injected in-memory registry (no filesystem);
    * raises a clear error when the directory is missing, the package fails to
      import, exposes no plugin class, exposes an ambiguous set, has the wrong
      base class, or reports a name that disagrees with the directory.

Orchestrator (``dispatch``):
    * loads the CI and target plugins named by the IR and calls them in the
      documented order (target verbs → CI emit → target IaC);
    * bundles CI files, verb support files, and IaC files into the result;
    * drives two *different-shaped* fake platforms through the identical core
      path (only the results differ);
    * passes ``settings`` through to the verbs.

Source hygiene:
    * the core dispatch + loader + stage-contract sources contain no vendor /
      platform string literals (github/aws-ecs/databricks/…), proving the core
      is axis-agnostic (R3.4).

Fake plugins are real subclasses written to a tmp dir on disk (no mocks), so the
tests exercise the genuine import + discovery machinery.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cicdgen.ir import Pipeline
from cicdgen.plugins import (
    Capability,
    CIVendorPlugin,
    DispatchResult,
    GeneratedFile,
    IaCArtifacts,
    PluginFamily,
    PluginLoadError,
    StageGraph,
    TargetPlatformPlugin,
    VerbContext,
    VerbResult,
    dispatch,
    load_ci_vendor_plugin,
    load_plugin,
    load_target_platform_plugin,
)

#: A full-capability stand-in set so the §11.2 gate (task 17) is satisfied for the
#: IRs these dispatch tests use; the gate has its own dedicated test module.
_ALL_CAPS = frozenset(Capability)


# ---------------------------------------------------------------------------
# IR helper — a representative two-axis pipeline
# ---------------------------------------------------------------------------
def _pipeline(
    *,
    ci_vendor: str = "github",
    target_platform: str = "aws-ecs",
    stage_ids: tuple[str, ...] = ("test", "build", "deploy-dev"),
) -> Pipeline:
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": ci_vendor,
            "target_platform": target_platform,
            "project": {"name": "demo"},
            "environments": [{"name": "dev"}],
            "stages": [
                ({"id": sid, "environment": "dev"} if sid.startswith("deploy") else {"id": sid})
                for sid in stage_ids
            ],
        }
    )


# ---------------------------------------------------------------------------
# In-memory fake plugins (for registry + dispatch tests)
# ---------------------------------------------------------------------------
class _FakeCI(CIVendorPlugin):
    name = "github"  # matches the IR ci_vendor used in tests

    def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
        stage_ids = ",".join(s.id for s in graph.stages)
        return (GeneratedFile(path=".ci/pipeline.yml", content=f"stages: {stage_ids}"),)


class _FakeTarget(TargetPlatformPlugin):
    name = "aws-ecs"  # matches the IR target_platform used in tests
    capabilities = _ALL_CAPS

    def _verb(self, ctx: VerbContext) -> VerbResult:
        return VerbResult(
            commands=(f"{ctx.verb.value}:{ctx.stage.id}",),
            files=(GeneratedFile(path=f"verb/{ctx.verb.value}.sh", content=ctx.verb.value),),
        )

    lint = _verb
    test = _verb
    build = _verb
    package = _verb
    deploy = _verb
    verify = _verb

    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        return IaCArtifacts(
            files=(GeneratedFile(path="iac/main.tf", content="# tf"),),
            summary="demo infra",
        )


# ---------------------------------------------------------------------------
# Helpers that materialise fake plugin packages on disk
# ---------------------------------------------------------------------------
def _write_pkg(root: Path, family: str, name: str, body: str) -> Path:
    pkg = root / family / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(body, encoding="utf-8")
    return pkg


_CI_FACTORY_PKG = '''
from cicdgen.plugins import CIVendorPlugin, GeneratedFile, StageGraph


class Emitter(CIVendorPlugin):
    name = "{name}"

    def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
        return (GeneratedFile(path=".ci/pipeline.yml", content="ok"),)


def get_plugin() -> Emitter:
    return Emitter()
'''

_TARGET_SUBCLASS_PKG = '''
from cicdgen.plugins import (
    GeneratedFile,
    IaCArtifacts,
    TargetPlatformPlugin,
    VerbContext,
    VerbResult,
)


class Impl(TargetPlatformPlugin):
    name = "{name}"

    def _v(self, ctx: VerbContext) -> VerbResult:
        return VerbResult(commands=(ctx.verb.value,))

    lint = _v
    test = _v
    build = _v
    package = _v
    deploy = _v
    verify = _v

    def supply_iac(self, pipeline) -> IaCArtifacts:
        return IaCArtifacts(summary="infra")
'''


# ===========================================================================
# Loader — filesystem (factory convention)
# ===========================================================================
def test_load_from_disk_via_factory(tmp_path: Path) -> None:
    _write_pkg(tmp_path, "ci", "myvendor", _CI_FACTORY_PKG.format(name="myvendor"))

    plugin = load_ci_vendor_plugin("myvendor", plugins_root=tmp_path)

    assert isinstance(plugin, CIVendorPlugin)
    assert plugin.name == "myvendor"
    assert plugin.family is PluginFamily.CI_VENDOR


def test_load_from_disk_via_subclass_discovery(tmp_path: Path) -> None:
    # Target plugins live provider-nested (§11.1): a provider-prefixed IR name
    # (gcp-myservice) resolves to plugins/target/gcp/myservice/ via the loader.
    _write_pkg(
        tmp_path, "target/gcp", "myservice", _TARGET_SUBCLASS_PKG.format(name="gcp-myservice")
    )

    plugin = load_target_platform_plugin("gcp-myservice", plugins_root=tmp_path)

    assert isinstance(plugin, TargetPlatformPlugin)
    assert plugin.name == "gcp-myservice"
    assert plugin.family is PluginFamily.TARGET_PLATFORM


def test_correct_plugin_loads_by_name(tmp_path: Path) -> None:
    # Two different ci plugins coexist; the loader returns the one named.
    _write_pkg(tmp_path, "ci", "alpha", _CI_FACTORY_PKG.format(name="alpha"))
    _write_pkg(tmp_path, "ci", "beta", _CI_FACTORY_PKG.format(name="beta"))

    assert load_ci_vendor_plugin("alpha", plugins_root=tmp_path).name == "alpha"
    assert load_ci_vendor_plugin("beta", plugins_root=tmp_path).name == "beta"


# ===========================================================================
# Loader — in-memory registry (no filesystem)
# ===========================================================================
def test_load_from_registry_instance() -> None:
    registry = {(PluginFamily.CI_VENDOR, "github"): _FakeCI()}
    plugin = load_plugin(PluginFamily.CI_VENDOR, "github", registry=registry)
    assert isinstance(plugin, _FakeCI)


def test_load_from_registry_factory() -> None:
    registry = {(PluginFamily.TARGET_PLATFORM, "aws-ecs"): _FakeTarget}
    plugin = load_plugin(PluginFamily.TARGET_PLATFORM, "aws-ecs", registry=registry)
    assert isinstance(plugin, _FakeTarget)


# ===========================================================================
# Loader — clear errors
# ===========================================================================
def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(PluginLoadError, match=r"not found"):
        load_ci_vendor_plugin("nope", plugins_root=tmp_path)


def test_not_a_package_raises(tmp_path: Path) -> None:
    # Directory exists but has no __init__.py.
    (tmp_path / "ci" / "bare").mkdir(parents=True)
    with pytest.raises(PluginLoadError, match=r"__init__\.py"):
        load_ci_vendor_plugin("bare", plugins_root=tmp_path)


def test_import_failure_raises_clear_error(tmp_path: Path) -> None:
    _write_pkg(tmp_path, "ci", "broken", "raise RuntimeError('boom at import')\n")
    with pytest.raises(PluginLoadError, match=r"failed to import"):
        load_ci_vendor_plugin("broken", plugins_root=tmp_path)


def test_no_plugin_class_raises(tmp_path: Path) -> None:
    _write_pkg(tmp_path, "ci", "empty", "x = 1\n")
    with pytest.raises(PluginLoadError, match=r"exposes no plugin class"):
        load_ci_vendor_plugin("empty", plugins_root=tmp_path)


def test_ambiguous_plugin_classes_raise(tmp_path: Path) -> None:
    body = (
        "from cicdgen.plugins import CIVendorPlugin, GeneratedFile, StageGraph\n"
        "class A(CIVendorPlugin):\n"
        "    name = 'dup'\n"
        "    def emit_pipeline_files(self, graph):\n"
        "        return ()\n"
        "class B(CIVendorPlugin):\n"
        "    name = 'dup'\n"
        "    def emit_pipeline_files(self, graph):\n"
        "        return ()\n"
    )
    _write_pkg(tmp_path, "ci", "dup", body)
    with pytest.raises(PluginLoadError, match=r"ambiguous"):
        load_ci_vendor_plugin("dup", plugins_root=tmp_path)


def test_wrong_family_base_class_raises(tmp_path: Path) -> None:
    # A target-style plugin placed under ci/ — wrong base class for the family.
    _write_pkg(tmp_path, "ci", "wrong", _TARGET_SUBCLASS_PKG.format(name="wrong"))
    with pytest.raises(PluginLoadError, match=r"no plugin class|did not produce"):
        load_ci_vendor_plugin("wrong", plugins_root=tmp_path)


def test_name_mismatch_raises(tmp_path: Path) -> None:
    # Class name attribute disagrees with the directory / IR name.
    _write_pkg(tmp_path, "ci", "declared", _CI_FACTORY_PKG.format(name="actually-different"))
    with pytest.raises(PluginLoadError, match=r"must equal its directory"):
        load_ci_vendor_plugin("declared", plugins_root=tmp_path)


# ===========================================================================
# Orchestrator — dispatch (registry-injected, so no filesystem needed)
# ===========================================================================
def _registry() -> dict:
    return {
        (PluginFamily.CI_VENDOR, "github"): _FakeCI(),
        (PluginFamily.TARGET_PLATFORM, "aws-ecs"): _FakeTarget(),
    }


def test_dispatch_loads_both_named_plugins() -> None:
    result = dispatch(_pipeline(), registry=_registry())

    assert isinstance(result, DispatchResult)
    assert result.ci_vendor == "github"
    assert result.target_platform == "aws-ecs"
    assert isinstance(result.ci_plugin, _FakeCI)
    assert isinstance(result.target_plugin, _FakeTarget)


def test_dispatch_calls_plugins_in_right_order() -> None:
    """Target verbs run, then CI emit sees the graph, then target IaC."""
    order: list[str] = []

    class _OrderedTarget(_FakeTarget):
        def _verb(self, ctx: VerbContext) -> VerbResult:  # type: ignore[override]
            order.append(f"verb:{ctx.verb.value}")
            return super()._verb(ctx)

        lint = _verb
        test = _verb
        build = _verb
        package = _verb
        deploy = _verb
        verify = _verb

        def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
            order.append("iac")
            return super().supply_iac(pipeline)

    class _OrderedCI(_FakeCI):
        def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
            order.append("ci-emit")
            return super().emit_pipeline_files(graph)

    registry = {
        (PluginFamily.CI_VENDOR, "github"): _OrderedCI(),
        (PluginFamily.TARGET_PLATFORM, "aws-ecs"): _OrderedTarget(),
    }

    dispatch(_pipeline(stage_ids=("build",)), registry=registry)

    # all verb invocations happen before ci-emit, and iac is collected last
    assert order == ["verb:build", "verb:package", "ci-emit", "iac"]


def test_dispatch_bundles_all_files() -> None:
    result = dispatch(_pipeline(stage_ids=("build",)), registry=_registry())

    assert [f.path for f in result.ci_files] == [".ci/pipeline.yml"]
    # build stage -> build + package verbs each emit a support file
    assert {f.path for f in result.verb_files} == {"verb/build.sh", "verb/package.sh"}
    assert [f.path for f in result.iac.files] == ["iac/main.tf"]

    all_paths = {f.path for f in result.all_files()}
    assert all_paths == {
        ".ci/pipeline.yml",
        "verb/build.sh",
        "verb/package.sh",
        "iac/main.tf",
    }


def test_dispatch_records_stage_invocations_in_order() -> None:
    result = dispatch(_pipeline(stage_ids=("test", "build", "deploy-dev")), registry=_registry())
    assert [inv.stage.id for inv in result.stage_invocations] == ["test", "build", "deploy-dev"]


def test_dispatch_passes_settings_to_verbs() -> None:
    captured: list[str] = []

    class _Capturing(_FakeTarget):
        def _verb(self, ctx: VerbContext) -> VerbResult:  # type: ignore[override]
            captured.append(ctx.settings.get("region", "<none>"))
            return super()._verb(ctx)

        lint = _verb
        test = _verb
        build = _verb
        package = _verb
        deploy = _verb
        verify = _verb

    registry = {
        (PluginFamily.CI_VENDOR, "github"): _FakeCI(),
        (PluginFamily.TARGET_PLATFORM, "aws-ecs"): _Capturing(),
    }
    dispatch(_pipeline(stage_ids=("build",)), registry=registry, settings={"region": "uksouth"})
    assert captured == ["uksouth", "uksouth"]


def test_dispatch_drives_two_different_platforms_identically() -> None:
    """Same core path; a no-image platform simply emits fewer files/commands."""

    class _Imageless(TargetPlatformPlugin):
        name = "aws-ecs"  # reuse the IR name; the shape is what differs
        capabilities = _ALL_CAPS

        def _cmd(self, ctx: VerbContext) -> VerbResult:
            return VerbResult(commands=(f"dbx-{ctx.verb.value}",))

        def _noop(self, ctx: VerbContext) -> VerbResult:
            return VerbResult()

        lint = _cmd
        test = _cmd
        build = _cmd
        package = _noop  # no image to package
        deploy = _cmd
        verify = _cmd

        def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
            return IaCArtifacts()

    ecs = dispatch(_pipeline(stage_ids=("build",)), registry=_registry())
    dbx_registry = {
        (PluginFamily.CI_VENDOR, "github"): _FakeCI(),
        (PluginFamily.TARGET_PLATFORM, "aws-ecs"): _Imageless(),
    }
    dbx = dispatch(_pipeline(stage_ids=("build",)), registry=dbx_registry)

    # Both asked the CI plugin for files and walked the same stages ...
    assert [inv.stage.id for inv in ecs.stage_invocations] == [
        inv.stage.id for inv in dbx.stage_invocations
    ]
    # ... but the imageless platform produced no verb support files.
    assert ecs.verb_files != ()
    assert dbx.verb_files == ()


def test_dispatch_propagates_loader_errors() -> None:
    # Target plugin not in the registry and no such dir under default root tmp.
    with pytest.raises(PluginLoadError):
        dispatch(
            _pipeline(),
            plugins_root="___definitely_not_a_real_plugins_root___",
        )


# ===========================================================================
# Source hygiene — the core contains NO vendor/platform string literals (R3.4)
# ===========================================================================
_CORE_DIR = Path(__file__).resolve().parents[1] / "generator" / "cicdgen" / "plugins"

_FORBIDDEN_LITERALS = (
    "github",
    "gitlab",
    "azure-devops",
    "bitbucket",
    "jenkins",
    "aws-ecs",
    "databricks",
    "azure-functions",
    "kubernetes",
)


@pytest.mark.parametrize("module_name", ["dispatch.py", "loader.py", "stage_contract.py"])
def test_core_contains_no_vendor_or_platform_literals(module_name: str) -> None:
    source = (_CORE_DIR / module_name).read_text(encoding="utf-8")
    # Strip comments and docstrings-ish lines so documentation references don't
    # trip the check; we only care about *executable* literals.
    code_lines = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)

    # Remove triple-quoted blocks (module/function docstrings).
    code_no_docstrings = re.sub(r'""".*?"""', "", code, flags=re.DOTALL)
    code_no_docstrings = re.sub(r"'''.*?'''", "", code_no_docstrings, flags=re.DOTALL)

    lowered = code_no_docstrings.casefold()
    offenders = [lit for lit in _FORBIDDEN_LITERALS if lit in lowered]
    assert offenders == [], (
        f"{module_name} contains hard-coded vendor/platform literal(s): {offenders}. "
        f"The core must be axis-agnostic (R3.4)."
    )
