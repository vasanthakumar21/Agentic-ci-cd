# Demo Guide — the CI/CD generator, explained end to end

A reference for presenting and answering questions. Grounded in what the tool
actually generates (verified: 2101 passing tests).

## 1. Vocabulary (define these if anyone is lost)

- **CI/CD** — an automated assembly line: on every push, code is tested, built,
  and shipped automatically.
- **Pipeline** — the assembly line itself (ordered stages). In GitHub it's a
  "workflow" file at `.github/workflows/*.yml`.
- **Runner** — the temporary Ubuntu machine GitHub rents to run your pipeline,
  then discards. (This is why the commands are Linux shell, not your laptop.)
- **Artifact** — the packaged, deployable output. Lambda → a `.zip`; container →
  a Docker image. "Build once, promote" = ship the SAME artifact dev→staging→prod.
- **IaC (Infrastructure as Code)** — describing cloud resources as text, not
  console clicks. We generate **Terraform**.
- **Terraform** — the IaC tool: `.tf` files describe what you want, `terraform
  apply` makes AWS match, and it remembers what it made (state).
- **OIDC** — GitHub proves its identity to AWS with a short-lived signed token
  instead of stored keys. Keyless; nothing permanent to leak.
- **IAM role** — an AWS permission set something temporarily assumes. Ours is
  scoped: deploy THIS function / write THIS bucket, nothing more.
- **Secret** — a sensitive value. Generated files hold only a *reference*
  (`${{ secrets.X }}`); the value lives in GitHub's secret store + a git-ignored
  local file.

## 2. The pipeline — six verbs, every platform

| Verb | Meaning | Lambda | Step Functions | Glue |
|------|---------|--------|----------------|------|
| lint    | static checks     | ruff          | validate ASL JSON        | ruff          |
| test    | run tests         | pytest        | pytest                   | pytest        |
| build   | make the artifact | zip + deps    | copy/validate ASL        | stage script  |
| package | publish artifact  | upload to S3  | upload ASL to S3         | upload to S3  |
| deploy  | make it live      | update fn + alias | create/update state machine | create/update job |
| verify  | prove it works    | invoke (smoke)| start-execution → SUCCEEDED | start run → SUCCEEDED |

The CI plugin arranges ORDER + GATES; the target plugin supplies WHAT each verb
does. Same skeleton, different commands underneath.

## 3. What happens on a push to branch `TEK-968`

1. Trigger — push matches `TEK-*`; a fresh runner starts.
2. Checkout — clone the repo.
3. OIDC login — runner gets 1-hour AWS creds via a signed token (no stored keys).
4. test — lint + pytest; fail here = nothing deploys.
5. build — produce artifact, upload to S3 (immutable version).
6. deploy-dev — real deploy commands, then verify. Manual-gated env pauses here.
7. Green = live + verified. Red = stopped before prod.

## 4. Lambda deploy, line by line (the CLI story)

- `update-function-code --s3-bucket ... --s3-key ... --publish` → Lambda loads the
  new zip from S3 and creates an immutable numbered version.
- `publish-version` → capture that version number.
- `update-alias --name live --function-version N` → flip the stable alias `live`
  to the new version. This is the atomic "go live"; rollback = flip alias back.
- `wait function-updated` → block until AWS finishes applying.
- `get-function-configuration ... State` → assert `Active`, else fail.
- `invoke ... --payload '{}'` → smoke test: actually call it; fail if it errors.

Where it lives: zip in **S3**, compute in the managed **Lambda** service, logs in
**CloudWatch** — all created by Terraform before the first deploy.

## 5. Deployment-tool landscape (SAM / CDK / etc.)

All of these ultimately create your Lambda/infra in AWS, at different abstraction
levels:

- **CloudFormation** — AWS-native IaC; verbose YAML; the engine others compile to.
- **AWS SAM** — shorthand YAML for serverless that expands to CloudFormation.
  Fast serverless prototyping; AWS-only; limited beyond serverless.
- **AWS CDK** — write infra in a real language (Python/TS); generates
  CloudFormation. Powerful; AWS-centric; more setup.
- **Serverless Framework** — open-source, multi-cloud YAML + plugins; dev velocity.
- **Terraform (what we generate)** — cloud-agnostic, state-tracked, previewable;
  the standard for infra that must scale and pass audits.

Two jobs people confuse:
- **Provisioning** the infra (bucket, role, function shell) = Terraform, run once.
- **Deploying code** into it (new zip, flip alias) = the pipeline's CLI step,
  every push.
We deliberately SEPARATE these; frameworks blur them.

### Why we chose Terraform + raw CLI
1. Multi-cloud (SAM/CDK are AWS-only; our tool targets AWS/Azure/GCP/K8s).
2. Transparency — every deploy step is a readable CLI call, no black box.
3. Separation — infra (rare changes) vs code deploy (every push).
4. Determinism — byte-identical generated output; Terraform's declarative plans fit.
5. Longevity/compliance.

Configurable: runtime, memory, timeout, packaging (Zip/Image), region are all
Terraform variables; CI vendor + target platform are one-line IR changes.

## 6. Branch-per-ticket (TekFlow discipline) — IMPLEMENTED

In `pipeline.base.yaml` (shared by all services):

```yaml
triggers:
  push_branches: ["main", "TEK-*", "feature/**"]
  pr_branches: ["main"]
```

Generated workflow triggers on all three patterns. A branch `TEK-968` runs its own
pipeline; filter GitHub Actions by that branch to monitor exactly that ticket's
changes. Omit `triggers` → defaults to main-only (backward compatible).

## 7. What the developer does vs. what the tool does

| Developer (by hand) | Tool (automatically) |
|---------------------|----------------------|
| Writes app code under src/<svc>/ | Zips/stages/uploads it |
| Writes short pipeline.yaml | Generates full CI/CD + Terraform |
| `aws configure` + `gh auth login` once | Everything between: repo layout, terraform, secrets, push |
| Creates branch TEK-xxx, types "y" to approve | Runs it; keeps secrets by reference |

## 8. Honest limitations (say these — they build trust)

- The tool does NOT write your application logic or your Step Functions graph.
- Services deploy as independent pipelines; runtime wiring lives in your code/ASL.
- First deploy needs a one-time admin `terraform apply` for IAM/OIDC.
- Pipelines run on CI runners against a real cloud — not fully runnable on a laptop.
- Account-global OIDC provider: created by the first service; a second service must
  reference it rather than recreate it.
