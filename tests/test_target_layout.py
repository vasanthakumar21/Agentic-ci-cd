"""Target-plugin layout resolution + backward-compat tests (task 16.5 / 16.6).

Two concerns, both tied to the §11.1 provider-nested reorg:

* **16.5 — Property 1 (name resolution is total over the matrix + alias-stable).**
  For any ``(provider, service)`` pair the IR value ``"<provider>-<service>"``
  resolves to directory ``plugins/target/<provider>/<service>/`` via the prefix
  rule; and every legacy flat alias resolves to — and loads — the SAME plugin as
  its canonical nested value. No hypothesis in this project (mirrors
  ``test_plugin_api_version.py``): the property is exercised across a generated
  matrix of provider/service pairs plus the full alias table.

* **16.6 — backward compatibility.** Each legacy ``target_platform`` value
  (``aws-ecs``, ``databricks``, ``azure-functions``, ``kubernetes``) still
  resolves, still loads a plugin whose ``.name`` is the legacy value, and still
  produces byte-identical generated output to the pre-migration plugin (R5.3).
  The pre-migration digests are captured in ``_target_migration_golden.json``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cicdgen.core import plan
from cicdgen.ir import AvailablePlugins, Pipeline
from cicdgen.plugins import load_target_platform_plugin
from cicdgen.plugins.target_layout import (
    TARGET_ALIASES,
    UnknownTargetError,
    ir_name_for_dir,
    resolve_target_dir,
)

# <repo>/tests/test_target_layout.py -> <repo>; the REAL plugin tree lives here.
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"
GOLDEN_PATH = Path(__file__).resolve().parent / "_target_migration_golden.json"

# The four legacy flat IR values that MUST keep working unchanged (§11.1).
LEGACY_TARGETS = ("aws-ecs", "databricks", "azure-functions", "kubernetes")

# Representative IR inputs per legacy target (same shape as the pre-migration
# golden capture — see tests/_target_migration_golden.json).
_IR_CASES = {
    "aws-ecs": ("github", "aws"),
    "databricks": ("azure-devops", "azure"),
    "azure-functions": ("github", "azure"),
    "kubernetes": ("github", "aws"),
}

# A generated matrix of provider/service pairs for the prefix-rule property.
_PROVIDERS = ("aws", "azure", "gcp")
_SERVICES = ("ecs", "lambda", "eks", "functions", "aks", "cloud-run", "gke", "dataproc")
_MATRIX = [(p, s) for p in _PROVIDERS for s in _SERVICES]


def _ir(ci_vendor: str, target_platform: str, provider: str) -> Pipeline:
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": ci_vendor,
            "target_platform": target_platform,
            "project": {"name": "demo-service", "language": "python"},
            "cloud": {"provider": provider, "region": "eu-west-2", "auth": "oidc"},
            "environments": [{"name": "prod", "gate": "manual"}],
            "stages": [
                {"id": "test", "runs": "make test"},
                {"id": "build", "runs": "make build"},
                {"id": "deploy-prod", "runs": "make deploy", "environment": "prod"},
            ],
        }
    )


def _output_digest(target_platform: str) -> str:
    """SHA-256 over the sorted (path, content, exec-bit) files the plan emits."""
    ci, provider = _IR_CASES[target_platform]
    the_plan = plan(
        _ir(ci, target_platform, provider),
        REPO_ROOT / "_never_written",
        plugins_root=PLUGINS_ROOT,
        check_plugins=False,
        scan_secrets=False,
    )
    h = hashlib.sha256()
    for f in sorted(the_plan.files, key=lambda g: g.path):
        h.update(f.path.encode("utf-8"))
        h.update(b"\x00")
        data = f.content.encode("utf-8") if isinstance(f.content, str) else f.content
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
        h.update(b"\x01" if f.executable else b"\x00")
    return h.hexdigest()


# ===========================================================================
# 16.5 — Property 1: resolution is total over the matrix + alias-stable
# ===========================================================================
@pytest.mark.parametrize(("provider", "service"), _MATRIX)
def test_property1_prefix_rule_is_total_over_the_matrix(provider: str, service: str) -> None:
    """For any (provider, service), "<provider>-<service>" -> that nested dir.

    Feature: agentic-cicd-generator, Property 1: Target name resolution is total
    over the known matrix and alias-stable — the prefix rule maps every
    "<provider>-<service>" IR value to plugins/target/<provider>/<service>/.
    Validates: Requirements R3.1, R3.4.
    """
    ir_value = f"{provider}-{service}"
    assert resolve_target_dir(ir_value) == (provider, service)


@pytest.mark.parametrize("alias", sorted(TARGET_ALIASES))
def test_property1_alias_resolves_to_its_canonical_dir(alias: str) -> None:
    """For any legacy alias, resolving it yields its canonical nested dir.

    Feature: agentic-cicd-generator, Property 1: Target name resolution is total
    over the known matrix and alias-stable.
    Validates: Requirements R3.1, R3.4.
    """
    assert resolve_target_dir(alias) == TARGET_ALIASES[alias]


@pytest.mark.parametrize("alias", sorted(TARGET_ALIASES))
def test_property1_alias_is_load_stable(alias: str) -> None:
    """For any legacy alias, loading the alias and loading its canonical nested
    value yield the SAME plugin class with the SAME reported name.

    Feature: agentic-cicd-generator, Property 1: Target name resolution is total
    over the known matrix and alias-stable — an alias and its canonical value
    load the same plugin instance.
    Validates: Requirements R3.1, R3.4.
    """
    provider, service = TARGET_ALIASES[alias]
    canonical = ir_name_for_dir(provider, service)

    via_alias = load_target_platform_plugin(alias, plugins_root=PLUGINS_ROOT)
    via_canonical = load_target_platform_plugin(canonical, plugins_root=PLUGINS_ROOT)

    # Same concrete class loaded from the same on-disk package ...
    assert type(via_alias) is type(via_canonical)
    # ... and both report the legacy IR name (the dir's canonical reported name).
    assert via_alias.name == via_canonical.name == canonical


def test_property1_unknown_name_raises_clearly() -> None:
    """A name with no provider prefix and no alias entry fails loudly."""
    with pytest.raises(UnknownTargetError) as excinfo:
        resolve_target_dir("no-such-platform-xyz")
    msg = str(excinfo.value)
    assert "no-such-platform-xyz" in msg
    # the message surfaces the known aliases so the failure is actionable
    for alias in TARGET_ALIASES:
        assert alias in msg


def test_ir_name_for_dir_is_inverse_of_resolve() -> None:
    """ir_name_for_dir ∘ resolve_target_dir is the identity on known names."""
    for alias in TARGET_ALIASES:
        provider, service = resolve_target_dir(alias)
        assert ir_name_for_dir(provider, service) == alias
    # and for a plain prefixed value, the inverse is the prefixed value itself
    assert ir_name_for_dir("aws", "lambda") == "aws-lambda"


# ===========================================================================
# 16.6 — backward compatibility: legacy values resolve, load, and stay stable
# ===========================================================================
@pytest.mark.parametrize("target", LEGACY_TARGETS)
def test_legacy_target_resolves_to_existing_nested_dir(target: str) -> None:
    provider, service = resolve_target_dir(target)
    assert (PLUGINS_ROOT / "target" / provider / service).is_dir()


@pytest.mark.parametrize("target", LEGACY_TARGETS)
def test_legacy_target_loads_with_its_legacy_name(target: str) -> None:
    """Each legacy value loads a plugin that still reports the legacy name."""
    plugin = load_target_platform_plugin(target, plugins_root=PLUGINS_ROOT)
    assert plugin.name == target


def test_discovery_enumerates_legacy_names_from_nested_tree() -> None:
    """AvailablePlugins.target still speaks the legacy flat names despite nesting.

    Discovery may ALSO surface newer ``<provider>-<service>`` matrix plugins (e.g.
    ``aws-lambda``, §11.9); the backward-compat guarantee is only that every legacy
    flat name is still reported, so this asserts a superset rather than equality.
    """
    available = AvailablePlugins.discover(REPO_ROOT)
    assert available.target >= set(LEGACY_TARGETS)


@pytest.mark.parametrize("target", LEGACY_TARGETS)
def test_legacy_output_is_byte_identical_to_pre_migration(target: str) -> None:
    """Generated output for each legacy value matches the pre-migration digest.

    The golden digests in ``_target_migration_golden.json`` were captured from
    the flat ``plugins/target/<name>/`` tree before the §11.1 reorg; the move is
    a pure relocation, so output must be byte-for-byte unchanged (R5.3).
    """
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert _output_digest(target) == golden[target]
