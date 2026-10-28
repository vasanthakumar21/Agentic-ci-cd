# IR Reference — `pipeline.yaml`

The IR (Intermediate Representation) is the single, vendor-neutral `pipeline.yaml`
a developer declares. It names a `ci_vendor` and a `target_platform`
**independently** — any valid combination is generatable — and describes the
pipeline as data: environments, ordered stages, gates, approvals, timeouts,
scans, thresholds, and notifications.

This reference is derived from the two authoritative sources that must always
agree:

- the published JSON Schema: [`ir/pipeline.schema.json`](../ir/pipeline.schema.json)
  (`additionalProperties: false` everywhere — unknown fields are rejected);
- the executable validator: `generator/cicdgen/ir/models.py` (Pydantic v2 with
  `extra="forbid"`, enum checks, default resolution, and cross-field rules).

Both enforce **fail-fast** validation: an unknown field, a bad enum, or a
missing required key raises a precise, field-level error (R1, R2, R3.1).

> **Plugin existence is a separate check.** The IR *shape* is validated by the
> models above. Whether the named `ci_vendor` / `target_platform` plugins exist
> on disk is a separate layer (`generator/cicdgen/ir/plugins.py`) so IR shape can
> be validated before any plugin is authored.

---

## Top-level object

| Field | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| `version` | integer | **yes** | — | IR schema version. Must be `1` (the only supported version; enforced by both the schema `const` and a model validator). |
| `ci_vendor` | enum | **yes** | — | Where the pipeline runs (CI-vendor axis, R3). See enum below. |
| `target_platform` | enum | **yes** | — | What is built and deployed (target-platform axis, R3). Independent of `ci_vendor`. See enum below. |
| `project` | object | **yes** | — | Project identity. See [Project](#project). |
| `cloud` | object | no | *(omitted)* | Cloud context, driven by `target_platform`. See [Cloud](#cloud). |
| `environments` | array of objects | no | `[]` | Deployment environments and their gate/protection rules. Order is not significant; stages reference them by name. See [Environment](#environment). |
| `stages` | array of objects | **yes** | — | Ordered pipeline stages. Array order defines pipeline order. **At least one** stage is required (`minItems: 1`). See [Stage](#stage). |
| `thresholds` | object | no | *(omitted)* | Quality/performance gates. See [Thresholds](#thresholds). |
| `notifications` | object | no | *(omitted)* | Notification configuration. See [Notifications](#notifications). |

### `ci_vendor` enum

Values reflect the v1 CI mapping table (design §8a). Pluggable — a value is
valid when a plugin directory `plugins/ci/<value>/` exists.

| Value | CI system |
|-------|-----------|
| `github` | GitHub Actions |
| `gitlab` | GitLab CI |
| `azure-devops` | Azure DevOps Pipelines |
| `bitbucket` | Bitbucket Pipelines |
| `jenkins` | Jenkins |

### `target_platform` enum

Values reflect the v1 target mapping table (design §8b). Pluggable — a value is
valid when a plugin directory `plugins/target/<value>/` exists.

| Value | Deploy target |
|-------|---------------|
| `aws-ecs` | AWS ECS (Fargate), container image |
| `databricks` | Databricks job (no image — wheel/notebooks) |
| `azure-functions` | Azure Functions |
| `kubernetes` | Kubernetes |

---

## `project`

Project identity.

| Field | Type | Required | Default | Constraints / notes |
|-------|------|----------|---------|---------------------|
| `name` | string | **yes** | — | Project / service name. `minLength: 1`. |
| `language` | string | no | *(none)* | Primary language (e.g. `python`, `node`, `java`). `minLength: 1`. The agent may infer it during discovery. |

---

## `cloud`

Cloud context, driven by `target_platform`. The whole block is optional; when
omitted, documented defaults apply downstream (R2.2). When present, `provider`
is required.

| Field | Type | Required | Default | Constraints / notes |
|-------|------|----------|---------|---------------------|
| `provider` | enum | **yes** (when `cloud` is present) | — | One of `aws`, `azure`, `gcp`. |
| `region` | string | no | *(none)* | Cloud region (e.g. `uksouth`, `eu-west-1`). `minLength: 1`. |
| `auth` | enum | no | `oidc` | `oidc` (preferred, keyless) or `keys` (discouraged). |

---

## `environment`

Each entry is a deployment environment and its gate/protection rules (R2).
Stages reference an environment by its `name`.

| Field | Type | Required | Default | Constraints / notes |
|-------|------|----------|---------|---------------------|
| `name` | string | **yes** | — | Environment name (e.g. `dev`, `staging`, `prod`). `minLength: 1`. Referenced by `stage.environment`. **Names must be unique** across `environments` (cross-field rule). |
| `gate` | enum | no | `auto` | `auto` = automatic promotion; `manual` = requires a manual gate. |
| `protection` | enum | no | `none` | `none` or `required-approvals` (branch/environment protection). |

---

## `stage`

Each entry is a single ordered pipeline stage (R1/R2). Array order = execution
order.

| Field | Type | Required | Default | Constraints / notes |
|-------|------|----------|---------|---------------------|
| `id` | string | **yes** | — | Unique stage identifier (e.g. `test`, `security`, `build`, `deploy-prod`). `minLength: 1`. **Stage ids must be unique** (cross-field rule). |
| `enabled` | boolean | no | `true` | Whether the stage is emitted/run. |
| `timeout_minutes` | integer | no | *(none)* | Max wall-clock minutes before the stage is cancelled. `minimum: 1`. |
| `retries` | integer | no | `0` | Number of automatic retries on failure. `minimum: 0`. |
| `runs` | string | no | *(none)* | Command the stage runs (e.g. `make test`). `minLength: 1`. Orchestration only — the platform verbs supply the real implementation (R4). |
| `scans` | array of enum | no | *(none)* | Security scans to run. Items from `sast`, `secrets`, `dependency`. **Unique** (no scan type twice — schema `uniqueItems` + a model validator). |
| `environment` | string | no | *(none)* | Name of the environment this stage targets. `minLength: 1`. **Must match a declared `environments[].name`** (cross-field rule enforced by the validator, not the flat schema). |
| `when` | enum | no | `auto` | `auto` or `manual`. `manual` requires an operator to proceed. |
| `approvals` | object | no | *(none)* | Approval requirements before the stage proceeds (R2.1). See [Approvals](#stageapprovals). |

### `stage.approvals`

| Field | Type | Required | Default | Constraints / notes |
|-------|------|----------|---------|---------------------|
| `required` | integer | **yes** (when `approvals` is present) | — | Number of required approvals. `minimum: 1`. |
| `from_group` | string | no | *(none)* | Reviewer group name. `minLength: 1`. Resolved from local config (`.cicd/*.local.yaml`), **never inlined** into generated files. |

---

## `thresholds`

Quality/performance gates (R2.1). Optional; documented defaults apply when
omitted.

| Field | Type | Required | Default | Constraints / notes |
|-------|------|----------|---------|---------------------|
| `coverage_min_percent` | number | no | *(none)* | Minimum test-coverage percentage. `0 ≤ x ≤ 100`. |
| `max_build_minutes` | integer | no | *(none)* | Maximum allowed build time in minutes. `minimum: 1`. |

---

## `notifications`

Notification configuration (R2.1). Optional.

| Field | Type | Required | Default | Constraints / notes |
|-------|------|----------|---------|---------------------|
| `recipients` | array of string | no | *(none)* | Recipients to notify (e.g. email addresses). Items `minLength: 1`. **Unique** and non-empty (schema `uniqueItems` + a model validator). |

---

## Documented defaults (summary)

These are resolved by the models when the field is omitted (R2.2):

| Field | Default |
|-------|---------|
| `cloud.auth` | `oidc` |
| `environment.gate` | `auto` |
| `environment.protection` | `none` |
| `stage.enabled` | `true` |
| `stage.retries` | `0` |
| `stage.when` | `auto` |

---

## Cross-field rules

The flat JSON Schema cannot express relationships between fields, so the
validator enforces these (each raises a precise message):

1. `version` must be `1`.
2. `environments[].name` values must be unique.
3. `stages[].id` values must be unique.
4. Every `stages[].environment` must name a declared environment.
5. `stages[].scans` must not repeat a scan type.
6. `notifications.recipients` must be non-empty and have no duplicates.

---

## Worked example

A `github × aws-ecs` IR (this is the shape the `examples/client-github-aws`
sample and the demo use):

```yaml
version: 1
ci_vendor: github
target_platform: aws-ecs
project:
  name: orders-api
  language: python
cloud:
  provider: aws
  region: eu-west-2
  auth: oidc            # default; shown for clarity
environments:
  - name: dev
    gate: auto          # default
  - name: prod
    gate: manual
    protection: required-approvals
stages:
  - id: test
    runs: make test
    scans: [sast, secrets]
  - id: build
    runs: docker build -t $ECR_REPO:$IMAGE_TAG .
    timeout_minutes: 30
  - id: deploy-dev
    environment: dev
    runs: aws ecs update-service --cluster $ECS_CLUSTER --service $ECS_SERVICE
  - id: deploy-prod
    environment: prod
    when: manual
    runs: aws ecs update-service --cluster $ECS_CLUSTER --service $ECS_SERVICE
```

The same IR with `ci_vendor: azure-devops` and `target_platform: databricks`
(and a matching `cloud.provider`) is equally valid — the two axes are
independent. Any `$VAR` in `runs` is a shell reference the pipeline resolves from
secrets/settings; the IR never inlines a secret value (R6).
