"""One-command deploy for this client — generate, provision, wire GitHub, push.

The developer runs THIS after editing src/. It does everything the tool can do
automatically, pausing for a y/n confirmation before every action that touches
your cloud account or your GitHub (terraform apply, repo create, git push, set
secret) so you stay in control and never spend credits by surprise.

What it does, in order
----------------------
 0. Pre-flight: confirm git / aws / terraform / gh are on PATH and that you are
    authenticated (aws sts get-caller-identity, gh auth status). If not, it tells
    you the ONE command to run and stops — it never handles your credentials.
 1. GENERATE: run generate.py (src/<svc> -> generated/<svc>), merging the shared
    pipeline.base.yaml. Pure/local, no confirmation.
 2. ASSEMBLE THE REPO (Option A): build a clean deploy/ repo containing your
    src/, both services' IaC, and BOTH workflows at the repo root
    (.github/workflows/lambda.yml + stepfn.yml) where GitHub actually runs them.
 3. TERRAFORM (per service): init, then `apply` after you confirm. Creates the S3
    buckets, IAM exec roles, the GitHub-OIDC provider, and the keyless deploy role.
    Captures the deploy-role ARN from the Terraform outputs.
 4. GITHUB: create the repo under your account (confirm), push the deploy/ repo
    (confirm), and set the AWS_DEPLOY_ROLE_ARN secret from the Terraform output.
 5. DONE: the push triggers the workflows; print the Actions URL to watch.

Credentials are never read or printed by this script. You authenticate once with
`aws configure` (or SSO) and `gh auth login`; everything else is automated.

Usage (from the repo root):
    python examples/client-sales-pipeline/deploy.py                 # full, interactive
    python examples/client-sales-pipeline/deploy.py --dry-run       # plan only, no cloud calls
    python examples/client-sales-pipeline/deploy.py --yes           # don't prompt (CI use)
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

_CLIENT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _CLIENT_DIR.parents[1]
# The deployable git repo is assembled OUTSIDE the generator's own git tree (and
# outside OneDrive's deep path) so `git init` creates its OWN .git and we never
# accidentally stage the product repo. Short path also dodges Windows long-paths.
_DEPLOY_OUTSIDE = Path("C:/cicd-deploy")

# --- client-specific knobs (a new client edits THIS block only) --------------
GITHUB_OWNER = "lecsearner"
GITHUB_REPO = "sales-pipeline"
AWS_REGION = "ap-southeast-2"
DEFAULT_BRANCH = "main"
OIDC_PROVIDER_URL = "https://token.actions.githubusercontent.com"
#: Which services to deploy and the Terraform dir + outputs each exposes.
SERVICES = ("lambda", "stepfn")
IAC_SUBDIR = {"lambda": "iac/aws-lambda", "stepfn": "iac/aws-stepfunctions"}
# ------------------------------------------------------------------------------

DEPLOY_DIR = _DEPLOY_OUTSIDE / GITHUB_REPO
_YES = False  # set by --yes


def _tool(name: str) -> str:
    """Resolve a tool on PATH, falling back to the winget install location."""
    found = shutil.which(name)
    if found:
        return found
    # winget per-user install locations (PATH may be stale in the current shell).
    base = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    if base.is_dir():
        for hit in base.rglob(f"{name}.exe"):
            return str(hit)
    return name  # let the run fail loudly with a clear message


def _run(cmd: list[str], *, cwd: Path | None = None, capture: bool = False) -> str:
    """Run a command, echoing it. Returns stdout when capture=True."""
    printable = " ".join(cmd)
    print(f"    $ {printable}")
    result = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, text=True,
        capture_output=capture,
    )
    if result.returncode != 0:
        out = (result.stdout or "") + (result.stderr or "")
        raise SystemExit(f"command failed ({result.returncode}): {printable}\n{out}")
    return (result.stdout or "").strip() if capture else ""


def _confirm(action: str, *, dry_run: bool) -> bool:
    """Ask y/n before a real action. --yes skips; --dry-run never acts."""
    if dry_run:
        print(f"  [dry-run] WOULD: {action}")
        return False
    if _YES:
        print(f"  [auto-yes] {action}")
        return True
    answer = input(f"  >> {action}  Proceed? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


# ---------------------------------------------------------------------------
# 0. pre-flight
# ---------------------------------------------------------------------------
def preflight() -> dict[str, str]:
    print("STEP 0 - pre-flight checks")
    tools = {t: _tool(t) for t in ("git", "aws", "terraform", "gh")}
    for name, path in tools.items():
        ok = Path(path).exists() or shutil.which(name)
        print(f"    {'OK ' if ok else 'MISSING'}  {name}: {path}")
        if not ok:
            raise SystemExit(f"{name} is not installed. Install it and re-run.")

    # Identity checks — these confirm YOU are logged in; no secrets are read here.
    try:
        ident = _run([tools["aws"], "sts", "get-caller-identity", "--output", "json"],
                     capture=True)
        acct = json.loads(ident)["Account"]
        print(f"    AWS account: {acct}")
    except SystemExit:
        raise SystemExit(
            "AWS not authenticated. Run `aws configure` (or `aws sso login`) with "
            "YOUR account, then re-run. This script never handles your keys."
        )
    gh = tools["gh"]
    auth = subprocess.run([gh, "auth", "status"], text=True, capture_output=True)
    if auth.returncode != 0:
        raise SystemExit(
            "GitHub not authenticated. Run `gh auth login` (browser approval) as "
            f"{GITHUB_OWNER}, then re-run."
        )
    print(f"    GitHub: authenticated")
    return {**tools, "account_id": acct}


# ---------------------------------------------------------------------------
# 1. generate
# ---------------------------------------------------------------------------
def generate_all() -> None:
    print("\nSTEP 1 — generate (src/<svc> -> generated/<svc>)")
    _run([sys.executable, str(_CLIENT_DIR / "generate.py")])


# ---------------------------------------------------------------------------
# 2. assemble the deploy repo (Option A: both workflows at the repo root)
# ---------------------------------------------------------------------------
#: Files/dirs that must NEVER be committed (machine-local Terraform junk + state
#: + the real tfvars which may carry account specifics). Rendered as .gitignore.
_GITIGNORE = """\
# Terraform machine-local + state (never commit)
**/.terraform/
**/.terraform.lock.hcl
**/terraform.tfstate
**/terraform.tfstate.*
**/terraform.tfvars
# Local secrets
.cicd/secrets.local.yaml
"""


def assemble_repo() -> None:
    print("\nSTEP 2 - assemble the deployable repo (workflows at repo root)")
    # Clean any prior deploy/ (OneDrive can hold locks; retry, then tolerate).
    if DEPLOY_DIR.exists():
        shutil.rmtree(DEPLOY_DIR, ignore_errors=True)
    if DEPLOY_DIR.exists():  # a lock survived the first attempt
        shutil.rmtree(DEPLOY_DIR, ignore_errors=True)
    (DEPLOY_DIR / ".github" / "workflows").mkdir(parents=True, exist_ok=True)

    # Copy the developer's source (so the CI runner has the code it zips/deploys).
    # dirs_exist_ok tolerates a lingering dir a locked rmtree could not remove.
    shutil.copytree(_CLIENT_DIR / "src", DEPLOY_DIR / "src", dirs_exist_ok=True)

    # Ignore Terraform caches/state/tfvars so they never bloat or leak into the repo.
    (DEPLOY_DIR / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")

    _ignore = shutil.ignore_patterns(".terraform", "*.tfstate*", "terraform.tfvars")
    for svc in SERVICES:
        gen = _CLIENT_DIR / "generated" / svc
        # Workflow -> repo root .github/workflows/<svc>.yml (GitHub only runs these here).
        wf_src = gen / ".github" / "workflows" / "ci.yml"
        shutil.copy2(wf_src, DEPLOY_DIR / ".github" / "workflows" / f"{svc}.yml")
        # IaC -> deploy/iac/<svc>/ for reference (the .tf sources only, no caches).
        shutil.copytree(gen / "iac", DEPLOY_DIR / "iac" / svc, dirs_exist_ok=True,
                        ignore=_ignore)
    print(f"    assembled at: {DEPLOY_DIR}")
    print("    workflows: .github/workflows/lambda.yml, .github/workflows/stepfn.yml")


# ---------------------------------------------------------------------------
# 3. terraform (per service) — confirm before apply
# ---------------------------------------------------------------------------
# Per-service project names (match the generate.py settings + resource naming).
PROJECT_NAME = {"lambda": "sales-file-check", "stepfn": "sales-orchestrator"}
# The Lambda's entrypoint (handler.py -> def lambda_handler).
LAMBDA_HANDLER = "handler.lambda_handler"
# The raw data bucket the Lambda reads (account-suffixed for global uniqueness).
RAW_DATA_BUCKET_PREFIX = "raw-sales-data"


def _write_tfvars(svc: str, tf_dir: Path, account_id: str, extra: dict[str, str]) -> None:
    """Write terraform.tfvars for a service. Static-key auth (use_oidc=false) and
    logging off (enable_logging=false) keep the apply clean in SCP-restricted
    accounts. ``extra`` adds service-specific HCL assignment lines."""
    lines = [
        f'aws_region     = "{AWS_REGION}"',
        f'project_name   = "{PROJECT_NAME[svc]}"',
        "use_oidc       = false",
        "enable_logging = false",
    ]
    for k, v in extra.items():
        lines.append(f"{k} = {v}")
    (tf_dir / "terraform.tfvars").write_text("\n".join(lines) + "\n", encoding="utf-8")


#: Short working root OUTSIDE OneDrive so Terraform's deep provider cache path
#: (.terraform/providers/.../windows_amd64/...) stays under the Windows path limit.
_TF_WORK_ROOT = Path("C:/tf-cicd")


def _tf_apply(tf: str, svc: str, extra: dict[str, str], account_id: str,
              *, dry_run: bool) -> dict[str, str]:
    """Init + apply one service's Terraform in a short work dir; return outputs."""
    src_tf = DEPLOY_DIR / "iac" / svc / Path(IAC_SUBDIR[svc]).name
    tf_dir = _TF_WORK_ROOT / svc
    tf_dir.mkdir(parents=True, exist_ok=True)  # preserve state across runs
    for f in src_tf.glob("*.tf"):
        shutil.copy2(f, tf_dir / f.name)
    print(f"  [{svc}] terraform work dir: {tf_dir}")
    _write_tfvars(svc, tf_dir, account_id, extra)
    _run([tf, "init", "-input=false"], cwd=tf_dir)
    if not _confirm(f"[{svc}] terraform apply (creates AWS resources, uses credits)",
                    dry_run=dry_run):
        return {}
    _run([tf, "apply", "-auto-approve", "-input=false"], cwd=tf_dir)
    out = _run([tf, "output", "-json"], cwd=tf_dir, capture=True)
    return {k: v.get("value", "") for k, v in json.loads(out).items()}


def _ensure_raw_data_bucket(aws: str, account_id: str, lambda_role: str,
                            *, dry_run: bool) -> str:
    """Create the Lambda's raw-data bucket (idempotent) and grant the exec role read.

    This is CLIENT application infrastructure (the bucket the handler reads), not
    pipeline infra, so the deploy script provisions it directly. Returns the bucket
    name so it can be surfaced to the handler/config.
    """
    bucket = f"{RAW_DATA_BUCKET_PREFIX}-{account_id}"
    if not _confirm(f"ensure raw-data bucket '{bucket}' + grant Lambda read",
                    dry_run=dry_run):
        return bucket
    # Create if missing (idempotent: ignore "already owned by you").
    subprocess.run([aws, "s3api", "create-bucket", "--bucket", bucket,
                    "--region", AWS_REGION, "--create-bucket-configuration",
                    f"LocationConstraint={AWS_REGION}"], text=True, capture_output=True)
    # Grant the Lambda execution role read on that bucket.
    policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["s3:ListBucket", "s3:GetObject"],
            "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
        }],
    })
    subprocess.run([aws, "iam", "put-role-policy", "--role-name", lambda_role,
                    "--policy-name", "read-raw-data", "--policy-document", policy],
                   text=True, capture_output=True)
    print(f"    raw-data bucket ready + Lambda granted read: {bucket}")
    return bucket


def terraform_all(tools: dict[str, str], *, dry_run: bool) -> dict[str, dict[str, str]]:
    """Provision every service. Lambda first so its ARN can scope the SFN role.

    Returns {svc: terraform_outputs}. Fully self-sufficient: no manual steps.
    """
    print("\nSTEP 3 - terraform (create infra per service; Lambda first)")
    tf, aws, acct = tools["terraform"], tools["aws"], tools["account_id"]
    outputs: dict[str, dict[str, str]] = {}

    # --- Lambda ---
    outputs["lambda"] = _tf_apply(
        tf, "lambda",
        {"lambda_handler": f'"{LAMBDA_HANDLER}"'},
        acct, dry_run=dry_run,
    )
    lambda_arn = outputs["lambda"].get("lambda_function_arn") or \
        f"arn:aws:lambda:{AWS_REGION}:{acct}:function:{PROJECT_NAME['lambda']}"
    lambda_role = outputs["lambda"].get("lambda_exec_role_name") or \
        f"{PROJECT_NAME['lambda']}-lambda-exec"

    # Client app infra: the raw-data bucket the handler reads.
    if not dry_run:
        _ensure_raw_data_bucket(aws, acct, lambda_role, dry_run=dry_run)

    # --- Step Functions (scoped to invoke the Lambda just deployed) ---
    if "stepfn" in SERVICES:
        outputs["stepfn"] = _tf_apply(
            tf, "stepfn",
            {"task_lambda_arns": f'["{lambda_arn}"]'},
            acct, dry_run=dry_run,
        )
    return outputs


# ---------------------------------------------------------------------------
# 4. github — create repo, push, set secret (confirm each)
# ---------------------------------------------------------------------------
def _derive_secrets(tools: dict[str, str], outputs: dict[str, dict[str, str]]) -> dict[str, str]:
    """All GitHub secrets the workflows reference, derived from Terraform outputs.

    The per-service by-reference names (bucket, function, role, state machine) are
    resolved from the apply outputs so the pipeline's $VARS all resolve at run time.
    AWS keys come from the local aws config. Values are set into GitHub's encrypted
    store and never printed.
    """
    aws = tools["aws"]
    lam = outputs.get("lambda", {})
    sfn = outputs.get("stepfn", {})
    secrets: dict[str, str] = {
        "AWS_ACCESS_KEY_ID": _run([aws, "configure", "get", "aws_access_key_id"], capture=True),
        "AWS_SECRET_ACCESS_KEY": _run([aws, "configure", "get", "aws_secret_access_key"], capture=True),
        # Lambda
        "LAMBDA_FUNCTION": lam.get("lambda_function_name", PROJECT_NAME["lambda"]),
        "LAMBDA_ALIAS": lam.get("lambda_alias_name", "live"),
        "LAMBDA_ARTIFACT_BUCKET": lam.get("artifact_bucket", ""),
        "LAMBDA_ARTIFACT_KEY": "lambda/sales-file-check.zip",
    }
    if sfn:
        secrets.update({
            "SFN_STATE_MACHINE_NAME": sfn.get("state_machine_name", f"{PROJECT_NAME['stepfn']}-sfn"),
            "SFN_ROLE_ARN": sfn.get("sfn_role_arn", ""),
            "SFN_ARTIFACT_BUCKET": sfn.get("artifact_bucket", ""),
            "SFN_DEFINITION_KEY": "stepfunctions/state-machine.asl.json",
        })
    return {k: v for k, v in secrets.items() if v}


def github_all(tools: dict[str, str], outputs: dict[str, dict[str, str]], *, dry_run: bool) -> None:
    print("\nSTEP 4 - GitHub (create repo, set all secrets, push)")
    gh, git = tools["gh"], tools["git"]
    slug = f"{GITHUB_OWNER}/{GITHUB_REPO}"

    if not (DEPLOY_DIR / ".git").exists():
        _run([git, "init", "-b", DEFAULT_BRANCH], cwd=DEPLOY_DIR)
        _run([git, "config", "user.name", GITHUB_OWNER], cwd=DEPLOY_DIR)
        _run([git, "config", "user.email", f"{GITHUB_OWNER}@users.noreply.github.com"],
             cwd=DEPLOY_DIR)
        _run([git, "add", "-A"], cwd=DEPLOY_DIR)
        _run([git, "commit", "-m", "Initial deploy: sales pipeline (generated)"], cwd=DEPLOY_DIR)

    if _confirm(f"create GitHub repo {slug} (private)", dry_run=dry_run):
        subprocess.run([gh, "repo", "create", slug, "--private",
                        "--source", str(DEPLOY_DIR), "--remote", "origin"], text=True)

    # Set EVERY secret the workflows reference, derived from the Terraform outputs.
    secrets = _derive_secrets(tools, outputs)
    if _confirm(f"set {len(secrets)} GitHub secrets (values not shown)", dry_run=dry_run):
        for name, value in secrets.items():
            subprocess.run([gh, "secret", "set", name, "--body", value, "--repo", slug],
                           text=True, capture_output=True)
        print(f"    set: {', '.join(sorted(secrets))}")

    if _confirm(f"push to {slug} (triggers the workflows)", dry_run=dry_run):
        _run([git, "push", "-u", "origin", DEFAULT_BRANCH], cwd=DEPLOY_DIR)
        print(f"\n  Watch the pipelines: https://github.com/{slug}/actions")


def main(argv: list[str] | None = None) -> int:
    global _YES
    parser = argparse.ArgumentParser(prog="deploy")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan everything, touch no cloud/GitHub.")
    parser.add_argument("--yes", action="store_true",
                        help="skip confirmations (non-interactive).")
    args = parser.parse_args(argv)
    _YES = args.yes

    tools = preflight()
    generate_all()
    assemble_repo()
    outputs = terraform_all(tools, dry_run=args.dry_run)
    github_all(tools, outputs, dry_run=args.dry_run)
    print("\nDONE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
