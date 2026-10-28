# Architecture & Status — Agentic, Vendor-Agnostic CI/CD Generator

This is the end-to-end explanation of the product for a stakeholder: what it is,
how data flows through it, where the code lives, the principles it is built on,
what is done versus still planned, and how to run it.

---

## 1. What the product is

It is a **pipeline-as-data compiler**. A developer declares *intent* in one
vendor-neutral file (`pipeline.yaml`, the IR) and the generator compiles it into a
complete, committed CI/CD setup — the pipeline file, the deploy infrastructure,
and any support files — for whatever CI system and deploy target they chose.

The defining idea is **two independent axes**:

- `ci_vendor` — *where the pipeline runs*. Five vendors: GitHub, Azure DevOps,
  GitLab, Bitbucket, Jenkins.
- `target_platform` — *what gets built and deployed*. The full cross-cloud matrix:
  **18 services across AWS / Azure / GCP, plus a provider-neutral generic
  Kubernetes target** (19 target plugins in all).
  - **AWS (8):** ecs, lambda, eks, glue, emr, stepfunctions, batch, sagemaker
  - **Azure (6):** functions, databricks, aks, synapse, data-factory, container-apps
  - **GCP (4):** cloud-run, gke, dataproc, dataflow
  - **generic k8s (1):** the legacy, provider-neutral `kubernetes` plugin

The two are chosen independently and **any `ci_vendor` × `target_platform`
combination is valid** (5 × 19 = 95 pairings), because neither axis knows about
the other. Everything is a plugin; the core hard-codes no vendor and no platform.
Switching either axis is a one-line IR edit and a regenerate.

```
            ci_vendor  (how the pipeline is expressed)
            ────────────────────────────────────────────────
            github   azure-devops   gitlab   bitbucket   jenkins
 t  AWS   ×8   ✓          ✓            ✓         ✓          ✓     ecs, lambda, eks, glue,
 a  (nested)                                                     emr, stepfunctions,
 r                                                               batch, sagemaker
 g  Azure ×6   ✓          ✓            ✓         ✓          ✓     functions, databricks,
 e  (nested)                                                     aks, synapse,
 t                                                               data-factory,
 _                                                               container-apps
 p  GCP   ×4   ✓          ✓            ✓         ✓          ✓     cloud-run, gke,
 l  (nested)                                                     dataproc, dataflow
 a  k8s   ×1   ✓          ✓            ✓         ✓          ✓     generic (provider-neutral)
 t
 f  (what gets built & deployed — every cell is a valid combination)
 o
 r
 m
```

---

## 2. The data flow, end to end

```
  client repo
      │
      ▼
  ┌─────────────┐   proposes a draft IR (evidence + confidence, no writes)
  │ discovery   │   cicdgen.discovery.discover_stack
  └─────┬───────┘
        │  developer confirms / edits
        ▼
   pipeline.yaml  ── the IR (two axes + stages + envs + gates …)
        │
        ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ cicdgen.core.generate  (validate → 4 gates → dispatch → write) │
  │                                                                │
  │  1. validate IR          cicdgen.ir.load_and_validate_ir       │
  │     four fail-fast, platform-blind PRE-RENDER gates (§11.0):    │
  │     1a API-version  loader._validate_plugin   (§11.4)          │
  │     1b capability   core.capabilities         (§11.2)          │
  │     1c stage-graph  core.stage_graph          (§11.3)          │
  │     1d policy       core.policy               (§11.5)          │
  │  2. dispatch to 2 plugins cicdgen.plugins.dispatch             │
  │       • CI-vendor plugin  emit_pipeline_files(graph)   (§8a)    │
  │       • target plugin     6 verbs + supply_iac         (§8b)    │
  │  2b. promotion plan  core.stage_graph.compose_promotion_plan   │
  │      (build once, deploy the same immutable ref per env, §11.6)│
  │  3. enforce secrets-by-reference  cicdgen.core.secrets_guard   │
  │  4. write files + .cicd/generated.manifest.json               │
  └─────┬──────────────────────────────────────────────────────────┘
        │  committed, reviewable artifacts (+ manifest of what it owns)
        ▼
  ┌─────────────┐   dry-run → --apply (idempotent) wires the vendor/cloud
  │ bootstrap   │   cicdgen.bootstrap  from .cicd/*.local.yaml
  └─────┬───────┘
        │
        ▼
  axis swap? (edit ci_vendor / target_platform, regenerate)
        │
        ▼
  manifest-driven cleanup removes the old axis's files + emits
  MIGRATION_RUNBOOK.md   cicdgen.core.migration / cicdgen.core.runbook
```

Step by step, with the real module for each:

1. **Discovery proposes a draft IR.** `cicdgen.discovery.discover_stack` inspects
   a client repo (filesystem signals only, no network, no writes) and proposes a
   draft IR with per-axis *evidence* and *confidence*. Weak inferences are flagged
   "PLEASE CONFIRM" — it never silently assumes (R10.2).
2. **Developer confirms `pipeline.yaml`.** The draft becomes the committed IR.
3. **`cicdgen.core.generate` validates the IR, then runs four pre-render gates.**
   `cicdgen.ir.load_and_validate_ir` runs the fail-fast Pydantic models
   (`cicdgen.ir.models.Pipeline`) — unknown fields, bad enums, missing keys, and
   cross-field violations all raise precise messages. On top of that, Plan B adds
   four platform-blind gates that all fail fast **before any file is written**
   (design §11.0):
   - **1a API-version** (`cicdgen.plugins.loader._validate_plugin`, §11.4) — a
     plugin loads only if its declared `plugin_api_version` is in
     `SUPPORTED_PLUGIN_API` `(1, 2)`; an incompatible contract fails loudly.
   - **1b capability** (`cicdgen.core.capabilities.validate_capabilities`, §11.2) —
     after both plugins load, the union of their declared `capabilities` must cover
     what the IR structure requires, else a value-free `CapabilityError` names the
     missing capability and which axis would supply it.
   - **1c stage-graph** (`cicdgen.core.stage_graph`, §11.3) — validates `depends_on`
     edges, rejects cycles, and computes a deterministic topological order (a
     pipeline with no `depends_on` is left byte-identically in declared order).
   - **1d policy** (`cicdgen.core.policy.enforce_policies`, §11.5) — evaluates the
     committed `.cicd/policies.yaml` (if present) against the IR and lists EVERY
     violation in one pass; an absent file is a no-op.
4. **It dispatches to the two named plugins.** `cicdgen.plugins.dispatch.dispatch`
   loads the CI-vendor plugin and the target-platform plugin *by name*
   (`cicdgen.plugins.loader`), resolving the nested target home via
   `cicdgen.plugins.target_layout.resolve_target_dir`, with zero vendor/platform
   branches in the core (R3.4). It runs the 1b/1c gates, builds a vendor-neutral
   `StageGraph` in topological order, drives the target plugin's six verbs uniformly
   (`cicdgen.plugins.stage_contract.invoke_pipeline`), hands the graph to the CI
   plugin's `emit_pipeline_files`, and collects the target plugin's IaC via
   `supply_iac`. When the IR requests promotion,
   `cicdgen.core.stage_graph.compose_promotion_plan` builds the artifact once and
   deploys the same immutable reference per environment (§11.6, wired to the
   `ARTIFACT_PROMOTION` capability).
5. **It renders files + verb steps + IaC, and enforces secrets-by-reference.**
   `cicdgen.core.secrets_guard.enforce_secrets_by_reference` refuses to write if a
   literal secret value leaks into generated content (R6.3/R6.6).
6. **It writes them + `.cicd/generated.manifest.json`.** `cicdgen.core.generate`
   writes files sorted by path (determinism, R5.3) and records every file it owns
   in the manifest (`cicdgen.core.manifest`) — its memory of "what I wrote here".
7. **Bootstrap wires the vendor/cloud.** `cicdgen.bootstrap` reads
   `.cicd/*.local.yaml`, plans (dry-run by default), and on `--apply` idempotently
   pushes secrets, creates the OIDC trust, registers runners, and sets branch
   protection through an injectable `Executor` seam.
8. **An axis swap regenerates, cleans up, and emits a runbook.** Changing
   `ci_vendor` and/or `target_platform` and regenerating: the manifest-driven
   cleanup removes the old axis's files (only files the generator owns — never
   client-authored ones, R8.3); `cicdgen.core.migration.detect_axis_change`
   classifies the swap; and `cicdgen.core.runbook.write_migration_runbook` emits
   `MIGRATION_RUNBOOK.md` naming the swap and the new `.local` keys + manual steps.

An agent driver (`cicdgen.agent`) ties discovery + generation together from a
single natural-language instruction, dry-run by default.

---

## 3. Where the code lives

```
agentic-cicd-generator/
├── ir/
│   ├── pipeline.schema.json      # the published IR contract (JSON Schema)
│   └── README.md
├── generator/cicdgen/            # the product packages
│   ├── ir/                       # IR models (Pydantic), loader, plugin-existence check
│   ├── plugins/                  # plugin contracts (base: API version + Capability enum),
│   │                             #   loader, dispatch, stage-verb contract, target_layout
│   ├── core/                     # generate, manifest, migration, runbook, secrets_guard,
│   │                             #   capabilities (1b), stage_graph (1c + promotion),
│   │                             #   policy (1d), doctor (drift)
│   ├── localconfig/              # .cicd/*.local.yaml models + loader (R6)
│   ├── bootstrap/                # planner (dry-run), apply (idempotent), report
│   ├── discovery/                # inspect a repo, propose a draft IR
│   ├── agent/                    # instruction parser, driver, one-instruction demo
│   ├── validate/                 # structural validators (github/azure YAML, terraform HCL, mapping table)
│   └── cli.py                    # console entry point (the `cicd doctor` subcommand is live)
├── plugins/
│   ├── ci/                       # CI-vendor plugins (flat): github, azure-devops, gitlab, bitbucket, jenkins
│   └── target/                   # target plugins, provider-nested <provider>/<service>/:
│       ├── aws/                  #   ecs, lambda, eks, glue, emr, stepfunctions, batch, sagemaker
│       ├── azure/                #   functions, databricks, aks, synapse, data-factory, container-apps
│       ├── gcp/                  #   cloud-run, gke, dataproc, dataflow
│       └── k8s/generic/          #   the legacy provider-neutral "kubernetes" plugin
├── tests/                        # the pytest suite (unit + e2e + determinism + isolation)
│   ├── harness/snapshot.py       #   render_plugin / assert_snapshot (UPDATE_SNAPSHOTS=1 to refresh)
│   ├── snapshots/                #   committed golden output per target (__full / __minimal)
│   └── fixtures/ir/              #   the IR fixtures the snapshot harness renders
├── examples/                     # sample client IRs + committed generated artifacts
├── .cicd/                        # committed *.example local-config templates + policies.yaml(.example)
├── docs/                         # this documentation
└── .kiro/
    ├── steering/cicd-generator.md  # the generation rules + §8a/§8b mapping tables (agent steering)
    └── agents/cicd-generator.md    # the custom agent definition
```

One line each for the `generator/cicdgen/*` packages:

| Package | Role |
|---------|------|
| `ir/` | The IR: Pydantic models mirroring the JSON Schema, the loader, and the separate plugin-existence check. |
| `plugins/` | The plugin contracts (`base.py` — `PLUGIN_API_VERSION`, `Capability` enum, `capabilities`), the name→instance loader (with the 1a API-version gate), the platform-blind `dispatch`, the stage→verbs `stage_contract`, and `target_layout` (the data-only name→nested-dir resolver). |
| `core/` | The orchestration: `generate` (validate→4 gates→dispatch→write+manifest), `manifest`, axis-swap `migration`, the `runbook` emitter, the `secrets_guard`, plus the Plan B gates — `capabilities` (1b), `stage_graph` (1c + `compose_promotion_plan`), `policy` (1d), and `doctor` (read-only drift detection). |
| `localconfig/` | The git-ignored `.cicd/*.local.yaml` models + loader — wiring + secret values, redacting (R6). |
| `bootstrap/` | The dry-run `planner`, the idempotent `apply` behind an `Executor` seam, and the automated-vs-manual `report`. |
| `discovery/` | Inspect a client repo and propose a draft IR with evidence/confidence; writes nothing. |
| `agent/` | Parse a free-text instruction, assemble the IR (discovery + explicit), and drive the core; plus the runnable `demo`. |
| `validate/` | Structural validators used by tests to confirm emitted YAML / Terraform is well-formed and matches the mapping tables. |

---

## 4. Design principles (and the requirements they map to)

| Principle | What it means | Requirements |
|-----------|---------------|--------------|
| Two independent axes | `ci_vendor` × `target_platform`, chosen independently; any combination generatable. | R3.1 |
| Everything is a plugin | The core hard-codes no vendor/platform; adding a plugin never edits the core. | R3.4 |
| Intent over syntax | The IR expresses *what* (stages, gates, approvals); plugins decide *how* in each vendor's syntax. | R1, R2, R4.3 |
| Templates for structure, agent for judgement | Plugins render deterministic structure; the agent gap-fills repo-specific inputs only, never bypassing plugins. | R5.1, R5.2 |
| Secrets by reference | Generated files reference secrets by key; values live only in git-ignored local config. | R6, R6.3, R6.6, R9.3 |
| Compile-time, committed, reviewable | Output is files written to the repo and reviewed in a PR — not runtime magic. | R5, R5.3 |
| Idempotent bootstrap | Dry-run by default; `--apply` detects-then-acts, creating no duplicates. | R7, R9.2 |
| Per-client isolation | Each client's repo/config is self-contained; generation is contained to the target repo. | R9.1, R10.3 |
| Honest about limits | Irreducible manual steps are documented, never hidden (bootstrap report + runbook). | R7.2, R8.1 |
| Fail fast, before any write | Four platform-blind pre-render gates (API-version, capability, stage-graph, policy) reject bad input with precise, value-free messages before a file is produced. | §11.0–11.5 |
| Capabilities are declared, not assumed | Each plugin declares a `Capability` set; the core validates the IR against the union of both axes rather than hard-coding what a vendor/platform can do. | §11.2, R3.4 |
| Policy as committed data | Org rules live in `.cicd/policies.yaml` as a data-only rule registry; adding a rule never adds a vendor/platform branch, and temporal rules take an injected clock so generation stays reproducible. | §11.5, R5.3 |
| Build once, promote immutably | Artifact promotion composes one build and deploys the same immutable reference across environments. | §11.6 |
| Versioned plugin contract | `PLUGIN_API_VERSION` + `SUPPORTED_PLUGIN_API` gate loading so a plugin built against an incompatible contract fails loudly instead of misbehaving. | §11.4 |
| Drift is observable | `cicd doctor` re-renders in memory and reports drift read-only (exits non-zero for CI), never writing. | §11.7, R9.2 |

---

## 5. Current status

Built in an isolated repo. The test suite is **green: 2101 passed** (run via
`.venv\Scripts\python.exe -m pytest -q`). The Plan B scope expansion — the eight
architecture enhancements and the full cross-cloud target matrix — is **COMPLETE,
built and tested** as part of that suite.

> Note on `tasks.md` numbering: the plan reuses the number "9" for two different
> groups (the Databricks plugin **and** the bootstrapper), and the bootstrapper's
> sub-tasks 9.3/9.4 are still shown unchecked there even though the functionality
> is implemented and tested (`test_bootstrap_automation_report.py`,
> `test_bootstrap_idempotency.py`). The status below reflects what the code and
> the green suite actually deliver.

| Task | Area | Status |
|------|------|--------|
| 1 | Project scaffold in an isolated repo | ✅ complete |
| 2 | IR schema + fail-fast validator (two axes) | ✅ complete |
| 3 | Plugin interface + stable stage-verb contract + core dispatch | ✅ complete |
| 4 | Local config (`.cicd/*.local.yaml`) + loader (R6) | ✅ complete |
| 5 | Generator core: render → write + manifest; secrets-by-reference; determinism | ✅ complete |
| 6 | CI-vendor plugin #1 — GitHub Actions | ✅ complete |
| 7 | CI-vendor plugin #2 — Azure DevOps | ✅ complete |
| 8 | Target plugin #1 — AWS ECS | ✅ complete |
| 9 (Databricks) | Target plugin #2 — Databricks (the different-shape proof) | ✅ complete |
| 9 (bootstrapper) | Dry-run planner, idempotent `--apply`, automated-vs-manual report | ✅ complete (and tested) |
| 10 | Discovery + custom agent + steering + one-instruction demo | ✅ complete |
| 11 | Migration / axis-swap: manifest cleanup, runbook, CI-swap + target-swap demos | ✅ complete |
| 12 | Additional plugins — GitLab, Bitbucket, Jenkins (CI); Azure Functions, Kubernetes (target) | ✅ present with tests |
| 13 | End-to-end multi-client, multi-combination isolation test | ✅ complete |
| 14 | Documentation (this doc) | ✅ complete |
| **Plan B** | **Eight architecture enhancements + full cross-cloud target matrix** | **✅ complete (see below)** |

### Delivered in Plan B

The enhancements previously listed as "still to come" are now shipped, each with
tests in the green suite:

- **Full cross-cloud target matrix.** 18 target services across AWS / Azure / GCP
  plus the generic k8s plugin — 19 target plugins (see §1 for the list). Any
  `ci_vendor` × `target_platform` combination is valid.
- **Provider-nested plugin layout.** Target plugins live at
  `plugins/target/<provider>/<service>/` (`aws/`, `azure/`, `gcp/`, and `k8s/` for
  the legacy generic plugin). Resolution is a data-only alias table + prefix rule
  in `cicdgen.plugins.target_layout.resolve_target_dir`; legacy IR values
  (`aws-ecs`, `databricks`, `azure-functions`, `kubernetes`) still resolve
  byte-identically.
- **Plugin capability matrix.** `base.py` `Capability` enum +
  `ProviderPlugin.capabilities`; `core/capabilities.py` validates the IR against
  the union of both axes' capabilities — the **1b** gate.
- **Canonical stage graph.** `core/stage_graph.py` validates `depends_on`, detects
  cycles, and computes a deterministic topological order — the **1c** gate.
- **Policy engine.** `core/policy.py` + committed `.cicd/policies.yaml(.example)`
  (`require_security_scan`, `minimum_coverage`, `block_prod_deploy_friday` with an
  injected `clock_day`) — the **1d** gate.
- **Artifact promotion.** IR promotion block + `core/stage_graph.compose_promotion_plan`
  (build once, deploy the same immutable reference per environment; wired to the
  `ARTIFACT_PROMOTION` capability).
- **Snapshot test harness.** `tests/harness/snapshot.py` (`render_plugin` /
  `assert_snapshot`) + committed `tests/snapshots/` + `tests/fixtures/ir/`.
- **Drift detection.** `core/doctor.py` + the `cicd doctor` CLI subcommand
  (read-only, exits non-zero on drift, optional `--live`).
- **Plugin API versioning.** `base.py` `PLUGIN_API_VERSION` / `SUPPORTED_PLUGIN_API`
  with a loader gate — the **1a** gate.

---

## 6. How to run it

The code lives under `agentic-cicd-generator/`. If your VS Code workspace does
not already include it, add that folder to the workspace. Commands below assume
the project's virtualenv Python at `.venv\Scripts\python.exe` and are run from the
project root.

### One-instruction end-to-end demo

The runnable proof of the headline claim — one natural-language instruction →
a complete setup via the real plugins, dry-run first then applied into a
throwaway temp dir:

```bash
.venv\Scripts\python.exe -m cicdgen.agent.demo
```

Useful variants:

```bash
# one specific combination
.venv\Scripts\python.exe -m cicdgen.agent.demo --instruction "generate for ci_vendor=github target_platform=aws-ecs with region=eu-west-2 project=orders-api"

# keep the output on disk to inspect
.venv\Scripts\python.exe -m cicdgen.agent.demo --out ./_demo_out

# migration demos
.venv\Scripts\python.exe -m cicdgen.agent.demo --ci-swap       # github -> azure-devops, same target
.venv\Scripts\python.exe -m cicdgen.agent.demo --target-swap   # aws-ecs -> databricks, same CI vendor
```

### Driving generation from code

```python
# agent: parse an instruction, assemble the IR, drive the core (dry-run by default)
from cicdgen.agent import run_instruction
res = run_instruction(
    "generate for ci_vendor=github target_platform=aws-ecs with region=eu-west-2 project=orders-api",
    repo_root="/path/to/client/repo",
    apply=False,       # dry-run: returns res.plan and writes nothing
)

# core directly: plan (dry-run) or generate (write)
from cicdgen.core import plan, generate
the_plan = plan("pipeline.yaml", repo_root="/path/to/client/repo")     # what WOULD be written
result   = generate("pipeline.yaml", repo_root="/path/to/client/repo") # writes + manifest
```

### Bootstrap the vendor/cloud wiring

```python
from cicdgen.bootstrap.planner import plan_bootstrap, print_plan
from cicdgen.bootstrap.apply import apply_bootstrap
from cicdgen.bootstrap.report import emit_automation_report

print_plan(plan_bootstrap(".cicd"))                 # dry-run: what would happen
apply_bootstrap(".cicd", executor=my_executor, confirm=True)   # live, idempotent
print(emit_automation_report(".cicd"))              # automated vs manual summary
```

### Check for drift with `cicd doctor`

`cicd doctor` is a **read-only** drift check: it loads the IR + committed manifest,
re-renders generation in memory, and reports what is missing / extra / changed /
in-sync. It writes nothing and has no `--apply`; it exits non-zero on any drift, so
it works as a CI guard that generated files are current.

```bash
.venv\Scripts\python.exe -m cicdgen.cli doctor                  # check the current dir
.venv\Scripts\python.exe -m cicdgen.cli doctor <repo> --ir pipeline.yaml
.venv\Scripts\python.exe -m cicdgen.cli doctor --show-in-sync   # also list in-sync paths
.venv\Scripts\python.exe -m cicdgen.cli doctor --live           # also probe vendor/cloud wiring (read-only)
```

### Run the tests

```bash
.venv\Scripts\python.exe -m pytest -q
```

The snapshot harness (`tests/harness/snapshot.py`) asserts generated output against
committed goldens under `tests/snapshots/`. To re-record them after an intentional
change, run the suite with the refresh flag and review the diff in the PR:

```bash
# PowerShell
$env:UPDATE_SNAPSHOTS = "1"; .venv\Scripts\python.exe -m pytest -q; Remove-Item Env:\UPDATE_SNAPSHOTS
```

### The console entry point

`pyproject.toml` wires a `cicdgen` console script to `cicdgen.cli:main`. The
`doctor` subcommand is fully implemented (above); `generate` / `bootstrap` /
`discover` are still a scaffold — use the module entry points above
(`python -m cicdgen.agent.demo` and the programmatic `cicdgen.core` /
`cicdgen.bootstrap` APIs) for those live flows.
