# client-sales-pipeline — a full multi-service AWS data pipeline

This is a **worked example** of how a developer hands a real project to the
generator. The business scenario:

> A **Step Functions** state machine orchestrates the flow. It triggers a
> **Lambda** that checks an S3 bucket for today's files (by `sales/YYYY-MM-DD/`
> prefix). If files exist, it triggers a **Glue** job, passing the S3 path; Glue
> cleans the data and writes it out as Parquet.

Three AWS services = **three independent deployables**. The generator's rule is
**one IR (`pipeline.yaml`) = one `target_platform`**, so this repo is a *monorepo*
with one sub-folder per service. Each service has its own `pipeline.yaml` and is
generated with a `source_dir` setting that points the build at its sub-folder
(the Option 2 enhancement). One `git push` runs all three pipelines.

## Layout — this is the format every new client/project follows

```
client-sales-pipeline/
├── .cicd/
│   └── secrets.local.yaml.example   # real account id / ARNs / buckets (by key)
├── services/
│   ├── lambda/                      # ── aws-lambda service ──
│   │   ├── pipeline.yaml            #    the IR (target_platform: aws-lambda)
│   │   ├── requirements.txt         #    deps shipped inside the zip
│   │   └── handler.py               #    YOUR Lambda code (build zips this folder)
│   │
│   ├── glue/                        # ── aws-glue service ──
│   │   ├── pipeline.yaml            #    the IR (target_platform: aws-glue)
│   │   ├── requirements.txt         #    optional extra libs
│   │   └── glue_jobs/               #    MUST be named exactly `glue_jobs/`
│   │       └── glue_job.py          #    YOUR Spark ETL script
│   │
│   └── stepfn/                      # ── aws-stepfunctions service ──
│       ├── pipeline.yaml            #    the IR (target_platform: aws-stepfunctions)
│       └── asl/
│           └── state-machine.json   #    YOUR real ASL graph (never clobbered)
```

## What the developer provides vs. what the generator produces

| Service | You provide (at these exact paths) | Generator produces |
|---|---|---|
| lambda | `services/lambda/handler.py`, `requirements.txt`, `pipeline.yaml` | `.github/workflows/ci.yml`, `iac/aws-lambda/*.tf`, manifest |
| glue   | `services/glue/glue_jobs/glue_job.py`, `requirements.txt`, `pipeline.yaml` | `glue/glue-job.json`, `iac/aws-glue/*.tf`, `ci.yml`, manifest |
| stepfn | `services/stepfn/asl/state-machine.json`, `pipeline.yaml` | `stepfunctions/state-machine.asl.json` (copied from your ASL at build), `iac/aws-stepfunctions/*.tf`, `ci.yml`, manifest |

## The three generation commands

Each service is generated with its folder as the repo root and a `source_dir`
setting so the build reads from the right place:

```text
# Lambda
generate for ci_vendor=github target_platform=aws-lambda \
  with project=sales-file-check region=us-east-1 \
       function_name=s3-file-check source_dir=services/lambda

# Glue
generate for ci_vendor=github target_platform=aws-glue \
  with project=sales-cleanup region=us-east-1 \
       job_name=sales-cleanup-job source_dir=services/glue

# Step Functions (points build at YOUR ASL, so the skeleton never overwrites it)
generate for ci_vendor=github target_platform=aws-stepfunctions \
  with project=sales-orchestrator region=us-east-1 \
       state_machine_name=sales-orchestrator \
       definition_src=services/stepfn/asl/state-machine.json
```

Real account values (account id, role ARNs, bucket names) live only in
`.cicd/secrets.local.yaml` (git-ignored); generated files reference them by key.
