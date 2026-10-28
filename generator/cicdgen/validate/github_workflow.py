"""Validate a generated GitHub Actions workflow.

Two layers, strongest-first:

1. **Schema validation (preferred).** When the SchemaStore GitHub Actions
 workflow schema can be fetched (``fetch_github_workflow_schema``), the parsed
 workflow is validated against it with :mod:`jsonschema`. This is the same
 schema editors use for workflow authoring.

2. **Structural validation (always).** A dependency-light set of assertions that
 encode the invariants the ``github`` plugin must satisfy and that
 a workflow must satisfy to run at all. This runs with no network and is the
 fallback when the schema cannot be fetched (offline / isolated environments):

 * top-level ``on:`` trigger key present (and read back as the *string*
 ``"on"``, not the YAML-1.1 boolean ``True``);
 * ``jobs:`` present and non-empty;
 * every job has ``runs-on`` and a non-empty ``steps`` list;
 * every ``needs`` reference resolves to a real job;
 * every ``environment`` key is a non-empty string (or the mapping form);
 * ``permissions.id-token: write`` is present when any step uses a cloud
 OIDC login action (the workflow would otherwise fail to mint a token).

The structural check is intentionally conservative: it rejects the things that
would make a workflow invalid or non-runnable, without re-implementing the whole
GitHub schema.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

#: SchemaStore's published GitHub Actions workflow schema.
SCHEMASTORE_WORKFLOW_SCHEMA_URL = "https://json.schemastore.org/github-workflow.json"

#: Substrings that identify a cloud-OIDC login step (needs id-token: write).
_OIDC_ACTION_MARKERS = (
    "aws-actions/configure-aws-credentials",
    "azure/login",
    "google-github-actions/auth",
)


class GitHubWorkflowValidationError(AssertionError):
    """Raised when a workflow fails structural validation."""


# ---------------------------------------------------------------------------
# Schema layer
# ---------------------------------------------------------------------------
def fetch_github_workflow_schema(*, timeout: float = 10.0) -> dict[str, Any] | None:
    """Fetch the SchemaStore GitHub workflow schema, or ``None`` if unreachable.

 Returns ``None`` (never raises) on any network/parse failure so callers can
 cleanly fall back to structural validation in an offline/isolated setting.
 """
    try:
        req = urllib.request.Request(  # noqa: S310 — fixed https schemastore URL
            SCHEMASTORE_WORKFLOW_SCHEMA_URL,
            headers={"User-Agent": "cicdgen-validator"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            if resp.status != 200:
                return None
            data = resp.read().decode("utf-8")
        schema = json.loads(data)
        return schema if isinstance(schema, dict) else None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def validate_against_schema(workflow: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    """Validate ``workflow`` against a JSON Schema, raising on the first error.

 Raises:
 GitHubWorkflowValidationError: wrapping the underlying schema error so the
 caller sees a single, consistent error type.
 """
    import jsonschema  # local import: optional at module import time

    try:
        jsonschema.validate(instance=dict(workflow), schema=dict(schema))
    except jsonschema.ValidationError as exc:  # pragma: no cover - message passthrough
        raise GitHubWorkflowValidationError(
            f"workflow does not satisfy the GitHub Actions schema: {exc.message} "
            f"(at {'/'.join(str(p) for p in exc.absolute_path) or '<root>'})"
        ) from exc


# ---------------------------------------------------------------------------
# Structural layer (no network)
# ---------------------------------------------------------------------------
def validate_workflow_structure(workflow: Mapping[str, Any]) -> None:
    """Assert the workflow is a structurally valid, runnable GitHub Actions file.

 Encodes the invariants + the baseline GitHub requires. Raises
:class:`GitHubWorkflowValidationError` on the first violation with a precise
 message; returns ``None`` when the workflow is structurally sound.
 """
    if not isinstance(workflow, Mapping):
        raise GitHubWorkflowValidationError(
            f"workflow must parse to a mapping, got {type(workflow).__name__}."
        )

    _check_on_trigger(workflow)
    jobs = _check_jobs_present(workflow)
    _check_jobs_well_formed(jobs)
    _check_needs_resolve(jobs)
    _check_environments(jobs)
    _check_oidc_permission(workflow, jobs)


def _check_on_trigger(workflow: Mapping[str, Any]) -> None:
    # The boolean True key must NOT be present: a round trip that turned `on:`
    # into YAML-1.1 `true:` would make the workflow silently trigger-less.
    if True in workflow:
        raise GitHubWorkflowValidationError(
            "top-level 'on' was read back as the boolean True — the trigger key "
            "must survive as the string 'on' (quote it in the emitter)."
        )
    if "on" not in workflow:
        raise GitHubWorkflowValidationError("workflow is missing the top-level 'on:' trigger.")


def _check_jobs_present(workflow: Mapping[str, Any]) -> Mapping[str, Any]:
    jobs = workflow.get("jobs")
    if not isinstance(jobs, Mapping) or not jobs:
        raise GitHubWorkflowValidationError("workflow 'jobs' must be a non-empty mapping.")
    return jobs


def _check_jobs_well_formed(jobs: Mapping[str, Any]) -> None:
    for job_id, job in jobs.items():
        if not isinstance(job, Mapping):
            raise GitHubWorkflowValidationError(f"job '{job_id}' must be a mapping.")
        # A job either runs steps (needs runs-on) or 'uses' a reusable workflow.
        if "uses" in job:
            continue
        if "runs-on" not in job:
            raise GitHubWorkflowValidationError(f"job '{job_id}' is missing 'runs-on'.")
        steps = job.get("steps")
        if not isinstance(steps, list) or not steps:
            raise GitHubWorkflowValidationError(
                f"job '{job_id}' must have a non-empty 'steps' list."
            )
        for i, step in enumerate(steps):
            if not isinstance(step, Mapping):
                raise GitHubWorkflowValidationError(
                    f"job '{job_id}' step #{i} must be a mapping."
                )
            if "uses" not in step and "run" not in step:
                raise GitHubWorkflowValidationError(
                    f"job '{job_id}' step #{i} must have either 'uses' or 'run'."
                )


def _check_needs_resolve(jobs: Mapping[str, Any]) -> None:
    job_ids = set(jobs)
    for job_id, job in jobs.items():
        needs = job.get("needs")
        if needs is None:
            continue
        refs = [needs] if isinstance(needs, str) else list(needs)
        for ref in refs:
            if ref not in job_ids:
                raise GitHubWorkflowValidationError(
                    f"job '{job_id}' needs '{ref}', which is not a defined job "
                    f"(defined: {sorted(job_ids)})."
                )


def _check_environments(jobs: Mapping[str, Any]) -> None:
    for job_id, job in jobs.items():
        if "environment" not in job:
            continue
        env = job["environment"]
        if isinstance(env, str):
            if not env:
                raise GitHubWorkflowValidationError(
                    f"job '{job_id}' has an empty 'environment' name."
                )
        elif isinstance(env, Mapping):
            if not env.get("name"):
                raise GitHubWorkflowValidationError(
                    f"job '{job_id}' environment mapping must carry a non-empty 'name'."
                )
        else:
            raise GitHubWorkflowValidationError(
                f"job '{job_id}' 'environment' must be a string or mapping, "
                f"got {type(env).__name__}."
            )


def _check_oidc_permission(workflow: Mapping[str, Any], jobs: Mapping[str, Any]) -> None:
    if not _uses_oidc_login(jobs):
        return
    perms = workflow.get("permissions")
    id_token = perms.get("id-token") if isinstance(perms, Mapping) else None
    if id_token != "write":
        raise GitHubWorkflowValidationError(
            "a cloud OIDC login step is present but 'permissions.id-token: write' "
            "is not granted — the job cannot mint an OIDC token."
        )


def _uses_oidc_login(jobs: Mapping[str, Any]) -> bool:
    for job in jobs.values():
        if not isinstance(job, Mapping):
            continue
        for step in job.get("steps", []) or []:
            if not isinstance(step, Mapping):
                continue
            uses = step.get("uses")
            if isinstance(uses, str) and any(m in uses for m in _OIDC_ACTION_MARKERS):
                return True
    return False
