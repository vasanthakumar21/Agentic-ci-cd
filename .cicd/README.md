# `.cicd/` — local, per-client config (R6)

Two files, **git-ignored** (`*.local.*`), hold everything the agent cannot own:

- `secrets.local.yaml` — secret **values** by name (a flat `NAME: value` map).
- `bootstrap.local.yaml` — vendor/cloud **wiring**: repo identity, OIDC trust,
  runner tokens (by env), reviewer groups, branch protection, and per-target
  wiring (ECR repo for `aws-ecs`, workspace URL/token for `databricks`, …).

Committed `*.example` files here document **every key** you must fill (R6.4). Copy an
example, drop the `.example` suffix, and fill it in:

```bash
cp .cicd/secrets.local.yaml.example   .cicd/secrets.local.yaml
cp .cicd/bootstrap.local.yaml.example .cicd/bootstrap.local.yaml
```

## Two axes, one file set

The shapes support any `ci_vendor` × `target_platform` the IR declares — v1 covers
**github / azure-devops** × **aws-ecs / databricks**, and the structure is extensible
(add a block under `oidc:` per cloud, under `target:` per platform). Only fill the
blocks your `pipeline.yaml` actually selects.

## How values flow

- `bootstrap.local.yaml` `secrets:` lists secret **names**; `secrets.local.yaml`
  holds the matching **values**. The two are cross-checked by the loader.
- Admin and runner tokens are read from the **environment** (`*_env` keys), never
  written to a file.
- Generated pipeline files reference secrets by key only — `${{ secrets.X }}`
  (GitHub) or `$(X)` (Azure DevOps) — never by value (R6.3).

## Rules enforced

- Secret values are never inlined, committed, or logged (R6.6 / R9.3).
- A missing required key aborts bootstrap with a message **naming the key** (R6.5).
- Dry-run is the bootstrap default; destructive/live actions need `--apply` (R9.2).

The generator also writes `generated.manifest.json` here — the tracked list of every
file it owns, enabling clean regeneration/removal on an axis swap (R8.3).
