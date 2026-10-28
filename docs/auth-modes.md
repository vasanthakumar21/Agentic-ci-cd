# Cloud auth modes — OIDC (keyless) vs static access keys

The generator supports two ways a GitHub Actions pipeline authenticates to AWS.
Both are the *same product* — you select the mode with one IR field and one
Terraform variable. There is no separate codebase or fork.

| | OIDC (recommended) | Static access keys |
|---|---|---|
| IR `cloud.auth` | `oidc` | `keys` |
| Terraform `use_oidc` | `true` | `false` |
| GitHub secrets needed | `AWS_DEPLOY_ROLE_ARN` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` |
| AWS credential lifetime | short-lived (minted per run, ~1h) | long-lived until rotated |
| Requires creating an OIDC provider | yes | no |
| Good when | your account allows IAM OIDC providers | an org Service Control Policy blocks OIDC providers |

Everything else — the pipeline shape, the Terraform resources, the deploy
commands — is identical between the two modes.

---

## Version A — OIDC (keyless, recommended)

GitHub proves its identity to AWS with a short-lived signed token and assumes a
scoped deploy role. Nothing long-lived is stored.

**`pipeline.base.yaml`:**
```yaml
cloud:
  provider: aws
  region: ap-southeast-2
  auth: oidc
```

**Terraform input (`terraform.tfvars`):**
```hcl
use_oidc     = true
oidc_subject = "repo:<org>/<repo>:ref:refs/heads/main"
```

**After `terraform apply`**, take the `ci_deploy_role_arn` output and set it as a
GitHub secret:
```
gh secret set AWS_DEPLOY_ROLE_ARN --body "<role-arn>" --repo <org>/<repo>
```

The generated workflow logs in with:
```yaml
- name: Configure AWS credentials (OIDC)
  uses: aws-actions/configure-aws-credentials@v4
  with:
    role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
    aws-region: ap-southeast-2
```

---

## Version B — static access keys

Used where an organization policy forbids creating an OIDC provider. The
pipeline logs in with an IAM user's access keys stored as GitHub secrets.
Terraform creates no OIDC provider or federated role.

**`pipeline.base.yaml`:**
```yaml
cloud:
  provider: aws
  region: ap-southeast-2
  auth: keys
```

**Terraform input (`terraform.tfvars`):**
```hcl
use_oidc = false
```

**Set the two key secrets** (from a scoped deploy IAM user):
```
gh secret set AWS_ACCESS_KEY_ID     --body "<id>"     --repo <org>/<repo>
gh secret set AWS_SECRET_ACCESS_KEY --body "<secret>" --repo <org>/<repo>
```

The generated workflow logs in with:
```yaml
- name: Configure AWS credentials (access keys)
  uses: aws-actions/configure-aws-credentials@v4
  with:
    aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
    aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
    aws-region: ap-southeast-2
```

---

## Switching modes

1. Change `cloud.auth` in `pipeline.base.yaml`.
2. Set `use_oidc` to match in the Terraform inputs.
3. Regenerate and re-apply Terraform.
4. Set the matching GitHub secret(s).

Prefer OIDC. Use keys only when OIDC is not permitted in the target account.
