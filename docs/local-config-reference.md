# Local-Config Reference — `.cicd/*.local.yaml`

The local config is the **non-generatable wiring** and the **secret values** a
client supplies once. It is git-ignored (`.gitignore` excludes `.cicd/*.local.*`)
so nothing here ever lands in version control. Two committed `*.example`
templates document every key; you copy them, drop the `.example` suffix, and fill
them in:

```bash
cp .cicd/bootstrap.local.yaml.example .cicd/bootstrap.local.yaml
cp .cicd/secrets.local.yaml.example   .cicd/secrets.local.yaml
```

This reference is **value-free** by construction: it names keys only, exactly as
the loader and models do. Sources:

- `generator/cicdgen/localconfig/models.py` — the Pydantic models
  (`extra="forbid"`, so an unknown key fails naming itself);
- `generator/cicdgen/localconfig/loader.py` — reads both files, cross-checks the
  secret catalog, resolves `*_env` tokens from the environment;
- the committed [`.cicd/bootstrap.local.yaml.example`](../.cicd/bootstrap.local.yaml.example)
  and [`.cicd/secrets.local.yaml.example`](../.cicd/secrets.local.yaml.example).

---

## The secrets contract (R6) — read this first

The product's headline safety rule is **secrets by reference, never by value**:

- **`bootstrap.local.yaml` holds NO secret values.** It holds secret *names* (the
  `secrets:` catalog), non-secret *wiring*, and env-var *names* (`*_env` keys)
  that point at where a token lives in the environment.
- **`secrets.local.yaml` holds the secret VALUES**, keyed by the names declared in
  the catalog. It is the only file that contains values, and it is git-ignored
  (R6.6 / R9.3).
- Generated pipeline files reference secrets by **key only** — e.g.
  `${{ secrets.ECR_REPO }}` on GitHub, `$(ECR_REPO)` on Azure DevOps — never the
  value (R6.3).
- Admin and runner tokens are read from the **environment by name** at bootstrap
  time via `*_env` keys; their values are never written into any file.
- Values are wrapped in Pydantic `SecretStr`, so they never appear in
  `repr`/`str`/logs/exceptions. A value is retrievable only via an explicit
  `get_secret_value()` call.
- A referenced-but-empty value is treated as **missing**; the loader fails
  naming the KEY only (R6.5).

**Two independent axes.** Both files must support any `ci_vendor` ×
`target_platform` combination. Only fill the blocks relevant to the axes your
`pipeline.yaml` selects; the rest may be omitted. The loader validates the keys
the chosen axes actually require.

---

## `bootstrap.local.yaml` — the wiring

Root model: `BootstrapConfig`. The required spine is `repo`, `vendor_admin`,
`runners`, and the `secrets` catalog; `oidc`, `reviewers`, `branch_protection`,
and `target` are optional.

### `repo` (required) — `RepoConfig`

Where the generated pipeline + IaC live.

| Key | Required | Default | Notes |
|-----|----------|---------|-------|
| `owner` | **yes** | — | Org/user/group that owns the repo (GitHub org, Azure DevOps org, GitLab group…). |
| `name` | **yes** | — | Repository name. |
| `create_if_missing` | no | `false` | When `true`, bootstrap may create the repo (gated by `--apply`). |
| `default_branch` | no | `main` | Branch `branch_protection` applies to. |
| `host` | no | *(none)* | Self-hosted/enterprise host (e.g. `github.acme.com`). Omit for the public SaaS host. |

### `vendor_admin` (required) — `VendorAdmin`

The one-time admin credential reference the bootstrapper authenticates with.

| Key | Required | Default | Notes |
|-----|----------|---------|-------|
| `token_env` | **yes** | — | **Name** of the env var holding the admin PAT/token. The value is read from the environment at bootstrap time and wrapped as a secret — never stored in the file. |
| `api_url` | no | *(none)* | Override the API base URL for enterprise/self-hosted. |

### `oidc` (optional) — `OidcConfig`

Keyless cloud auth. Both sub-blocks are optional; fill the one(s) your target
uses. A cross-cloud setup may fill both.

**`oidc.aws`** — `AwsOidc`

| Key | Required | Default | Notes |
|-----|----------|---------|-------|
| `account_id` | **yes** (when `oidc.aws` present) | — | 12-digit AWS account id. |
| `role_name` | **yes** | — | IAM role the pipeline assumes via OIDC. |
| `region` | **yes** | — | Default region for deploy actions. |
| `thumbprint` | no | `auto` | OIDC provider cert thumbprint; `auto` = fetch at bootstrap. |
| `provider_arn` | no | *(none)* | Reuse an existing IAM OIDC provider ARN. When absent, provider creation becomes a manual step (see the bootstrap guide). |

**`oidc.azure`** — `AzureOidc`

| Key | Required | Default | Notes |
|-----|----------|---------|-------|
| `tenant_id` | **yes** (when `oidc.azure` present) | — | Entra ID tenant (GUID). |
| `subscription_id` | **yes** | — | Target subscription (GUID). |
| `client_id` | **yes** | — | App registration (workload-identity-federation) client id. |
| `resource_group` | no | *(none)* | Resource group for created resources. |

### `runners` (required) — `RunnersConfig`

Where pipeline jobs execute.

| Key | Required | Default | Notes |
|-----|----------|---------|-------|
| `mode` | no | `shared` | `shared` (vendor-hosted) or `self-hosted`. |
| `registration_token_env` | conditional | *(none)* | **Name** of the env var holding the runner registration token. **Required only when `mode: self-hosted`** (enforced by a model validator). Value read from env, never written. |
| `labels` | no | *(none)* | Labels/pool tags to target specific runners. |

### `reviewers` (optional)

A map of logical group name → list of real accounts. The group name is what the
IR's `approvals.from_group` references; the bootstrapper wires the members into
the vendor's environment reviewers / approval checks.

```yaml
reviewers:
  platform-approvers: [alice, bob]
```

### `branch_protection` (optional)

A map keyed by branch name → `BranchProtection`.

| Key | Required | Default | Notes |
|-----|----------|---------|-------|
| `required_approvals` | **yes** | — | PR approvals before merge. `minimum: 0`. |
| `required_checks` | no | `[]` | Status checks that must pass (match generated stage/job names). |
| `enforce_admins` | no | `false` | When `true`, rules also apply to admins. |
| `dismiss_stale_reviews` | no | `true` | Drop approvals when new commits are pushed. |

### `target` (optional) — `TargetConfig`

Per-target-platform wiring. Every block is optional so the file supports any
`target_platform`; only the chosen block need be present. The YAML key is the
hyphenated `target_platform` enum value (e.g. `aws-ecs`), which maps to the
`aws_ecs` model field via an alias.

**`target.aws-ecs`** — `AwsEcsTarget`

| Key | Required | Default | Notes |
|-----|----------|---------|-------|
| `ecr_repo_secret` | **yes** (when block present) | — | Secret **name** holding the ECR repo URI (value lives in `secrets.local.yaml`). |
| `cluster` | **yes** | — | ECS cluster name. |
| `service` | **yes** | — | ECS service name. |
| `task_family` | **yes** | — | ECS task-definition family. |
| `container_port` | no | *(none)* | Port the container listens on (`1–65535`). |

**`target.databricks`** — `DatabricksTarget`

| Key | Required | Default | Notes |
|-----|----------|---------|-------|
| `host_secret` | **yes** (when block present) | — | Secret **name** holding the workspace URL. |
| `token_secret` | **yes** | — | Secret **name** holding the workspace PAT. |
| `job_name` | **yes** | — | Databricks job to create/update. |
| `artifact_path` | no | *(none)* | DBFS/Volumes upload target. |
| `unity_catalog` | no | *(none)* | Unity Catalog name if UC wiring is used. |

### `secrets` (required) — the catalog

A list of secret **names** this client uses. **Names only.** Every name listed
here must have a value in `secrets.local.yaml`, and vice versa — the loader
cross-checks and fails naming any missing/stray key (R6.5). Names must be
non-empty and unique.

```yaml
secrets:
  - ECR_REPO
  - DATABRICKS_HOST
  - DATABRICKS_TOKEN
```

---

## `secrets.local.yaml` — the values (git-ignored)

A flat `SECRET_NAME: value` map — the only file that holds values. Keyed by the
names declared in the catalog above.

| Rule | Detail |
|------|--------|
| Cross-checked | Every key must appear in the `bootstrap.local.yaml` `secrets:` catalog, and every catalogued name must have a value here. Mismatches fail naming the KEY. |
| Empty = missing | A key left as `""` is treated as missing; the loader names it. |
| Referenced by key only | Generated files use `${{ secrets.NAME }}` (GitHub) / `$(NAME)` (Azure DevOps). The value is never inlined, committed, or logged (R6.3 / R6.6 / R9.3). |
| Pushed by name | The bootstrapper pushes each value to the vendor secret store by name. |

Shape (values shown empty, to fill locally):

```yaml
ECR_REPO: ""            # ECR repository URI
DATABRICKS_HOST: ""     # Databricks workspace URL
DATABRICKS_TOKEN: ""    # Databricks workspace PAT
```

---

## How env-var tokens are resolved

The `*_env` keys (`vendor_admin.token_env`, and `runners.registration_token_env`
when self-hosted) name environment variables. The loader resolves them from the
process environment at load time and wraps each value in `SecretStr`. A
referenced-but-unset (or empty) variable fails naming the **variable**, never its
value:

```bash
export CICD_ADMIN_TOKEN=...     # the name is what bootstrap.local.yaml references
```

---

## What the loader returns

`load_local_config(cicd_dir)` returns a frozen `LocalConfig` whose `repr`/`str`
expose only keys (secret names + resolved env-var names), never values. Reading a
value is an explicit, auditable opt-in (`get_secret(name)` /
`get_env_token(name)`), and the returned value must never be logged.
