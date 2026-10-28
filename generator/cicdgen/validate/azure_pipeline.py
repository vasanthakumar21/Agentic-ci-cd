"""Validate a generated Azure Pipelines file (task 7.2, R3.2, design §8a).

A dependency-light, no-network structural validator for the ``azure-pipelines.yml``
the ``azure-devops`` CI-vendor plugin emits. It encodes the design §8a **Azure
DevOps column** of the CI mapping table so the committed sample artifact can be
checked locally — no real Azure DevOps account / run required (R9 isolation).

Design §8a — Azure DevOps column (the rows this validator enforces)
-------------------------------------------------------------------
    * stage order   -> ``stages`` with ``dependsOn``: a non-empty list; each stage
      has a stage name; every stage except the chain head carries a ``dependsOn``
      that resolves to a real *earlier* stage (so declared order is a real chain).
    * manual gate   -> env / manual stages use a ``deployment`` job bound to an
      ``environment:`` with a ``runOnce`` strategy (approvals/checks are enforced
      on that environment in Azure DevOps).
    * approvals     -> same ``environment:`` reference (env approvals/checks).
    * timeout       -> ``timeoutInMinutes`` present on a job wherever the IR set a
      timeout (checked by the caller against the IR; structurally we assert any
      ``timeoutInMinutes`` is a positive int).
    * secrets ref   -> a variable *group* is linked (``variables: - group: …``) and
      the document contains no literal secrets (only ``$(NAME)`` macro references).
    * cloud OIDC    -> a workload-identity / service-connection login step is
      present (``AzureCLI@2`` with an ``azureSubscription`` service connection, or
      ``AWSShellScript@1`` with ``awsCredentials``) when the pipeline authenticates
      to a cloud via OIDC.

The check is intentionally conservative: it rejects the things that would make an
Azure Pipelines file invalid or non-runnable (duplicate stage names, dangling or
forward ``dependsOn``, env stages that are not deployment jobs, literal secrets)
without re-implementing the whole Azure Pipelines schema.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

#: Task refs that constitute a cloud workload-identity / OIDC login step (§8a).
_OIDC_LOGIN_TASKS = ("AzureCLI@2", "AWSShellScript@1")

#: Patterns that would indicate a *literal* secret leaked into the pipeline
#: (the §8a contract is secrets-by-reference only — ``$(NAME)`` / variable group).
_LITERAL_SECRET_PATTERNS = (
    re.compile(r"arn:aws:iam", re.IGNORECASE),
    re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9/+_-]{8,}"),
)


class AzurePipelineValidationError(AssertionError):
    """Raised when an Azure Pipelines document fails structural validation."""


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------
def validate_azure_pipeline_structure(
    doc: Mapping[str, Any], *, raw_text: str | None = None
) -> None:
    """Assert ``doc`` is a structurally valid Azure Pipelines file (design §8a).

    Encodes the §8a Azure DevOps invariants. Raises
    :class:`AzurePipelineValidationError` on the first violation with a precise
    message; returns ``None`` when the document is structurally sound.

    Args:
        doc: the parsed ``azure-pipelines.yml`` mapping.
        raw_text: optional original YAML text. When supplied, the no-literal-secret
            check runs against it too (so a leaked secret anywhere in the file is
            caught, not only inside the structures we walk).
    """
    if not isinstance(doc, Mapping):
        raise AzurePipelineValidationError(
            f"pipeline must parse to a mapping, got {type(doc).__name__}."
        )

    _check_trigger(doc)
    stages = _check_stages_present(doc)
    stage_names = _check_stage_names_unique(stages)
    _check_depends_on_resolves_backwards(stages, stage_names)
    _check_jobs_well_formed(stages)
    _check_env_stages_are_deployment_jobs(stages)
    _check_timeouts_positive(stages)
    _check_variable_group_linked(doc)
    _check_no_literal_secrets(doc, raw_text)


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------
def _check_trigger(doc: Mapping[str, Any]) -> None:
    if "trigger" not in doc:
        raise AzurePipelineValidationError("pipeline is missing a top-level 'trigger'.")


# ---------------------------------------------------------------------------
# Stages / order (stage order -> dependsOn)
# ---------------------------------------------------------------------------
def _check_stages_present(doc: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    stages = doc.get("stages")
    if not isinstance(stages, Sequence) or isinstance(stages, (str, bytes)) or not stages:
        raise AzurePipelineValidationError("pipeline 'stages' must be a non-empty list.")
    out: list[Mapping[str, Any]] = []
    for i, stage in enumerate(stages):
        if not isinstance(stage, Mapping):
            raise AzurePipelineValidationError(f"stage #{i} must be a mapping.")
        out.append(stage)
    return out


def _check_stage_names_unique(stages: list[Mapping[str, Any]]) -> list[str]:
    names: list[str] = []
    for i, stage in enumerate(stages):
        name = stage.get("stage")
        if not isinstance(name, str) or not name:
            raise AzurePipelineValidationError(
                f"stage #{i} must carry a non-empty 'stage' name."
            )
        names.append(name)
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise AzurePipelineValidationError(
            f"duplicate stage name(s) {dupes} — Azure DevOps stage names must be unique."
        )
    return names


def _check_depends_on_resolves_backwards(
    stages: list[Mapping[str, Any]], stage_names: list[str]
) -> None:
    """Every stage except the head must dependsOn an *earlier* stage.

    This is what makes the declared order a real linear chain (§8a stage order).
    The first stage is the chain head and must NOT depend on anything.
    """
    seen: set[str] = set()
    for idx, stage in enumerate(stages):
        name = stage_names[idx]
        depends = stage.get("dependsOn")
        if idx == 0:
            if depends is not None:
                raise AzurePipelineValidationError(
                    f"head stage '{name}' must not declare 'dependsOn' (it is the chain head)."
                )
            seen.add(name)
            continue
        if depends is None:
            raise AzurePipelineValidationError(
                f"stage '{name}' is not the head stage but has no 'dependsOn' — "
                f"the declared order would not be enforced."
            )
        refs = [depends] if isinstance(depends, str) else list(depends)
        for ref in refs:
            if ref not in stage_names:
                raise AzurePipelineValidationError(
                    f"stage '{name}' dependsOn '{ref}', which is not a defined stage "
                    f"(defined: {sorted(stage_names)})."
                )
            if ref not in seen:
                raise AzurePipelineValidationError(
                    f"stage '{name}' dependsOn '{ref}', which is not an *earlier* stage "
                    f"(forward/cyclic dependency)."
                )
        seen.add(name)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def _iter_jobs(stage: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    jobs = stage.get("jobs")
    if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes)) or not jobs:
        raise AzurePipelineValidationError(
            f"stage '{stage.get('stage')}' must have a non-empty 'jobs' list."
        )
    out: list[Mapping[str, Any]] = []
    for job in jobs:
        if not isinstance(job, Mapping):
            raise AzurePipelineValidationError(
                f"stage '{stage.get('stage')}' has a job that is not a mapping."
            )
        out.append(job)
    return out


def _job_steps(job: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return a job's step list, whether a plain job or a deployment runOnce job."""
    if "steps" in job:
        steps = job.get("steps")
    else:
        strategy = job.get("strategy")
        run_once = strategy.get("runOnce") if isinstance(strategy, Mapping) else None
        deploy = run_once.get("deploy") if isinstance(run_once, Mapping) else None
        steps = deploy.get("steps") if isinstance(deploy, Mapping) else None
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)) or not steps:
        raise AzurePipelineValidationError(
            f"job '{job.get('job') or job.get('deployment')}' must have a non-empty "
            f"'steps' list."
        )
    return [s for s in steps if isinstance(s, Mapping)]


def _check_jobs_well_formed(stages: list[Mapping[str, Any]]) -> None:
    for stage in stages:
        for job in _iter_jobs(stage):
            is_plain = "job" in job
            is_deploy = "deployment" in job
            if not (is_plain or is_deploy):
                raise AzurePipelineValidationError(
                    f"stage '{stage.get('stage')}' has a job with neither 'job' nor "
                    f"'deployment' key."
                )
            # Every job must have resolvable, non-empty steps.
            _job_steps(job)


# ---------------------------------------------------------------------------
# Manual gate / approvals (environment deployment jobs)
# ---------------------------------------------------------------------------
def _check_env_stages_are_deployment_jobs(stages: list[Mapping[str, Any]]) -> None:
    """A job bound to an ``environment:`` must be a deployment job with runOnce.

    This is the §8a manual-gate / approvals row: environment approvals and checks
    only apply to a ``deployment`` job bound to that environment.
    """
    for stage in stages:
        for job in _iter_jobs(stage):
            has_env = "environment" in job
            is_deploy = "deployment" in job
            if has_env and not is_deploy:
                raise AzurePipelineValidationError(
                    f"stage '{stage.get('stage')}' binds an 'environment' on a plain "
                    f"job — environment approvals require a 'deployment' job."
                )
            if is_deploy:
                env = job.get("environment")
                if not isinstance(env, str) or not env:
                    raise AzurePipelineValidationError(
                        f"deployment job in stage '{stage.get('stage')}' must bind a "
                        f"non-empty 'environment'."
                    )
                strategy = job.get("strategy")
                run_once = strategy.get("runOnce") if isinstance(strategy, Mapping) else None
                if not isinstance(run_once, Mapping):
                    raise AzurePipelineValidationError(
                        f"deployment job in stage '{stage.get('stage')}' must use a "
                        f"'runOnce' strategy."
                    )


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------
def _check_timeouts_positive(stages: list[Mapping[str, Any]]) -> None:
    """Any ``timeoutInMinutes`` present must be a positive integer (§8a timeout)."""
    for stage in stages:
        for job in _iter_jobs(stage):
            if "timeoutInMinutes" in job:
                val = job["timeoutInMinutes"]
                if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
                    raise AzurePipelineValidationError(
                        f"job in stage '{stage.get('stage')}' has a non-positive "
                        f"'timeoutInMinutes': {val!r}."
                    )


# ---------------------------------------------------------------------------
# Secrets ref (variable group + no literals)
# ---------------------------------------------------------------------------
def _check_variable_group_linked(doc: Mapping[str, Any]) -> None:
    """A variable group must be linked for secret values (§8a secrets ref)."""
    variables = doc.get("variables")
    if not isinstance(variables, Sequence) or isinstance(variables, (str, bytes)):
        raise AzurePipelineValidationError(
            "pipeline 'variables' must be a list linking a secret variable group."
        )
    linked = [
        v.get("group")
        for v in variables
        if isinstance(v, Mapping) and isinstance(v.get("group"), str) and v.get("group")
    ]
    if not linked:
        raise AzurePipelineValidationError(
            "no variable group is linked (variables: - group: <name>); secret values "
            "must come from a variable group, referenced as $(NAME)."
        )


def _check_no_literal_secrets(doc: Mapping[str, Any], raw_text: str | None) -> None:
    """No literal secrets anywhere — only ``$(NAME)`` references (§8a secrets ref)."""
    haystacks: list[str] = []
    if raw_text is not None:
        haystacks.append(raw_text)
    else:
        haystacks.extend(_iter_scalar_strings(doc))
    for text in haystacks:
        for pat in _LITERAL_SECRET_PATTERNS:
            m = pat.search(text)
            if m:
                raise AzurePipelineValidationError(
                    f"possible literal secret in the pipeline: matched {pat.pattern!r} "
                    f"near {m.group(0)[:40]!r}. Secrets must be $(NAME) references only."
                )


def _iter_scalar_strings(node: Any) -> list[str]:
    out: list[str] = []
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, Mapping):
        for k, v in node.items():
            if isinstance(k, str):
                out.append(k)
            out.extend(_iter_scalar_strings(v))
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for item in node:
            out.extend(_iter_scalar_strings(item))
    return out


# ---------------------------------------------------------------------------
# Mapping-table coverage — assert each §8a row is represented
# ---------------------------------------------------------------------------
def assert_mapping_table_rows_represented(
    doc: Mapping[str, Any],
    *,
    expect_manual_gate: bool = True,
    expect_timeout: bool = True,
    expect_oidc: bool = True,
) -> dict[str, bool]:
    """Assert each §8a Azure DevOps mapping-table row is represented in ``doc``.

    Returns a dict mapping each row name to ``True`` (every asserted row is
    present). Rows that depend on the IR (manual gate, timeout, cloud OIDC) can be
    toggled off via the ``expect_*`` flags for IRs that do not exercise them.

    Raises :class:`AzurePipelineValidationError` naming the first missing row.
    """
    stages = _check_stages_present(doc)
    results: dict[str, bool] = {}

    # Row: stage order -> stages/jobs dependsOn (a chain of >1 stage with dependsOn).
    has_depends = any("dependsOn" in s for s in stages)
    if len(stages) > 1 and not has_depends:
        raise AzurePipelineValidationError(
            "mapping-table row 'stage order' not represented: multiple stages but no "
            "'dependsOn' chaining them."
        )
    results["stage order (dependsOn)"] = len(stages) == 1 or has_depends

    # Row: manual gate / approvals -> a deployment job bound to an environment.
    if expect_manual_gate:
        if not _any_deployment_with_environment(stages):
            raise AzurePipelineValidationError(
                "mapping-table row 'manual gate / approvals' not represented: no "
                "deployment job bound to an environment."
            )
        results["manual gate / approvals (env deployment job)"] = True

    # Row: timeout -> timeoutInMinutes somewhere.
    if expect_timeout:
        if not _any_job_has_key(stages, "timeoutInMinutes"):
            raise AzurePipelineValidationError(
                "mapping-table row 'timeout' not represented: no job declares "
                "'timeoutInMinutes'."
            )
        results["timeout (timeoutInMinutes)"] = True

    # Row: secrets ref -> a linked variable group.
    _check_variable_group_linked(doc)
    results["secrets ref (variable group / $(X))"] = True

    # Row: cloud OIDC -> a WIF / service-connection login step.
    if expect_oidc:
        if not _any_oidc_login_step(stages):
            raise AzurePipelineValidationError(
                "mapping-table row 'cloud OIDC' not represented: no workload-identity / "
                "service-connection login step found."
            )
        results["cloud OIDC (WIF service connection)"] = True

    return results


def _any_deployment_with_environment(stages: list[Mapping[str, Any]]) -> bool:
    for stage in stages:
        for job in _iter_jobs(stage):
            if "deployment" in job and isinstance(job.get("environment"), str):
                return True
    return False


def _any_job_has_key(stages: list[Mapping[str, Any]], key: str) -> bool:
    for stage in stages:
        for job in _iter_jobs(stage):
            if key in job:
                return True
    return False


def _any_oidc_login_step(stages: list[Mapping[str, Any]]) -> bool:
    for stage in stages:
        for job in _iter_jobs(stage):
            for step in _job_steps(job):
                if step.get("task") in _OIDC_LOGIN_TASKS:
                    inputs = step.get("inputs")
                    if isinstance(inputs, Mapping) and (
                        inputs.get("azureSubscription") or inputs.get("awsCredentials")
                    ):
                        return True
    return False
