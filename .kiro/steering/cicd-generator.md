---
inclusion: always
---

# CI/CD Generator — Agent Steering

> This is the knowledge backbone that lets a Kiro agent drive the generator and
> gap-fill correctly. It encodes the product's model, the two mapping tables
> (design §8a CI-vendor axis, §8b target-platform axis), and the non-negotiable
> generation rules. Keep it in sync with `design.md`, `requirements.md`, and the
> implemented code under `generator/cicdgen/` + `plugins/`.
>
> This doc lives in the **product repo** so the agent that develops and runs the
> generator has it in context. When the product is shipped to a client repo, copy
> this file to `<client-repo>/.kiro/steering/cicd-generator.md` so the agent
> driving generation there inherits the same rules. It is pure guidance — it ships
> no secrets and references nothing client-specific.

---

## 1. What this product is

A **pipeline-as-data compiler with two independent plugin axes**. One declarative
config — the **IR** (`pipeline.yaml`, validated against `ir/pipeline.schema.json`)
— names a **CI vendor** and a **target platform** independently. An agent plus
deterministic templates/plugins compile the IR into concrete pipeline files,
platform-specific deploy steps, and backing IaC. Sensitive/client-specific values
live in local, git-ignored config and are injected **by reference** only.

This is a **product for many clients**, so it assumes nothing about the stack: a
client could be GitHub + AWS ECS (Docker image) or Azure DevOps + Databricks
(notebooks/jobs, no image).

### The two-axis model

```
  ci_vendor   ×   target_platform        →   any combination is valid
  (where it runs)   (what it deploys)
  github            aws-ecs
  gitlab            databricks
  azure-devops      azure-functions
  bitbucket         kubernetes
  jenkins           …
```

- `ci_vendor` answers **how the pipeline is expressed** (design §8a).
- `target_platform` answers **what the stage verbs actually do** (design §8b).
- The axes are **independent**. Switching either axis = change the IR + regenerate.
  Adding a new vendor or platform is **adding a plugin**, never rewriting the core.

### Design principles (do not violate)

- **Two independent axes** — `ci_vendor` × `target_platform`; any combination valid.
- **Everything is a provider plugin** — the core never hard-codes a vendor/platform.
- **Intent over syntax** — the IR says *what*; plugins + templates encode *how*.
- **Templates for structure, agent for judgement** — deterministic core, adaptive edges.
- **Secrets by reference, never by value** — values stay local; files hold keys only.
- **Compile-time, committed, reviewable** — generation produces git-tracked diffs.
- **Idempotent bootstrap** — re-runnable, dry-run by default.
- **Per-client isolation** — each client has its own IR + local config; no cross-contamination.

---

## 2. Mapping table §8a — CI-vendor axis (how the pipeline is expressed)

Reproduced from design §8a. A CI-vendor plugin
(`plugins/ci/<name>/`) renders the vendor-neutral stage graph into this vendor's
syntax. It knows order/gates/approvals/timeouts/secret-refs/OIDC — it does **not**
know what a stage *does* (that comes from the target verbs).

| IR concept   | GitHub Actions         | GitLab CI          | Azure DevOps          | Bitbucket          | Jenkins            |
|--------------|------------------------|--------------------|-----------------------|--------------------|--------------------|
| stage order  | `jobs` + `needs`       | `stages` + `stage` | `stages`/`jobs` dep   | `pipelines.*.step` | `stage{}` in order |
| manual gate  | `environment` + review | `when: manual`     | approval check / env  | `trigger: manual`  | `input` step       |
| approvals    | env reviewers          | protected env      | env approvals/checks  | deployment perms   | `input submitter`  |
| timeout      | `timeout-minutes`      | `timeout:`         | `timeoutInMinutes`    | `max-time`         | `timeout(){}`      |
| secrets ref  | `${{ secrets.X }}`     | `$X` (masked)      | `$(X)` / var group    | `$X` (repo var)    | `credentials()`    |
| cloud OIDC   | `aws-actions` OIDC     | ID tokens OIDC     | workload identity fed | OIDC identity      | plugin/assume-role |

Implemented CI plugins today: `plugins/ci/github/`, `plugins/ci/azure-devops/`.
The other three columns are the roadmap for later plugins (task 12) — encoded here
so the agent knows the target mapping before the plugin exists.

**Pipeline file each vendor emits:** `.github/workflows/*.yml` (GitHub),
`azure-pipelines.yml` (Azure DevOps), `.gitlab-ci.yml` (GitLab),
`bitbucket-pipelines.yml` (Bitbucket), `Jenkinsfile` (Jenkins).

---

## 3. Mapping table §8b — target-platform axis (what the verbs do)

Reproduced from design §8b. A target-platform plugin
(`plugins/target/<name>/`) implements the six stage verbs and supplies the backing
IaC. The core calls the verbs **uniformly**; what each verb *does* is the
platform's business.

| Verb     | aws-ecs                        | databricks                              | azure-functions        | kubernetes          |
|----------|--------------------------------|-----------------------------------------|------------------------|---------------------|
| build    | docker build                   | assemble notebooks / build wheel        | build function package | docker build        |
| package  | push image to ECR              | upload artifacts to DBFS/Volumes        | zip + stage            | push to registry    |
| deploy   | update ECS service/task def    | create/update Databricks job via CLI/API| `func azure publish`   | `kubectl`/helm apply|
| verify   | ECS health check               | run job / check run state               | function smoke test    | rollout status      |
| IaC      | ECR, ECS, IAM/OIDC (TF)        | workspace, Unity Catalog, job (TF/API)  | Function App, ident.   | cluster refs, RBAC  |

Implemented target plugins today: `plugins/target/aws-ecs/`,
`plugins/target/databricks/`. The other two columns are roadmap (task 12).

### The Databricks "different shape" note (critical)

Databricks is deliberately the shape **MOST unlike aws-ecs** — it proves the
two-axis model is platform-shaped, not Docker-shaped:

- There is **NO container image, NO ECR, NO ECS**.
- `build` assembles a wheel / notebooks (e.g. `python -m build`).
- `package` uploads artifacts to **DBFS/Volumes**, not an image registry.
- `deploy` creates/updates a **Databricks job via CLI/API**, not an ECS service.
- `verify` runs the job and checks its run state.
- IaC provisions a `databricks_job` + Unity Catalog schema (Terraform/API), not
  ECR/ECS/IAM.

A verb that is a no-op on a platform (e.g. an image `package` step on Databricks)
returns an **empty `VerbResult`** — the uniform contract holds even when the
platform does nothing. Never fake a Dockerfile/ECR step for Databricks.

---

## 4. Generation rules / guardrails (non-negotiable)

These map onto the real code in `generator/cicdgen/`. Follow them; do not route
around them.

### 4.1 Validate first, fail fast
- Always validate the IR before generating (`cicdgen.ir.load_and_validate_ir`).
  Unknown fields, bad enums, missing required keys, or a named plugin that doesn't
  exist → fail with a precise, field-level message. Mirrors tekflow-ingest's
  fail-fast Pydantic pattern. Never generate from an unvalidated IR.

### 4.2 Never hard-code a vendor/platform in the core
- The core (`core/generate.py`, `plugins/dispatch.py`, `plugins/stage_contract.py`,
  `plugins/loader.py`) contains **zero** `if ci_vendor == …` / `if target_platform
  == …` branches. All vendor/platform behaviour goes **through plugins**. If you
  find yourself wanting to special-case a vendor in the core, that logic belongs in
  a plugin instead.

### 4.3 Secrets by reference ONLY — never bypass the guard
- Plugins emit secret **references** (`${{ secrets.X }}`, `$X`, `$(X)`), never
  values. The core's `core/secrets_guard.py` runs an enforcement gate before
  writing: an exact-value scan (when real values are supplied) plus a heuristic
  pattern scan. A detected leak raises and names the secret **KEY + file PATH only,
  never the value** (R9.3).
- Do **not** disable `scan_secrets`, suppress `SecretLeak`/`GenerationError`, or
  inline a value "just to get it working". If the guard fires, fix the plugin to
  emit a reference.
- Secret values live only in `.cicd/secrets.local.yaml` (git-ignored); never log,
  echo, repr, or write them into any generated/committed file.

### 4.4 Deterministic output + the manifest
- Same IR + repo → **byte-identical** output (R5.3). Preserved by: files written
  sorted by path, exact plugin content (no re-formatting), a deterministic manifest
  (sorted keys, no wall-clock timestamp).
- The generator records every file it owns in `.cicd/generated.manifest.json`
  (`MANIFEST_RELPATH`, built by `core/manifest.py`). **Commit this manifest** — it
  is the generator's memory of what it wrote.
- On an **axis swap** (change `ci_vendor` or `target_platform` + regenerate), the
  generator reads the old manifest and **removes stale files it previously owned**,
  then writes the new ones (R8.3). Do not hand-delete generated files; let the
  manifest-driven cleanup do it.

### 4.5 Dry-run by default; `--apply` requires explicit confirm
- Bootstrap (`bootstrap/planner.py` + `bootstrap/apply.py`) is **dry-run by
  default**: `apply_bootstrap(..., confirm=False)` performs ZERO mutating calls, it
  only plans (push N secrets, create OIDC role, set branch protection, register
  runners, create repo).
- Live/destructive actions are gated behind **`--apply` + explicit `confirm=True`**.
  Never set confirm on the user's behalf for destructive or shared-system actions.
- `--apply` is **idempotent**: detect-then-act (absent → create, equal → unchanged,
  different → update); re-running creates no duplicates.
- Generation itself also supports dry-run (`generate(..., dry_run=True)` / `plan()`)
  returning the exact files that would be written without touching disk.
- **Irreducible manual steps** (first admin token, org-level identity-provider
  creation, etc.) are never hidden — they go into the generated migration runbook
  (`MIGRATION_RUNBOOK.md`), with the exact `.local` keys to populate.

### 4.6 How to add a plugin WITHOUT touching the core
Plugins are discovered **from the filesystem**, not a hand-maintained registry
(R3.4). To add one:

1. Create a directory under the right family:
   - CI vendor → `plugins/ci/<name>/`
   - target platform → `plugins/target/<name>/`
   The directory `<name>` MUST equal the IR value (`ci_vendor` / `target_platform`).
2. Make it an importable package (`__init__.py`).
3. Expose the concrete class one of two ways (loader tries them in this order):
   - **Preferred — `get_plugin()` factory**: a module-level callable returning an
     instance of the right base class. Example (`plugins/target/databricks/__init__.py`):
     ```python
     from .plugin import Databricks
     def get_plugin() -> Databricks:
         return Databricks()
     ```
   - **Fallback — subclass discovery**: expose exactly one concrete subclass of the
     family base class; the loader instantiates it with no args.
4. Subclass the correct base from `generator/cicdgen/plugins/base.py`:
   - CI vendor → `CIVendorPlugin`, implement `emit_pipeline_files(graph)`.
   - target platform → `TargetPlatformPlugin`, implement the six verbs (`lint`,
     `test`, `build`, `package`, `deploy`, `verify`) + `supply_iac(pipeline)`.
5. Set `name` on the plugin to equal the directory/IR name. The loader validates
   that the IR value, the folder, and `plugin.name` all agree, and that the family
   matches — otherwise it raises `PluginLoadError` naming the plugin and cause.

Never register a plugin by editing the core. Existence of the directory + the
exposure convention *is* the registration.

### 4.7 Stage → verb mapping policy, and the orchestration/business-logic split
Stage ids are user-defined, so the core maps a stage to verbs by **semantics**, as
declarative data in `plugins/stage_contract.py` (`STAGE_VERB_POLICY`), not control
flow. First matching stage-id substring wins (case-insensitive):

| Stage shape | id contains (any of)                                   | Verbs (in order)   |
|-------------|--------------------------------------------------------|--------------------|
| deploy      | `deploy`, `release`, `rollout`, `promote`              | `deploy`, `verify` |
| build       | `build`, `package`, `publish`, `image`, `compile`      | `build`, `package` |
| test        | `test`, `lint`, `check`, `verify`, `qa`, `unit`, `integration` | `lint`, `test` |
| none (CI-only) | `security`, `scan`, `approval`, `notify`            | *(none)*           |

- A `security`/scan stage runs **no target verbs** — scans are expressed entirely
  by the CI vendor (§8a). A disabled stage (`enabled: false`) contributes no verbs.
- An id the policy doesn't recognise yields **no verbs** (safe default) rather than
  guessing. A client whose stage ids don't match can pin verbs via an `explicit`
  override — still platform-agnostic data, not code.
- **Hard rule:** *orchestration stays in the CI plugin; business logic lives in the
  target verbs.* Generated CI files only arrange order/gates/triggers and **call**
  the verbs; they contain no platform business logic (R4.3). The CI plugin never
  inspects what a verb's commands do. The target plugin never arranges pipeline
  order. Keep that boundary clean.

---

## 5. How the agent gap-fills (R5.2)

The agent adapts **repo-specific context** without ever rewriting core structure or
bypassing plugins. Gap-filling is for judgement at the edges, not for replacing the
deterministic core.

**You MAY gap-fill:**
- Detected **languages** and the right toolchain commands (e.g. `make test`,
  `pytest`, `npm test`) for a stage's `runs`.
- **Existing scripts** in the repo — wire the pipeline to call them rather than
  reinventing them.
- **Matrix needs** (e.g. multiple Python/Node versions) where the IR/plugin exposes
  that knob.
- Sensible **defaults** for omitted IR knobs (documented defaults apply; R2.2).
- Repo-specific **non-secret settings** forwarded via `settings` into verb contexts
  (region, project name, …).

**You MUST NOT:**
- Rewrite or bypass the **templates/plugins** for core structure (R5.2). Structure
  comes from the plugin; the agent fills gaps around it.
- Hard-code a vendor/platform anywhere in the core.
- Inline a secret value or weaken the secrets guard (see §4.3).
- Add or invent **new verbs** or new pipeline-file shapes — use the six verbs and
  the plugin's emitted files.
- **Silently assume** a client's `ci_vendor` / `target_platform`. On onboarding,
  detect stack signals (language, existing pipeline, Dockerfile vs notebooks, IaC,
  cloud hints) and **propose a draft IR** for the developer to confirm/adjust before
  generating (R10.1/R10.2). Never generate off an unconfirmed guess.
- Cross-contaminate clients — each client has its own IR + `.cicd/*.local.yaml`
  (R10.3).

**The gap-fill contract in one line:** adapt the *inputs* (IR values, `runs`,
settings, matrix) to the repo; never alter the *machinery* (core, plugins, verb
contract, secrets guard, manifest).

---

## 6. Quick reference — real paths & conventions

- IR schema: `ir/pipeline.schema.json`
- Plugin interface / base classes: `generator/cicdgen/plugins/base.py`
  (`CIVendorPlugin`, `TargetPlatformPlugin`, `StageVerb`, `VerbContext`, `VerbResult`,
  `GeneratedFile`, `IaCArtifacts`, `StageGraph`)
- Plugin loader (name → instance, `get_plugin()` convention): `generator/cicdgen/plugins/loader.py`
- Core dispatch (loads both plugins, no vendor knowledge): `generator/cicdgen/plugins/dispatch.py`
- Stage → verb policy: `generator/cicdgen/plugins/stage_contract.py`
- Generator core (validate → dispatch → write → manifest): `generator/cicdgen/core/generate.py`
- Secrets guard: `generator/cicdgen/core/secrets_guard.py`
- Manifest (`.cicd/generated.manifest.json`): `generator/cicdgen/core/manifest.py`
- Local config loader (git-ignored, value-free logging): `generator/cicdgen/localconfig/`
- Bootstrap (dry-run planner + `--apply` executor): `generator/cicdgen/bootstrap/`
- CI plugins: `plugins/ci/github/`, `plugins/ci/azure-devops/`
- Target plugins: `plugins/target/aws-ecs/`, `plugins/target/databricks/`
- Local config (NOT committed): `.cicd/secrets.local.yaml`, `.cicd/bootstrap.local.yaml`
  (committed `*.example` docs every key)
