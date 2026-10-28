# `iac/aws-ecs` — generated Terraform for the AWS ECS target (task 8.2)

These files are the **generated, committed, reviewable** backing infrastructure for
the `client-github-aws` sample (ci_vendor: `github` × target_platform: `aws-ecs`).
They are produced by the `aws-ecs` target-platform plugin's `supply_iac(pipeline)`
from `../pipeline.yaml` and are byte-for-byte deterministic (R5.3) — the test
`tests/test_aws_ecs_terraform.py` fails if they drift.

```
iac/aws-ecs/
├── main.tf                     # ECR + ECS (cluster/task def/service) + IAM/OIDC
├── variables.tf                # every client/account input (no literals — R6)
├── outputs.tf                  # repo url / cluster / service / task family / deploy role arn
├── terraform.tfvars.example    # documented placeholder values (copy to terraform.tfvars)
└── README.md                   # this file (incl. the isolated-account runbook)
```

## What it provisions (design §8b)

| Area      | Resources                                                                 |
| --------- | ------------------------------------------------------------------------- |
| ECR       | `aws_ecr_repository` (scan-on-push) — the `package` verb's push target    |
| ECS       | `aws_ecs_cluster`, `aws_ecs_task_definition`, `aws_ecs_service` (Fargate) |
| Logging   | `aws_cloudwatch_log_group` for the task                                   |
| IAM       | `aws_iam_role` ×2 — ECS task-execution role + a scoped CI deploy role     |
| OIDC      | `aws_iam_openid_connect_provider` — keyless CI deploy (no static keys)    |

Every account specific — the account id, subnets, security groups, OIDC subject,
image tag — is a Terraform **variable** or is resolved at apply time via
`data.aws_caller_identity`. No literal account id, ARN, access key, or secret is
committed (R6).

## Local validation (no account, no terraform binary)

A real `terraform plan` needs the terraform binary **and** a (throwaway) AWS account.
Per **R9 (isolation)** neither is available in this environment — `terraform` and
`docker` are not installed and there is no isolated account configured here. So the
repo validates the HCL as strongly as it can locally, via
`cicdgen.validate.terraform_hcl` and `tests/test_aws_ecs_terraform.py`:

1. **Determinism** — committed files match a fresh generation byte-for-byte.
2. **Structure** — balanced braces/brackets, a top-level `terraform { required_providers }`
   block, a `provider "aws"` block, and every block well-formed. This is the local
   stand-in for `terraform validate` / the parse phase of `terraform plan`.
3. **Contract** — the §8b resources, the subnet/OIDC variables, and the repo/cluster/
   service/role outputs all exist; two IAM roles; account id via `aws_caller_identity`.
4. **No secrets** — no `AKIA…` keys, no account-id-bearing ARNs, no bare 12-digit
   account id, no literal `secret`/`password` assignments.

A local pass does **not** prove a live `terraform plan` succeeds (only a real plan in a
real account can) — but a failure here means the HCL could never plan. The live plan is
the manual runbook below.

---

## ⚠️ Runbook — `terraform plan` + verb execution in a throwaway (isolated) account

> **Not executed by this repo.** These steps require the `terraform` binary, `docker`,
> and a **throwaway** AWS account that touches no production or existing TekCor4
> resources (R9). Nothing here runs automatically; the generator and tests perform no
> network/account operations. Installing terraform/docker is **not** expected in this
> local environment.

### Prerequisites
- A throwaway AWS account dedicated to this test (nothing shared with prod).
- `terraform >= 1.5`, `docker`, and `aws-cli v2` installed on the operator's machine.
- Credentials for the throwaway account (an admin session or a bootstrap role) — used
  only to `apply`, never committed.

### 1. Provision the infrastructure (`terraform plan` / `apply`)
```bash
cd examples/client-github-aws/iac/aws-ecs

cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars: real subnet_ids / security_group_ids for the throwaway VPC,
# the OIDC subject scoped to your throwaway repo (repo:your-org/your-repo:ref:...),
# and the image_tag you intend to deploy. These stay LOCAL (git-ignored) — R6.

terraform init           # downloads the aws provider (>= 5.0)
terraform plan           # <-- the task-8.2 "terraform plan succeeds" step
terraform apply          # create ECR + ECS + IAM/OIDC in the throwaway account

# capture the outputs the verbs consume:
terraform output ecr_repository_url     # -> $ECR_REPO
terraform output ecs_cluster_name       # -> $ECS_CLUSTER
terraform output ecs_service_name       # -> $ECS_SERVICE
terraform output ci_deploy_role_arn     # -> the role the CI OIDC step assumes
```

### 2. Run the verbs (build → package → deploy → verify)
The verbs emit shell the CI vendor wires with by-reference knobs. Run them manually in
the throwaway account to reproduce what the pipeline does:
```bash
export AWS_REGION=eu-west-2
export ECR_REPO="$(terraform -chdir=iac/aws-ecs output -raw ecr_repository_url)"
export IMAGE_TAG="$(git rev-parse --short HEAD)"
export ECS_CLUSTER="$(terraform -chdir=iac/aws-ecs output -raw ecs_cluster_name)"
export ECS_SERVICE="$(terraform -chdir=iac/aws-ecs output -raw ecs_service_name)"
export ECS_TASK_FAMILY="orders-api-task"
export ECS_CONTAINER="orders-api-app"

# build  — docker build against the generated, resilient Dockerfile (R7.1)
docker build -t "$ECR_REPO:$IMAGE_TAG" -f Dockerfile .

# package — OIDC login + push to ECR
ECR_REGISTRY="$(echo "$ECR_REPO" | cut -d'/' -f1)"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"
docker push "$ECR_REPO:$IMAGE_TAG"

# deploy — register a new task def revision + roll the service (see plugin for the
#          exact json rewrite), then:
aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
  --task-definition "$NEW_TASK_DEF_ARN" --region "$AWS_REGION"

# verify — wait for steady state
aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
  --region "$AWS_REGION"
```

### 3. Which steps CAN run locally here, and which cannot
| Step                          | Local here? | Why                                                        |
| ----------------------------- | ----------- | ---------------------------------------------------------- |
| HCL structural validation     | ✅ yes      | pure Python (`cicdgen.validate.terraform_hcl`)             |
| `aws ecs help` (CLI present)  | ✅ yes      | aws-cli v2 is installed — smoke-tested in the suite        |
| the `deploy` json rewrite     | ✅ partial  | it's a pure `python -c` json edit (needs a real taskdef)   |
| `terraform plan` / `apply`    | ❌ no       | terraform binary + a throwaway account not available (R9)  |
| `docker build` / `push`       | ❌ no       | docker not installed; no registry (R9)                     |
| `aws ecs update-service`/wait | ❌ no       | needs a live cluster/service in a throwaway account (R9)   |

### 4. Isolation + teardown (R9.1 / R13.2)
- Confirm the plan/apply touched only the throwaway account — no existing TekCor4
  resources and not the TEK-969 branch.
- `terraform destroy` and delete any pushed images when finished.
