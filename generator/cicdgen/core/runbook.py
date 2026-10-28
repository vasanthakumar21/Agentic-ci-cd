"""The ``MIGRATION_RUNBOOK.md`` emitter — the vendor/target-swap guide.

The generator carries a first-class *axis-swap* signal
(:class:`~cicdgen.core.migration.AxisChange`): a regenerate that changes
``ci_vendor`` and/or ``target_platform`` vs. the prior manifest. This module turns
that signal into a committed ``MIGRATION_RUNBOOK.md`` that tells the operator, for
the **new** vendor/target:

 1. **What changed** — the ``ci_vendor`` and/or ``target_platform`` old -> new
 swap (from :meth:`AxisChange.summary`).
 2. **Which ``.local`` keys to populate** for the new vendor/target — the
 vendor-specific wiring keys (admin token env, OIDC/service-connection,
 secret-reference style, runners, branch protection) and the target-specific
 wiring keys (aws-ecs: ECR repo, OIDC role, cluster/service; databricks:
 host/token, artifact path, Unity Catalog). These mirror the
 ``bootstrap.local.yaml`` / ``secrets.local.yaml`` shapes
 (:mod:`cicdgen.localconfig.models`). **KEY NAMES only** — never values.
 3. **Irreducible manual steps** for the new axes: the first admin
 token, org/account-level OIDC identity-provider creation, Databricks
 workspace/metastore creation, environment-reviewer setup — reusing the
 bootstrap report's :class:`~cicdgen.bootstrap.report.ManualStep` concept.
 4. **An ordered checklist** the operator follows to complete the migration.

Contracts
---------
* **Value-free.** The runbook names ``.local`` KEYS and env-var NAMES only,
 derived structurally from the new axes. No secret value is read or rendered — it
 is impossible for one to leak because none is ever consulted.
* **Deterministic.** The body is a pure function of the ``AxisChange`` and the new
 axes — no timestamps, no clock, no environment. Two renders of the same input
 are byte-identical, so a committed runbook is stable across re-runs.
* **Data-driven, not hard-branchy.** Per-axis facts live in small tables keyed by
 the vendor / target identifier, so adding a vendor/target is a table entry, not
 a new ``if``. An unknown axis degrades gracefully to a generic note rather than
 crashing (the generator supports any axis combination).
* **Standalone.** This module provides a renderer + writer; it does not touch
 ``generate`` / ``plan`` signatures. The agent/CLI calls
:func:`write_migration_runbook` after ``generate`` when
 ``result.axis_change.is_axis_change`` is ``True``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cicdgen.bootstrap.report import ManualStep
from cicdgen.core.migration import AxisChange

#: Repo-relative location of the committed runbook.
RUNBOOK_RELPATH = "MIGRATION_RUNBOOK.md"


# ---------------------------------------------------------------------------
# Per-axis knowledge tables — KEY NAMES only, value-free
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class KeySpec:
    """One ``.local`` key the operator must populate for the new axis.

 Attributes:
 key: the dotted path into ``bootstrap.local.yaml`` (or a bare secret NAME
 for entries in the ``secrets:`` catalog). A KEY, never a value.
 note: a value-free hint about what the key holds / where it comes from.
 """

    key: str
    note: str


# -- CI-vendor axis: how the pipeline authenticates + references secrets ------
# Keyed by ci_vendor identifier (matches CiVendor enum values). Each entry lists
# the vendor-specific bootstrap.local.yaml keys the operator populates for that
# vendor. Shared keys (repo, runners, branch_protection) are added once below.
_CI_VENDOR_KEYS: dict[str, tuple[KeySpec, ...]] = {
    "github": (
        KeySpec("vendor_admin.token_env", "env-var NAME of the GitHub admin PAT"),
        KeySpec(
            "oidc.aws.role_name",
            "IAM role GitHub Actions assumes via OIDC (keyless) — if the target is on AWS",
        ),
        KeySpec(
            "reviewers.<group>",
            "GitHub usernames wired into environment reviewers for manual-gate stages",
        ),
    ),
    "azure-devops": (
        KeySpec("vendor_admin.token_env", "env-var NAME of the Azure DevOps admin PAT"),
        KeySpec(
            "oidc.azure.client_id",
            "workload-identity-federation / service-connection app id — if the target is on Azure",
        ),
        KeySpec(
            "oidc.azure.tenant_id",
            "Entra ID tenant the service connection federates against",
        ),
        KeySpec(
            "reviewers.<group>",
            "Azure DevOps member ids wired into environment approvals (variable-group / approval checks)",
        ),
    ),
}

# Secret-reference *style* per vendor — value-free, documents how generated files
# reference secrets by KEY so the operator knows what to expect.
_CI_VENDOR_SECRET_STYLE: dict[str, str] = {
    "github": "generated files reference secrets by key as ${{ secrets.NAME }}",
    "azure-devops": "generated files reference secrets by key as $(NAME) via a variable group",
}

# -- Target-platform axis: what the deploy wiring needs -----------------------
# Keyed by target_platform identifier (matches TargetPlatform enum values). Lists
# the target block's bootstrap.local.yaml keys + the secret catalog entries the
# new target needs. KEY NAMES only.
_TARGET_PLATFORM_KEYS: dict[str, tuple[KeySpec, ...]] = {
    "aws-ecs": (
        KeySpec("target.aws-ecs.ecr_repo_secret", "secret NAME holding the ECR repo URI"),
        KeySpec("target.aws-ecs.cluster", "ECS cluster name"),
        KeySpec("target.aws-ecs.service", "ECS service name"),
        KeySpec("target.aws-ecs.task_family", "ECS task-definition family"),
        KeySpec("oidc.aws.account_id", "12-digit AWS account id for the deploy role trust"),
        KeySpec("oidc.aws.region", "default region for deploy actions"),
    ),
    "databricks": (
        KeySpec("target.databricks.host_secret", "secret NAME holding the workspace URL"),
        KeySpec("target.databricks.token_secret", "secret NAME holding the workspace PAT"),
        KeySpec("target.databricks.job_name", "Databricks job to create/update"),
        KeySpec("target.databricks.artifact_path", "DBFS/Volumes upload target (optional)"),
        KeySpec("target.databricks.unity_catalog", "Unity Catalog name if UC wiring is used (optional)"),
    ),
}

# Secret-catalog NAMES each target typically needs (the secrets: list). KEY NAMES
# only — the values live in the git-ignored secrets.local.yaml.
_TARGET_PLATFORM_SECRET_NAMES: dict[str, tuple[str, ...]] = {
    "aws-ecs": ("ECR_REPO",),
    "databricks": ("DATABRICKS_HOST", "DATABRICKS_TOKEN"),
}


# ---------------------------------------------------------------------------
# Irreducible manual steps for the NEW axes — value-free
# ---------------------------------------------------------------------------
def _manual_steps_for_axes(
    *, new_ci_vendor: str, new_target_platform: str
) -> list[ManualStep]:
    """Derive the irreducible manual prerequisites for the NEW vendor/target.

 Mirrors :func:`cicdgen.bootstrap.report._manual_steps` but is driven purely by
 the new *axes* (not a loaded config), since the runbook is emitted at generate
 time before the new ``.local`` wiring necessarily exists. Each step is emitted
 only when the new axes make it applicable, and is value-free.
 """
    steps: list[ManualStep] = []

    # ALWAYS: the first admin token is a human-provided credential.
    steps.append(
        ManualStep(
            concern="secrets",
            description=(
                "Create a first admin token/PAT for the new vendor and export it into "
                "the env var named by 'vendor_admin.token_env' before running bootstrap."
            ),
            why=(
                "Providing the first admin credential is inherently a human action — "
                "the bootstrapper authenticates with it but cannot mint it for you."
            ),
        )
    )

    # OIDC / identity-provider creation is axis-dependent and often org/account-level.
    if new_target_platform == "aws-ecs":
        steps.append(
            ManualStep(
                concern="oidc",
                description=(
                    "Create or confirm the account/org-level AWS IAM OIDC identity "
                    "provider for the new CI vendor, then record its ARN as "
                    "'oidc.aws.provider_arn' (or let bootstrap create the role trust)."
                ),
                why=(
                    "Org-level IdP creation is often account-managed (one provider "
                    "shared across repos), so it sits outside a per-repo bootstrap."
                ),
            )
        )
    if new_ci_vendor == "azure-devops" or new_target_platform == "databricks":
        # Azure DevOps federates at tenant level; a Databricks-on-Azure target also
        # needs the Azure app registration / federated-credential subject.
        steps.append(
            ManualStep(
                concern="oidc",
                description=(
                    "Register or confirm the Azure app registration and its "
                    "federated-credential subject (service connection) at tenant level "
                    "for this repo; record 'oidc.azure.client_id' and 'oidc.azure.tenant_id'."
                ),
                why=(
                    "Tenant-level app registration is usually org-managed and may require "
                    "directory privileges a per-repo bootstrap does not hold."
                ),
            )
        )

    # Databricks workspace/metastore provisioning is account-level.
    if new_target_platform == "databricks":
        steps.append(
            ManualStep(
                concern="target",
                description=(
                    "Create or confirm the Databricks workspace and Unity Catalog "
                    "metastore before bootstrap wires the deploy job."
                ),
                why=(
                    "Workspace and metastore provisioning is an account-level action "
                    "(flagged in the Databricks IaC README); it precedes per-repo wiring."
                ),
            )
        )

    # Environment reviewers must be real accounts the vendor recognises.
    steps.append(
        ManualStep(
            concern="reviewers",
            description=(
                "Populate the 'reviewers.<group>' groups with real accounts on the new "
                "vendor so manual-gate stages map to environment reviewers/approvals."
            ),
            why=(
                "Reviewer identities are vendor-side accounts the operator controls; the "
                "generator references the group name but cannot invent the members."
            ),
        )
    )

    return steps


# ---------------------------------------------------------------------------
# Rendering — the value-free, deterministic MIGRATION_RUNBOOK.md body
# ---------------------------------------------------------------------------
def _required_keys_for_axes(
    *, new_ci_vendor: str, new_target_platform: str
) -> tuple[tuple[KeySpec, ...], tuple[KeySpec, ...], tuple[str, ...]]:
    """Collect the vendor keys, target keys, and secret NAMES for the new axes.

 Shared, every-vendor keys (repo / runners / branch_protection) are prepended
 to the vendor-specific set so the operator sees the full required spine.
 Unknown axes contribute nothing beyond the shared spine (graceful fallback).
 """
    shared = (
        KeySpec("repo.owner", "org/user/group that owns the repo on the new vendor"),
        KeySpec("repo.name", "repository name on the new vendor"),
        KeySpec("runners.mode", "shared (vendor-hosted) or self-hosted runners"),
        KeySpec(
            "runners.registration_token_env",
            "env-var NAME of the runner registration token — required only when mode is self-hosted",
        ),
        KeySpec("branch_protection.<branch>", "merge rules + required checks for the new vendor"),
    )
    vendor_keys = shared + _CI_VENDOR_KEYS.get(new_ci_vendor, ())
    target_keys = _TARGET_PLATFORM_KEYS.get(new_target_platform, ())
    secret_names = _TARGET_PLATFORM_SECRET_NAMES.get(new_target_platform, ())
    return vendor_keys, target_keys, secret_names


def render_migration_runbook(
    axis_change: AxisChange,
    *,
    new_ci_vendor: str | None = None,
    new_target_platform: str | None = None,
) -> str:
    """Render the ``MIGRATION_RUNBOOK.md`` body for a vendor/target swap.

 The runbook covers, for the NEW vendor/target: what changed, the required
 ``.local`` KEYS to populate (value-free), the irreducible manual steps, and an
 ordered checklist. Deterministic: a pure function of the inputs with no
 timestamp, so two renders are byte-identical.

 Args:
 axis_change: the classified swap. Its ``new_*`` fields are the default
 source of the new axes.
 new_ci_vendor: override for the new CI vendor (defaults to
 ``axis_change.new_ci_vendor``).
 new_target_platform: override for the new target platform (defaults to
 ``axis_change.new_target_platform``).

 Returns:
 The full Markdown body (ends with a single trailing newline).
 """
    ci_vendor = new_ci_vendor or axis_change.new_ci_vendor
    target_platform = new_target_platform or axis_change.new_target_platform

    vendor_keys, target_keys, secret_names = _required_keys_for_axes(
        new_ci_vendor=ci_vendor, new_target_platform=target_platform
    )
    manual = _manual_steps_for_axes(
        new_ci_vendor=ci_vendor, new_target_platform=target_platform
    )
    secret_style = _CI_VENDOR_SECRET_STYLE.get(ci_vendor)

    lines: list[str] = []
    lines.append("# Migration Runbook")
    lines.append("")
    lines.append(
        "Generated on a vendor/target swap. It is **value-free**: it names the "
        "`.cicd/*.local.yaml` KEYS and env-var NAMES you must populate for the new "
        "vendor/target — never any secret value."
    )
    lines.append("")

    # 1. What changed -------------------------------------------------------
    lines.append("## 1. What changed")
    lines.append("")
    lines.append(f"- {axis_change.summary()}")
    if axis_change.ci_changed:
        lines.append(
            f"- CI vendor: `{axis_change.old_ci_vendor}` -> `{axis_change.new_ci_vendor}`"
        )
    if axis_change.target_changed:
        lines.append(
            f"- Target platform: `{axis_change.old_target_platform}` -> "
            f"`{axis_change.new_target_platform}`"
        )
    lines.append("")

    # 2. Required.local keys ----------------------------------------------
    lines.append("## 2. Required `.local` keys to populate")
    lines.append("")
    lines.append(
        "Populate these in `.cicd/bootstrap.local.yaml` (wiring) and "
        "`.cicd/secrets.local.yaml` (secret VALUES, git-ignored). KEY NAMES only below."
    )
    lines.append("")
    lines.append(f"### CI vendor: `{ci_vendor}`")
    lines.append("")
    if secret_style:
        lines.append(f"- _Secret reference style_: {secret_style}.")
    for spec in vendor_keys:
        lines.append(f"- `{spec.key}` — {spec.note}")
    lines.append("")
    lines.append(f"### Target platform: `{target_platform}`")
    lines.append("")
    if target_keys:
        for spec in target_keys:
            lines.append(f"- `{spec.key}` — {spec.note}")
    else:
        lines.append(
            "- (no target-specific keys recognised for this platform — see the "
            "per-target IaC README for its wiring)"
        )
    lines.append("")
    lines.append("#### Secret catalog names (`bootstrap.local.yaml` `secrets:`)")
    lines.append("")
    if secret_names:
        for name in secret_names:
            lines.append(
                f"- `{name}` — list in the `secrets:` catalog; put its VALUE only in "
                f"`secrets.local.yaml` (never committed)"
            )
    else:
        lines.append("- (no target-specific secret names recognised for this platform)")
    lines.append("")

    # 3. Irreducible manual steps ------------------------------------------
    lines.append("## 3. Irreducible manual steps")
    lines.append("")
    lines.append(
        "These cannot be automated by bootstrap and must be done by a human "
        "(documented, never hidden):"
    )
    lines.append("")
    for step in manual:
        lines.append(f"- **({step.concern})** {step.description}")
        lines.append(f"  - _why_: {step.why}")
    lines.append("")

    # 4. Ordered checklist --------------------------------------------------
    lines.append("## 4. Migration checklist")
    lines.append("")
    checklist: list[str] = []
    checklist.append(
        "Copy `.cicd/bootstrap.local.yaml.example` and `.cicd/secrets.local.yaml.example` "
        "(drop the `.example` suffix) if the new `.local` files do not yet exist."
    )
    checklist.append(
        "Fill the required `.local` KEYS from section 2 for the new vendor/target."
    )
    checklist.append("Complete every irreducible manual step in section 3.")
    checklist.append(
        "Run bootstrap in dry-run first, then `--apply`, and review the "
        "automated-vs-manual report."
    )
    checklist.append(
        "Regenerate and commit; the generator removes the old vendor/target files it "
        "owns and writes the new ones."
    )
    for i, item in enumerate(checklist, start=1):
        lines.append(f"{i}. [ ] {item}")
    lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Writing the committed runbook
# ---------------------------------------------------------------------------
def write_migration_runbook(
    repo_root: Path | str,
    axis_change: AxisChange,
    *,
    new_ci_vendor: str | None = None,
    new_target_platform: str | None = None,
) -> Path:
    """Render and write ``MIGRATION_RUNBOOK.md`` at the repo root; return its path.

 Intended to be called by the agent/CLI after ``generate`` when
 ``result.axis_change`` is a real axis change
 (``axis_change.is_axis_change`` is ``True``). The content is deterministic:
 writing it on two identical swaps yields byte-identical files.

 Args:
 repo_root: the target repo the swap regenerated into.
 axis_change: the classified swap.
 new_ci_vendor / new_target_platform: optional overrides for the new axes
 (default to ``axis_change.new_*``).

 Returns:
 The path to the written ``MIGRATION_RUNBOOK.md``.
 """
    body = render_migration_runbook(
        axis_change,
        new_ci_vendor=new_ci_vendor,
        new_target_platform=new_target_platform,
    )
    path = Path(repo_root) / RUNBOOK_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so the single trailing "\n" in the body is written verbatim —
    # byte-stability on every platform, matching the generator's writer.
    path.write_text(body, encoding="utf-8", newline="")
    return path
