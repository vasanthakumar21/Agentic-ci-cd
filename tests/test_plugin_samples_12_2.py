"""End-to-end generate + validate for the task-12.1 plugins (task 12.2, R3.4).

Task 12.1 authored five NEW provider plugins, each with its own isolated unit
test. Those tests call a plugin's methods directly. This module is the
complementary proof that each new plugin ALSO works through the REAL
loader/dispatch + generator core — exactly what a client gets — composed with a
real plugin on the OTHER axis, with everything written to disk and recorded in
the manifest.

Nothing is faked: ``generate(...)`` runs against the on-disk plugin tree
(``plugins_root=<repo>/plugins``, NO injected registry), so the on-disk plugin
existence check (``check_plugins=True``, the default) is genuinely exercised. We
generate into pytest's ``tmp_path`` only — never the product repo.

Representative combinations (each exercises ONE new plugin):

    gitlab    x aws-ecs          new CI    x existing target
    bitbucket x aws-ecs          new CI    x existing target
    jenkins   x aws-ecs          new CI    x existing target
    github    x azure-functions  existing CI x new target  (NO Dockerfile)
    github    x kubernetes       existing CI x new target  (Dockerfile + k8s)

For each: the committed sample IR validates, generation succeeds, the emitted CI
file PARSES (YAML for gitlab/bitbucket/github; Groovy text checks for jenkins),
the target's shape-defining artifacts are present, the manifest records the two
axes, and secrets appear by reference only (no literal ``arn:aws:iam`` / ``AKIA``).

Finally we assert all five CI + all four target plugins are discoverable from the
real tree (R3.4 — adding plugins never touched the core; they're all present).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from cicdgen.core import GenerationResult, generate, load_manifest
from cicdgen.ir import AvailablePlugins, Pipeline
from cicdgen.plugins.target_layout import resolve_target_dir

# <repo>/tests/test_plugin_samples_12_2.py -> <repo>; the REAL plugin tree + samples live here.
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"
SAMPLES_DIR = REPO_ROOT / "examples" / "samples-12-2"

# CI pipeline file each vendor emits (design §8a).
CI_FILE = {
    "github": ".github/workflows/ci.yml",
    "gitlab": ".gitlab-ci.yml",
    "bitbucket": "bitbucket-pipelines.yml",
    "jenkins": "Jenkinsfile",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_sample(name: str) -> Pipeline:
    """Parse + validate a committed sample IR into a Pipeline."""
    path = SAMPLES_DIR / f"{name}.yaml"
    assert path.is_file(), f"missing sample IR: {path}"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Pipeline.model_validate(data)


def _gen(sample: str, repo: Path) -> GenerationResult:
    """Generate a committed sample through the REAL on-disk plugins into ``repo``."""
    pipeline = _load_sample(sample)
    result = generate(
        pipeline,
        repo,
        plugins_root=PLUGINS_ROOT,
        check_plugins=True,  # real plugins exist on disk -> the check must pass
    )
    assert isinstance(result, GenerationResult)
    return result


def _read(repo: Path, rel: str) -> str:
    return (repo / rel).read_text(encoding="utf-8")


# An account-SPECIFIC IAM ARN embeds a 12-digit account id: arn:aws:iam::123456789012:...
# That is the leak we must never commit. The PUBLIC AWS-managed-policy ARN form
# (arn:aws:iam::aws:policy/...) carries no account id and no secret — it is a
# standard, documented Terraform reference (e.g. AmazonECSTaskExecutionRolePolicy)
# and is explicitly allowed.
_ACCOUNT_SPECIFIC_IAM_ARN = re.compile(r"arn:aws:iam::\d{12}:")
_AWS_ACCESS_KEY_ID = re.compile(r"\bAKIA[0-9A-Z]{16}\b")


def _assert_no_literal_secrets(repo: Path) -> None:
    """No literal cloud credentials anywhere in the generated tree (R6.3/R6.6).

    "Secrets by reference only" forbids an ACCOUNT-SPECIFIC IAM ARN
    (``arn:aws:iam::<account-id>:...``) or an AWS access-key id (``AKIA...``)
    from being committed. The public AWS-managed-policy ARN
    (``arn:aws:iam::aws:policy/...``) is NOT account-specific or secret and is
    allowed — target plugins reference it legitimately in their Terraform.
    """
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        assert not _ACCOUNT_SPECIFIC_IAM_ARN.search(content), (
            f"account-specific IAM ARN leaked into {path}"
        )
        assert not _AWS_ACCESS_KEY_ID.search(content), (
            f"literal AWS access-key id leaked into {path}"
        )


def _assert_ci_file_parses(repo: Path, vendor: str) -> None:
    """The emitted CI file is written and parses for its vendor's format."""
    rel = CI_FILE[vendor]
    assert (repo / rel).is_file(), f"{vendor}: expected CI file {rel} to be written"
    content = _read(repo, rel)

    if vendor == "github":
        wf = yaml.safe_load(content)
        assert isinstance(wf, dict)
        # a GitHub Actions workflow: jobs mapping + `on` survives as a string key
        assert isinstance(wf.get("jobs"), dict) and wf["jobs"]
        assert "on" in wf and True not in wf
    elif vendor == "gitlab":
        doc = yaml.safe_load(content)
        assert isinstance(doc, dict)
        assert isinstance(doc.get("stages"), list) and doc["stages"]
    elif vendor == "bitbucket":
        doc = yaml.safe_load(content)
        assert isinstance(doc, dict)
        default = doc["pipelines"]["default"]
        assert isinstance(default, list) and default
        # each entry is a {"step": {...}} mapping (the ordered step sequence)
        assert all(isinstance(e, dict) and "step" in e for e in default)
    elif vendor == "jenkins":
        # a Jenkinsfile is Groovy, not YAML: assert over the text/structure
        assert content.startswith("pipeline {")
        assert "stages {" in content
        assert content.count("{") == content.count("}")  # balanced braces
    else:  # pragma: no cover - guard against a new vendor slipping in untested
        raise AssertionError(f"no CI-parse assertion for vendor {vendor}")


def _assert_manifest(repo: Path, ci_vendor: str, target_platform: str) -> None:
    manifest = load_manifest(repo)
    assert manifest is not None
    assert manifest.ci_vendor == ci_vendor
    assert manifest.target_platform == target_platform
    # the emitted CI file is a tracked, owned artifact
    assert CI_FILE[ci_vendor] in manifest.paths


# ===========================================================================
# The real plugin tree + committed samples are present
# ===========================================================================
def test_real_plugin_tree_and_samples_exist() -> None:
    for vendor in ("github", "gitlab", "bitbucket", "jenkins"):
        assert (PLUGINS_ROOT / "ci" / vendor).is_dir()
    # Target plugins live provider-nested after the §11.1 reorg; resolve each
    # legacy IR name to its (provider, service) home via the data-only table.
    for target in ("aws-ecs", "azure-functions", "kubernetes"):
        provider, service = resolve_target_dir(target)
        assert (PLUGINS_ROOT / "target" / provider / service).is_dir()
    for sample in (
        "gitlab-aws-ecs",
        "bitbucket-aws-ecs",
        "jenkins-aws-ecs",
        "github-azure-functions",
        "github-kubernetes",
    ):
        assert (SAMPLES_DIR / f"{sample}.yaml").is_file()


# ===========================================================================
# gitlab x aws-ecs  (new CI x existing target)
# ===========================================================================
def test_gitlab_aws_ecs_generates_and_validates(tmp_path: Path) -> None:
    _gen("gitlab-aws-ecs", tmp_path)

    # CI file written + parses as valid GitLab YAML with a stages list
    _assert_ci_file_parses(tmp_path, "gitlab")
    doc = yaml.safe_load(_read(tmp_path, ".gitlab-ci.yml"))
    assert "build" in doc and doc["build"]["stage"] == "build"

    # aws-ecs target shape: Dockerfile + iac/aws-ecs/*
    assert (tmp_path / "Dockerfile").is_file()
    assert (tmp_path / "iac" / "aws-ecs" / "main.tf").is_file()

    _assert_manifest(tmp_path, "gitlab", "aws-ecs")
    _assert_no_literal_secrets(tmp_path)


# ===========================================================================
# bitbucket x aws-ecs  (new CI x existing target)
# ===========================================================================
def test_bitbucket_aws_ecs_generates_and_validates(tmp_path: Path) -> None:
    _gen("bitbucket-aws-ecs", tmp_path)

    # CI file written + parses (pipelines.default step sequence)
    _assert_ci_file_parses(tmp_path, "bitbucket")
    doc = yaml.safe_load(_read(tmp_path, "bitbucket-pipelines.yml"))
    names = [e["step"]["name"] for e in doc["pipelines"]["default"]]
    assert "build" in names

    # aws-ecs target shape present
    assert (tmp_path / "Dockerfile").is_file()
    assert (tmp_path / "iac" / "aws-ecs" / "main.tf").is_file()

    _assert_manifest(tmp_path, "bitbucket", "aws-ecs")
    _assert_no_literal_secrets(tmp_path)


# ===========================================================================
# jenkins x aws-ecs  (new CI x existing target)
# ===========================================================================
def test_jenkins_aws_ecs_generates_and_validates(tmp_path: Path) -> None:
    _gen("jenkins-aws-ecs", tmp_path)

    # Jenkinsfile written (Groovy: starts with `pipeline {`, has stages)
    _assert_ci_file_parses(tmp_path, "jenkins")
    content = _read(tmp_path, "Jenkinsfile")
    assert "stage('build')" in content

    # aws-ecs target shape present
    assert (tmp_path / "Dockerfile").is_file()
    assert (tmp_path / "iac" / "aws-ecs" / "main.tf").is_file()

    _assert_manifest(tmp_path, "jenkins", "aws-ecs")
    _assert_no_literal_secrets(tmp_path)


# ===========================================================================
# github x azure-functions  (existing CI x new target) — NO Dockerfile
# ===========================================================================
def test_github_azure_functions_generates_and_validates(tmp_path: Path) -> None:
    _gen("github-azure-functions", tmp_path)

    # github workflow written + parses
    _assert_ci_file_parses(tmp_path, "github")

    # azure-functions IaC present: iac/azure-functions/*.tf + a Function App resource
    af_iac = tmp_path / "iac" / "azure-functions"
    tf_files = list(af_iac.glob("*.tf"))
    assert tf_files, "expected azure-functions Terraform under iac/azure-functions/"
    blob = "\n".join(f.read_text(encoding="utf-8") for f in tf_files)
    assert "azurerm_linux_function_app" in blob or "azurerm_function_app" in blob

    # azure-functions is NOT docker-shaped -> no Dockerfile anywhere
    assert not (tmp_path / "Dockerfile").exists()
    assert not any(p.name == "Dockerfile" for p in tmp_path.rglob("Dockerfile"))

    _assert_manifest(tmp_path, "github", "azure-functions")
    _assert_no_literal_secrets(tmp_path)


# ===========================================================================
# github x kubernetes  (existing CI x new target) — Dockerfile + k8s manifests
# ===========================================================================
def test_github_kubernetes_generates_and_validates(tmp_path: Path) -> None:
    _gen("github-kubernetes", tmp_path)

    # github workflow written + parses
    _assert_ci_file_parses(tmp_path, "github")

    # k8s manifests: iac/kubernetes/deployment.yaml with kind: Deployment
    deployment = tmp_path / "iac" / "kubernetes" / "deployment.yaml"
    assert deployment.is_file()
    assert "kind: Deployment" in deployment.read_text(encoding="utf-8")

    # kubernetes deploys an image -> a Dockerfile is present
    assert (tmp_path / "Dockerfile").is_file()

    _assert_manifest(tmp_path, "github", "kubernetes")
    _assert_no_literal_secrets(tmp_path)


# ===========================================================================
# R3.4 — the full plugin set is discoverable from the real tree
# ===========================================================================
def test_all_plugins_discoverable_from_real_tree() -> None:
    available = AvailablePlugins.discover(REPO_ROOT)
    # all five CI vendors present (two original + three added in 12.1)
    assert available.ci == {
        "github",
        "azure-devops",
        "gitlab",
        "bitbucket",
        "jenkins",
    }
    # the four 12.1-era target platforms are present; newer §11.9 matrix plugins
    # (e.g. aws-lambda) may ALSO be discovered, so this is a superset check.
    assert available.target >= {
        "aws-ecs",
        "databricks",
        "azure-functions",
        "kubernetes",
    }
    assert len(available.ci) == 5
    assert len(available.target) >= 4


# ===========================================================================
# Every committed sample IR validates (fail-fast shape check)
# ===========================================================================
@pytest.mark.parametrize(
    ("sample", "ci_vendor", "target_platform"),
    [
        ("gitlab-aws-ecs", "gitlab", "aws-ecs"),
        ("bitbucket-aws-ecs", "bitbucket", "aws-ecs"),
        ("jenkins-aws-ecs", "jenkins", "aws-ecs"),
        ("github-azure-functions", "github", "azure-functions"),
        ("github-kubernetes", "github", "kubernetes"),
    ],
)
def test_sample_ir_is_valid(sample: str, ci_vendor: str, target_platform: str) -> None:
    pipeline = _load_sample(sample)
    assert str(pipeline.ci_vendor) == ci_vendor
    assert str(pipeline.target_platform) == target_platform
    assert pipeline.stages  # at least one stage
