# Migration Runbook Template

When a regenerate **changes an axis** — a different `ci_vendor`, a different
`target_platform`, or both — the generator records the swap and emits a committed
`MIGRATION_RUNBOOK.md` at the repo root. It is the honest, value-free guide that
tells an operator exactly what to do to complete the migration.

Code: `generator/cicdgen/core/runbook.py` (`render_migration_runbook`,
`write_migration_runbook`). The axis-swap signal it consumes comes from
`generator/cicdgen/core/migration.py` (`AxisChange`), which compares the prior
`.cicd/generated.manifest.json` axes against the new run's axes.

---

## When it is emitted

After `generate()` on a regenerate whose `result.axis_change.is_axis_change` is
`True`. A first run (no prior manifest) and a same-axes regenerate do **not**
emit a runbook — there is nothing to migrate. The generator already removes the
old axis's files via manifest ownership (R8.3); the runbook documents the *human*
side of the swap.

## Guarantees

- **Value-free (R9.3).** The runbook names `.local` KEYS and env-var NAMES only.
  No secret value is ever read or rendered — it is structurally impossible for
  one to leak, because none is consulted.
- **Deterministic (R5.3).** The body is a pure function of the `AxisChange` and
  the new axes — no timestamps, no clock. Two renders of the same swap are
  byte-identical.
- **Data-driven.** Per-axis facts live in small tables keyed by the vendor /
  target identifier, so an unknown axis degrades gracefully to a generic note
  rather than crashing.

---

## The four sections

### 1. What changed

The old → new swap for each axis that changed, from `AxisChange.summary()`:

```
- axis change — ci_vendor: github -> azure-devops
- CI vendor: `github` -> `azure-devops`
```

(For a target swap it names `target_platform: aws-ecs -> databricks` instead or
as well.)

### 2. Required `.local` keys to populate

The `bootstrap.local.yaml` / `secrets.local.yaml` KEYS the **new** axes need —
KEY NAMES only (see the [local-config reference](local-config-reference.md)).
The shared spine (`repo.owner`, `repo.name`, `runners.mode`,
`runners.registration_token_env`, `branch_protection.<branch>`) is always listed,
then the vendor-specific keys and the target-specific keys and secret-catalog
names for the new axes. It also states the vendor's **secret-reference style**,
e.g. `${{ secrets.NAME }}` for GitHub or `$(NAME)` via a variable group for Azure
DevOps.

### 3. Irreducible manual steps

The human-only prerequisites for the new axes (same honesty as the bootstrap
report): the first admin token, org/account-level OIDC identity-provider
creation, Azure app-registration at tenant level, Databricks workspace/metastore
creation, and environment-reviewer setup — each emitted only when the new axes
make it applicable, each with a plain-language *why*.

### 4. Migration checklist

An ordered, checkbox checklist the operator follows:

1. Copy the `.cicd/*.local.yaml.example` files (drop `.example`) if the `.local`
   files don't yet exist.
2. Fill the required `.local` KEYS from section 2 for the new vendor/target.
3. Complete every irreducible manual step in section 3.
4. Run bootstrap in dry-run first, then `--apply`, and review the
   automated-vs-manual report.
5. Regenerate and commit; the generator removes the old vendor/target files it
   owns and writes the new ones.

---

## Example — a `github → azure-devops` CI swap (target `aws-ecs` unchanged)

This is the literal shape `render_migration_runbook` produces for that swap:

```markdown
# Migration Runbook

Generated on a vendor/target swap. It is **value-free**: it names the
`.cicd/*.local.yaml` KEYS and env-var NAMES you must populate for the new
vendor/target — never any secret value.

## 1. What changed

- axis change — ci_vendor: github -> azure-devops
- CI vendor: `github` -> `azure-devops`

## 2. Required `.local` keys to populate

Populate these in `.cicd/bootstrap.local.yaml` (wiring) and
`.cicd/secrets.local.yaml` (secret VALUES, git-ignored). KEY NAMES only below.

### CI vendor: `azure-devops`

- _Secret reference style_: generated files reference secrets by key as $(NAME) via a variable group.
- `repo.owner` — org/user/group that owns the repo on the new vendor
- `repo.name` — repository name on the new vendor
- `runners.mode` — shared (vendor-hosted) or self-hosted runners
- `runners.registration_token_env` — env-var NAME of the runner registration token — required only when mode is self-hosted
- `branch_protection.<branch>` — merge rules + required checks for the new vendor
- `vendor_admin.token_env` — env-var NAME of the Azure DevOps admin PAT
- `oidc.azure.client_id` — workload-identity-federation / service-connection app id — if the target is on Azure
- `oidc.azure.tenant_id` — Entra ID tenant the service connection federates against
- `reviewers.<group>` — Azure DevOps member ids wired into environment approvals (variable-group / approval checks)

### Target platform: `aws-ecs`

- `target.aws-ecs.ecr_repo_secret` — secret NAME holding the ECR repo URI
- `target.aws-ecs.cluster` — ECS cluster name
- `target.aws-ecs.service` — ECS service name
- `target.aws-ecs.task_family` — ECS task-definition family
- `oidc.aws.account_id` — 12-digit AWS account id for the deploy role trust
- `oidc.aws.region` — default region for deploy actions

#### Secret catalog names (`bootstrap.local.yaml` `secrets:`)

- `ECR_REPO` — list in the `secrets:` catalog; put its VALUE only in `secrets.local.yaml` (never committed)

## 3. Irreducible manual steps

These cannot be automated by bootstrap and must be done by a human
(documented, never hidden):

- **(secrets)** Create a first admin token/PAT for the new vendor and export it into the env var named by 'vendor_admin.token_env' before running bootstrap.
  - _why_: Providing the first admin credential is inherently a human action — the bootstrapper authenticates with it but cannot mint it for you.
- **(oidc)** Create or confirm the account/org-level AWS IAM OIDC identity provider for the new CI vendor, then record its ARN as 'oidc.aws.provider_arn' (or let bootstrap create the role trust).
  - _why_: Org-level IdP creation is often account-managed (one provider shared across repos), so it sits outside a per-repo bootstrap.
- **(oidc)** Register or confirm the Azure app registration and its federated-credential subject (service connection) at tenant level for this repo; record 'oidc.azure.client_id' and 'oidc.azure.tenant_id'.
  - _why_: Tenant-level app registration is usually org-managed and may require directory privileges a per-repo bootstrap does not hold.
- **(reviewers)** Populate the 'reviewers.<group>' groups with real accounts on the new vendor so manual-gate stages map to environment reviewers/approvals.
  - _why_: Reviewer identities are vendor-side accounts the operator controls; the generator references the group name but cannot invent the members.

## 4. Migration checklist

1. [ ] Copy `.cicd/bootstrap.local.yaml.example` and `.cicd/secrets.local.yaml.example` (drop the `.example` suffix) if the new `.local` files do not yet exist.
2. [ ] Fill the required `.local` KEYS from section 2 for the new vendor/target.
3. [ ] Complete every irreducible manual step in section 3.
4. [ ] Run bootstrap in dry-run first, then `--apply`, and review the automated-vs-manual report.
5. [ ] Regenerate and commit; the generator removes the old vendor/target files it owns and writes the new ones.
```

> The exact set of keys and manual steps varies with the new axes. A
> `target_platform: aws-ecs → databricks` swap, for instance, lists the
> `target.databricks.*` keys, the `DATABRICKS_HOST` / `DATABRICKS_TOKEN` secret
> names, and a Databricks workspace/metastore manual step instead of the ECS
> ones. The structure of the four sections is always the same.
