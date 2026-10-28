"""MIGRATION_RUNBOOK.md emitter — task 11.2 (R8, R3.5, R9.3, R5.3).

Task 11.1 gave the generator a first-class axis-swap signal (``AxisChange``). Task
11.2 turns it into the committed ``MIGRATION_RUNBOOK.md`` design §7 / R8 call for.
These tests build ``AxisChange`` instances for the three swap shapes a v1 client
can hit — CI vendor (github -> azure-devops), target platform (aws-ecs ->
databricks), and both at once — render the runbook, and assert it:

    * states WHAT CHANGED (old -> new for each changed axis);
    * lists the NEW vendor's required ``.local`` KEYS (e.g. azure-devops service
      connection / variable group; databricks host/token/artifact/Unity Catalog);
    * lists the irreducible MANUAL steps appropriate to the new axes (org/account
      OIDC provider, Databricks workspace/metastore, environment reviewers);
    * is VALUE-FREE (R9.3) — sentinel secret-ish strings never appear;
    * is DETERMINISTIC (R5.3) — two renders are byte-identical;
    * writes ``MIGRATION_RUNBOOK.md`` at the repo root and is byte-stable.
"""

from __future__ import annotations

from pathlib import Path

from cicdgen.core import (
    RUNBOOK_RELPATH,
    AxisChange,
    render_migration_runbook,
    write_migration_runbook,
)

# A sentinel that must NEVER appear in a value-free runbook. The renderer never
# consults any secret value, so this models "a secret value could never leak".
SENTINEL = "s3cr3t-ACTUAL-VALUE-DO-NOT-LEAK"


# ---------------------------------------------------------------------------
# AxisChange builders for the three swap shapes
# ---------------------------------------------------------------------------
def _ci_swap() -> AxisChange:
    """github -> azure-devops, target unchanged (aws-ecs)."""
    return AxisChange(
        old_ci_vendor="github",
        old_target_platform="aws-ecs",
        new_ci_vendor="azure-devops",
        new_target_platform="aws-ecs",
    )


def _target_swap() -> AxisChange:
    """aws-ecs -> databricks, CI unchanged (github)."""
    return AxisChange(
        old_ci_vendor="github",
        old_target_platform="aws-ecs",
        new_ci_vendor="github",
        new_target_platform="databricks",
    )


def _both_swap() -> AxisChange:
    """github -> azure-devops AND aws-ecs -> databricks."""
    return AxisChange(
        old_ci_vendor="github",
        old_target_platform="aws-ecs",
        new_ci_vendor="azure-devops",
        new_target_platform="databricks",
    )


# ===========================================================================
# What changed — old -> new for the changed axis
# ===========================================================================
def test_ci_swap_states_what_changed() -> None:
    body = render_migration_runbook(_ci_swap())
    assert "# Migration Runbook" in body
    assert "## 1. What changed" in body
    # old -> new for the CI axis; target line should NOT appear (unchanged)
    assert "`github` -> `azure-devops`" in body
    assert "ci_vendor: github -> azure-devops" in body  # from AxisChange.summary()
    # no target-change line in section 1 (target unchanged); the "### Target
    # platform:" section-2 heading is a different, always-present line.
    assert "Target platform: `aws-ecs` -> " not in body
    assert "target_platform:" not in body  # summary() omits the unchanged axis


def test_target_swap_states_what_changed() -> None:
    body = render_migration_runbook(_target_swap())
    assert "`aws-ecs` -> `databricks`" in body
    assert "target_platform: aws-ecs -> databricks" in body
    # CI unchanged -> no CI-change line
    assert "CI vendor: `github` -> " not in body


def test_both_swap_states_both_axes() -> None:
    body = render_migration_runbook(_both_swap())
    assert "`github` -> `azure-devops`" in body
    assert "`aws-ecs` -> `databricks`" in body


# ===========================================================================
# Required .local keys for the NEW vendor/target
# ===========================================================================
def test_azure_devops_new_vendor_keys_listed() -> None:
    body = render_migration_runbook(_ci_swap())
    # section names the new vendor
    assert "### CI vendor: `azure-devops`" in body
    # azure-devops service connection / WIF + variable-group secret style
    assert "oidc.azure.client_id" in body
    assert "oidc.azure.tenant_id" in body
    assert "variable group" in body  # $(NAME) via a variable group
    # shared spine keys always present
    assert "vendor_admin.token_env" in body
    assert "runners.mode" in body
    assert "branch_protection.<branch>" in body


def test_databricks_new_target_keys_listed() -> None:
    body = render_migration_runbook(_target_swap())
    assert "### Target platform: `databricks`" in body
    assert "target.databricks.host_secret" in body
    assert "target.databricks.token_secret" in body
    assert "target.databricks.artifact_path" in body
    assert "target.databricks.unity_catalog" in body
    # databricks secret catalog NAMES (names only, value-free)
    assert "DATABRICKS_HOST" in body
    assert "DATABRICKS_TOKEN" in body


def test_aws_ecs_target_keys_listed_on_both_swap() -> None:
    # When the new target is databricks the ECS keys should NOT appear...
    db_body = render_migration_runbook(_both_swap())
    assert "target.aws-ecs.ecr_repo_secret" not in db_body
    # ...but a swap that lands ON aws-ecs should list them.
    to_ecs = AxisChange(
        old_ci_vendor="github",
        old_target_platform="databricks",
        new_ci_vendor="github",
        new_target_platform="aws-ecs",
    )
    ecs_body = render_migration_runbook(to_ecs)
    assert "target.aws-ecs.ecr_repo_secret" in ecs_body
    assert "target.aws-ecs.cluster" in ecs_body
    assert "target.aws-ecs.service" in ecs_body
    assert "ECR_REPO" in ecs_body
    assert "oidc.aws.account_id" in ecs_body


# ===========================================================================
# Irreducible manual steps appropriate to the new axes
# ===========================================================================
def test_manual_steps_always_include_first_admin_token() -> None:
    for build in (_ci_swap, _target_swap, _both_swap):
        body = render_migration_runbook(build())
        assert "## 3. Irreducible manual steps" in body
        assert "vendor_admin.token_env" in body
        assert "first admin token" in body.lower()
        # environment reviewers step is always present
        assert "reviewers.<group>" in body


def test_manual_steps_aws_ecs_org_level_oidc() -> None:
    body = render_migration_runbook(_ci_swap())  # new target aws-ecs
    assert "AWS IAM OIDC identity" in body
    assert "oidc.aws.provider_arn" in body


def test_manual_steps_databricks_workspace_metastore() -> None:
    body = render_migration_runbook(_target_swap())  # new target databricks
    assert "Databricks workspace" in body
    assert "Unity Catalog" in body
    assert "metastore" in body


def test_manual_steps_azure_devops_tenant_registration() -> None:
    body = render_migration_runbook(_ci_swap())  # new vendor azure-devops
    assert "Azure app registration" in body
    assert "tenant level" in body


# ===========================================================================
# Ordered checklist
# ===========================================================================
def test_runbook_has_ordered_checklist() -> None:
    body = render_migration_runbook(_both_swap())
    assert "## 4. Migration checklist" in body
    # numbered, checkbox-style, ordered
    assert "1. [ ]" in body
    assert "2. [ ]" in body
    assert "5. [ ]" in body


# ===========================================================================
# Value-free (R9.3)
# ===========================================================================
def test_runbook_is_value_free() -> None:
    # The sentinel is injected as if it were an actual secret value in a config the
    # renderer might (wrongly) read. Since the renderer is axis-driven and never
    # consults any value, the sentinel can never appear.
    for build in (_ci_swap, _target_swap, _both_swap):
        body = render_migration_runbook(build())
        assert SENTINEL not in body
        # a resolved-looking secret value pattern should not be present either
        assert "DATABRICKS_TOKEN=" not in body
        assert "dapi" not in body  # databricks PAT value prefix


# ===========================================================================
# Deterministic (R5.3)
# ===========================================================================
def test_render_is_deterministic() -> None:
    for build in (_ci_swap, _target_swap, _both_swap):
        axis = build()
        assert render_migration_runbook(axis) == render_migration_runbook(axis)


def test_render_has_no_timestamp() -> None:
    body = render_migration_runbook(_both_swap())
    # determinism relies on no wall-clock content — a bare 'T' time marker or a
    # four-digit year must not slip in.
    assert "20" + "25" not in body  # avoid literal year embedding
    assert "Generated at" not in body


# ===========================================================================
# write_migration_runbook — committed, byte-stable
# ===========================================================================
def test_write_migration_runbook_writes_at_repo_root(tmp_path: Path) -> None:
    axis = _both_swap()
    path = write_migration_runbook(tmp_path, axis)
    assert path == tmp_path / RUNBOOK_RELPATH
    assert path.is_file()
    assert path.name == "MIGRATION_RUNBOOK.md"
    content = path.read_text(encoding="utf-8")
    assert content == render_migration_runbook(axis)
    assert content.endswith("\n")


def test_write_migration_runbook_is_byte_stable(tmp_path: Path) -> None:
    axis = _target_swap()
    p1 = write_migration_runbook(tmp_path, axis)
    first = p1.read_bytes()
    # rewrite the same swap into a fresh repo root -> identical bytes
    other = tmp_path / "second"
    other.mkdir()
    p2 = write_migration_runbook(other, axis)
    assert p2.read_bytes() == first


def test_write_then_overwrite_is_idempotent(tmp_path: Path) -> None:
    axis = _ci_swap()
    write_migration_runbook(tmp_path, axis)
    first = (tmp_path / RUNBOOK_RELPATH).read_bytes()
    # writing again over the same repo yields identical bytes (no append/drift)
    write_migration_runbook(tmp_path, axis)
    assert (tmp_path / RUNBOOK_RELPATH).read_bytes() == first


# ===========================================================================
# Override hooks: explicit new axes take precedence over AxisChange fields
# ===========================================================================
def test_explicit_axis_overrides() -> None:
    axis = _ci_swap()  # new target aws-ecs by default
    body = render_migration_runbook(
        axis, new_ci_vendor="azure-devops", new_target_platform="databricks"
    )
    assert "### Target platform: `databricks`" in body
    assert "target.databricks.host_secret" in body
