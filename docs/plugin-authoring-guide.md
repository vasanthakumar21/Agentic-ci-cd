# Plugin-Authoring Guide

Everything in this product is a **provider plugin**. The core never hard-codes a
vendor or a platform — adding a plugin never touches the core (R3.4). This guide
shows how to add a new CI-vendor plugin or a new target-platform plugin.

Sources: `generator/cicdgen/plugins/base.py` (the contracts + data types),
`generator/cicdgen/plugins/loader.py` (discovery), and the existing
`plugins/ci/github/` and `plugins/target/aws-ecs/` plugins as worked examples.

---

## The two axes

| Family | Directory | Base class | Question it answers |
|--------|-----------|------------|---------------------|
| CI vendor | `plugins/ci/<name>/` | `CIVendorPlugin` | *How is the pipeline expressed?* (GitHub workflow, Azure Pipelines YAML, …) |
| Target platform | `plugins/target/<name>/` | `TargetPlatformPlugin` | *What do the stage verbs actually do?* (docker build, ECR push, Databricks job, …) |

The two families share nothing beyond `ProviderPlugin` (an identity). Any
`ci_vendor` × `target_platform` combination is valid because neither knows about
the other; the core composes one of each.

---

## Discovery convention — no registry, no core edit (R3.4)

A plugin "registers" simply by **existing as a directory** under the right family
folder. The directory name must equal the IR value (`ci_vendor` /
`target_platform`). The loader imports the package at that path and obtains the
concrete class in one of two ways, tried in order:

1. **Factory (preferred).** A module-level `get_plugin()` that returns an
   instance of the right base class:

   ```python
   # plugins/ci/github/__init__.py
   from .plugin import GitHubActions

   def get_plugin() -> GitHubActions:
       return GitHubActions()
   ```

2. **Single-subclass discovery (fallback).** If there is no `get_plugin()`, the
   loader scans the package's public attributes for **exactly one** concrete
   subclass of the expected base class and instantiates it with no arguments.
   Zero or multiple candidates raise a clear `PluginLoadError`.

**Hard rule:** the plugin's `name` attribute **must equal its directory / IR
name**, so the IR value, the on-disk folder, and the loaded class all agree. The
loader validates this and raises if they disagree. Every load failure
(missing directory, no `__init__.py`, import error, wrong base class, ambiguous
candidates, name mismatch) raises `PluginLoadError` naming the plugin, its
family, and the cause.

A minimal plugin package therefore looks like:

```
plugins/ci/<name>/
  __init__.py      # exposes get_plugin() -> YourPlugin()
  plugin.py        # class YourPlugin(CIVendorPlugin): name = "<name>"; ...
  ...              # any helper modules (e.g. a deterministic yaml emitter)
```

---

## CI-vendor plugin contract

Subclass `CIVendorPlugin` and implement a single method:

```python
def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
    ...
```

- Input: a `StageGraph` — the vendor-neutral view of the pipeline (the validated
  `Pipeline` IR + the stages in declared/execution order). You read it; the core
  builds it.
- Output: every file this vendor needs written (relative POSIX paths), e.g.
  `.github/workflows/ci.yml` or `azure-pipelines.yml`. **The core owns writing +
  the manifest** — do not touch the filesystem yourself.

A CI-vendor plugin is **orchestration only** (R4.3): it arranges stage order,
manual gates, approvals, timeouts, security scans, and cloud OIDC. It does **not**
decide what a stage does — the shell command for a stage comes from the IR's
`stage.runs` (what build/deploy actually do is the target plugin's job).

### §8a — CI mapping (how a vendor expresses IR concepts)

| IR concept | GitHub Actions (worked example) |
|------------|---------------------------------|
| stage order | `jobs` + `needs` (each job `needs` the previous enabled stage → linear chain) |
| manual gate / approvals | reference a GitHub `environment:`; reviewers/protection enforced at the environment level (wired by bootstrap) |
| timeout | `timeout-minutes` on the job (from `stage.timeout_minutes`) |
| secrets | `${{ secrets.NAME }}` — by reference only (R6.3/R6.6) |
| cloud OIDC | `permissions: id-token: write` + a provider-appropriate credentials step (aws → `configure-aws-credentials`, azure → `azure/login`, gcp → `google-github-actions/auth`) |
| scans | a single `security` job gathering each requested scan step |
| retries | GitHub has no native job retry — the plugin records the intent in a comment rather than silently dropping it (an honest limitation) |

The Azure DevOps plugin maps the same IR concepts to `azure-pipelines.yml`
(stages + `dependsOn`, approval checks, environment gates, `timeoutInMinutes`,
variable groups, workload-identity federation) — same intent, different syntax.

---

## Target-platform plugin contract

Subclass `TargetPlatformPlugin` and implement **six stage verbs** plus
`supply_iac`. The core calls the verbs uniformly via `run_verb(ctx)` — you never
write a per-verb `if` ladder in the core.

```python
def lint(self, ctx: VerbContext) -> VerbResult: ...
def test(self, ctx: VerbContext) -> VerbResult: ...
def build(self, ctx: VerbContext) -> VerbResult: ...
def package(self, ctx: VerbContext) -> VerbResult: ...
def deploy(self, ctx: VerbContext) -> VerbResult: ...
def verify(self, ctx: VerbContext) -> VerbResult: ...
def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts: ...
```

Each verb returns the concrete shell steps (and any support files) for one stage
on this platform. A verb that is a no-op on a platform returns empty tuples — the
uniform contract holds even when the platform does nothing. `supply_iac` returns
the backing infrastructure (Terraform, or an API-driver script where no Terraform
provider fits) plus a short `summary` used in the migration runbook.

### §8b — target mapping (what each verb does)

| verb | `aws-ecs` (worked example) | `databricks` (the different-shape proof) |
|------|----------------------------|------------------------------------------|
| `lint` | lint the Dockerfile (hadolint if present) | lint notebooks / Python |
| `test` | run the project's tests (from `stage.runs`) | run the project's tests |
| `build` | `docker build` **+ emit a resilient Dockerfile** | assemble notebooks + build a wheel (**NO image**); emit `databricks/job.json` |
| `package` | push image to ECR | upload wheel/notebooks to DBFS/Volumes (no image push) |
| `deploy` | register a task-def revision + `update-service` | create/update a Databricks **job** via CLI/API |
| `verify` | `ecs wait services-stable` (health check) | run the job + poll run state until `SUCCESS` |
| IaC | ECR + ECS + IAM/OIDC Terraform | workspace + job + optional Unity Catalog Terraform |

The Databricks shape (no image; `deploy` = create a job) is the proof that the
verb contract is platform-shaped, not Docker-shaped.

---

## Data crossing the plugin boundary

All defined in `plugins/base.py` as frozen dataclasses:

| Type | Carries | Direction |
|------|---------|-----------|
| `StageGraph` | the validated `Pipeline` + stages in declared order | core → CI-vendor plugin |
| `VerbContext` | `pipeline`, `stage`, `verb`, and non-secret `settings` (region, project name, …) | core → each target verb |
| `VerbResult` | `commands` (shell steps) + `files` (support files) | target verb → core |
| `GeneratedFile` | `path` (POSIX, repo-relative), `content`, `executable` | plugin → core (which writes + manifests it) |
| `IaCArtifacts` | `files` (the IaC) + `summary` (for the runbook) | target plugin → core |

`VerbContext` is read-only and **secrets-free**: `settings` carries resolved,
non-secret knobs; secret *references* (key names) are emitted by the verb itself.

---

## The two rules every plugin must honour

### Determinism (R5.3)

Identical IR + repo must produce **byte-identical** output. So:

- build collections in a fixed, documented order (no `set` iteration, no
  hash-ordered dicts in output);
- emit no timestamps and no clock-derived values;
- prefer an explicit, ordered serializer over a general YAML/JSON dumper whose
  key order can drift (the GitHub plugin ships its own small `yaml_emit`
  precisely for this).

The core sorts files by path and writes exactly what you return (no
re-formatting), so determinism is yours to preserve.

### Secrets by reference (R6)

Never emit a secret value. Reference secrets by **key name** only —
`${{ secrets.NAME }}` (GitHub), `$(NAME)` (Azure DevOps), or a shell variable
(`$ECR_REPO`, `$DATABRICKS_TOKEN`) the pipeline resolves from the vendor secret
store. No ARN, key, account id, or token literal belongs in generated output. The
core runs a secrets-by-reference guard that refuses to write if a literal value
leaks, so a mis-behaving plugin can't slip a credential into a committed file.

---

## Checklist for a new plugin

1. Create `plugins/<ci|target>/<name>/` with an `__init__.py` exposing
   `get_plugin()` (or a single concrete subclass).
2. Set the class `name` equal to `<name>`.
3. Implement the family contract (`emit_pipeline_files` for CI; the six verbs +
   `supply_iac` for target).
4. Reference secrets by key only; emit deterministic output.
5. Add `<name>` to the IR enum in `ir/pipeline.schema.json` and the matching
   `StrEnum` in `generator/cicdgen/ir/models.py` so the IR accepts it.
6. Add a test modelled on `tests/test_plugin_<existing>.py` (byte-identical
   determinism, secrets-by-reference, the mapping-table rows). The core needs no
   changes.
