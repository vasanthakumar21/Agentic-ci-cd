# Bootstrap Guide

Generation produces the *committed* artifacts (pipeline file, IaC, support files,
manifest). **Bootstrap** performs the one-time vendor/cloud *wiring* those
artifacts assume: pushing secrets, creating the OIDC trust, registering runners,
setting branch protection, and (optionally) creating the repo. It reads the
non-generatable wiring from `.cicd/*.local.yaml` (see the
[local-config reference](local-config-reference.md)) and nothing else.

Code: `generator/cicdgen/bootstrap/` — `planner.py` (plan), `apply.py` (execute),
`report.py` (automated-vs-manual report).

---

## Dry-run by default (R7 / R9.2)

Bootstrap is safe to run blind: it does **nothing** mutating unless you
explicitly opt in. Two entry points plan without executing:

- `plan_bootstrap(cicd_dir)` → a `BootstrapPlan` describing what an `--apply`
  run *would* do, grouped by concern, in a stable order. It only loads +
  validates the config; it never touches a vendor/cloud API.
- `apply_bootstrap(cicd_dir, executor=..., confirm=False)` (the default) →
  re-plans and returns an `ApplyResult` where every action is `SKIPPED` and **no
  mutating executor method is called**.

Live actions happen only when you pass `confirm=True` (the `--apply` + confirm
gate). This mirrors the generator's own dry-run posture (design §6).

```python
from cicdgen.bootstrap.planner import plan_bootstrap, print_plan

plan = plan_bootstrap(".cicd")     # loads + validates, executes nothing
print_plan(plan)                   # value-free "what would happen" report
```

---

## `--apply` + confirm, and the `Executor` seam

All live vendor/cloud operations go through a single injectable interface,
`Executor` (a `Protocol` in `apply.py`). A real implementation wraps the
GitHub / Azure DevOps / AWS / Azure APIs; tests inject a fake that simulates
existing/absent resources and records calls — so the idempotency logic is
verifiable **without a live account** (R9).

```python
from cicdgen.bootstrap.apply import apply_bootstrap, print_apply_result

# dry-run (default): no mutations
dry = apply_bootstrap(".cicd", executor=my_executor)            # confirm=False
# live: perform the plan idempotently
done = apply_bootstrap(".cicd", executor=my_executor, confirm=True)
print_apply_result(done)
```

The `Executor` methods are plain **detect-or-mutate** operations grouped by
concern:

| Concern | Detect | Mutate |
|---------|--------|--------|
| repo | `repo_exists()` | `create_repo()` |
| secrets | `secret_exists(name)` | `push_secret(name, value)` |
| oidc | `oidc_trust_exists(provider, role_name)` | `create_oidc_trust(provider, role_name)` |
| runners | `runner_registered(labels)` | `register_runner(labels, token)` |
| branch protection | `branch_protection_current(branch)` | `set_branch_protection(branch, rule)` |

**Value-free by design (R9.3 / R6.6).** `push_secret` is the only method that
ever receives a raw secret value, and `register_runner` the only one to receive a
token. The value/token is read via `LocalConfig.get_secret` /
`get_env_token` *at the moment it is handed to the executor* and never enters a
log, a `repr`, or an `ApplyResult`. Every other method sees names/wiring only.

---

## Idempotency (R7 / design §6.4)

For each planned action the apply layer **detects first**, then acts only if
needed:

- absent → **create** (`CREATED`);
- present and equal → **no-op** (`UNCHANGED`);
- present but different → **update** (`UPDATED`);
- nothing to do (shared runners, no OIDC) or a dry-run → **skip** (`SKIPPED`).

So the first `--apply` creates everything; a second `--apply` against unchanged
state yields all `UNCHANGED` and creates no duplicates; drift (e.g. a
branch-protection rule changed out of band) is reconciled on the next apply, then
stable. Secrets are a deliberate exception: a secret that already exists is left
as-is (`UNCHANGED`) rather than overwritten every run, to avoid churn and avoid
clobbering a rotated value.

Actions run in a fixed concern order — repo first (it must exist before anything
is pushed into it), then secrets, OIDC, runners, and branch protection last.

---

## The automated-vs-manual report (R7 / design §6.5 / §10)

Bootstrap is honest about its limits. `report.py` emits a two-column summary that
separates what it **automates** from the **irreducible manual** prerequisites a
human must still perform — and points each manual step at the migration runbook,
so the limitations are *documented, never hidden*.

```python
from cicdgen.bootstrap.report import emit_automation_report

print(emit_automation_report(".cicd"))   # value-free; safe to log
```

- **AUTOMATED** — the plan's automatable actions: repo, secrets, OIDC trust,
  runner registration, branch protection.
- **MANUAL** — derived from the loaded config, each emitted only when it applies.

The one-line summary reads, e.g., `Automated 7 of 9 steps; 2 manual (see
MIGRATION_RUNBOOK.md).`

---

## The irreducible manual steps

These cannot be automated by a per-repo bootstrap and are surfaced in the MANUAL
column (and in the migration runbook):

| When | Manual step | Why it can't be automated |
|------|-------------|---------------------------|
| **always** | Export the vendor admin token into the env var named by `vendor_admin.token_env`. | Providing the first admin credential is inherently a human action — bootstrap authenticates with it but cannot mint it. |
| `oidc.aws` present with no `provider_arn` | Create/confirm the account/org-level AWS IAM OIDC identity provider. | Org-level IdP creation is often account-managed (one provider shared across repos). |
| `oidc.azure` present | Register/confirm the Azure app registration + federated-credential subject at tenant level. | Tenant-level app registration is usually org-managed and may need directory privileges a per-repo bootstrap lacks. |
| `runners.mode: self-hosted` | Obtain a runner registration token into its env var. | Registration tokens are short-lived and human-minted from the vendor UI/API. |
| a `databricks` target configured | Create/confirm the Databricks workspace and Unity Catalog metastore. | Workspace/metastore provisioning is an account-level action that precedes per-repo wiring. |
| any manual gate | Populate `reviewers.<group>` with real vendor accounts. | Reviewer identities are vendor-side accounts the operator controls; the generator references the group name but cannot invent members. |

---

## Failure behaviour

A missing/empty required local-config key, or an unset required token env var,
fails **fast** in the loader with a message naming the **key** (or the env-var
name) only — never a value (R6.5 / R9.3). `plan_bootstrap` / `apply_bootstrap` /
`emit_automation_report` all let that `LocalConfigError` propagate unchanged, so
the operator sees exactly which key to fix.
