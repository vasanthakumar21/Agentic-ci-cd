"""E2E isolation test — multi-client, multi-combination (task 13, R9.1 / R3.1 / R10.3).

The capstone proof that the generator serves MANY clients on DIFFERENT two-axis
combinations without any cross-contamination, and that it only ever writes inside
the repo root it is handed. Everything here runs against the **REAL** on-disk
plugin tree (``<repo>/plugins``, ``check_plugins=True``) and pytest's ``tmp_path`` —
no fakes for generation, no network, no cloud account.

Two independent axes are exercised with two genuinely different clients:

    client1 : github       x aws-ecs     (cloud.provider aws)    -> IMAGE shape
              .github/workflows/ci.yml + Dockerfile + iac/aws-ecs/main.tf
    client2 : azure-devops x databricks   (cloud.provider azure)  -> JOB shape
              azure-pipelines.yml + databricks/job.json + iac/databricks/main.tf
              and NO Dockerfile

Each client is generated into its OWN per-client ``tmp_path`` subdirectory with its
OWN ``.cicd`` manifest, proving per-client isolation (R10.3): neither client's
artifacts bleed into the other's tree, each manifest lists only its own files, and
generating client2 leaves client1's directory byte-for-byte unchanged.

The bootstrap half is exercised in **dry-run** (R9.2): a valid
``bootstrap.local.yaml`` + ``secrets.local.yaml`` is written into a per-client
``.cicd`` dir, an ``environ`` dict supplies the admin-token env var, and
``apply_bootstrap(confirm=False)`` is asserted to be a pure dry-run — every outcome
SKIPPED, the local :class:`FakeExecutor`'s ``mutating_call_count`` is zero, and no
secret VALUE sentinel ever reaches the plan / automation report.

--------------------------------------------------------------------------------
LIVE STEPS (NOT executed here — documented per the environment constraint)
--------------------------------------------------------------------------------
This environment has NO throwaway cloud accounts, no Terraform, and no Docker, so
the live ``run``/``deploy`` legs of task 13.1 cannot be executed. They are recorded
here so the manual operator can complete them in a real throwaway environment:

    1. In a throwaway GitHub org + AWS account, run the real ``apply_bootstrap``
       (``confirm=True``) for client1, push the generated repo, let the
       ``.github/workflows/ci.yml`` pipeline run, and confirm the ECS service is
       deployed via the generated ``iac/aws-ecs`` Terraform (``terraform apply``).
    2. In a throwaway Azure DevOps org + Azure subscription + Databricks workspace,
       do the same for client2: bootstrap (``confirm=True``), run the generated
       ``azure-pipelines.yml``, and confirm the Databricks job is created from
       ``databricks/job.json`` via the generated ``iac/databricks`` Terraform.
    3. Confirm each client's resources are fully isolated from the other's.

--------------------------------------------------------------------------------
R9 CONTAINMENT (automated here — task 13.2)
--------------------------------------------------------------------------------
Per R9 the generated product is its OWN isolated repo; the generator never writes
into any existing TekCor4 repository nor onto the TEK-969 branch. We cannot assert
against those real repos from a hermetic test, so the ``test_no_touch_*`` cases
below provide the equivalent automated proof: ``generate()`` writes ONLY under the
``repo_root`` it is given — a sentinel sibling directory is captured before/after
and must be unchanged, and every path in ``result.written`` + the persisted
manifest resolves underneath the passed ``repo_root``. That containment guarantee
is exactly what keeps real TekCor4 repos / the TEK-969 branch untouched.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml

from cicdgen.bootstrap import (
    ApplyResult,
    BranchProtectionRule,
    Outcome,
    apply_bootstrap,
    emit_automation_report,
)
from cicdgen.core import GenerationResult, generate, load_manifest
from cicdgen.ir import Pipeline

# <repo>/tests/test_e2e_multi_client_isolation.py -> <repo>; the REAL plugins live here.
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"

# Shape-defining artifacts per axis (design §8).
GITHUB_WORKFLOW = ".github/workflows/ci.yml"
AZURE_PIPELINE = "azure-pipelines.yml"
DOCKERFILE = "Dockerfile"
JOB_SPEC = "databricks/job.json"
AWS_ECS_IAC_MAIN = "iac/aws-ecs/main.tf"
DBX_IAC_MAIN = "iac/databricks/main.tf"

# Secret VALUE sentinels that MUST NEVER leak into a plan / report (R9.3).
AWS_SECRET_SENTINEL = "aws-s3cr3t-SHOULD-NEVER-LEAK-11"
DBX_SECRET_SENTINEL = "dbx-s3cr3t-SHOULD-NEVER-LEAK-22"
ADMIN_TOKEN_SENTINEL = "adminTOKEN-SHOULD-NEVER-LEAK-99"
ADMIN_ENV = "CICD_ADMIN_TOKEN"


# ===========================================================================
# Local FakeExecutor — mirrors cicdgen.bootstrap.apply.Executor, records calls.
# (Defined locally per the task; same contract as tests/test_bootstrap_apply.py.)
# ===========================================================================
class FakeExecutor:
    """In-memory resource store that records every call.

    An empty instance simulates a brand-new environment. Every mutating method
    both updates the store and bumps a counter, so a test can assert that a
    dry-run performed ZERO mutations.
    """

    def __init__(
        self,
        *,
        repo_present: bool = False,
        secrets: set[str] | None = None,
        oidc_trusts: set[tuple[str, str]] | None = None,
        runners: set[tuple[str, ...]] | None = None,
        branch_rules: dict[str, BranchProtectionRule] | None = None,
    ) -> None:
        self._repo_present = repo_present
        self._secrets: set[str] = set(secrets or set())
        self._oidc_trusts: set[tuple[str, str]] = set(oidc_trusts or set())
        self._runners: set[tuple[str, ...]] = set(runners or set())
        self._branch_rules: dict[str, BranchProtectionRule] = dict(branch_rules or {})

        self.pushed_secret_values: dict[str, str] = {}
        self.registered_tokens: list[str] = []
        self.calls: Counter[str] = Counter()

    # --- repo --------------------------------------------------------------
    def repo_exists(self) -> bool:
        self.calls["repo_exists"] += 1
        return self._repo_present

    def create_repo(self) -> None:
        self.calls["create_repo"] += 1
        self._repo_present = True

    # --- secrets -----------------------------------------------------------
    def secret_exists(self, name: str) -> bool:
        self.calls["secret_exists"] += 1
        return name in self._secrets

    def push_secret(self, name: str, value: str) -> None:
        self.calls["push_secret"] += 1
        self._secrets.add(name)
        self.pushed_secret_values[name] = value

    # --- oidc --------------------------------------------------------------
    def oidc_trust_exists(self, provider: str, role_name: str) -> bool:
        self.calls["oidc_trust_exists"] += 1
        return (provider, role_name) in self._oidc_trusts

    def create_oidc_trust(self, provider: str, role_name: str) -> None:
        self.calls["create_oidc_trust"] += 1
        self._oidc_trusts.add((provider, role_name))

    # --- runners -----------------------------------------------------------
    def runner_registered(self, labels: tuple[str, ...]) -> bool:
        self.calls["runner_registered"] += 1
        return labels in self._runners

    def register_runner(self, labels: tuple[str, ...], token: str) -> None:
        self.calls["register_runner"] += 1
        self._runners.add(labels)
        self.registered_tokens.append(token)

    # --- branch protection -------------------------------------------------
    def branch_protection_current(self, branch: str) -> BranchProtectionRule | None:
        self.calls["branch_protection_current"] += 1
        return self._branch_rules.get(branch)

    def set_branch_protection(self, branch: str, rule: BranchProtectionRule) -> None:
        self.calls["set_branch_protection"] += 1
        self._branch_rules[branch] = rule

    # --- assertion helper --------------------------------------------------
    @property
    def mutating_call_count(self) -> int:
        mutators = (
            "create_repo",
            "push_secret",
            "create_oidc_trust",
            "register_runner",
            "set_branch_protection",
        )
        return sum(self.calls[m] for m in mutators)


# ===========================================================================
# IR builders — two genuinely different two-axis combinations, built in-memory.
# ===========================================================================
def _client1_github_aws() -> Pipeline:
    """client1: github x aws-ecs, cloud.provider aws -> IMAGE shape."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-ecs",
            "project": {"name": "orders-api", "language": "python"},
            "cloud": {"provider": "aws", "region": "eu-west-2", "auth": "oidc"},
            "environments": [
                {"name": "dev", "gate": "auto"},
                {"name": "prod", "gate": "manual", "protection": "required-approvals"},
            ],
            "stages": [
                {"id": "test", "runs": "make test", "scans": ["sast", "secrets"]},
                {"id": "build", "runs": "docker build -t $ECR_REPO:$IMAGE_TAG .", "timeout_minutes": 30},
                {"id": "deploy-dev", "environment": "dev", "runs": "aws ecs update-service"},
            ],
        }
    )


def _client2_azdo_databricks() -> Pipeline:
    """client2: azure-devops x databricks, cloud.provider azure -> JOB shape."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "azure-devops",
            "target_platform": "databricks",
            "project": {"name": "my-service", "language": "python"},
            "cloud": {"provider": "azure", "region": "uksouth", "auth": "oidc"},
            "environments": [
                {"name": "dev", "gate": "auto"},
                {"name": "prod", "gate": "manual", "protection": "required-approvals"},
            ],
            "stages": [
                {"id": "test", "runs": "make test", "scans": ["sast", "secrets"]},
                {"id": "build", "runs": "python -m build --wheel --outdir dist/", "timeout_minutes": 30},
                {"id": "deploy-dev", "environment": "dev", "runs": "databricks jobs create --json @databricks/job.json"},
            ],
        }
    )


def _gen(pipeline: Pipeline, repo: Path) -> GenerationResult:
    """Generate through the REAL on-disk plugins with plugin checking on."""
    result = generate(pipeline, repo, plugins_root=PLUGINS_ROOT, check_plugins=True)
    assert isinstance(result, GenerationResult)
    return result


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    """Map every file under ``root`` to its bytes (relative-path keyed)."""
    return {
        str(p.relative_to(root)).replace("\\", "/"): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ---------------------------------------------------------------------------
# bootstrap builders (dry-run only)
# ---------------------------------------------------------------------------
def _aws_bootstrap() -> dict:
    return {
        "repo": {
            "owner": "client1-org",
            "name": "orders-api",
            "create_if_missing": True,
            "default_branch": "main",
        },
        "vendor_admin": {"token_env": ADMIN_ENV},
        "runners": {"mode": "shared"},
        "oidc": {
            "aws": {
                "account_id": "123456789012",
                "role_name": "orders-deploy-role",
                "region": "eu-west-2",
            }
        },
        "branch_protection": {
            "main": {"required_approvals": 2, "required_checks": ["build", "test"]},
        },
        "target": {
            "aws-ecs": {
                "ecr_repo_secret": "ECR_REPO",
                "cluster": "orders-cluster",
                "service": "orders-api",
                "task_family": "orders-api-task",
            }
        },
        "secrets": ["ECR_REPO"],
    }


def _dbx_bootstrap() -> dict:
    return {
        "repo": {
            "owner": "client2-org",
            "name": "my-service",
            "create_if_missing": True,
            "default_branch": "main",
        },
        "vendor_admin": {"token_env": ADMIN_ENV},
        "runners": {"mode": "shared"},
        "oidc": {
            "azure": {
                "tenant_id": "tenant-2",
                "subscription_id": "sub-2",
                "client_id": "client-2",
            }
        },
        "branch_protection": {
            "main": {"required_approvals": 2, "required_checks": ["build", "test"]},
        },
        "target": {
            "databricks": {
                "host_secret": "DBX_HOST",
                "token_secret": "DBX_TOKEN",
                "job_name": "my-job",
            }
        },
        "secrets": ["DBX_HOST", "DBX_TOKEN"],
    }


def _write_cicd(cicd_dir: Path, bootstrap: dict, secrets: dict) -> Path:
    cicd_dir.mkdir(parents=True, exist_ok=True)
    (cicd_dir / "bootstrap.local.yaml").write_text(yaml.safe_dump(bootstrap), encoding="utf-8")
    (cicd_dir / "secrets.local.yaml").write_text(yaml.safe_dump(secrets), encoding="utf-8")
    return cicd_dir


def _admin_env() -> dict[str, str]:
    return {ADMIN_ENV: ADMIN_TOKEN_SENTINEL}


# ===========================================================================
# 0. the REAL plugin tree exists (otherwise this module proves nothing)
# ===========================================================================
def test_real_plugin_tree_exists() -> None:
    assert (PLUGINS_ROOT / "ci" / "github").is_dir()
    assert (PLUGINS_ROOT / "ci" / "azure-devops").is_dir()
    # Target plugins live provider-nested after the §11.1 reorg.
    assert (PLUGINS_ROOT / "target" / "aws" / "ecs").is_dir()
    assert (PLUGINS_ROOT / "target" / "azure" / "databricks").is_dir()


# ===========================================================================
# 1. Two clients, DIFFERENT combinations, SEPARATE dirs, real plugins (R3.1)
# ===========================================================================
def test_two_clients_different_combinations_generate_correct_shapes(tmp_path: Path) -> None:
    client1 = tmp_path / "client1"
    client2 = tmp_path / "client2"

    r1 = _gen(_client1_github_aws(), client1)
    r2 = _gen(_client2_azdo_databricks(), client2)

    # --- client1: github x aws-ecs -> IMAGE shape --------------------------
    assert (client1 / GITHUB_WORKFLOW).is_file()
    assert (client1 / DOCKERFILE).is_file()
    assert (client1 / AWS_ECS_IAC_MAIN).is_file()
    # the github workflow parses as YAML with jobs
    wf = yaml.safe_load((client1 / GITHUB_WORKFLOW).read_text(encoding="utf-8"))
    assert isinstance(wf, dict) and isinstance(wf["jobs"], dict) and wf["jobs"]

    # --- client2: azure-devops x databricks -> JOB shape, NO Dockerfile ----
    assert (client2 / AZURE_PIPELINE).is_file()
    assert (client2 / JOB_SPEC).is_file()
    assert (client2 / DBX_IAC_MAIN).is_file()
    assert not (client2 / DOCKERFILE).exists(), "databricks is a job, not an image"
    # the azure pipeline parses as YAML with stages
    az = yaml.safe_load((client2 / AZURE_PIPELINE).read_text(encoding="utf-8"))
    assert isinstance(az, dict) and isinstance(az["stages"], list) and az["stages"]

    # --- each manifest records its own axes --------------------------------
    assert r1.ci_vendor == "github" and r1.target_platform == "aws-ecs"
    assert r2.ci_vendor == "azure-devops" and r2.target_platform == "databricks"

    m1 = load_manifest(client1)
    m2 = load_manifest(client2)
    assert m1 is not None and m2 is not None
    assert m1.ci_vendor == "github" and m1.target_platform == "aws-ecs"
    assert m2.ci_vendor == "azure-devops" and m2.target_platform == "databricks"


# ===========================================================================
# 2. Per-client isolation (R10.3)
# ===========================================================================
def test_client1_has_no_databricks_artifacts(tmp_path: Path) -> None:
    client1 = tmp_path / "client1"
    _gen(_client1_github_aws(), client1)

    assert not (client1 / JOB_SPEC).exists()
    assert not (client1 / DBX_IAC_MAIN).exists()
    assert not (client1 / AZURE_PIPELINE).exists()
    # manifest lists only aws-ecs/github paths, never a databricks one
    m1 = load_manifest(client1)
    assert m1 is not None
    assert JOB_SPEC not in m1.paths
    assert AZURE_PIPELINE not in m1.paths
    assert all("databricks" not in p for p in m1.paths)


def test_client2_has_no_dockerfile(tmp_path: Path) -> None:
    client2 = tmp_path / "client2"
    _gen(_client2_azdo_databricks(), client2)

    assert not (client2 / DOCKERFILE).exists()
    assert not (client2 / GITHUB_WORKFLOW).exists()
    assert not (client2 / AWS_ECS_IAC_MAIN).exists()
    # manifest lists only databricks/azure paths, never a Dockerfile/aws-ecs one
    m2 = load_manifest(client2)
    assert m2 is not None
    assert DOCKERFILE not in m2.paths
    assert GITHUB_WORKFLOW not in m2.paths
    assert all("aws-ecs" not in p for p in m2.paths)


def test_each_manifest_lists_only_its_own_files(tmp_path: Path) -> None:
    client1 = tmp_path / "client1"
    client2 = tmp_path / "client2"
    _gen(_client1_github_aws(), client1)
    _gen(_client2_azdo_databricks(), client2)

    m1 = load_manifest(client1)
    m2 = load_manifest(client2)
    assert m1 is not None and m2 is not None

    # Every path a manifest claims actually exists in THAT client's tree only.
    for p in m1.paths:
        assert (client1 / p).is_file()
    for p in m2.paths:
        assert (client2 / p).is_file()

    # The two manifests' shape-defining artifacts are disjoint.
    assert GITHUB_WORKFLOW in m1.paths and GITHUB_WORKFLOW not in m2.paths
    assert AZURE_PIPELINE in m2.paths and AZURE_PIPELINE not in m1.paths
    assert DOCKERFILE in m1.paths and DOCKERFILE not in m2.paths
    assert JOB_SPEC in m2.paths and JOB_SPEC not in m1.paths


def test_generating_client2_does_not_touch_client1(tmp_path: Path) -> None:
    client1 = tmp_path / "client1"
    client2 = tmp_path / "client2"

    # Generate client1, snapshot its ENTIRE tree (paths + bytes).
    _gen(_client1_github_aws(), client1)
    before = _snapshot_tree(client1)
    assert before, "client1 should have produced files"

    # Generating client2 into a separate dir must not create/modify/delete
    # anything under client1.
    _gen(_client2_azdo_databricks(), client2)
    after = _snapshot_tree(client1)

    assert after == before, "generating client2 must not touch client1's tree"


def test_the_two_cicd_dirs_are_separate(tmp_path: Path) -> None:
    client1 = tmp_path / "client1"
    client2 = tmp_path / "client2"
    _gen(_client1_github_aws(), client1)
    _gen(_client2_azdo_databricks(), client2)

    cicd1 = client1 / ".cicd" / "generated.manifest.json"
    cicd2 = client2 / ".cicd" / "generated.manifest.json"
    assert cicd1.is_file() and cicd2.is_file()
    assert cicd1.resolve() != cicd2.resolve()
    # the two manifests differ (different axes)
    assert cicd1.read_text(encoding="utf-8") != cicd2.read_text(encoding="utf-8")


# ===========================================================================
# 3. Bootstrap each client in dry-run (R9.2 / R9.3)
# ===========================================================================
@pytest.mark.parametrize(
    ("bootstrap", "secrets", "secret_sentinel"),
    [
        (_aws_bootstrap(), {"ECR_REPO": AWS_SECRET_SENTINEL}, AWS_SECRET_SENTINEL),
        (
            _dbx_bootstrap(),
            {"DBX_HOST": "https://dbx.example", "DBX_TOKEN": DBX_SECRET_SENTINEL},
            DBX_SECRET_SENTINEL,
        ),
    ],
    ids=["client1-aws", "client2-databricks"],
)
def test_bootstrap_dry_run_performs_no_mutations_and_leaks_no_secret(
    tmp_path: Path, bootstrap: dict, secrets: dict, secret_sentinel: str
) -> None:
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, secrets)
    fake = FakeExecutor()  # brand-new environment

    result = apply_bootstrap(cicd, executor=fake, environ=_admin_env(), confirm=False)

    # dry-run with every outcome SKIPPED and ZERO mutating calls
    assert isinstance(result, ApplyResult)
    assert result.dry_run is True
    assert result.results, "dry run should still describe planned actions"
    assert all(r.outcome is Outcome.SKIPPED for r in result.results)
    assert fake.mutating_call_count == 0

    # the injected secret VALUE leaks nowhere into the automation report / plan
    report = emit_automation_report(cicd, environ=_admin_env())
    assert secret_sentinel not in report
    assert ADMIN_TOKEN_SENTINEL not in report
    # nor into any structured dry-run result field
    for r in result.results:
        assert secret_sentinel not in r.detail
        assert secret_sentinel not in r.target
        assert ADMIN_TOKEN_SENTINEL not in r.detail


def test_both_clients_bootstrap_dirs_are_independent(tmp_path: Path) -> None:
    cicd1 = _write_cicd(tmp_path / "c1" / ".cicd", _aws_bootstrap(), {"ECR_REPO": AWS_SECRET_SENTINEL})
    cicd2 = _write_cicd(
        tmp_path / "c2" / ".cicd",
        _dbx_bootstrap(),
        {"DBX_HOST": "https://dbx.example", "DBX_TOKEN": DBX_SECRET_SENTINEL},
    )
    fake1 = FakeExecutor()
    fake2 = FakeExecutor()

    r1 = apply_bootstrap(cicd1, executor=fake1, environ=_admin_env(), confirm=False)
    r2 = apply_bootstrap(cicd2, executor=fake2, environ=_admin_env(), confirm=False)

    assert r1.dry_run is True and r2.dry_run is True
    assert fake1.mutating_call_count == 0
    assert fake2.mutating_call_count == 0
    # neither client's secret value appears in the other's report
    report1 = emit_automation_report(cicd1, environ=_admin_env())
    report2 = emit_automation_report(cicd2, environ=_admin_env())
    assert DBX_SECRET_SENTINEL not in report1
    assert AWS_SECRET_SENTINEL not in report2


# ===========================================================================
# 4. No-touch containment (R9.1 / task 13.2)
# ===========================================================================
def test_generate_writes_only_under_repo_root_sentinel_sibling(tmp_path: Path) -> None:
    repo = tmp_path / "product-repo"
    sibling = tmp_path / "existing-tekcor4-repo"  # stands in for a real TekCor4 repo
    sibling.mkdir()
    (sibling / "DO_NOT_TOUCH.txt").write_text("sentinel", encoding="utf-8")

    before_sibling = _snapshot_tree(sibling)

    _gen(_client1_github_aws(), repo)

    after_sibling = _snapshot_tree(sibling)
    # the sibling "existing repo" is byte-for-byte unchanged — generation never
    # reached outside the repo_root it was handed (R9.1).
    assert after_sibling == before_sibling


def test_every_written_path_and_manifest_entry_resolves_under_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "product-repo"
    result = _gen(_client1_github_aws(), repo)
    repo_resolved = repo.resolve()

    assert result.written, "a real generation writes files"
    # Every written path is repo-relative and resolves strictly under repo_root.
    for rel in result.written:
        resolved = (repo / rel).resolve()
        assert resolved.is_relative_to(repo_resolved), f"{rel} escaped repo_root"

    # Every manifest-owned path likewise resolves under repo_root and exists.
    manifest = load_manifest(repo)
    assert manifest is not None
    for rel in manifest.paths:
        resolved = (repo / rel).resolve()
        assert resolved.is_relative_to(repo_resolved), f"{rel} escaped repo_root"
        assert resolved.is_file()


def test_generation_creates_nothing_above_repo_root(tmp_path: Path) -> None:
    # The whole tmp_path is the "universe". Generate into a nested repo and assert
    # that the ONLY files that appeared anywhere in tmp_path are under that repo.
    repo = tmp_path / "nested" / "product-repo"
    before_universe = set(_snapshot_tree(tmp_path).keys())

    _gen(_client1_github_aws(), repo)

    after_universe = set(_snapshot_tree(tmp_path).keys())
    new_paths = after_universe - before_universe
    assert new_paths, "generation should have created files"
    repo_prefix = "nested/product-repo/"
    assert all(p.startswith(repo_prefix) for p in new_paths), (
        f"generation wrote outside repo_root: {sorted(p for p in new_paths if not p.startswith(repo_prefix))}"
    )
