"""Sample IaC generation + local validation for the aws-ecs plugin (task 8.2).

Task 8.2 asks that "`terraform plan` succeeds in the isolated account; verbs run
locally where able" (R3.3 target verbs, R4.2 supply-IaC, R7.1 resilient build).

A *real* ``terraform plan`` needs the terraform binary **and** a throwaway AWS
account — both kept out of this environment by R9 (isolation): terraform and
docker are not installed and no isolated account is configured here. So this
module does the strongest thing possible locally, deterministically, and with no
network/account operations:

    1. Generate the aws-ecs IaC + Dockerfile from the committed sample IR
       ``examples/client-github-aws/pipeline.yaml`` through the real plugin.
    2. Prove the committed artifacts under
       ``examples/client-github-aws/iac/aws-ecs/`` and the committed
       ``examples/client-github-aws/Dockerfile`` match a fresh generation
       **byte-for-byte** (determinism, R5.3) — the reviewable files in the repo
       are exactly what the generator produces (and what the isolated-account
       ``terraform plan`` step would consume).
    3. Structurally validate the HCL with :mod:`cicdgen.validate.terraform_hcl`
       (balanced braces, well-formed blocks, ``terraform``/``provider "aws"``
       present) — the local stand-in for ``terraform validate``/``plan`` parsing.
    4. Assert the design §8b resources/variables/outputs exist and that NO literal
       account specifics or secrets are committed (R6).
    5. "Verbs run locally where able": validate the verb *commands* structurally
       (non-empty, well-formed, by-reference knobs) and the Dockerfile structurally
       (multi-stage, pinned base, no fragile apt mirror). The one step that can
       actually run locally — the AWS CLI the deploy/verify verbs invoke — is
       smoke-tested via ``aws ecs help`` when the CLI is installed; the real
       build/push/deploy need docker + a live cluster and are documented manual
       steps (see the iac runbook), not executed here.

The live ``terraform plan`` + docker build/push + ECS deploy are an irreducible
manual step for a throwaway account; they are documented in
``examples/client-github-aws/iac/aws-ecs/README.md`` and intentionally NOT run
here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from cicdgen.ir import Pipeline
from cicdgen.plugins import StageVerb, VerbContext, load_target_platform_plugin
from cicdgen.validate import (
    EXPECTED_OUTPUTS,
    EXPECTED_RESOURCES,
    EXPECTED_VARIABLES,
    TerraformHclValidationError,
    assert_expected_resources,
    assert_no_account_specifics,
    validate_hcl_structure,
    validate_terraform_module,
)

# <repo>/tests/test_aws_ecs_terraform.py -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"
SAMPLE_DIR = REPO_ROOT / "examples" / "client-github-aws"
SAMPLE_IR = SAMPLE_DIR / "pipeline.yaml"
IAC_DIR = SAMPLE_DIR / "iac" / "aws-ecs"
COMMITTED_DOCKERFILE = SAMPLE_DIR / "Dockerfile"

TF_FILES = ("main.tf", "variables.tf", "outputs.tf", "terraform.tfvars.example")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_sample_pipeline() -> Pipeline:
    data = yaml.safe_load(SAMPLE_IR.read_text(encoding="utf-8"))
    return Pipeline.model_validate(data)


def _plugin():
    return load_target_platform_plugin("aws-ecs", plugins_root=PLUGINS_ROOT)


def _fresh_iac_files() -> dict[str, str]:
    """Freshly generate the IaC (name -> content) from the sample IR."""
    iac = _plugin().supply_iac(_load_sample_pipeline())
    # paths are iac/aws-ecs/<name>; key by basename for convenience
    return {Path(f.path).name: f.content for f in iac.files}


def _fresh_dockerfile() -> str:
    pipeline = _load_sample_pipeline()
    stage = next((s for s in pipeline.stages if s.id == "build"), pipeline.stages[0])
    ctx = VerbContext(pipeline=pipeline, stage=stage, verb=StageVerb.BUILD, settings={})
    result = _plugin().build(ctx)
    dockerfile = next(f for f in result.files if f.path == "Dockerfile")
    return dockerfile.content


def _committed_iac_files() -> dict[str, str]:
    return {name: (IAC_DIR / name).read_bytes().decode("utf-8") for name in TF_FILES}


# ---------------------------------------------------------------------------
# Artifacts exist
# ---------------------------------------------------------------------------
def test_committed_artifacts_exist() -> None:
    assert SAMPLE_IR.is_file(), f"missing sample IR: {SAMPLE_IR}"
    for name in TF_FILES:
        assert (IAC_DIR / name).is_file(), f"missing committed IaC artifact: {name}"
    assert COMMITTED_DOCKERFILE.is_file(), "missing committed Dockerfile"


# ---------------------------------------------------------------------------
# Determinism: committed artifacts == fresh generation, byte-for-byte (R5.3)
# ---------------------------------------------------------------------------
def test_committed_iac_matches_fresh_generation_byte_for_byte() -> None:
    fresh = _fresh_iac_files()
    committed = _committed_iac_files()
    for name in TF_FILES:
        assert committed[name] == fresh[name], (
            f"committed iac/aws-ecs/{name} is stale — regenerate it from "
            "examples/client-github-aws/pipeline.yaml so the reviewable artifact "
            "matches the generator output."
        )


def test_committed_dockerfile_matches_fresh_generation_byte_for_byte() -> None:
    fresh = _fresh_dockerfile()
    committed = COMMITTED_DOCKERFILE.read_bytes().decode("utf-8")
    assert committed == fresh, (
        "committed examples/client-github-aws/Dockerfile is stale — regenerate it "
        "from the build verb so the reviewable artifact matches the generator."
    )


def test_generation_is_deterministic_across_calls() -> None:
    assert _fresh_iac_files() == _fresh_iac_files()
    assert _fresh_dockerfile() == _fresh_dockerfile()


# ---------------------------------------------------------------------------
# Structural HCL validation (the local stand-in for terraform validate/plan)
# ---------------------------------------------------------------------------
def test_each_committed_tf_file_is_structurally_valid() -> None:
    for name in ("main.tf", "variables.tf", "outputs.tf"):
        content = (IAC_DIR / name).read_text(encoding="utf-8")
        validate_hcl_structure(content, filename=name)  # raises on malformed HCL


def test_module_passes_structural_validation() -> None:
    summary = validate_terraform_module(_committed_iac_files())
    # terraform block + aws provider were asserted inside; sanity-check provider set
    assert "aws" in summary.providers()
    assert summary.has_terraform_block()


def test_structural_validator_rejects_unbalanced_braces() -> None:
    bad = 'terraform {\n  required_providers {\n}\n'  # missing one close
    with pytest.raises(TerraformHclValidationError):
        validate_hcl_structure(bad, filename="bad.tf")


def test_structural_validator_rejects_unknown_block_type() -> None:
    bad = 'resorce "aws_x" "y" {\n}\n'  # typo'd 'resource'
    with pytest.raises(TerraformHclValidationError):
        validate_hcl_structure(bad, filename="bad.tf")


# ---------------------------------------------------------------------------
# Expected resources / variables / outputs (design §8b)
# ---------------------------------------------------------------------------
def test_module_declares_expected_resources_vars_outputs() -> None:
    summary = validate_terraform_module(_committed_iac_files())
    assert_expected_resources(summary)  # raises if any required item missing
    # spell out the contract so a regression names what's gone
    assert EXPECTED_RESOURCES <= summary.resources()
    assert EXPECTED_VARIABLES <= summary.variables()
    assert EXPECTED_OUTPUTS <= summary.outputs()


def test_module_has_two_iam_roles_execution_and_ci_deploy() -> None:
    summary = validate_terraform_module(_committed_iac_files())
    iam_roles = [
        b for b in summary.blocks
        if b.type == "resource" and b.labels[:1] == ("aws_iam_role",)
    ]
    names = {b.labels[1] for b in iam_roles}
    assert "ecs_task_execution" in names
    assert "ci_deploy" in names


def test_account_id_comes_from_caller_identity_not_a_literal() -> None:
    summary = validate_terraform_module(_committed_iac_files())
    assert "aws_caller_identity" in summary.data_sources()


# ---------------------------------------------------------------------------
# No committed account specifics / secrets (R6)
# ---------------------------------------------------------------------------
def test_no_account_specifics_or_secrets_committed() -> None:
    files = _committed_iac_files()
    files["Dockerfile"] = COMMITTED_DOCKERFILE.read_text(encoding="utf-8")
    assert_no_account_specifics(files)  # raises on AKIA/ARN/account-id/secret


def test_no_twelve_digit_account_id_in_resources() -> None:
    import re

    main = (IAC_DIR / "main.tf").read_text(encoding="utf-8")
    # No bare 12-digit account-id token anywhere in main.tf.
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", main)


# ---------------------------------------------------------------------------
# "verbs run locally where able" — structural validation of the verb commands
# ---------------------------------------------------------------------------
def _ctx(verb: StageVerb, stage_id: str):
    pipeline = _load_sample_pipeline()
    stage = next((s for s in pipeline.stages if s.id == stage_id), pipeline.stages[0])
    return VerbContext(pipeline=pipeline, stage=stage, verb=verb, settings={})


def test_all_verbs_emit_nonempty_wellformed_commands() -> None:
    plugin = _plugin()
    for verb, stage_id in (
        (StageVerb.LINT, "build"),
        (StageVerb.TEST, "test"),
        (StageVerb.BUILD, "build"),
        (StageVerb.PACKAGE, "build"),
        (StageVerb.DEPLOY, "deploy-prod"),
        (StageVerb.VERIFY, "deploy-prod"),
    ):
        result = plugin.run_verb(_ctx(verb, stage_id))
        assert result.commands, f"verb {verb} produced no commands"
        for cmd in result.commands:
            assert isinstance(cmd, str) and cmd.strip(), f"empty command in {verb}"
            # well-formed shell: balanced double quotes per command line
            assert cmd.count('"') % 2 == 0, f"unbalanced quotes in {verb}: {cmd!r}"


def test_build_and_package_reference_knobs_by_reference() -> None:
    plugin = _plugin()
    pkg = "\n".join(plugin.package(_ctx(StageVerb.PACKAGE, "build")).commands)
    # no settings -> account specifics are shell references, not literals (R6)
    assert "$ECR_REPO" in pkg
    assert "docker push" in pkg
    assert "aws ecr get-login-password" in pkg


def test_deploy_and_verify_reference_cluster_service_by_reference() -> None:
    plugin = _plugin()
    deploy = "\n".join(plugin.deploy(_ctx(StageVerb.DEPLOY, "deploy-prod")).commands)
    verify = "\n".join(plugin.verify(_ctx(StageVerb.VERIFY, "deploy-prod")).commands)
    assert "$ECS_CLUSTER" in deploy and "$ECS_SERVICE" in deploy
    assert "aws ecs register-task-definition" in deploy
    assert "aws ecs update-service" in deploy
    assert "aws ecs wait services-stable" in verify


# ---------------------------------------------------------------------------
# Dockerfile structural validity (R7.1: resilient build)
# ---------------------------------------------------------------------------
def test_dockerfile_is_structurally_valid() -> None:
    content = COMMITTED_DOCKERFILE.read_text(encoding="utf-8")
    import re

    # at least one FROM; multi-stage => >= 2 "FROM ... AS <stage>"
    assert re.search(r"(?mi)^FROM\s+\S+", content), "Dockerfile has no FROM line"
    stages = re.findall(r"(?mi)^FROM\s+\S+\s+AS\s+\w+", content)
    assert len(stages) >= 2, "Dockerfile is not multi-stage"
    # pinned base, never :latest
    assert "python:3.12-slim" in content
    assert ":latest" not in content
    # resilient: no live apt mirror dependency (R7.1)
    low = content.lower()
    assert "apt-get update" not in low
    assert "apt-get install" not in low
    assert "apt install" not in low
    # deps via pip from a pinned requirements file instead
    assert "pip install" in content
    assert "requirements.txt" in content
    # has a runtime CMD/ENTRYPOINT
    assert re.search(r"(?mi)^(CMD|ENTRYPOINT)\s", content)


# ---------------------------------------------------------------------------
# The one verb dependency that CAN run locally: the AWS CLI smoke test
# ---------------------------------------------------------------------------
def test_aws_cli_recognises_the_ecs_subcommands_the_verbs_invoke() -> None:
    """Smoke-test the AWS CLI the deploy/verify verbs call — run locally where able.

    docker + a live ECS cluster are not available here (documented manual steps),
    but the AWS CLI itself is a pure-local dependency we can verify *without touching
    any account*: invoking ``aws ecs update-service`` with no args makes the CLI
    parse-validate the subcommand and fail with a *parameter-validation* error
    (``the following arguments are required: --service``). That proves the CLI knows
    the ``ecs update-service`` operation the ``deploy`` verb uses — no network call,
    no credentials, no cluster. A genuinely unknown operation yields a different
    "invalid choice" error, which we assert is NOT what we get for a real subcommand.

    Skips cleanly when the CLI isn't installed (keeps the suite green in any env).
    ``aws ... help`` is deliberately NOT used: on some platforms it shells out to a
    pager/help viewer and captures no output.
    """
    aws = shutil.which("aws")
    if aws is None:
        pytest.skip(
            "aws CLI not installed — build/push/deploy are documented manual steps"
        )
    env = {**os.environ, "AWS_PAGER": ""}

    # Known subcommand, missing required arg -> ParamValidation (recognised op).
    proc = subprocess.run(
        [aws, "ecs", "update-service"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    combined = (proc.stdout + proc.stderr).lower()
    assert proc.returncode != 0  # no --service given
    assert "invalid choice" not in combined, (
        "aws CLI does not recognise `ecs update-service` — the deploy verb's CLI "
        "dependency is unavailable"
    )
    assert "--service" in combined or "arguments are required" in combined

    # Unknown operation -> a DIFFERENT, 'invalid choice' error (control case).
    bogus = subprocess.run(
        [aws, "ecs", "definitely-not-a-real-ecs-op"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert bogus.returncode != 0
    assert "invalid choice" in (bogus.stdout + bogus.stderr).lower()
